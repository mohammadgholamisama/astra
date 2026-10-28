package com.v2rayping.xray

import android.util.Log
import libv2ray.CoreCallbackHandler
import libv2ray.Libv2ray
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.URL
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.roundToInt

/**
 * Packet-loss estimate through a temporary Xray SOCKS outbound.
 * Sends N short HTTP probes; loss% = failed / sent * 100.
 */
object XrayPacketHelper {
  private const val TAG = "XrayPacket"
  private const val PROBE_URL = "https://www.gstatic.com/generate_204"
  private val socksPorts = AtomicInteger(28_560)

  data class Result(
    /** 0..100; −1 = measure failed entirely */
    val lossPct: Double,
    val sent: Int,
    val ok: Int,
  )

  fun measure(
    shareLink: String,
    probes: Int,
    perProbeTimeoutMs: Long,
    isCancelled: () -> Boolean,
  ): Result {
    val n = probes.coerceIn(5, 30)
    val timeout = perProbeTimeoutMs.coerceIn(600L, 4_000L)
    val port = nextPort()
    val configJson =
      XrayConfigBuilder.fromShareLinkWithSocks(shareLink, port)
        ?: return Result(-1.0, 0, 0)

    val controller =
      Libv2ray.newCoreController(
        object : CoreCallbackHandler {
          override fun startup(): Long = 0

          override fun shutdown(): Long = 0

          override fun onEmitStatus(p0: Long, p1: String?): Long = 0
        },
      )

    var sent = 0
    var ok = 0
    try {
      if (isCancelled()) return Result(-1.0, 0, 0)
      controller.startLoop(configJson, port)
      Thread.sleep(280)
      if (isCancelled() || !controller.isRunning) {
        return Result(-1.0, 0, 0)
      }

      val proxy = Proxy(Proxy.Type.SOCKS, InetSocketAddress("127.0.0.1", port))
      for (i in 0 until n) {
        if (isCancelled()) break
        sent += 1
        if (probeOnce(proxy, timeout)) ok += 1
        // Tiny gap so path isn't perfectly pipelined
        if (i < n - 1 && !isCancelled()) {
          Thread.sleep(40)
        }
      }
    } catch (t: Throwable) {
      Log.w(TAG, "packet measure failed: ${t.message}")
    } finally {
      try {
        if (controller.isRunning) controller.stopLoop()
      } catch (_: Throwable) {
      }
    }

    if (sent < 1) return Result(-1.0, 0, 0)
    val loss = ((sent - ok).toDouble() / sent.toDouble()) * 100.0
    return Result((loss * 10.0).roundToInt() / 10.0, sent, ok)
  }

  private fun probeOnce(proxy: Proxy, timeoutMs: Long): Boolean {
    var conn: HttpURLConnection? = null
    return try {
      conn = (URL(PROBE_URL).openConnection(proxy) as HttpURLConnection).apply {
        connectTimeout = timeoutMs.toInt()
        readTimeout = timeoutMs.toInt()
        instanceFollowRedirects = false
        requestMethod = "GET"
        setRequestProperty("Connection", "close")
        setRequestProperty("Accept-Encoding", "identity")
      }
      val code = conn.responseCode
      try {
        conn.inputStream?.close()
      } catch (_: Throwable) {
        try {
          conn.errorStream?.close()
        } catch (_: Throwable) {
        }
      }
      code in 200..299 || code == 204
    } catch (_: Throwable) {
      false
    } finally {
      try {
        conn?.disconnect()
      } catch (_: Throwable) {
      }
    }
  }

  private fun nextPort(): Int {
    while (true) {
      val p = socksPorts.getAndIncrement()
      if (p in 28_560..38_000) return p
      socksPorts.compareAndSet(p + 1, 28_560)
    }
  }
}
