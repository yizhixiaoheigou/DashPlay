#!/bin/sh
# Verify boot launch policy and USB resolution before unlock; no real USB or permission changes.
set -eu
api=${1:-25}
test "$api" -ge 24 || { echo 'USB Direct Boot probe requires API 24+'; exit 2; }
tag=default
certificate_public_network=true
out=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)/artifacts/android-test/usb-boot-$api
. "$(dirname -- "$0")/emulator-lib.sh"
"$adb" -s "$serial" install -r "$apk" >/dev/null
"$adb" -s "$serial" install -r "$here/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk" >/dev/null
"$adb" -s "$serial" shell am instrument -w -e probe usb-boot \
    com.dashplay.android.test/com.dashplay.android.CertificateNetworkProbe > "$out/result.txt"
cat "$out/result.txt"
grep -q 'success=true' "$out/result.txt"
