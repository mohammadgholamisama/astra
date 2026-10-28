package com.v2rayping.net

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.Looper
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import com.facebook.react.modules.core.DeviceEventManagerModule

/**
 * Detects system VPN (any app tunnel). Tests must not run while VPN is up.
 */
class NetworkGuardModule(
  private val reactContext: ReactApplicationContext,
) : ReactContextBaseJavaModule(reactContext) {

  private val mainHandler = Handler(Looper.getMainLooper())
  private var callback: ConnectivityManager.NetworkCallback? = null
  private var lastEmitted: Boolean? = null

  override fun getName(): String = "NetworkGuard"

  override fun getConstants(): MutableMap<String, Any> =
    hashMapOf("VPN_EVENT" to EVENT_NAME)

  @ReactMethod
  fun isVpnActive(promise: Promise) {
    try {
      promise.resolve(checkVpnActive(reactContext.applicationContext))
    } catch (t: Throwable) {
      promise.reject("VPN_CHECK", t)
    }
  }

  @ReactMethod
  fun startWatching(promise: Promise) {
    try {
      if (callback != null) {
        promise.resolve(true)
        return
      }
      val cm = connectivity()
      if (cm == null) {
        promise.reject("VPN_WATCH", "ConnectivityManager missing")
        return
      }
      val cb =
        object : ConnectivityManager.NetworkCallback() {
          override fun onAvailable(network: Network) = emitVpnState()

          override fun onLost(network: Network) = emitVpnState()

          override fun onCapabilitiesChanged(
            network: Network,
            networkCapabilities: NetworkCapabilities,
          ) = emitVpnState()
        }
      // All networks — VPN up/down shows up here
      val req = NetworkRequest.Builder().build()
      cm.registerNetworkCallback(req, cb)
      callback = cb
      lastEmitted = null
      emitVpnState()
      promise.resolve(true)
    } catch (t: Throwable) {
      promise.reject("VPN_WATCH", t)
    }
  }

  @ReactMethod
  fun stopWatching(promise: Promise) {
    try {
      unregister()
      promise.resolve(true)
    } catch (t: Throwable) {
      promise.reject("VPN_UNWATCH", t)
    }
  }

  @ReactMethod
  fun addListener(eventName: String) {
    /* RN event emitter contract */
  }

  @ReactMethod
  fun removeListeners(count: Int) {
    /* RN event emitter contract */
  }

  override fun invalidate() {
    unregister()
    super.invalidate()
  }

  private fun unregister() {
    val cb = callback ?: return
    callback = null
    lastEmitted = null
    try {
      connectivity()?.unregisterNetworkCallback(cb)
    } catch (_: Throwable) {
      /* already gone */
    }
  }

  private fun connectivity(): ConnectivityManager? =
    reactContext.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager

  private fun emitVpnState() {
    mainHandler.post {
      val active = checkVpnActive(reactContext.applicationContext)
      if (lastEmitted == active) return@post
      lastEmitted = active
      if (!reactContext.hasActiveReactInstance()) return@post
      val payload = Arguments.createMap().apply { putBoolean("active", active) }
      reactContext
        .getJSModule(DeviceEventManagerModule.RCTDeviceEventEmitter::class.java)
        .emit(EVENT_NAME, payload)
    }
  }

  companion object {
    const val EVENT_NAME = "vpnStateChanged"

    fun checkVpnActive(context: Context): Boolean {
      val cm =
        context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
          ?: return false
      return try {
        cm.allNetworks.any { net ->
          val caps = cm.getNetworkCapabilities(net) ?: return@any false
          caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
        }
      } catch (_: Throwable) {
        false
      }
    }
  }
}
