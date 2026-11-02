#!/bin/sh
# Disposable emulator: independent client and Ethernet peer, production-generated routing/NAT commands.
set -eu
api=34
out=../artifacts/android-sdk/usb-share
. "$(dirname -- "$0")/emulator-lib.sh"
"$adb" -s "$serial" root
"$adb" -s "$serial" wait-for-device
# Boot completion precedes Wi-Fi DHCP/netd rules. Compare against its settled default network.
wifi_default_ready() { "$adb" -s "$serial" shell ip -4 rule show | grep -q '31000:.*lookup wlan0'; }
if ! wait_for 60 "emulator Wi-Fi default policy" wifi_default_ready; then
    "$adb" -s "$serial" shell ip -4 rule show
    exit 1
fi
# This test's only upstream is its private Ethernet peer. Remove emulator Wi-Fi/mobile scoring
# changes from the strict rule comparison, and exercise a device with no ordinary Internet uplink.
[ "$("$adb" -s "$serial" shell getprop ro.kernel.qemu | tr -d '\r')" = 1 ]
"$adb" -s "$serial" shell svc wifi disable
"$adb" -s "$serial" shell svc data disable
ordinary_uplink_gone() {
    "$adb" -s "$serial" shell ip -4 rule show > "$out/preflight-rules.txt" || return 1
    ! grep -q '^31000:' "$out/preflight-rules.txt"
}
wait_for 30 "emulated ordinary uplinks disconnected" ordinary_uplink_gone
fixture="$here/app/build/test-artifacts/usb-share"
test -f "$fixture/install.sh" && test -f "$fixture/remove.sh"
"$adb" -s "$serial" shell mkdir -p /data/local/tmp/dashplay-usb-share
"$adb" -s "$serial" push "$fixture/install.sh" "$fixture/remove.sh" "$here/scripts/usb-share-kernel-fixture.sh" /data/local/tmp/dashplay-usb-share/ >/dev/null
if "$adb" -s "$serial" shell sh /data/local/tmp/dashplay-usb-share/usb-share-kernel-fixture.sh; then result=0; else result=$?; fi
mkdir -p "$out/fixture"
"$adb" -s "$serial" pull /data/local/tmp/dashplay-usb-share/. "$out/fixture/" >/dev/null
exit "$result"
