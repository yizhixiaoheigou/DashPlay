#!/bin/sh
# Runs only inside our disposable emulator. Never changes the Mac or a user's phone firewall.
set -eu
api=34
out=../artifacts/android-sdk/tesla-root
. "$(dirname -- "$0")/emulator-lib.sh"
"$adb" -s "$serial" root
"$adb" -s "$serial" wait-for-device
fixture="$here/app/build/test-artifacts/tesla-root"
test -f "$fixture/install.sh" && test -f "$fixture/remove.sh"
"$adb" -s "$serial" shell mkdir -p /data/local/tmp/dashplay-tesla-root
"$adb" -s "$serial" push "$fixture/install.sh" "$fixture/remove.sh" "$here/scripts/tesla-root-kernel-fixture.sh" /data/local/tmp/dashplay-tesla-root/ >/dev/null
"$adb" -s "$serial" shell sh /data/local/tmp/dashplay-tesla-root/tesla-root-kernel-fixture.sh
