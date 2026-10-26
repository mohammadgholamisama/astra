package com.v2rayping.xray

import android.util.Log
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import go.Seq
import libv2ray.Libv2ray
import java.io.File
import java.net.InetSocketAddress
import java.net.Socket
import java.security.MessageDigest
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Real-delay ping via LibXray (AndroidLibXrayLite):
 * spins an in-memory Xray instance per config (no system VPN) and measures
 * HTTP RTT to the probe URL — works for VLESS Reality / WS / gRPC / etc.
 */
class XrayPingModule(
  private val reactContext: ReactApplicationContext
) : ReactContextBaseJavaModule(reactContext) {

  /** Parallel Xray measures — sized for 200+ config batches. */
  private val measurePool = Executors.newFixedThreadPool(28)
  /** Lightweight waiters so measure threads never block on Future.get. */
  private val replyPool = Executors.newCachedThreadPool()
  private val coreReady = AtomicBoolean(false)

  override fun getName(): String = "XrayPing"

  @ReactMethod
  fun isAvailable(promise: Promise) {
    promise.resolve(ensureCore())
  }

  @ReactMethod
  fun sha256(input: String, promise: Promise) {
    try {
      val digest = MessageDigest.getInstance("SHA-256")
        .digest(input.toByteArray(Charsets.UTF_8))
      promise.resolve(digest.joinToString("") { "%02x".format(it) })
    } catch (t: Throwable) {
      promise.reject("SHA256_ERROR", t)
    }
  }

  /**
   * Real outbound delay through Xray core.
   * Hard-caps wait with [timeoutMs] so dead configs don't stall the queue.
   */
  @ReactMethod
  fun pingRealDelay(shareLink: String, timeoutMs: Double, promise: Promise) {
    val capMs = timeoutMs.toLong().coerceIn(800L, 8_000L)
    val future = measurePool.submit<Long> {
      try {
        if (!ensureCore()) {
          -1L
        } else {
          val configJson = XrayConfigBuilder.fromShareLink(shareLink.trim())
          if (configJson.isNullOrBlank()) {
            Log.w(TAG, "failed to build xray json from link")
            -1L
          } else {
            val delay = Libv2ray.measureOutboundDelay(configJson, PROBE_URL)
            when {
              delay <= 0L -> -1L
              delay > capMs -> -1L
              else -> delay
            }
          }
        }
      } catch (t: Throwable) {
        Log.w(TAG, "measureOutboundDelay failed: ${t.message}")
        -1L
      }
    }
    replyPool.execute {
      val ms = try {
        future.get(capMs + 250L, TimeUnit.MILLISECONDS)
      } catch (_: TimeoutException) {
        future.cancel(true)
        -1L
      } catch (t: Throwable) {
        future.cancel(true)
        Log.w(TAG, "pingRealDelay wait failed: ${t.message}")
        -1L
      }
      promise.resolve(ms.toDouble())
    }
  }

  @ReactMethod
  fun pingTcp(host: String, port: Double, timeoutMs: Double, promise: Promise) {
    measurePool.execute {
      val ms = try {
        tcpConnectMs(
          host.trim(),
          port.toInt().coerceIn(1, 65535),
          timeoutMs.toInt().coerceIn(200, 5_000),
        )
      } catch (_: Throwable) {
        -1L
      }
      promise.resolve(ms.toDouble())
    }
  }

  private fun ensureCore(): Boolean {
    if (coreReady.get()) return true
    synchronized(this) {
      if (coreReady.get()) return true
      return try {
        Seq.setContext(reactContext.applicationContext)
        val assetsDir = prepareGeoAssets()
        Libv2ray.initCoreEnv(assetsDir.absolutePath, "")
        coreReady.set(true)
        Log.i(TAG, "Libv2ray ready · ${Libv2ray.checkVersionX()}")
        true
      } catch (t: Throwable) {
        Log.e(TAG, "initCoreEnv failed", t)
        false
      }
    }
  }

  private fun prepareGeoAssets(): File {
    val dir = File(reactContext.filesDir, "xray-assets")
    if (!dir.exists()) dir.mkdirs()
    for (name in listOf("geoip.dat", "geosite.dat")) {
      val out = File(dir, name)
      if (!out.exists()) {
        try {
          out.writeBytes(ByteArray(0))
        } catch (t: Throwable) {
          Log.w(TAG, "stub $name: ${t.message}")
        }
      }
    }
    return dir
  }

  private fun tcpConnectMs(host: String, port: Int, timeoutMs: Int): Long {
    if (host.isEmpty()) return -1L
    val socket = Socket()
    val started = System.nanoTime()
    return try {
      socket.connect(InetSocketAddress(host, port), timeoutMs)
      val elapsed = (System.nanoTime() - started) / 1_000_000L
      if (elapsed <= 0) 1L else elapsed
    } catch (_: Throwable) {
      -1L
    } finally {
      try {
        socket.close()
      } catch (_: Throwable) {
      }
    }
  }

  companion object {
    private const val TAG = "XrayPing"
    const val PROBE_URL = "https://www.gstatic.com/generate_204"
  }
}
