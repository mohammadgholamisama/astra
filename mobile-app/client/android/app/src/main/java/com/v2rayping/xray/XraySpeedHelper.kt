package com.v2rayping.xray

import android.util.Log
import libv2ray.CoreCallbackHandler
import libv2ray.CoreController
import libv2ray.Libv2ray
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.URL
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.max

/**
 * Short burst download + upload through a temporary Xray SOCKS outbound.
 * Relative ranking metric — not a full speedtest.
 */
object XraySpeedHelper {
  private const val TAG = "XraySpeed"
  private const val DL_URL = "https://speed.cloudflare.com/__down?bytes=5000000"
  private const val UL_URL = "https://speed.cloudflare.com/__up"
  private val socksPorts = AtomicInteger(18_560)

  data class Result(
    val downloadKbps: Double,
    val uploadKbps: Double,
  )

  fun measure(
    shareLink: String,
    downloadBudgetMs: Long,
    uploadBudgetMs: Long,
    isCancelled: () -> Boolean,
  ): Result {
    val dlBudget = downloadBudgetMs.coerceIn(800L, 5_000L)
    val ulBudget = uploadBudgetMs.coerceIn(800L, 5_000L)
    val port = nextPort()
    val configJson =
      XrayConfigBuilder.fromShareLinkWithSocks(shareLink, port)
        ?: return Result(-1.0, -1.0)

    val controller =
      Libv2ray.newCoreController(
        object : CoreCallbackHandler {
          override fun startup(): Long = 0

          override fun shutdown(): Long = 0

          override fun onEmitStatus(p0: Long, p1: String?): Long = 0
        },
      )

    var downloadKbps = -1.0
    var uploadKbps = -1.0
    try {
      if (isCancelled()) return Result(-1.0, -1.0)
      controller.startLoop(configJson, port)
      // Brief settle for SOCKS listen
      Thread.sleep(180)
      if (isCancelled() || !controller.isRunning) {
        return Result(-1.0, -1.0)
      }

      downloadKbps = downloadBurst(port, dlBudget, isCancelled)
      if (!isCancelled()) {
        uploadKbps = uploadBurst(port, ulBudget, isCancelled)
      }
    } catch (t: Throwable) {
      Log.w(TAG, "speed measure failed: ${t.message}")
    } finally {
      try {
        if (controller.isRunning) controller.stopLoop()
      } catch (_: Throwable) {
      }
    }
    return Result(downloadKbps, uploadKbps)
  }

  private fun nextPort(): Int {
    while (true) {
      val p = socksPorts.getAndIncrement()
      if (p in 18_560..28_000) return p
      socksPorts.compareAndSet(p + 1, 18_560)
    }
  }

  private fun proxy(port: Int): Proxy =
    Proxy(Proxy.Type.SOCKS, InetSocketAddress("127.0.0.1", port))

  private fun downloadBurst(
    socksPort: Int,
    budgetMs: Long,
    isCancelled: () -> Boolean,
  ): Double {
    var conn: HttpURLConnection? = null
    return try {
      conn = (URL(DL_URL).openConnection(proxy(socksPort)) as HttpURLConnection).apply {
        connectTimeout = budgetMs.toInt().coerceAtLeast(800)
        readTimeout = budgetMs.toInt().coerceAtLeast(800)
        instanceFollowRedirects = true
        requestMethod = "GET"
        setRequestProperty("Connection", "close")
        setRequestProperty("Accept-Encoding", "identity")
      }
      val started = System.nanoTime()
      val code = conn.responseCode
      if (code !in 200..299) return -1.0
      val input = conn.inputStream
      val buf = ByteArray(32 * 1024)
      var total = 0L
      while (true) {
        if (isCancelled()) break
        val elapsed = (System.nanoTime() - started) / 1_000_000L
        if (elapsed >= budgetMs) break
        val n = input.read(buf)
        if (n < 0) break
        total += n
        // Cap absolute size so a hyper-fast link doesn't fill memory/time
        if (total >= 4_000_000L) break
      }
      try {
        input.close()
      } catch (_: Throwable) {
      }
      val elapsedMs = max(1L, (System.nanoTime() - started) / 1_000_000L)
      if (total < 16_384L) return -1.0
      // kibps = (bytes * 8) / ms
      (total.toDouble() * 8.0) / elapsedMs.toDouble()
    } catch (t: Throwable) {
      Log.w(TAG, "download burst: ${t.message}")
      -1.0
    } finally {
      try {
        conn?.disconnect()
      } catch (_: Throwable) {
      }
    }
  }

  private fun uploadBurst(
    socksPort: Int,
    budgetMs: Long,
    isCancelled: () -> Boolean,
  ): Double {
    var conn: HttpURLConnection? = null
    return try {
      conn = (URL(UL_URL).openConnection(proxy(socksPort)) as HttpURLConnection).apply {
        connectTimeout = budgetMs.toInt().coerceAtLeast(800)
        readTimeout = budgetMs.toInt().coerceAtLeast(800)
        doOutput = true
        requestMethod = "POST"
        setRequestProperty("Content-Type", "application/octet-stream")
        setRequestProperty("Connection", "close")
        // Unknown length → chunked
        setChunkedStreamingMode(32 * 1024)
      }
      val started = System.nanoTime()
      val out: OutputStream = conn.outputStream
      val chunk = ByteArray(32 * 1024) { 0x61 }
      var total = 0L
      while (true) {
        if (isCancelled()) break
        val elapsed = (System.nanoTime() - started) / 1_000_000L
        if (elapsed >= budgetMs) break
        out.write(chunk)
        total += chunk.size
        if (total >= 2_500_000L) break
      }
      try {
        out.flush()
        out.close()
      } catch (_: Throwable) {
      }
      // Drain response (Cloudflare may 200 with empty body)
      try {
        conn.responseCode
        conn.inputStream?.close()
      } catch (_: Throwable) {
        try {
          conn.errorStream?.close()
        } catch (_: Throwable) {
        }
      }
      val elapsedMs = max(1L, (System.nanoTime() - started) / 1_000_000L)
      if (total < 16_384L) return -1.0
      (total.toDouble() * 8.0) / elapsedMs.toDouble()
    } catch (t: Throwable) {
      Log.w(TAG, "upload burst: ${t.message}")
      -1.0
    } finally {
      try {
        conn?.disconnect()
      } catch (_: Throwable) {
      }
    }
  }
}
