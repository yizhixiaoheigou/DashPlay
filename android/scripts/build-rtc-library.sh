#!/bin/sh
# Rebuild the RTC native libraries the app ships (libdatachannel + mbedTLS) for Android.
#
# Why a script: the .so files in app/src/main/jniLibs/ are build products. Their origin must be reproducible, or the
# APK contains a binary nobody can regenerate. This pins the versions, the flags that matter (DTLS-SRTP, -fPIC) and
# the ABI, and prints the SHA-256 of what it produced.
#
# What it does NOT do: vendor anything into the repository or touch the app build. Run it, copy the .so into
# app/src/main/jniLibs/<abi>/, and the Gradle CMakeLists picks it up.
#
# Versions (2026-11-03):
#   libdatachannel v0.24.6   MPL-2.0   https://github.com/paullouisageneau/libdatachannel
#   mbedTLS         v3.6.7   Apache-2.0 https://github.com/Mbed-TLS/mbedtls
#   NDK             r27 (27.0.12077973), iOS/macOS host builds via the same NDK clang
#
# The DTLS-SRTP extension is mandatory: libdatachannel's SRTP transport calls mbedtls_ssl_srtp_* and without
# MBEDTLS_SSL_DTLS_SRTP the link fails with "undeclared identifier 'mbedtls_dtls_srtp_info'".
#
# Result: libdatachannel.so links mbedTLS statically and needs only libc/libm/libdl/liblog, so it loads on
# Android 5.0+ (the app's minimum) without a separate crypto .so.
set -eu
abi=${1:-arm64-v8a}
case "$abi" in
    arm64-v8a) android_abi="$abi" ;;
    armeabi-v7a) android_abi="$abi" ;;
    x86_64|x86) android_abi="$abi" ;;
    *) echo "unknown ABI: $abi (want arm64-v8a, armeabi-v7a, x86_64 or x86)" >&2; exit 2 ;;
esac
root=${RTC_BUILD_DIR:-/tmp/dashplay-rtc-build}
ndk=${ANDROID_NDK_HOME:-/opt/homebrew/share/android-commandlinetools/ndk/27.0.12077973}
[ -d "$ndk" ] || { echo "NDK not found at $ndk; set ANDROID_NDK_HOME" >&2; exit 2; }
jobs=${JOBS:-8}
mkdir -p "$root"

toolchain="$root/android-toolchain-$abi.cmake"
cat > "$toolchain" <<EOF
set(CMAKE_SYSTEM_NAME Android)
set(CMAKE_SYSTEM_VERSION 21)
set(CMAKE_ANDROID_ARCH_ABI $android_abi)
set(CMAKE_ANDROID_NDK $ndk)
set(CMAKE_ANDROID_STL_TYPE c++_static)
EOF

clone() { # url tag dir
    [ -d "$3" ] && return 0
    git clone --depth 1 --branch "$2" "$1" "$3"
}

clone https://github.com/Mbed-TLS/mbedtls.git v3.6.7 "$root/mbedtls"
[ "$(git -C "$root/mbedtls" rev-parse HEAD)" = 068ff080b369adfac81509f9b57b2afabaf82dc5 ] || {
    echo 'mbedTLS build directory has a different revision; choose a fresh RTC_BUILD_DIR' >&2; exit 2;
}
(cd "$root/mbedtls" && git submodule update --init --depth 1)
python3 - "$root/mbedtls/include/mbedtls/mbedtls_config.h" <<'PY'
import sys, re
path = sys.argv[1]
text = open(path).read()
if not re.search(r"^#define MBEDTLS_SSL_DTLS_SRTP\b", text, re.M):
    text = text.replace("//#define MBEDTLS_SSL_DTLS_SRTP", "#define MBEDTLS_SSL_DTLS_SRTP", 1)
    open(path, "w").write(text)
PY
cmake -S "$root/mbedtls" -B "$root/mbedtls/$abi" \
    -DCMAKE_TOOLCHAIN_FILE="$toolchain" -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_TESTING=OFF -DENABLE_PROGRAMS=OFF \
    -DUSE_SHARED_MBEDTLS_LIBRARY=OFF -DUSE_STATIC_MBEDTLS_LIBRARY=ON \
    -DMBEDTLS_FATAL_WARNINGS=OFF -DCMAKE_C_FLAGS="-fPIC"
cmake --build "$root/mbedtls/$abi" -j"$jobs"
cmake --install "$root/mbedtls/$abi" --prefix "$root/mbedtls-$abi"

clone https://github.com/paullouisageneau/libdatachannel.git v0.24.6 "$root/libdatachannel"
[ "$(git -C "$root/libdatachannel" rev-parse HEAD)" = 6b1e2e620f1e37f0eafeee702eaea0043cb305fd ] || {
    echo 'libdatachannel build directory has a different revision; choose a fresh RTC_BUILD_DIR' >&2; exit 2;
}
(cd "$root/libdatachannel" && git submodule update --init --depth 1 \
    deps/json deps/libsrtp deps/usrsctp deps/plog deps/libjuice)
cmake -S "$root/libdatachannel" -B "$root/libdatachannel/$abi" \
    -DCMAKE_TOOLCHAIN_FILE="$toolchain" -DCMAKE_BUILD_TYPE=Release \
    -DUSE_MBEDTLS=ON -DNO_EXAMPLES=ON -DNO_TESTS=ON \
    -DMbedTLS_INCLUDE_DIR="$root/mbedtls-$abi/include" \
    -DMbedTLS_LIBRARY="$root/mbedtls-$abi/lib/libmbedtls.a" \
    -DMbedCrypto_LIBRARY="$root/mbedtls-$abi/lib/libmbedcrypto.a" \
    -DMbedX509_LIBRARY="$root/mbedtls-$abi/lib/libmbedx509.a" \
    -DMbedTLS_FIND_QUIETLY=ON
cmake --build "$root/libdatachannel/$abi" -j"$jobs"

out="$root/libdatachannel/$abi/libdatachannel.so"
[ -f "$out" ] || { echo "build produced no $out" >&2; exit 1; }
"$ndk/toolchains/llvm/prebuilt/darwin-x86_64/bin/llvm-strip" "$out" 2>/dev/null || true
echo "built $out"
shasum -a 256 "$out"
echo "copy it to: app/src/main/jniLibs/$abi/libdatachannel.so"
