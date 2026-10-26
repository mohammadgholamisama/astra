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
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * Real-delay ping via LibXray. Supports hard cancel so closing the app / tapping
 * «قطع تست» does not leave 16 Xray cores spinning (black screen / ANR on reopen).
 */
class XrayPingModule(
  private val reactContext: ReactApplicationContext
) : ReactContextBaseJavaModule(reactContext) {

  @Volatile private var measurePool: ExecutorService = Executors.newFixedThreadPool(12)
  private val replyPool: ExecutorService = Executors.newCachedThreadPool()
  private val coreReady = AtomicBoolean(false)
  private val generation = AtomicInteger(0)
  private val inflight = ConcurrentHashMap.newKeySet<Future<*>>()

  override fun getName(): String = "XrayPing"

  @ReactMethod
  fun isAvailable(promise: Promise) {
    promise.resolve(ensureCore())
  }

  /** Abort every in-flight measure immediately and bump generation. */
  @ReactMethod
  fun cancelAllPings(promise: Promise) {
    try {
      generation.incrementAndGet()
      val copy = inflight.toTypedArray()
      inflight.clear()
      for (f in copy) {
        try {
          f.cancel(true)
        } catch (_: Throwable) {
        }
      }
      // Drain stuck workers — LibXray may ignore interrupt, so replace the pool
      val old = measurePool
      measurePool = Executors.newFixedThreadPool(12)
      try {
        old.shutdownNow()
      } catch (_: Throwable) {
      }
      promise.resolve(true)
    } catch (t: Throwable) {
      Log.w(TAG, "cancelAllPings: ${t.message}")
      promise.resolve(false)
    }
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

  @ReactMethod
  fun pingRealDelay(shareLink: String, timeoutMs: Double, promise: Promise) {
    val capMs = timeoutMs.toLong().coerceIn(700L, 5_000L)
    val myGen = generation.get()
    val pool = measurePool
    val future: Future<Long> = try {
      pool.submit<Long> {
        if (generation.get() != myGen) return@submit -1L
        try {
          if (!ensureCore()) {
            -1L
          } else {
            val configJson = XrayConfigBuilder.fromShareLink(shareLink.trim())
            if (configJson.isNullOrBlank()) {
              -1L
            } else {
              if (generation.get() != myGen) return@submit -1L
              val delay = Libv2ray.measureOutboundDelay(configJson, PROBE_URL)
              when {
                generation.get() != myGen -> -1L
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
    } catch (t: Throwable) {
      promise.resolve(-1.0)
      return
    }
    inflight.add(future)
    replyPool.execute {
      val ms = try {
        if (generation.get() != myGen) {
          future.cancel(true)
          -1L
        } else {
          future.get(capMs + 200L, TimeUnit.MILLISECONDS)
        }
      } catch (_: TimeoutException) {
        future.cancel(true)
        -1L
      } catch (_: Throwable) {
        future.cancel(true)
        -1L
      } finally {
        inflight.remove(future)
      }
      try {
        promise.resolve(ms.toDouble())
      } catch (_: Throwable) {
        // Bridge already torn down (activity destroyed)
      }
    }
  }

  @ReactMethod
  fun pingTcp(host: String, port: Double, timeoutMs: Double, promise: Promise) {
    val myGen = generation.get()
    val pool = measurePool
    val future: Future<*> = try {
      pool.submit {
        val ms = try {
          if (generation.get() != myGen) {
            -1L
          } else {
            tcpConnectMs(
              host.trim(),
              port.toInt().coerceIn(1, 65535),
              timeoutMs.toInt().coerceIn(150, 3_000),
            )
          }
        } catch (_: Throwable) {
          -1L
        }
        try {
          promise.resolve(ms.toDouble())
        } catch (_: Throwable) {
        }
      }
    } catch (_: Throwable) {
      promise.resolve(-1.0)
      return
    }
    inflight.add(future)
    // Remove when done — wrapped above resolves inside submit; clean up via reply
    replyPool.execute {
      try {
        future.get(4, TimeUnit.SECONDS)
      } catch (_: Throwable) {
        future.cancel(true)
      } finally {
        inflight.remove(future)
      }
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
