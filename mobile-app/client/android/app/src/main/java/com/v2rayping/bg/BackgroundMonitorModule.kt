package com.v2rayping.bg

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.core.content.ContextCompat
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import com.facebook.react.bridge.WritableNativeMap

class BackgroundMonitorModule(
  private val reactContext: ReactApplicationContext
) : ReactContextBaseJavaModule(reactContext) {

  override fun getName(): String = "BackgroundMonitor"

  @ReactMethod
  fun startMonitor(intervalMs: Double, promise: Promise) {
    try {
      val ms = intervalMs.toLong().coerceAtLeast(60_000L)
      PingMonitorService.start(reactContext.applicationContext, ms)
      promise.resolve(true)
    } catch (t: Throwable) {
      promise.reject("BG_START", t)
    }
  }

  @ReactMethod
  fun stopMonitor(promise: Promise) {
    try {
      PingMonitorService.stop(reactContext.applicationContext)
      promise.resolve(true)
    } catch (t: Throwable) {
      promise.reject("BG_STOP", t)
    }
  }

  @ReactMethod
  fun isMonitoring(promise: Promise) {
    promise.resolve(PingMonitorService::class.java.name.isNotEmpty())
  }

  @ReactMethod
  fun getPermissionStatus(promise: Promise) {
    val map = WritableNativeMap()
    map.putBoolean("notifications", hasNotificationPermission())
    map.putBoolean("batteryUnrestricted", isIgnoringBatteryOptimizations())
    promise.resolve(map)
  }

  @ReactMethod
  fun requestBatteryOptimizationExemption(promise: Promise) {
    try {
      val ctx = reactContext.applicationContext
      if (isIgnoringBatteryOptimizations()) {
        promise.resolve(true)
        return
      }
      val pkg = ctx.packageName
      val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
        data = Uri.parse("package:$pkg")
        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
      }
      ctx.startActivity(intent)
      promise.resolve(true)
    } catch (t: Throwable) {
      try {
        val fallback = Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS).apply {
          addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        reactContext.applicationContext.startActivity(fallback)
        promise.resolve(true)
      } catch (t2: Throwable) {
        promise.reject("BATTERY_PERM", t2)
      }
    }
  }

  private fun hasNotificationPermission(): Boolean {
    if (Build.VERSION.SDK_INT < 33) return true
    return ContextCompat.checkSelfPermission(
      reactContext,
      Manifest.permission.POST_NOTIFICATIONS,
    ) == PackageManager.PERMISSION_GRANTED
  }

  private fun isIgnoringBatteryOptimizations(): Boolean {
    val pm = reactContext.getSystemService(PowerManager::class.java) ?: return true
    return pm.isIgnoringBatteryOptimizations(reactContext.packageName)
  }
}
