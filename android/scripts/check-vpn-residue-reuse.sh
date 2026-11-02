#!/bin/sh
# Diagnostic (does not replace the full filter gate of check-vpn-process-death.sh): after a force-stop leaves the
# system's bw_costly_tun* quota chains behind, does the next VPN still carry traffic, and do the chains pile up
# when it happens again? Production VPN only, no Root manager or sharing rules, emulator API 34.
set -eu
api=34
certificate_public_network=true
out=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)/artifacts/android-sdk/vpn-residue-reuse
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
    "$adb" -s "$serial" shell ip link show > "$out/$1-links"
}
costly() { cat "$out/$1-filter" "$out/$1-filter6" | grep -c 'bw_costly_tun' || true; }
crash() {
    "$adb" -s "$serial" shell run-as com.dashplay.android rm -f cache/vpn-control
    "$adb" -s "$serial" shell am instrument -w -e probe usb-internet -e lanPort 9999 -e vpnCrashControl true \
        com.dashplay.android.test/com.dashplay.android.CertificateNetworkProbe > "$out/crash-$1.txt" 2>&1 &
    pid=$!
    ready() { "$adb" -s "$serial" shell run-as com.dashplay.android test -f cache/vpn-control; }
    wait_for 60 "control VPN $1" ready
    "$adb" -s "$serial" shell run-as com.dashplay.android cat cache/vpn-control > "$out/process-$1.txt"
    "$adb" -s "$serial" shell am force-stop com.dashplay.android
    wait "$pid" || true
    grep -q 'Process crashed' "$out/crash-$1.txt"
    sleep 30
    snapshot "after-crash-$1"
}
snapshot before
crash 1
# A normal VPN generation while the first one's chains are still there: UDP, TCP and DNS through it.
python3 "$here/scripts/usb-internet-probe.py" --adb "$adb" --serial "$serial" --output "$out/probe-with-residue.txt"
grep -q 'success=true' "$out/probe-with-residue.txt"
snapshot after-probe
crash 2
crash 3
"$adb" -s "$serial" shell dumpsys network_management > "$out/network-management.txt"
before=$(costly before); one=$(costly after-crash-1); probe=$(costly after-probe)
two=$(costly after-crash-2); three=$(costly after-crash-3)
echo "bw_costly_tun lines: before=$before after-crash-1=$one after-probe=$probe after-crash-2=$two after-crash-3=$three"
echo "interfaces in the chains after crash 3: $(cat "$out/after-crash-3-filter" | grep -o 'bw_costly_tun[0-9]*' | sort -u | tr '\n' ' ')"
echo "VPN_RESIDUE_TRAFFIC=ok (UDP/TCP/DNS through a new VPN with the residue present)"
if [ "$three" -le "$one" ]; then echo "VPN_RESIDUE_BOUNDED=yes"; else echo "VPN_RESIDUE_BOUNDED=no (grows per force-stop)"; fi
