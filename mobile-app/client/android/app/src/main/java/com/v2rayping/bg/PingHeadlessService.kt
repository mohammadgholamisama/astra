package com.v2rayping.bg

import android.content.Intent
import com.facebook.react.HeadlessJsTaskService
import com.facebook.react.bridge.Arguments
import com.facebook.react.jstasks.HeadlessJsTaskConfig

class PingHeadlessService : HeadlessJsTaskService() {
  override fun getTaskConfig(intent: Intent?): HeadlessJsTaskConfig? {
    if (intent == null) return null
    return HeadlessJsTaskConfig(
      "BackgroundPingCycle",
      Arguments.createMap(),
      180_000, // allow up to 3 minutes for a full Real Delay pass
      true, // allowed in foreground
    )
  }
}
