// JNI bridge from DashPlay (Kotlin) to libdatachannel (C API).
//
// Why libdatachannel and not the WebRTC Java SDK: the SDK's CodecSpecificInfo in this release has no field for
// H.264 parameter sets, so a pre-encoded stream cannot be handed to it without re-encoding. libdatachannel takes
// arbitrary samples through rtcSendMessage and packetises them itself (rtcSetH264Packetizer), which is exactly what
// CarPlay's already-encoded H.264 needs. Its DTLS/SRTP/ICE are standard, built against mbedTLS.
//
// Only signalling boundaries cross this file: offer/candidate strings out, answer/candidate strings in, and the
// encoded access unit. No SDP parsing, no crypto and no RTP logic lives here.
#include <jni.h>
#include "rtc/rtc.h"
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <pthread.h>
#include <stdatomic.h>

#ifdef __ANDROID__
#include <android/log.h>
// Only fixed library failure descriptions are logged, never SDP, ICE credentials or candidates.
static void rtc_diagnostic(rtcLogLevel level, const char *message) {
    (void)level;
    if (!message) return;
    const char *detail = strstr(message, "SSL - "); // mbedTLS's static error descriptions
    if (!detail) detail = strstr(message, "X509 - ");
    const char *known[] = {"DTLS handshake failed", "DTLS transport initialization failed",
        "ICE transport initialization failed", "Failed to create the ICE agent", "ICE timeout",
        "Failed to gather local ICE candidates", "Failed to get SRTP profile",
        "Failed to derive SRTP keys", "SRTP profile is not supported", "TLS PRF type is not set",
        "Invalid fingerprint", "DTLS certificate is null", "MTU is too low"};
    if (!detail) for (unsigned i=0; i<sizeof(known)/sizeof(known[0]); i++) {
        if (strstr(message, known[i])) { detail=known[i]; break; }
    }
    __android_log_print(ANDROID_LOG_WARN, "DashPlayRTC", "%.*s", 180,
        detail ? detail : "RTC library reported an unclassified warning/error");
}
static pthread_once_t logger_once = PTHREAD_ONCE_INIT;
static void init_rtc_logger(void) { rtcInitLogger(RTC_LOG_WARNING, rtc_diagnostic); }
#endif

static JavaVM *g_vm = NULL;

// One bridge object owns one peer connection and one send-only video track.
typedef struct bridge {
    jobject listener;      // global ref: the Kotlin RtcBridge.Listener (weak in Kotlin, strong here)
    int pc;
    int track;
    _Atomic int lastState;  // rtcState from the callback: the C API has no getter
    struct bridge *next;
    unsigned callbacks;
} bridge_t;

// Callbacks resolve a live registry entry by peer id, never dereference libdatachannel's stale user pointer.
static pthread_mutex_t registry_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t registry_idle = PTHREAD_COND_INITIALIZER;
static bridge_t *registry = NULL;
static bridge_t *callback_acquire(int pc) {
    pthread_mutex_lock(&registry_lock);
    bridge_t *b = registry;
    while (b && b->pc != pc && b->track != pc) b = b->next;
    if (b) b->callbacks++;
    pthread_mutex_unlock(&registry_lock);
    return b;
}
static void callback_release(bridge_t *b) {
    pthread_mutex_lock(&registry_lock);
    b->callbacks--; pthread_cond_broadcast(&registry_idle);
    pthread_mutex_unlock(&registry_lock);
}


static void call_string_method(JNIEnv *env, jobject listener, const char *name, jint a, const char *b, const char *c) {
    if (!listener) return;
    jclass cls = (*env)->GetObjectClass(env, listener);
    if (!cls) return;
    jmethodID mid = (*env)->GetMethodID(env, cls, name, "(ILjava/lang/String;Ljava/lang/String;)V");
    if (mid) {
        jstring jb = b ? (*env)->NewStringUTF(env, b) : NULL;
        jstring jc = c ? (*env)->NewStringUTF(env, c) : NULL;
        (*env)->CallVoidMethod(env, listener, mid, a, jb, jc);
        if (jb) (*env)->DeleteLocalRef(env, jb);
        if (jc) (*env)->DeleteLocalRef(env, jc);
    }
    if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
    (*env)->DeleteLocalRef(env, cls);
}

static void call_state(JNIEnv *env, jobject listener, jint pc, jint state) {
    if (!listener) return;
    jclass cls = (*env)->GetObjectClass(env, listener);
    if (!cls) return;
    jmethodID mid = (*env)->GetMethodID(env, cls, "onState", "(II)V");
    if (mid) (*env)->CallVoidMethod(env, listener, mid, pc, state);
    if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
    (*env)->DeleteLocalRef(env, cls);
}

static JNIEnv *attach_env(JNIEnv *cached, int *attached) {
    *attached = 0;
    if (cached) return cached;
    JNIEnv *env = NULL;
    if (!g_vm) return NULL;
    if ((*g_vm)->GetEnv(g_vm, (void **)&env, JNI_VERSION_1_6) != JNI_OK) {
#ifdef __ANDROID__
        if ((*g_vm)->AttachCurrentThread(g_vm, &env, NULL) != JNI_OK) return NULL;
#else
        if ((*g_vm)->AttachCurrentThread(g_vm, (void **)&env, NULL) != JNI_OK) return NULL;
#endif
        *attached = 1;
    }
    return env;
}

static void on_description(int pc, const char *sdp, const char *type, void *ptr) {
    (void)ptr;
    bridge_t *b = callback_acquire(pc);
    if (!b) return;
    int attached;
    JNIEnv *env = attach_env(NULL, &attached);
    if (env) call_string_method(env, b ? b->listener : NULL, "onLocalDescription", pc, sdp, type);
    if (attached) (*g_vm)->DetachCurrentThread(g_vm);
    callback_release(b);
}

static void on_candidate(int pc, const char *cand, const char *mid, void *ptr) {
    (void)ptr;
    bridge_t *b = callback_acquire(pc);
    if (!b) return;
    int attached;
    JNIEnv *env = attach_env(NULL, &attached);
    if (env) call_string_method(env, b ? b->listener : NULL, "onLocalCandidate", pc, cand, mid);
    if (attached) (*g_vm)->DetachCurrentThread(g_vm);
    callback_release(b);
}

static void on_state(int pc, rtcState state, void *ptr) {
    (void)ptr;
    bridge_t *b = callback_acquire(pc);
    if (!b) return;
    if (b) b->lastState = (int)state;
    int attached;
    JNIEnv *env = attach_env(NULL, &attached);
    if (env) call_state(env, b ? b->listener : NULL, pc, (jint)state);
    if (attached) (*g_vm)->DetachCurrentThread(g_vm);
    callback_release(b);
}

static void on_pli(int track, void *ptr) {
    (void)ptr;
    bridge_t *b = callback_acquire(track);
    if (!b) return;
    int attached;
    JNIEnv *env = attach_env(NULL, &attached);
    if (env) {
        jclass cls = (*env)->GetObjectClass(env, b->listener);
        jmethodID method = cls ? (*env)->GetMethodID(env, cls, "onKeyframe", "(I)V") : NULL;
        if (method) (*env)->CallVoidMethod(env, b->listener, method, b->pc);
        if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
        if (cls) (*env)->DeleteLocalRef(env, cls);
    }
    if (attached) (*g_vm)->DetachCurrentThread(g_vm);
    callback_release(b);
}

JNIEXPORT jlong JNICALL
Java_com_dashplay_android_tesla_RtcBridge_00024Companion_nativeCreate(JNIEnv *env, jobject thiz, jobject listener, jint width, jint height, jstring profile) {
#ifdef __ANDROID__
    pthread_once(&logger_once, init_rtc_logger);
#endif
    (*env)->GetJavaVM(env, &g_vm);
    bridge_t *b = (bridge_t *)calloc(1, sizeof(bridge_t));
    if (!b) return 0;
    b->listener = (*env)->NewGlobalRef(env, listener);
    if (!b->listener) { free(b); return 0; }
    rtcConfiguration config;
    memset(&config, 0, sizeof(config));
    config.disableAutoNegotiation = true;
    config.forceMediaTransport = false;
    config.certificateType = RTC_CERTIFICATE_DEFAULT;
    config.iceTransportPolicy = RTC_TRANSPORT_POLICY_ALL;
    b->pc = rtcCreatePeerConnection(&config);
    if (b->pc < 0) { (*env)->DeleteGlobalRef(env, b->listener); free(b); return 0; }

    rtcTrackInit track;
    memset(&track, 0, sizeof(track));
    track.direction = RTC_DIRECTION_SENDONLY;
    track.codec = RTC_CODEC_H264;
    track.payloadType = 96;
    track.ssrc = 42;
    track.mid = "video";
    track.name = "carplay";
    track.trackId = "carplay";
    const char *profileText = profile ? (*env)->GetStringUTFChars(env, profile, NULL) : NULL;
    track.profile = profileText;
    b->track = rtcAddTrackEx(b->pc, &track);
    if (profileText) (*env)->ReleaseStringUTFChars(env, profile, profileText);
    if (b->track < 0) { rtcDeletePeerConnection(b->pc); (*env)->DeleteGlobalRef(env, b->listener); free(b); return 0; }
    rtcPacketizerInit packetizer;
    memset(&packetizer, 0, sizeof(packetizer));
    packetizer.ssrc = track.ssrc;
    packetizer.cname = "dashplay";
    packetizer.payloadType = track.payloadType;
    packetizer.clockRate = 90000;
    packetizer.nalSeparator = RTC_NAL_SEPARATOR_LENGTH;
    packetizer.maxFragmentSize = 1200;
    if (rtcSetH264Packetizer(b->track, &packetizer) < 0 ||
        rtcChainRtcpSrReporter(b->track) < 0 || rtcChainRtcpNackResponder(b->track, 512) < 0 || rtcChainPliHandler(b->track, on_pli) < 0) {
        rtcDeleteTrack(b->track); rtcDeletePeerConnection(b->pc);
        (*env)->DeleteGlobalRef(env, b->listener); free(b); return 0;
    }
    pthread_mutex_lock(&registry_lock);
    b->next = registry; registry = b;
    pthread_mutex_unlock(&registry_lock);
    rtcSetUserPointer(b->pc, b);
    rtcSetUserPointer(b->track, b);
    rtcSetLocalDescriptionCallback(b->pc, on_description);
    rtcSetLocalCandidateCallback(b->pc, on_candidate);
    rtcSetStateChangeCallback(b->pc, on_state);

    (void)width; (void)height;              // the stream's size comes with the frames
    return (jlong)(intptr_t)b;
}

JNIEXPORT jint JNICALL
Java_com_dashplay_android_tesla_RtcBridge_nativeStart(JNIEnv *env, jobject thiz, jlong handle) {
    bridge_t *b = (bridge_t *)(intptr_t)handle;
    if (!b) return -1;
    // An offer is created only once a sender wants it: without a viewer there is no negotiation to run.
    return rtcSetLocalDescription(b->pc, "offer");
}

JNIEXPORT jint JNICALL
Java_com_dashplay_android_tesla_RtcBridge_nativeAnswer(JNIEnv *env, jobject thiz, jlong handle, jstring sdp) {
    bridge_t *b = (bridge_t *)(intptr_t)handle;
    if (!b || !sdp) return -1;
    const char *text = (*env)->GetStringUTFChars(env, sdp, NULL);
    int rc = rtcSetRemoteDescription(b->pc, text, "answer");
    (*env)->ReleaseStringUTFChars(env, sdp, text);
    return rc;
}

JNIEXPORT jint JNICALL
Java_com_dashplay_android_tesla_RtcBridge_nativeCandidate(JNIEnv *env, jobject thiz, jlong handle, jstring candidate, jstring mid) {
    bridge_t *b = (bridge_t *)(intptr_t)handle;
    if (!b || !candidate) return -1;
    const char *cand = (*env)->GetStringUTFChars(env, candidate, NULL);
    const char *m = mid ? (*env)->GetStringUTFChars(env, mid, NULL) : NULL;
    int rc = rtcAddRemoteCandidate(b->pc, cand, m);
    (*env)->ReleaseStringUTFChars(env, candidate, cand);
    if (m) (*env)->ReleaseStringUTFChars(env, mid, m);
    return rc;
}

// Sends one AVCC access unit. The packetizer was configured for length-prefixed NAL units, so the caller passes
// AVCC data (see H264Framing.pictureAvcc); rtcSendMessage copies it before this function returns.
JNIEXPORT jint JNICALL
Java_com_dashplay_android_tesla_RtcBridge_nativeSend(JNIEnv *env, jobject thiz, jlong handle, jbyteArray data, jboolean keyframe) {
    bridge_t *b = (bridge_t *)(intptr_t)handle;
    if (!b || !data) return -1;
    jsize size = (*env)->GetArrayLength(env, data);
    jbyte *bytes = (*env)->GetByteArrayElements(env, data, NULL);
    if (!bytes) return -1;
    // A keyframe is announced to the packetizer with the frame; libdatachannel tracks the sender-side state itself.
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint32_t timestamp = (uint32_t)((uint64_t)now.tv_sec * 90000 + (uint64_t)now.tv_nsec * 9 / 100000);
    int rc = rtcSetTrackRtpTimestamp(b->track, timestamp);
    if (rc >= 0) rc = rtcSendMessage(b->track, (const char *)bytes, size);
    (*env)->ReleaseByteArrayElements(env, data, bytes, JNI_ABORT);
    (void)keyframe;
    return rc;
}

JNIEXPORT jint JNICALL
Java_com_dashplay_android_tesla_RtcBridge_nativeRequestKeyframe(JNIEnv *env, jobject thiz, jlong handle) {
    bridge_t *b = (bridge_t *)(intptr_t)handle;
    return b ? rtcRequestKeyframe(b->track) : -1;
}

JNIEXPORT jint JNICALL
Java_com_dashplay_android_tesla_RtcBridge_nativeState(JNIEnv *env, jobject thiz, jlong handle) {
    bridge_t *b = (bridge_t *)(intptr_t)handle;
    return b ? (jint)b->lastState : -1;
}

JNIEXPORT void JNICALL
Java_com_dashplay_android_tesla_RtcBridge_nativeClose(JNIEnv *env, jobject thiz, jlong handle) {
    bridge_t *b = (bridge_t *)(intptr_t)handle;
    if (!b) return;
    pthread_mutex_lock(&registry_lock);
    bridge_t **entry = &registry;
    while (*entry && *entry != b) entry = &(*entry)->next;
    if (*entry) *entry = b->next;
    while (b->callbacks) pthread_cond_wait(&registry_idle, &registry_lock);
    pthread_mutex_unlock(&registry_lock);
    rtcSetLocalDescriptionCallback(b->pc, NULL);
    rtcSetLocalCandidateCallback(b->pc, NULL);
    rtcSetStateChangeCallback(b->pc, NULL);
    if (b->track >= 0) rtcDeleteTrack(b->track);
    if (b->pc >= 0) rtcDeletePeerConnection(b->pc);
    if (b->listener) (*env)->DeleteGlobalRef(env, b->listener);
    free(b);
}
