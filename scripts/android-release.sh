#!/bin/sh
# Build the Android APK for a DashPlay release and put it where prepare-release.sh left room for it.
# Run on the Mac (the Android SDK and the Gradle cache live there):
#   sh scripts/android-release.sh 0.3.0-beta.1
#   sh scripts/android-release.sh 0.3.0-beta.1 --release-dir artifacts/release-<box version>
#
# The app has its own version line: box releases are dated (2026.10.31.3), the app is 0.3.0-beta.1. The two are never
# compared; the signed manifest carries both (see scripts/make-update-manifest.py --android-apk).
#
# Without android/release-keystore.properties the APK is debug-signed, which is what the beta ships (adaptation:
# install the release-signed build fresh, Android rejects in-place upgrades across signers).
set -eu
app_version=${1:?Usage: android-release.sh APP_VERSION [--release-dir DIR]}
shift || true
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
release_dir=""
while [ $# -gt 0 ]; do
    case $1 in
        --release-dir) release_dir=${2:?--release-dir needs a path}; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# versionCode must grow with every published build, in the same order the app's update check uses:
# X.Y.Z, then dev < alpha < beta < rc < the release (scripts/android_version.py; 0.3.0-beta.1 -> 300301).
code=$(python3 "$here/scripts/android_version.py" "$app_version") || exit 2

sdk=${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}
jdk=${JAVA_HOME:-/opt/homebrew/opt/openjdk}
[ -d "$sdk" ] || { echo "Android SDK not found at $sdk (set ANDROID_HOME)" >&2; exit 1; }
[ -d "$jdk" ] || { echo "JDK not found at $jdk (set JAVA_HOME)" >&2; exit 1; }

cd "$here/android"
echo "android $app_version (versionCode $code)"
JAVA_HOME=$jdk ANDROID_HOME=$sdk ./gradlew --console=plain \
    -I scripts/license-audit.init.gradle :app:auditRuntimeNotices
python3 "$here/scripts/android-collect-licenses.py" --check
JAVA_HOME=$jdk ANDROID_HOME=$sdk ./gradlew --console=plain \
    -PdashplayVersionName="$app_version" -PdashplayVersionCode="$code" \
    :app:testDebugUnitTest :app:assembleRelease

# Every deliverable must be signed, including a beta signed with the debug key.
apk=app/build/outputs/apk/release/app-release.apk
[ -f "$apk" ] || { echo "no signed release APK was produced" >&2; exit 1; }
apksigner=""
for candidate in "$sdk"/build-tools/*/apksigner; do
    [ ! -x "$candidate" ] || apksigner=$candidate
done
[ -n "$apksigner" ] || { echo "apksigner not found in Android SDK build-tools" >&2; exit 1; }
JAVA_HOME=$jdk "$apksigner" verify "$apk"
# Without a release key the build is debug-signed but still named app-release.apk, so check the key, not the name.
[ -f "$here/android/release-keystore.properties" ] ||
    echo "WARNING: no android/release-keystore.properties, so this APK is debug-signed: beta only" >&2

name="DashPlay-android-$app_version.apk"
out="$here/artifacts/android-release-$app_version"
mkdir -p "$out"
cp "$apk" "$out/$name"
(cd "$out" && shasum -a 256 "$name" > "$name.sha256")
echo "built: $out/$name"

if [ -n "$release_dir" ]; then
    [ -d "$release_dir/dashplay" ] || { echo "$release_dir/dashplay does not exist (run prepare-release.sh first)" >&2; exit 1; }
    cp "$out/$name" "$release_dir/dashplay/$name"
    echo "copied into $release_dir/dashplay"
    echo "now regenerate the signed manifest on the Mac:"
    echo "  python3 scripts/make-update-manifest.py <box version> CHANGES.md \"$release_dir\" \\"
    echo "      --android-apk \"$release_dir/dashplay/$name\" --android-version $app_version"
fi
