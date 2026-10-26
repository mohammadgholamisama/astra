#!/bin/bash
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"
APK="/Users/Mohammad/Desktop/اپ کانفیگ/app/mobile-app/android/app/build/outputs/apk/debug/app-debug.apk"
LOG=/tmp/emu-terminal.log

pkill -9 -f qemu-system-aarch64 2>/dev/null || true
sleep 1
rm -f "$HOME/.android/avd/Pixel_7_Pro.avd"/*.lock 2>/dev/null || true

echo "▶ شبیه‌ساز بزرگ Pixel_7_Pro…"
"$ANDROID_HOME/emulator/emulator" -avd Pixel_7_Pro -memory 3072 -gpu host -no-snapshot-save \
  -skin 1260x2520 >"$LOG" 2>&1 &
EMU_PID=$!
echo "emu pid=$EMU_PID"

echo "▶ منتظر بوت…"
for i in $(seq 1 90); do
  if ! kill -0 "$EMU_PID" 2>/dev/null; then
    echo "✗ امولاتور بسته شد"; tail -30 "$LOG"; exit 1
  fi
  if grep -q "Boot completed" "$LOG"; then
    echo "✓ بوت شد"
    break
  fi
  sleep 2
done

sleep 3
adb wait-for-device
adb install -r "$APK"
adb shell am start -n com.v2rayping/.MainActivity
sleep 2
adb shell input tap 900 2100 2>/dev/null || true
adb shell am start -n com.v2rayping/.MainActivity

echo ""
echo "════════════════════════════════════"
echo "  اپ آماده — Reality Ping فعال است"
echo "  این پنجره را باز نگه دارید"
echo "════════════════════════════════════"
wait "$EMU_PID"
