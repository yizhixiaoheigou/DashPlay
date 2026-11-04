# The RTC bridge (rtc_bridge.c) links against a prebuilt, stripped libdatachannel for each ABI.
#
# Where the library comes from is documented in docs/安卓免Root特斯拉与WebRTC接入开发方案-20261103.md and in
# scripts/build-rtc-library.sh: libdatachannel v0.24.6, mbedTLS 3.6.7 with MBEDTLS_SSL_DTLS_SRTP, NDK r27, both
# static, so the .so depends only on libc/libm/libdl/liblog. The ABI set matches the WebRTC SDK's and the app's
# minimum (Android 5.0 / API 21).
#
# ARMv7 and ARM64 are required for this APK. Missing libraries must fail packaging, not masquerade as JPEG fallback.
cmake_minimum_required(VERSION 3.22.1)
project(rtc_bridge C)

set(RTC_JNI rtc_bridge)

foreach(abi ${ANDROID_ABI})
    set(lib_dir ${CMAKE_CURRENT_SOURCE_DIR}/../jniLibs/${abi})
    if(EXISTS ${lib_dir}/libdatachannel.so)
        add_library(datachannel_${abi} SHARED IMPORTED)
        set_target_properties(datachannel_${abi} PROPERTIES IMPORTED_LOCATION ${lib_dir}/libdatachannel.so)
        list(APPEND RTC_LIBS datachannel_${abi})
    endif()
endforeach()

if(RTC_LIBS)
    add_library(${RTC_JNI} SHARED rtc_bridge.c)
    target_include_directories(${RTC_JNI} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    target_link_libraries(${RTC_JNI} PRIVATE ${RTC_LIBS} log)
else()
    message(FATAL_ERROR "Missing libdatachannel for ${ANDROID_ABI}; run scripts/build-rtc-library.sh ${ANDROID_ABI}")
endif()

# JPEG plane packing is independent of the optional RTC session.
add_library(image_bridge SHARED image_bridge.c)
target_compile_options(image_bridge PRIVATE -O3 -Wall -Wextra -Werror)
