#!/bin/sh
set -eu
api=${1:?Usage: emulator-update.sh API}
[ "$api" -ne 21 ] || test_sdcard_size=256M
out=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)/artifacts/android-sdk/update-$api
. "$(dirname -- "$0")/emulator-lib.sh"
fixtures=$here/../artifacts/android-product/update-tests/apks
"$adb" -s "$serial" install -r "$fixtures/base.apk" >/dev/null
# Unrelated app-start permission dialogs are outside this update test.
if [ "$api" -ge 31 ]; then "$adb" -s "$serial" shell pm grant com.dashplay.android android.permission.BLUETOOTH_CONNECT; fi
if [ "$api" -ge 33 ]; then "$adb" -s "$serial" shell pm grant com.dashplay.android android.permission.POST_NOTIFICATIONS; fi
"$adb" -s "$serial" shell mkdir -p /sdcard/Download
# A shell redirection can be refused before the emulated shared storage finishes mounting; adb push waits for it.
mkdir -p "$out"
printf 'broken' > "$out/Broken.apk"
"$adb" -s "$serial" push "$out/Broken.apk" /sdcard/Download/Broken.apk >/dev/null

for pair in base:Same-version newer:New-version other-package:Other-app; do
    source=${pair%:*}; target=${pair#*:}
    "$adb" -s "$serial" push "$fixtures/$source.apk" "/sdcard/Download/$target.apk" >/dev/null
done
python3 "$here/scripts/check-update.py" --adb "$adb" --serial "$serial" --output "$out" --api "$api"
