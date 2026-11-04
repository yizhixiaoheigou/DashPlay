#!/bin/sh
# Boot an Android emulator at the given API level, install the debug APK and check that the receiver comes up:
# the page over HTTPS, then (with a DiPlay APK) the identity import and the AirPlay listener. Runs on the Mac.
# The emulator proves the app starts on that Android version; Bluetooth, hotspot and CarPlay need a real device.
#
# Usage (from android/): sh scripts/emulator-smoke.sh API [DIPLAY_APK]
#   SDK: $DASHPLAY_ANDROID_SDK, default ../artifacts/android-sdk with emulator, platform-tools and
#   system-images;android-API;default;arm64-v8a installed (sdkmanager --sdk_root=...).
set -eu
api=${1:?Usage: emulator-smoke.sh API [DIPLAY_APK]}
diplay=${2:-}
out=${DASHPLAY_ANDROID_SDK:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/../artifacts/android-sdk}/smoke-$api
web=$((18000 + api))
. "$(dirname -- "$0")/emulator-lib.sh"
shasum -a 256 "$apk"
"$adb" -s "$serial" install -r "$apk" >/dev/null
# Android 13+ asks for nearby devices when the hotspot starts; that modal covers the home screen and the later
# native UI checks would tap through it. Grant the same set the screens walk grants, before any screen opens.
if [ "$api" -ge 23 ]; then
    for p in ACCESS_FINE_LOCATION READ_EXTERNAL_STORAGE RECORD_AUDIO; do "$adb" -s "$serial" shell pm grant com.dashplay.android android.permission.$p 2>/dev/null || true; done
fi
if [ "$api" -ge 31 ]; then
    for p in BLUETOOTH_CONNECT BLUETOOTH_SCAN; do "$adb" -s "$serial" shell pm grant com.dashplay.android android.permission.$p 2>/dev/null || true; done
fi
if [ "$api" -ge 33 ]; then
    for p in POST_NOTIFICATIONS NEARBY_WIFI_DEVICES; do "$adb" -s "$serial" shell pm grant com.dashplay.android android.permission.$p 2>/dev/null || true; done
fi
"$adb" -s "$serial" logcat -c
"$adb" -s "$serial" shell am start -n com.dashplay.android/.MainActivity >/dev/null
wait_for 90 "page" logged "page on"
"$adb" -s "$serial" logcat -d -s DashPlay:I | grep "page on" | tail -1
"$adb" -s "$serial" forward "tcp:$web" tcp:18743 >/dev/null
for attempt in 1 2 3 4; do
    echo "https attempt=$attempt $(curl -sk --max-time 15 -o "$out/index.html" -w 'code=%{http_code} connect=%{time_connect} tls=%{time_appconnect} total=%{time_total}' "https://127.0.0.1:$web/" ; echo " exit=$?")"
done
if grep -q "<html" "$out/index.html"; then echo "page_https=ok bytes=$(wc -c < "$out/index.html" | tr -d ' ')"; else
    backend=$("$adb" -s "$serial" logcat -d -s DashPlay:I | grep -o "backend=[0-9]*" | tail -1 | cut -d= -f2)
    "$adb" -s "$serial" forward "tcp:$((web + 100))" "tcp:$backend" >/dev/null
    echo "backend_plain_http $(curl -s --max-time 10 -o /dev/null -w 'code=%{http_code}' "http://127.0.0.1:$((web + 100))/")"
    sleep 2; "$adb" -s "$serial" logcat -d | grep -E "DashPlay|System.err" | tail -20
    echo "SMOKE_FAIL page over HTTPS"; exit 1
fi
curl -sk --max-time 10 "https://127.0.0.1:$web/api/status" > "$out/status.json" && echo "status=$(head -c 300 "$out/status.json")"
"$adb" -s "$serial" exec-out screencap -p > "$out/before-import.png"

if [ -n "$diplay" ]; then
    "$adb" -s "$serial" push "$diplay" /data/local/tmp/diplay.apk >/dev/null
    "$adb" -s "$serial" shell chmod 644 /data/local/tmp/diplay.apk
    "$adb" -s "$serial" shell am start -a android.intent.action.SEND -t application/vnd.android.package-archive \
        --eu android.intent.extra.STREAM file:///data/local/tmp/diplay.apk -n com.dashplay.android/.MainActivity >/dev/null
    wait_for 120 "AirPlay listener" logged "AirPlay listener on"
    "$adb" -s "$serial" logcat -d -s DashPlay:I | grep "AirPlay listener on" | tail -1
    python3 "$here/scripts/check-wired-setup.py" --adb "$adb" --serial "$serial" --base "https://127.0.0.1:$web" --screens "$out"
    python3 "$here/scripts/check-own-tls.py" --adb "$adb" --serial "$serial" --base "https://127.0.0.1:$web" --fixtures "$here/app/build/test-artifacts/own-tls" --screens "$out"
    if [ "$api" = 21 ]; then
        python3 "$here/scripts/check-automatic-tls.py" --adb "$adb" --serial "$serial" --base "https://127.0.0.1:$web" --screens "$out" --offline
    else
        python3 "$here/scripts/check-automatic-tls.py" --adb "$adb" --serial "$serial" --base "https://127.0.0.1:$web" --screens "$out"
    fi
    python3 "$here/scripts/check-viewer.py" "https://127.0.0.1:$web"
    python3 "$here/scripts/check-management.py" --adb "$adb" --serial "$serial" --base "http://127.0.0.1:$web" --screens "$out"
    python3 "$here/scripts/check-usb-ui.py" --adb "$adb" --serial "$serial" --screens "$out"
    "$adb" -s "$serial" shell rm /data/local/tmp/diplay.apk
    sleep 2
    "$adb" -s "$serial" exec-out screencap -p > "$out/after-import.png"

    # A box without a screen: after a reboot the receiver must come up by itself, without opening the app.
    "$adb" -s "$serial" logcat -d > "$out/logcat-before-reboot.txt"
    # Android records that the app has been opened (else it gets no boot broadcast) about 10 s later, lazily.
    sleep 15
    "$adb" -s "$serial" shell sync
    "$adb" -s "$serial" reboot
    sleep 5
    "$adb" -s "$serial" wait-for-device
    wait_for 300 "boot after reboot" booted
    prepare_test_lan
    reboot_listener="AirPlay listener on"
    [ "$api" != 21 ] || reboot_listener="AirPlay listener on 192.0.2.1:"
    if ! wait_for 240 "AirPlay listener after reboot" logged "$reboot_listener"; then
        "$adb" -s "$serial" shell dumpsys package com.dashplay.android | grep -i -E "stopped|enabled=" | head -5
        "$adb" -s "$serial" shell dumpsys activity broadcasts | grep -E "BOOT_COMPLETED|Active ordered|Pending" | head -10
        "$adb" -s "$serial" shell am broadcast -a android.intent.action.BOOT_COMPLETED -n com.dashplay.android/.BootReceiver
        sleep 20
        "$adb" -s "$serial" logcat -d -s DashPlay:I | tail -3
        exit 1
    fi
    "$adb" -s "$serial" forward "tcp:$web" tcp:18743 >/dev/null
    curl -sk --fail --max-time 10 "https://127.0.0.1:$web/api/status" > "$out/status-after-reboot.json"
    python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["deviceName"]' "$out/status-after-reboot.json"
    echo "after_reboot_https=ok $("$adb" -s "$serial" logcat -d -s DashPlay:I | grep "$reboot_listener" | tail -1)"
fi

"$adb" -s "$serial" logcat -d > "$out/logcat.txt"
# Only this app's processes (the emulator's own system_server sometimes dies once while booting).
: > "$out/crashes.txt"
for log in "$out/logcat-before-reboot.txt" "$out/logcat.txt"; do
    [ -f "$log" ] || continue
    for pid in $(grep "Start proc [0-9:]*com.dashplay.android" "$log" | grep -o -E "pid=[0-9]+|Start proc [0-9]+:" | grep -o -E "[0-9]+"); do
        grep -E "\( *$pid\)|^[0-9-]+ [0-9:.]+ +$pid " "$log" | grep -E "FATAL EXCEPTION|NoSuchMethodError|NoClassDefFoundError|AbstractMethodError" >> "$out/crashes.txt" || true
    done
done
if [ -s "$out/crashes.txt" ]; then
    echo "SMOKE_FAIL crashes (see $out/crashes.txt):"; head -20 "$out/crashes.txt"; exit 1
fi
echo "SMOKE_OK api=$api"
