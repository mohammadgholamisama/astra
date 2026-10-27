package com.v2rayping

import com.facebook.react.ReactActivity
import com.facebook.react.ReactActivityDelegate
import com.facebook.react.defaults.DefaultNewArchitectureEntryPoint.fabricEnabled
import com.facebook.react.defaults.DefaultReactActivityDelegate
import com.v2rayping.xray.XrayPingModule

class MainActivity : ReactActivity() {

  override fun getMainComponentName(): String = "V2RayPing"

  override fun createReactActivityDelegate(): ReactActivityDelegate =
      DefaultReactActivityDelegate(this, mainComponentName, fabricEnabled)

  // Only tear down Xray when the Activity is actually destroyed (process dying /
  // finishing). Cancelling onPause/onStop was aborting mid-test whenever the
  // user left the app briefly — configs never finished.
  override fun onDestroy() {
    XrayPingModule.cancelFromNative()
    super.onDestroy()
  }
}
