# Shared by emulator-smoke.sh and emulator-screens.sh (sourced, not run). Before sourcing set: api, out, and
# optionally tag (default, or android-tv). Defines adb, serial and the helpers below; boots the emulator.
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
sdk=${DASHPLAY_ANDROID_SDK:-$here/../artifacts/android-sdk}
apk=$here/app/build/outputs/apk/debug/app-debug.apk
tag=${tag:-default}
mkdir -p "$out"
# Its own adb server port, so another task's adb (5037) is left alone.
export ANDROID_SDK_ROOT="$sdk" ANDROID_HOME="$sdk" ANDROID_AVD_HOME="$sdk/avd" ANDROID_ADB_SERVER_PORT=5039
adb="$sdk/platform-tools/adb"
name=dashplay-$api-$tag
console=$((5600 + api * 2))
serial=emulator-$console

# Do not reuse or kill another live test's emulator on the same API/port.
if "$adb" -s "$serial" get-state >/dev/null 2>&1; then
    echo "Emulator $serial is already in use; wait for its owning test to finish." >&2
    exit 2
fi

image=system-images/android-$api/$tag/arm64-v8a
[ -d "$sdk/$image" ] || { echo "missing $sdk/$image" >&2; exit 2; }
if [ "$tag" = android-tv ]; then
    screen="hw.lcd.width=1920
hw.lcd.height=1080
hw.lcd.density=320
hw.dPad=yes"
else
    screen="hw.lcd.width=1080
hw.lcd.height=2340
hw.lcd.density=440"
fi
mkdir -p "$ANDROID_AVD_HOME/$name.avd"
cat > "$ANDROID_AVD_HOME/$name.ini" <<EOI
avd.ini.encoding=UTF-8
path=$ANDROID_AVD_HOME/$name.avd
target=android-$api
EOI
cat > "$ANDROID_AVD_HOME/$name.avd/config.ini" <<EOI
image.sysdir.1=$image/
abi.type=arm64-v8a
hw.cpu.arch=arm64
tag.id=$tag
hw.ramSize=2048
disk.dataPartition.size=2G
$screen
hw.gpu.enabled=yes
hw.gpu.mode=swiftshader_indirect
hw.keyboard=yes
EOI

# Optional removable storage for tests using the real system document picker.
# The API 21 image has no emulated internal shared-storage volume.
if [ -n "${test_sdcard_size:-}" ]; then
    "$sdk/emulator/mksdcard" "$test_sdcard_size" "$out/sdcard.img"
    cat >> "$ANDROID_AVD_HOME/$name.avd/config.ini" <<EOI
hw.sdCard=yes
sdcard.path=$out/sdcard.img
EOI
fi

"$sdk/emulator/emulator" -avd "$name" -port "$console" -no-window -no-audio -no-boot-anim -no-snapshot \
    -wipe-data > "$out/emulator.log" 2>&1 &
emulator=$!
cleanup() {
    "$adb" -s "$serial" logcat -d > "$out/logcat.txt" 2>/dev/null || true
    "$adb" -s "$serial" emu kill >/dev/null 2>&1 || true
    sleep 2
    kill "$emulator" 2>/dev/null || true
}
trap cleanup EXIT

wait_for() { # seconds, description, command...
    limit=$1; what=$2; shift 2; i=0
    until "$@" >/dev/null 2>&1; do
        i=$((i + 2)); [ "$i" -lt "$limit" ] || { echo "SMOKE_FAIL timeout: $what" >&2; return 1; }
        sleep 2
    done
}
booted() { [ "$("$adb" -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; }
logged() { "$adb" -s "$serial" logcat -d -s DashPlay:I 2>/dev/null | grep -q "$1"; }

# API 21 exposes its virtual eth0 as CELLULAR, not Ethernet. Do not make the app
# accept mobile/VPN interfaces just to satisfy this fixture. Turn off the emulated
# radio data and give the existing virtual NIC an offline, unmanaged LAN address.
# This exercises real local sockets, not Internet access or physical Ethernet.
prepare_test_lan() {
    [ "${certificate_public_network:-false}" = true ] && return 0
    [ "$api" = 21 ] || return 0
    [ "$("$adb" -s "$serial" shell getprop ro.kernel.qemu | tr -d '\r')" = 1 ] || return 1
    "$adb" -s "$serial" shell svc data disable
    disconnected() { ! "$adb" -s "$serial" shell dumpsys connectivity | grep -q 'InterfaceName: eth0'; }
    wait_for 30 "emulated mobile network disconnect" disconnected
    "$adb" -s "$serial" shell ip addr del 10.0.2.15/24 dev eth0 >/dev/null
    "$adb" -s "$serial" shell ip addr add 192.0.2.1/24 dev eth0 >/dev/null
    "$adb" -s "$serial" shell ip link set eth0 up
    "$adb" -s "$serial" shell ip addr show eth0 | grep -q '192.0.2.1/24' || return 1
    "$adb" -s "$serial" shell dumpsys connectivity > "$out/offline-lan-connectivity.txt"
    grep -q 'Active default network: none' "$out/offline-lan-connectivity.txt" || return 1
    echo "offline_test_lan=eth0/192.0.2.1 no_default_network=true"
}

start=$(date +%s)
wait_for 400 "boot" booted
echo "booted api=$api tag=$tag after $(( $(date +%s) - start ))s"
prepare_test_lan
