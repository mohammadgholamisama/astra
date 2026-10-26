#!/bin/bash
set -e
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"
APK="/Users/Mohammad/Desktop/اپ کانفیگ/app/mobile-app/android/app/build/outputs/apk/debug/app-debug.apk"
SHOT="/Users/Mohammad/Desktop/اپ کانفیگ/app/mobile-app/screenshot-home.png"
LOG=/tmp/emu-launch.log

pkill -f qemu-system-aarch64 2>/dev/null || true
sleep 1
rm -f "$HOME/.android/avd/Pixel_7_Pro.avd"/*.lock 2>/dev/null || true

"$ANDROID_HOME/emulator/emulator" -avd Pixel_7_Pro -no-snapshot-load -memory 2048 -gpu host >"$LOG" 2>&1 &
EMU_PID=$!
echo "emu=$EMU_PID"

for i in $(seq 1 80); do
  if ! kill -0 "$EMU_PID" 2>/dev/null; then
    echo "emulator died"; tail -30 "$LOG"; exit 1
  fi
  if grep -q "Boot completed" "$LOG"; then
    echo "booted"
    break
  fi
  sleep 2
done

sleep 4
adb wait-for-device
adb install -r "$APK"
adb shell am start -n com.v2rayping/.MainActivity
sleep 4
# Dismiss 16KB dialog (OK / Don't show again)
adb shell input tap 270 1460 || true
sleep 0.5
adb shell input tap 620 1460 || true
sleep 0.5
adb shell input keyevent KEYCODE_ESCAPE || true
sleep 1
adb shell am start -n com.v2rayping/.MainActivity
sleep 5
adb exec-out screencap -p > "$SHOT"
echo "screenshot=$(wc -c < "$SHOT") bytes"
adb shell dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity' | head -5 || true
echo DONE
