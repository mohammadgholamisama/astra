package com.v2rayping.xray

import android.util.Log
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import go.Seq
import libv2ray.Libv2ray
import java.io.File
import java.io.FileOutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.security.MessageDigest
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Real-delay ping via LibXray (AndroidLibXrayLite):
 * spins an in-memory Xray instance per config (no system VPN) and measures
 * HTTP RTT to the probe URL — works for VLESS Reality / WS / gRPC / etc.
 */
class XrayPingModule(
  private val reactContext: ReactApplicationContext
) : ReactContextBaseJavaModule(reactContext) {

  private val executor = Executors.newFixedThreadPool(3)
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
   * @param shareLink vless/vmess/trojan/ss URI
   * @param timeoutMs soft hint (core uses its own ~12s cap)
   */
  @ReactMethod
  fun pingRealDelay(shareLink: String, timeoutMs: Double, promise: Promise) {
    executor.execute {
      val ms = try {
        if (!ensureCore()) {
          -1L
        } else {
          val configJson = XrayConfigBuilder.fromShareLink(shareLink.trim())
          if (configJson.isNullOrBlank()) {
            Log.w(TAG, "failed to build xray json from link")
            -1L
          } else {
            // Prefer HTTPS generate_204 (same family as v2rayNG)
            val delay = Libv2ray.measureOutboundDelay(configJson, PROBE_URL)
            when {
              delay <= 0L -> -1L
              timeoutMs > 0 && delay > timeoutMs -> -1L
              else -> delay
            }
          }
        }
      } catch (t: Throwable) {
        Log.w(TAG, "measureOutboundDelay failed: ${t.message}")
        -1L
      }
      promise.resolve(ms.toDouble())
    }
  }

  @ReactMethod
  fun pingTcp(host: String, port: Double, timeoutMs: Double, promise: Promise) {
    executor.execute {
      val ms = try {
        tcpConnectMs(
          host.trim(),
          port.toInt().coerceIn(1, 65535),
          timeoutMs.toInt().coerceIn(500, 15_000),
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
    val names = listOf("geoip.dat", "geosite.dat", "geoip-only-cn-private.dat")
    for (name in names) {
      val out = File(dir, name)
      if (out.exists() && out.length() > 1024) continue
      try {
        reactContext.assets.open(name).use { input ->
          FileOutputStream(out).use { output -> input.copyTo(output) }
        }
      } catch (t: Throwable) {
        // AAR assets may already be merged; missing optional files are fine
        Log.w(TAG, "asset $name: ${t.message}")
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
