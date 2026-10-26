# React Native / Hermes
-keep class com.facebook.react.** { *; }
-keep class com.facebook.hermes.** { *; }
-keep class com.facebook.jni.** { *; }
-keepclassmembers class * { @com.facebook.react.uimanager.annotations.ReactProp <methods>; }
-keepclassmembers class * { @com.facebook.react.uimanager.annotations.ReactPropGroup <methods>; }

# App native modules
-keep class com.v2rayping.** { *; }

# LibXray / gomobile
-keep class libv2ray.** { *; }
-keep class go.** { *; }
-keepclasseswithmembernames class * { native <methods>; }
-dontwarn libv2ray.**
-dontwarn go.**
