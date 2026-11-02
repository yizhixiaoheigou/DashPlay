#!/bin/sh
# Walk the app's screens on an emulator and save a screenshot of each: first setup (all six questions and the
# summary), add iPhone, home, settings, and the home in the dark theme. Runs on the Mac. For looking at the
# layout on an Android version or a TV; it does not test Bluetooth, the hotspot or CarPlay.
#
# Usage (from android/): sh scripts/emulator-screens.sh API [default|android-tv] [DIPLAY_APK]
set -eu
api=${1:?Usage: emulator-screens.sh API [default|android-tv] [DIPLAY_APK]}
tag=${2:-default}
diplay=${3:-}
out=${DASHPLAY_ANDROID_SDK:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/../artifacts/android-sdk}/screens-$api-$tag
rm -rf "$out"
. "$(dirname -- "$0")/emulator-lib.sh"

sh_() { "$adb" -s "$serial" shell "$@"; }
shot() { sleep 2; "$adb" -s "$serial" exec-out screencap -p > "$out/$1.png"; echo "shot $1"; }
ime_down() { sh_ dumpsys input_method | grep -q "mInputShown=true" && sh_ input keyevent 4 || true; sleep 1; }
# Tap the view with this id (ids, not words: Android 5's uiautomator dumps Chinese as "?"), scrolling down to it.
# Screen height of the current orientation, so the scroll stride below stays inside the display.
screen_height() {
    # The first node that carries bounds is the root: its height is the real display height for this rotation
    # (the XML declaration line has none, and guessing a portrait size puts the stroke off a landscape screen).
    sh_ uiautomator dump /data/local/tmp/ui.xml >/dev/null 2>&1 || true
    sh_ cat /data/local/tmp/ui.xml | tr '>' '\n' | grep -o 'bounds="[^"]*"' | head -1 | tr -c '0-9' ' ' | awk '{print $4}'
}
tap() {
    # The settings page grew past one screen (online update came in), so a short stride plus four tries no
    # longer reaches rows near the bottom.
    for _ in 1 2 3 4 5 6 7 8; do
        sh_ uiautomator dump /data/local/tmp/ui.xml >/dev/null 2>&1 || true
        bounds=$(sh_ cat /data/local/tmp/ui.xml | tr '>' '\n' | grep -F "resource-id=\"com.dashplay.android:id/$1\"" | head -1 | grep -o 'bounds="[^"]*"' || true)
        if [ -n "$bounds" ]; then
            set -- $(echo "$bounds" | tr -c '0-9' ' ')
            sh_ input tap $(( ($1 + $3) / 2 )) $(( ($2 + $4) / 2 ))
            sleep 1
            return 0
        fi
        height=$(screen_height)
        [ -n "$height" ] || continue
        sh_ input swipe 500 $(( height * 3 / 4 )) 500 $(( height / 4 )) 400
        sleep 1
    done
    echo "SCREENS_FAIL not found: $1" >&2
    return 1
}
# A TV keyboard covers the whole screen: put it away after each field.
type_into() { tap "$1"; sh_ input text "$2"; sleep 1; ime_down; }

"$adb" -s "$serial" install -r "$apk" >/dev/null
[ "$api" -ge 23 ] && for p in ACCESS_FINE_LOCATION READ_EXTERNAL_STORAGE RECORD_AUDIO; do sh_ pm grant com.dashplay.android android.permission.$p 2>/dev/null || true; done
[ "$api" -ge 31 ] && for p in BLUETOOTH_CONNECT BLUETOOTH_SCAN; do sh_ pm grant com.dashplay.android android.permission.$p 2>/dev/null || true; done
[ "$api" -ge 33 ] && sh_ pm grant com.dashplay.android android.permission.POST_NOTIFICATIONS 2>/dev/null || true
sh_ am start -n com.dashplay.android/.MainActivity >/dev/null
shot 01-setup-identity

if [ -n "$diplay" ]; then
    "$adb" -s "$serial" push "$diplay" /data/local/tmp/diplay.apk >/dev/null
    sh_ chmod 644 /data/local/tmp/diplay.apk
    sh_ am start -a android.intent.action.SEND -t application/vnd.android.package-archive \
        --eu android.intent.extra.STREAM file:///data/local/tmp/diplay.apk -n com.dashplay.android/.MainActivity >/dev/null
    sleep 6
    shot 02-identity-imported
    sh_ input keyevent 4   # leave the one-step import page
    sleep 1
fi

# The full setup from the start.
sh_ am start -n com.dashplay.android/.MainActivity >/dev/null
tap settings_button
tap row_redo
shot 03-setup-device
# A television is set up as a box; a phone AVD as a phone, so both layouts get walked.
if [ "$tag" = android-tv ]; then tap choice_2; else tap choice_1; fi
tap primary; shot 03b-setup-identity
tap primary; shot 04-car
tap choice_1; shot 05-car-tesla
tap choice_2; tap primary; shot 06-network
tap primary; shot 07-voice
tap primary; shot 08-iphone
tap primary; shot 09-wifi
type_into field_ssid DashPlay; type_into field_password 12345678; ime_down; shot 10-wifi-filled
tap primary; shot 11-summary
tap primary; shot 12-pair
sh_ input keyevent 4
sleep 2
sh_ am start -n com.dashplay.android/.MainActivity >/dev/null
shot 13-home
tap settings_button; shot 14-settings
sh_ input swipe 500 900 500 200 300; shot 15-settings-bottom
sh_ input keyevent 4

if [ "$tag" = android-tv ]; then
    # The remote: move focus with the D-pad and look at the ring.
    sh_ am start -n com.dashplay.android/.MainActivity >/dev/null
    sh_ input keyevent 22; shot 16-tv-focus-right
fi
if [ "$api" -ge 29 ]; then
    sh_ cmd uimode night yes
    sh_ am force-stop com.dashplay.android
    sh_ am start -n com.dashplay.android/.MainActivity >/dev/null
    sleep 3
    shot 17-home-dark
    sh_ cmd uimode night no
fi
"$adb" -s "$serial" logcat -d > "$out/logcat.txt"
if grep -E "FATAL EXCEPTION" -A3 "$out/logcat.txt" | grep -q "com.dashplay"; then
    echo "SCREENS_FAIL crash:"; grep -E "FATAL EXCEPTION" -A8 "$out/logcat.txt" | head -20; exit 1
fi
echo "SCREENS_OK api=$api tag=$tag -> $out"
