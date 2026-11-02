#!/bin/sh
# Read-only public HTTPS compatibility on a disposable emulator; no account/DNS/certificate mutations.
set -eu
api=${1:?Usage: emulator-certificate-network.sh API}
certificate_public_network=true
out=${DASHPLAY_ANDROID_SDK:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/../artifacts/android-sdk}/certificate-network-$api
. "$(dirname -- "$0")/emulator-lib.sh"
shasum -a 256 "$apk"
online() { "$adb" -s "$serial" shell dumpsys connectivity | grep -E 'Active default network: [0-9]+'; }
wait_for 60 "emulator Internet network" online
"$adb" -s "$serial" shell dumpsys connectivity > "$out/connectivity.txt"
"$adb" -s "$serial" install -r "$apk" >/dev/null
"$adb" -s "$serial" install -r "$here/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk" >/dev/null
"$adb" -s "$serial" shell am instrument -w com.dashplay.android.test/com.dashplay.android.CertificateNetworkProbe > "$out/result.txt"
cat "$out/result.txt"
grep -q 'success=true' "$out/result.txt"
echo "CERTIFICATE_NETWORK_OK api=$api read-only public HTTPS; no certificate issued"
