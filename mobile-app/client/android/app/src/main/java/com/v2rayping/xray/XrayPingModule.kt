package com.v2rayping.xray

import android.util.Log
import com.facebook.react.bridge.Arguments
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
 * Real-delay ping via LibXray.
 * Cancel is non-blocking on the RN bridge — resolve first, tear down pool off-thread.
 */
class XrayPingModule(
  private val reactContext: ReactApplicationContext
) : ReactContextBaseJavaModule(reactContext) {

  @Volatile private var measurePool: ExecutorService = newMeasurePool()
  private val replyPool: ExecutorService = Executors.newCachedThreadPool()
  private val coreReady = AtomicBoolean(false)
  private val generation = AtomicInteger(0)
  private val inflight = ConcurrentHashMap.newKeySet<Future<*>>()

  init {
    holder = this
  }

  override fun getName(): String = "XrayPing"

  override fun invalidate() {
    holder = null
    hardCancelAsync()
    super.invalidate()
  }

  @ReactMethod
  fun isAvailable(promise: Promise) {
    promise.resolve(ensureCore())
  }

  /**
   * Resolve immediately so JS cancel button never waits on pool teardown.
   */
  @ReactMethod
  fun cancelAllPings(promise: Promise) {
    generation.incrementAndGet()
    try {
      promise.resolve(true)
    } catch (_: Throwable) {
    }
    hardCancelAsync()
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
    // waitMs = how long we wait for LibXray; accept any positive delay up to ACCEPT_MAX
    // (old code discarded valid pings when delay > waitMs → false «قطع»)
    val waitMs = timeoutMs.toLong().coerceIn(1_500L, 12_000L)
    val myGen = generation.get()
    if (myGen != generation.get()) {
      promise.resolve(-1.0)
      return
    }
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
                delay > ACCEPT_MAX_MS -> -1L
                else -> delay
              }
            }
          }
        } catch (t: Throwable) {
          Log.w(TAG, "measureOutboundDelay failed: ${t.message}")
          -1L
        }
      }
    } catch (_: Throwable) {
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
          // Extra slack so a slow-but-valid measure can still return
          future.get(waitMs + 800L, TimeUnit.MILLISECONDS)
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
      }
    }
  }

  /**
   * Short download+upload burst through the share-link outbound (SOCKS).
   * Resolves { download_kbps, upload_kbps } (−1 on fail).
   */
  @ReactMethod
  fun measureSpeed(
    shareLink: String,
    downloadBudgetMs: Double,
    uploadBudgetMs: Double,
    promise: Promise,
  ) {
    val myGen = generation.get()
    val dl = downloadBudgetMs.toLong().coerceIn(800L, 5_000L)
    val ul = uploadBudgetMs.toLong().coerceIn(800L, 5_000L)
    val pool = measurePool
    val future: Future<Pair<Double, Double>> =
      try {
        pool.submit<Pair<Double, Double>> {
          try {
            if (generation.get() != myGen || !ensureCore()) {
              -1.0 to -1.0
            } else {
              val res =
                XraySpeedHelper.measure(
                  shareLink.trim(),
                  dl,
                  ul,
                ) { generation.get() != myGen }
              res.downloadKbps to res.uploadKbps
            }
          } catch (t: Throwable) {
            Log.w(TAG, "measureSpeed: ${t.message}")
            -1.0 to -1.0
          }
        }
      } catch (_: Throwable) {
        val map = Arguments.createMap()
        map.putDouble("download_kbps", -1.0)
        map.putDouble("upload_kbps", -1.0)
        promise.resolve(map)
        return
      }
    inflight.add(future)
    replyPool.execute {
      val pair =
        try {
          if (generation.get() != myGen) {
            future.cancel(true)
            -1.0 to -1.0
          } else {
            future.get(dl + ul + 4_000L, TimeUnit.MILLISECONDS)
          }
        } catch (_: Throwable) {
          future.cancel(true)
          -1.0 to -1.0
        } finally {
          inflight.remove(future)
        }
      try {
        val map = Arguments.createMap()
        map.putDouble("download_kbps", pair.first)
        map.putDouble("upload_kbps", pair.second)
        promise.resolve(map)
      } catch (_: Throwable) {
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
              timeoutMs.toInt().coerceIn(150, 2_500),
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
    replyPool.execute {
      try {
        future.get(3, TimeUnit.SECONDS)
      } catch (_: Throwable) {
        future.cancel(true)
      } finally {
        inflight.remove(future)
      }
    }
  }

  private fun hardCancelAsync() {
    replyPool.execute {
      try {
        val copy = inflight.toTypedArray()
        inflight.clear()
        for (f in copy) {
          try {
            f.cancel(true)
          } catch (_: Throwable) {
          }
        }
        val old = measurePool
        measurePool = newMeasurePool()
        try {
          old.shutdownNow()
        } catch (_: Throwable) {
        }
        // Don't awaitTermination — LibXray may ignore interrupt; abandoning
        // the old pool keeps the UI responsive and reopen clean.
      } catch (t: Throwable) {
        Log.w(TAG, "hardCancel: ${t.message}")
      }
    }
  }

  /**
   * Cached pool: hung JNI measureOutboundDelay threads must not starve the
   * fixed queue (classic stuck-at-98% when the last slots never free).
   */
  private fun newMeasurePool(): ExecutorService =
    Executors.newCachedThreadPool { r ->
      Thread(r, "xray-ping").apply { isDaemon = true }
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
    /** Accept real measured delays up to this (ms); wait timeout is separate. */
    private const val ACCEPT_MAX_MS = 15_000L
    const val PROBE_URL = "https://www.gstatic.com/generate_204"

    @Volatile
    private var holder: XrayPingModule? = null

    /** Called from Activity lifecycle — no JS bridge required. */
    @JvmStatic
    fun cancelFromNative() {
      try {
        holder?.let {
          it.generation.incrementAndGet()
          it.hardCancelAsync()
        }
      } catch (_: Throwable) {
      }
    }
  }
}
