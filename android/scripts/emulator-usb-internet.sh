#!/bin/sh
# Disposable emulator only: actual VPN/kernel networking, synthetic Ethernet peer; no real iPhone/Internet.
set -eu
api=${1:?Usage: emulator-usb-internet.sh API}
share=${2:-}
certificate_public_network=true
out=${DASHPLAY_ANDROID_SDK:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/../artifacts/android-sdk}/usb-internet-$api
if [ "$share" = crash ] || [ "$share" = late ]; then out=$out-$share; fi
. "$(dirname -- "$0")/emulator-lib.sh"
if [ "$share" = share ] || [ "$share" = manager ] || [ "$share" = crash ] || [ "$share" = late ]; then
    [ "$api" = 34 ]
    "$adb" -s "$serial" root
    "$adb" -s "$serial" wait-for-device
    # Retain the emulator Wi-Fi LAN for the independently observed Mac socket. Its secondary
    # cellular eth0 otherwise gets re-scored during the strict firewall/policy cleanup comparison.
    [ "$("$adb" -s "$serial" shell getprop ro.kernel.qemu | tr -d '\r')" = 1 ]
    wifi_ready() { "$adb" -s "$serial" shell ip -4 rule show | grep -q '31000:.*lookup wlan0'; }
    wait_for 60 "emulator Wi-Fi default network" wifi_ready
    "$adb" -s "$serial" shell svc data disable
    "$adb" -s "$serial" emu gsm data unregistered
    wifi_only_ready() {
        "$adb" -s "$serial" shell ip -4 rule show > "$out/preflight-rules.txt" || return 1
        grep -q '31000:.*lookup wlan0' "$out/preflight-rules.txt" && ! grep -q 'lookup eth0' "$out/preflight-rules.txt"
    }
    wait_for 60 "emulator Wi-Fi LAN without secondary mobile network" wifi_only_ready
    base=/data/local/tmp/dashplay-shared-vpn; fixture=usb-share-vpn-fixture.sh
    if [ "$share" != share ]; then base=/data/local/tmp/dashplay-share-manager; fixture=usb-share-manager-fixture.sh; fi
    "$adb" -s "$serial" shell mkdir -p "$base"
    "$adb" -s "$serial" push "$here/scripts/$fixture" "$base/" >/dev/null
fi
shasum -a 256 "$apk"
"$adb" -s "$serial" install -r "$apk" >/dev/null
"$adb" -s "$serial" install -r "$here/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk" >/dev/null
[ "$("$adb" -s "$serial" shell getprop ro.kernel.qemu | tr -d '\r')" = 1 ]
# Lollipop's strOpToOp accepts only the fully qualified operation string.
operation=ACTIVATE_VPN
[ "$api" != 21 ] || operation=android:activate_vpn
"$adb" -s "$serial" shell appops set com.dashplay.android "$operation" allow
if [ "$share" = share ]; then
    python3 "$here/scripts/usb-internet-probe.py" --adb "$adb" --serial "$serial" --output "$out/result.txt" --share
elif [ "$share" = manager ]; then
    python3 "$here/scripts/usb-internet-probe.py" --adb "$adb" --serial "$serial" --output "$out/result.txt" --manager
elif [ "$share" = crash ]; then
    python3 "$here/scripts/usb-internet-probe.py" --adb "$adb" --serial "$serial" --output "$out/result.txt" --manager --crash
elif [ "$share" = late ]; then
    python3 "$here/scripts/usb-internet-probe.py" --adb "$adb" --serial "$serial" --output "$out/result.txt" --manager --crash --late
else
    python3 "$here/scripts/usb-internet-probe.py" --adb "$adb" --serial "$serial" --output "$out/result.txt"
fi
grep -q 'success=true' "$out/result.txt"
echo "USB_INTERNET_KERNEL_OK api=$api synthetic peer; VPN UDP TCP DNS LAN recovery cleanup"
