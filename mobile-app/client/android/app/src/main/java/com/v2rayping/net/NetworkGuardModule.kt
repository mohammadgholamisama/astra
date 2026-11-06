package com.v2rayping.net

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Build
import android.os.Handler
import android.os.Looper
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import com.facebook.react.modules.core.DeviceEventManagerModule

/**
 * Detects whether **this app's traffic** is routed through a system VPN.
 *
 * - VPN on + this app excluded from VPN → false → tests allowed
 * - VPN on + this app included → true → tests blocked
 * - VPN off → false
 *
 * Uses the process active/bound network only — never "any VPN on device".
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
      promise.resolve(isAppTrafficThroughVpn(reactContext.applicationContext))
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

          override fun onLinkPropertiesChanged(
            network: Network,
            linkProperties: android.net.LinkProperties,
          ) = emitVpnState()
        }
      // Any network change → re-evaluate *this process* routing
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
      val active = isAppTrafficThroughVpn(reactContext.applicationContext)
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

    /** @see isAppTrafficThroughVpn */
    @JvmStatic
    fun checkVpnActive(context: Context): Boolean = isAppTrafficThroughVpn(context)

    /**
     * True only if this process's default/bound network is a VPN tunnel.
     * If the VPN app excludes this package, activeNetwork is Wi‑Fi/cell → false.
     */
    @JvmStatic
    fun isAppTrafficThroughVpn(context: Context): Boolean {
      val cm =
        context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
          ?: return false
      return try {
        val network = processNetwork(cm) ?: return false
        val caps = cm.getNetworkCapabilities(network) ?: return false
        caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
      } catch (_: Throwable) {
        false
      }
    }

    /** Network this process currently uses (honours bindProcessToNetwork). */
    private fun processNetwork(cm: ConnectivityManager): Network? {
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
        cm.boundNetworkForProcess?.let { return it }
      }
      return cm.activeNetwork
    }
  }
}
