#!/bin/sh
# Diagnostic control: production VPN only, no sharing/Tesla Root commands.
set -eu
api=34
certificate_public_network=true
out=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)/artifacts/android-sdk/vpn-process-death-control
. "$(dirname -- "$0")/emulator-lib.sh"
"$adb" -s "$serial" root
"$adb" -s "$serial" wait-for-device
[ "$("$adb" -s "$serial" shell getprop ro.kernel.qemu | tr -d '\r')" = 1 ]
wifi_ready() { "$adb" -s "$serial" shell ip -4 rule show | grep -q '31000:.*lookup wlan0'; }
wait_for 60 "emulator Wi-Fi" wifi_ready
"$adb" -s "$serial" shell svc data disable
"$adb" -s "$serial" emu gsm data unregistered
wifi_only() { ! "$adb" -s "$serial" shell ip -4 rule show | grep -q 'lookup eth0'; }
wait_for 60 "secondary network removal" wifi_only
"$adb" -s "$serial" install -r "$apk" >/dev/null
"$adb" -s "$serial" install -r "$here/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk" >/dev/null
"$adb" -s "$serial" shell appops set com.dashplay.android ACTIVATE_VPN allow
shasum -a 256 "$apk"
snapshot() {
    "$adb" -s "$serial" shell iptables -w 2 -t filter -S > "$out/$1-filter"
    "$adb" -s "$serial" shell ip6tables -w 2 -t filter -S > "$out/$1-filter6"
    "$adb" -s "$serial" shell iptables -w 2 -t nat -S > "$out/$1-nat"
    "$adb" -s "$serial" shell ip link show > "$out/$1-links"
}
snapshot before
"$adb" -s "$serial" shell am instrument -w -e probe usb-internet -e lanPort 9999 -e vpnCrashControl true \
    com.dashplay.android.test/com.dashplay.android.CertificateNetworkProbe > "$out/instrumentation.txt" 2>&1 &
instrumentation=$!
ready() { "$adb" -s "$serial" shell run-as com.dashplay.android test -f cache/vpn-control; }
wait_for 60 "control VPN" ready
"$adb" -s "$serial" shell run-as com.dashplay.android cat cache/vpn-control > "$out/process.txt"
snapshot running
"$adb" -s "$serial" shell am force-stop com.dashplay.android
wait "$instrumentation"
grep -q 'Process crashed' "$out/instrumentation.txt"
for delay in 1 5 30; do
    sleep "$delay"
    snapshot "after-$delay"
done
"$adb" -s "$serial" shell dumpsys netpolicy > "$out/netpolicy.txt"
"$adb" -s "$serial" shell dumpsys network_management > "$out/network-management.txt"
"$adb" -s "$serial" shell dumpsys connectivity > "$out/connectivity.txt"
! grep -q 'tun[0-9]*:' "$out/after-30-links"
! grep -q 'DP_USB_\|DP_TESLA_' "$out/running-nat"
cmp "$out/before-nat" "$out/after-30-nat"
echo 'VPN_CONTROL_OBSERVED no Root manager or sharing rules; process killed and TUN removed'
for family in filter filter6; do
    if cmp -s "$out/before-$family" "$out/after-30-$family"; then
        echo "VPN_CONTROL_$family=identical"
    else
        echo "VPN_CONTROL_$family=changed"
        diff -u "$out/before-$family" "$out/after-30-$family" || [ "$?" = 1 ]
    fi
done
