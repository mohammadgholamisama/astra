package com.v2rayping.xray

import android.provider.Settings
import android.util.Base64
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
 * Real-delay ping via LibXray — same path as v2rayNG / v2box:
 * share-link → outbound JSON → Libv2ray.measureOutboundDelay (≈12s, 2 HTTP tries).
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
    // LibXray measureInstDelay uses 12s HTTP client + 2 attempts — do not cut earlier
    val waitMs = timeoutMs.toLong().coerceIn(8_000L, 20_000L)
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
              Log.w(TAG, "ping: unsupported / bad share link")
              -1L
            } else {
              if (generation.get() != myGen) return@submit -1L
              measureLikeV2ray(configJson)
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
          // Slack past LibXray's own 12s so we don't cancel a valid late result
          future.get(waitMs + 2_000L, TimeUnit.MILLISECONDS)
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
   * Same URLs v2rayNG / v2box try — gstatic first (Iran-friendly), then google.
   */
  private fun measureLikeV2ray(configJson: String): Long {
    for (url in PROBE_URLS) {
      if (generation.get() < 0) return -1L // unreachable guard
      try {
        val delay = Libv2ray.measureOutboundDelay(configJson, url)
        if (delay > 0L && delay <= ACCEPT_MAX_MS) return delay
      } catch (t: Throwable) {
        Log.w(TAG, "probe $url: ${t.message}")
      }
    }
    return -1L
  }

  /**
   * Packet-loss % through share-link SOCKS (N short HTTP probes).
   * Resolves { loss_pct, sent, ok } (−1 loss = fail).
   */
  @ReactMethod
  fun measurePacketLoss(
    shareLink: String,
    probes: Double,
    perProbeTimeoutMs: Double,
    promise: Promise,
  ) {
    val myGen = generation.get()
    val n = probes.toInt().coerceIn(5, 30)
    val per = perProbeTimeoutMs.toLong().coerceIn(600L, 4_000L)
    val pool = measurePool
    val future: Future<Triple<Double, Int, Int>> =
      try {
        pool.submit<Triple<Double, Int, Int>> {
          try {
            if (generation.get() != myGen || !ensureCore()) {
              Triple(-1.0, 0, 0)
            } else {
              val res =
                XrayPacketHelper.measure(
                  shareLink.trim(),
                  n,
                  per,
                ) { generation.get() != myGen }
              Triple(res.lossPct, res.sent, res.ok)
            }
          } catch (t: Throwable) {
            Log.w(TAG, "measurePacketLoss: ${t.message}")
            Triple(-1.0, 0, 0)
          }
        }
      } catch (_: Throwable) {
        val map = Arguments.createMap()
        map.putDouble("loss_pct", -1.0)
        map.putInt("sent", 0)
        map.putInt("ok", 0)
        promise.resolve(map)
        return
      }
    inflight.add(future)
    replyPool.execute {
      val triple =
        try {
          if (generation.get() != myGen) {
            future.cancel(true)
            Triple(-1.0, 0, 0)
          } else {
            val budget = (n * (per + 80L) + 4_000L).coerceAtMost(90_000L)
            future.get(budget, TimeUnit.MILLISECONDS)
          }
        } catch (_: Throwable) {
          future.cancel(true)
          Triple(-1.0, 0, 0)
        } finally {
          inflight.remove(future)
        }
      try {
        val map = Arguments.createMap()
        map.putDouble("loss_pct", triple.first)
        map.putInt("sent", triple.second)
        map.putInt("ok", triple.third)
        promise.resolve(map)
      } catch (_: Throwable) {
      }
    }
  }

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
      } catch (t: Throwable) {
        Log.w(TAG, "hardCancel: ${t.message}")
      }
    }
  }

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
        // Required for VLESS xudp / packetEncoding — same as v2rayNG
        val deviceId = deviceIdForXudp()
        Libv2ray.initCoreEnv(assetsDir.absolutePath, deviceId)
        coreReady.set(true)
        Log.i(TAG, "Libv2ray ready · ${Libv2ray.checkVersionX()}")
        true
      } catch (t: Throwable) {
        Log.e(TAG, "initCoreEnv failed", t)
        false
      }
    }
  }

  /** Mirrors v2rayNG Utils.getDeviceIdForXUDPBaseKey() */
  private fun deviceIdForXudp(): String {
    return try {
      val androidId =
        Settings.Secure.getString(
          reactContext.contentResolver,
          Settings.Secure.ANDROID_ID,
        ) ?: "v2rayping"
      val bytes = androidId.toByteArray(Charsets.UTF_8).copyOf(32)
      Base64.encodeToString(bytes, Base64.NO_PADDING or Base64.URL_SAFE)
    } catch (t: Throwable) {
      Log.w(TAG, "xudp device id: ${t.message}")
      ""
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
    private const val ACCEPT_MAX_MS = 20_000L
    /** Prefer gstatic (often reachable), then google — same family as v2rayNG defaults. */
    private val PROBE_URLS =
      listOf(
        "https://www.gstatic.com/generate_204",
        "https://www.google.com/generate_204",
        "https://cp.cloudflare.com/",
      )

    @Volatile
    private var holder: XrayPingModule? = null

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
