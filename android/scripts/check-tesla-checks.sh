#!/bin/sh
# Full production DNS/HTTP server and generated NAT commands inside a disposable Android emulator.
set -eu
api=${1:-34}
mode=${2:-kernel}
case "$mode" in kernel|rules) ;; *) exit 2;; esac
out=../artifacts/android-sdk/tesla-checks-$api
[ "$mode" != rules ] || out=$out-rules
. "$(dirname -- "$0")/emulator-lib.sh"
"$adb" -s "$serial" root
"$adb" -s "$serial" wait-for-device
"$adb" -s "$serial" install -r "$apk" >/dev/null
"$adb" -s "$serial" install -r "$here/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk" >/dev/null
"$adb" -s "$serial" push "$here/scripts/tesla-checks-kernel-fixture.sh" /data/local/tmp/tesla-checks-kernel-fixture.sh >/dev/null
shasum -a 256 "$apk"
result=0
"$adb" -s "$serial" shell sh ${DASHPLAY_KERNEL_TRACE:+-x} /data/local/tmp/tesla-checks-kernel-fixture.sh "$mode" > "$out/result.txt" 2>&1 || result=$?
cat "$out/result.txt"
"$adb" -s "$serial" pull /data/data/com.dashplay.android/cache/tesla-check-probe "$out/" >/dev/null 2>&1 || true
[ "$result" = 0 ]
# Old adbd may report status zero for a failed remote shell command.
if [ "$mode" = rules ]; then grep -q '^TESLA_CHECKS_RULES_OK ' "$out/result.txt"
else grep -q '^TESLA_CHECKS_KERNEL_OK ' "$out/result.txt"; fi
