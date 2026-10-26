#!/usr/bin/env bash
# Build a sideload-ready release APK with embedded JS (no Metro needed).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> metro header fix (non-ASCII paths)"
node scripts/fix-metro-header.js || true

echo "==> release bundle + APK"
cd android
./gradlew clean :app:assembleRelease

OUT="$ROOT/android/app/build/outputs/apk/release"
echo "==> outputs:"
ls -lh "$OUT"/*.apk 2>/dev/null || ls -lhR "$OUT"
echo
echo "Install on phone (arm64 most devices):"
echo "  adb install -r $OUT/app-arm64-v8a-release.apk"
echo "Or universal:"
echo "  adb install -r $OUT/app-universal-release.apk"
