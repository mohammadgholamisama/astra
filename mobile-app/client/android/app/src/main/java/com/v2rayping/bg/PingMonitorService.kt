package com.v2rayping.bg

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.app.NotificationCompat
import com.facebook.react.HeadlessJsTaskService
import com.v2rayping.MainActivity
import com.v2rayping.R

/**
 * Sticky foreground service — every [intervalMs] starts Headless JS Real Delay cycle.
 * Uses an in-process Handler (no SCHEDULE_EXACT_ALARM needed).
 */
class PingMonitorService : Service() {

  private var intervalMs = DEFAULT_INTERVAL
  private val handler = Handler(Looper.getMainLooper())
  private val tickRunnable = object : Runnable {
    override fun run() {
      fireHeadless()
      handler.postDelayed(this, intervalMs)
    }
  }

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    when (intent?.action) {
      ACTION_STOP -> {
        handler.removeCallbacks(tickRunnable)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        return START_NOT_STICKY
      }
      else -> {
        intervalMs = intent?.getLongExtra(EXTRA_INTERVAL, DEFAULT_INTERVAL) ?: DEFAULT_INTERVAL
        if (intervalMs < 60_000L) intervalMs = DEFAULT_INTERVAL
        createChannel()
        val mins = (intervalMs / 60_000L).coerceAtLeast(1)
        startForeground(
          NOTIF_ID,
          buildNotification("پایش خودکار هر $mins دقیقه — حتی با اپ بسته‌شده"),
        )
        handler.removeCallbacks(tickRunnable)
        // Don't fire immediately — manual «تست الان» covers that; timer owns background cycles
        handler.postDelayed(tickRunnable, intervalMs)
        return START_STICKY
      }
    }
  }

  override fun onDestroy() {
    handler.removeCallbacks(tickRunnable)
    super.onDestroy()
  }

  private fun fireHeadless() {
    try {
      HeadlessJsTaskService.acquireWakeLockNow(applicationContext)
      startService(Intent(applicationContext, PingHeadlessService::class.java))
    } catch (_: Throwable) {
      /* ignore */
    }
  }

  private fun createChannel() {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
    val mgr = getSystemService(NotificationManager::class.java)
    val ch = NotificationChannel(CHANNEL_ID, "پایش پینگ", NotificationManager.IMPORTANCE_LOW)
    ch.description = "تست خودکار Real Delay در پس‌زمینه"
    mgr.createNotificationChannel(ch)
  }

  private fun buildNotification(text: String): Notification {
    val open = PendingIntent.getActivity(
      this,
      0,
      Intent(this, MainActivity::class.java),
      PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )
    return NotificationCompat.Builder(this, CHANNEL_ID)
      .setContentTitle("پینگ واقعی")
      .setContentText(text)
      .setSmallIcon(R.mipmap.ic_launcher)
      .setContentIntent(open)
      .setOngoing(true)
      .setSilent(true)
      .build()
  }

  companion object {
    const val ACTION_STOP = "com.v2rayping.bg.STOP"
    const val EXTRA_INTERVAL = "interval_ms"
    const val DEFAULT_INTERVAL = 5 * 60 * 1000L
    private const val CHANNEL_ID = "ping_monitor"
    private const val NOTIF_ID = 7101

    fun start(context: Context, intervalMs: Long = DEFAULT_INTERVAL) {
      val i = Intent(context, PingMonitorService::class.java).putExtra(EXTRA_INTERVAL, intervalMs)
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        context.startForegroundService(i)
      } else {
        context.startService(i)
      }
    }

    fun stop(context: Context) {
      context.startService(Intent(context, PingMonitorService::class.java).setAction(ACTION_STOP))
    }
  }
}
