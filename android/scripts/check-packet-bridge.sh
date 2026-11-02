#!/bin/sh
# Isolated emulator regression for idle PacketBridge shutdown. No USB or VPN consent required.
set -eu
api=${1:-34}
tag=default
certificate_public_network=true # This local pipe test does not need any emulator network setup.
out=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)/artifacts/sy910/tun-fix/probe-$api
. "$(dirname -- "$0")/emulator-lib.sh"
"$adb" -s "$serial" install -r "$apk" >/dev/null
"$adb" -s "$serial" install -r "$here/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk" >/dev/null
"$adb" -s "$serial" shell am instrument -w -e probe packet-bridge \
    com.dashplay.android.test/com.dashplay.android.CertificateNetworkProbe > "$out/result.txt"
cat "$out/result.txt"
grep -q 'success=true' "$out/result.txt"
