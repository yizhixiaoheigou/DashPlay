#!/bin/sh
# Same existing debug key, two version codes; never a release/signing-key generator.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$here"
out=../artifacts/android-product/update-tests/apks
mkdir -p "$out"
# Pin the base build's versionCode only: its name stays the project default so the rebuild at the end of this
# script (no properties) still matches it byte for byte in content.
./gradlew --console=plain :app:assembleDebug -PdashplayVersionCode=3 -x prepareReceiver -x buildWeb
cp app/build/outputs/apk/debug/app-debug.apk "$out/base.apk"
cp app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk "$out/other-package.apk"
./gradlew --console=plain :app:assembleDebug -PdashplayVersionCode=4 -PdashplayVersionName=0.2.0-update-test -x prepareReceiver -x buildWeb
cp app/build/outputs/apk/debug/app-debug.apk "$out/newer.apk"
./gradlew --console=plain :app:assembleDebug -x prepareReceiver -x buildWeb
python3 scripts/compare-apk-content.py app/build/outputs/apk/debug/app-debug.apk "$out/base.apk"
cp app/build/outputs/apk/debug/app-debug.apk "$out/base.apk"
shasum -a 256 "$out"/*.apk
