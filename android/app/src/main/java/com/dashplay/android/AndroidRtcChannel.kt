package com.dashplay.android

import com.playport.server.PlatformRtc
import com.playport.server.WebHub
import com.dashplay.android.tesla.RtcBridge
import com.dashplay.android.tesla.RtcSession
import com.shilapi.xcertplay.airplay.VideoCodec
import java.util.concurrent.atomic.AtomicLong

/**
 * The Android RTC channel (2026-11-03): one page at a time, H.264 only, and the JPEG fallback whenever it is not
 * proven to work.
 *
 * This class is the bridge between three things that must not know about each other:
 *  - [RtcSession]: what the channel may claim and when it may carry a frame (unit-tested);
 *  - [RtcBridge]: libdatachannel through JNI (signalling strings and encoded bytes only);
 *  - [WebHub]: the receiver's per-page signalling and the stills fan-out.
 *
 * The page decides the transport. This object never stops CarPlay, never changes the audio declarations and never
 * restarts the session when RTC fails: a failed channel simply reports its reason, and the page keeps its stills.
 */
internal class AndroidRtcChannel : PlatformRtc {
    private val lock = Any()
    private var bridge: RtcBridge? = null
    @Volatile private var session: RtcSession = RtcSession(null)
    @Volatile private var viewerId: Long = -1
    @Volatile private var hub: WebHub? = null

    /** The same codec/size information the JPEG encoder gets, so a frame can be framed before it is sent. */
    @Volatile private var codec: VideoCodec = VideoCodec.H264
    @Volatile private var streamType = -1
    @Volatile private var width = 0
    @Volatile private var height = 0

    val available: Boolean get() = RtcBridge.available

    /** Called by the media bridge on the CarPlay thread: config and frames, never blocking. */
    fun onConfig(value: VideoCodec, size: Pair<Int, Int>, codecData: ByteArray, type: Int) {
        streamType = type
        retainedHeader = if (value == VideoCodec.H264) com.dashplay.android.tesla.H264Framing.parameterSets(codecData) else null
        codec = value
        if (size.first > 0) width = size.first
        if (size.second > 0) height = size.second
        session.onCodec(value, retainedHeader)
        if (value != VideoCodec.H264 && viewerId != -1L) hub?.rtcFailed(viewerId, "codec")
    }

    @Volatile private var retainedHeader: ByteArray? = null
    private val callbackGeneration = AtomicLong(0)
    private var sentFrames = 0L
    private var lastSentLog = 0L

    fun onStreamEnded(type: Int) {
        if (type != streamType) return
        streamType = -1
        retainedHeader = null
        width = 0; height = 0
        session.onCodec(codec, null)
    }

    fun onFrame(annexB: ByteArray, keyframe: Boolean, type: Int) {
        if (type != streamType) return
        if (keyframe) retainedHeader = com.dashplay.android.tesla.H264Framing.parameterSets(annexB) ?: retainedHeader
        session.onCodec(codec, retainedHeader)
        if (session.onFrame(annexB, keyframe, width, height)) {
            sentFrames++
            val now = System.nanoTime()
            if (sentFrames <= 3 || now - lastSentLog > 5_000_000_000L) {
                lastSentLog = now
                DriveLog.event("rtc sent frames=$sentFrames width=$width height=$height keyframe=$keyframe")
            }
        }
    }

    override fun start(clientId: Long, hub: WebHub, report: (String?) -> Unit) {
        if (!available || codec != VideoCodec.H264) {
            DriveLog.event("rtc rejected reason=unsupported codec=$codec nativeAvailable=$available")
            report("unsupported"); return
        }
        synchronized(lock) {
            if (viewerId != -1L) { report("busy"); return }
            val header = retainedHeader
            if (streamType < 0 || width <= 0 || height <= 0 || header == null) { report("not-ready"); return }
            val generation = callbackGeneration.incrementAndGet()
            val profile = if (header != null && header.size >= 8 && header[4].toInt() and 31 == 7)
                header.copyOfRange(5,8).joinToString("") { "%02x".format(it.toInt() and 255) } else "42e01f"
            val created = RtcBridge.create(listener(generation), width, height, "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=$profile")
            if (created == null) { report("unavailable"); return }
            bridge = created
            session = RtcSession(created)
            sentFrames = 0; lastSentLog = 0
            DriveLog.event("rtc start width=$width height=$height profile=$profile")
            viewerId = clientId
            this.hub = hub
            session.onCodec(codec, retainedHeader)
            if (!session.start(width, height)) {
                release()
                report("failed")
            }
        }
    }

    override fun message(clientId: Long, sdp: String?, candidate: String?, mid: String?) {
        if (clientId != viewerId) return
        if (!sdp.isNullOrEmpty()) session.onAnswer(sdp)
        if (!candidate.isNullOrEmpty()) session.onCandidate(candidate, mid ?: "video")
    }

    override fun closed(clientId: Long) {
        if (clientId != viewerId) return
        synchronized(lock) { release() }
    }

    /** The page said it decoded a frame: only now may the channel be called the running one. */
    override fun firstFrame(clientId: Long) {
        if (clientId != viewerId) return
        session.onFirstFrame()
        DriveLog.event("rtc browser confirmed first frame")
    }

    fun close() = synchronized(lock) { release() }

    private fun release() {
        callbackGeneration.incrementAndGet()
        session.close()
        bridge = null
        session = RtcSession(null)
        viewerId = -1
        hub = null
    }

    private fun listener(generation: Long) = object : RtcBridge.Listener {
        override fun onLocalDescription(handle: Int, sdp: String, type: String) {
            if (callbackGeneration.get() != generation) return
            val current = hub ?: return
            current.sendRtc(viewerId, kotlinx.serialization.json.buildJsonObject {
                put("kind", kotlinx.serialization.json.JsonPrimitive(type))
                put("sdp", kotlinx.serialization.json.JsonPrimitive(sdp))
            }.toString())
        }

        override fun onLocalCandidate(handle: Int, candidate: String, mid: String) {
            if (callbackGeneration.get() != generation) return
            val current = hub ?: return
            current.sendRtc(viewerId, kotlinx.serialization.json.buildJsonObject {
                put("kind", kotlinx.serialization.json.JsonPrimitive("candidate"))
                put("candidate", kotlinx.serialization.json.JsonPrimitive(candidate))
                put("mid", kotlinx.serialization.json.JsonPrimitive(mid))
            }.toString())
        }

        override fun onKeyframe(handle: Int) { if (callbackGeneration.get() == generation && streamType >= 0) hub?.requestVideoKeyframe(streamType) }

        override fun onState(handle: Int, state: Int) {
            if (callbackGeneration.get() != generation) return
            DriveLog.event("rtc native state=$state")
            session.onNativeState(state)
            if (state == 2 && streamType >= 0) hub?.requestVideoKeyframe(streamType)
            if (state == 4) hub?.rtcFailed(viewerId, "ice")
        }
    }
}

/**
 * Fans one encoded CarPlay picture out to the transports that are running (stills, RTC), on the media thread.
 *
 * Each transport decides for itself whether the frame is usable: stills dedupe and rate-limit, RTC needs a keyframe
 * with parameter sets. Neither may block or throw into the CarPlay session, and both are optional.
 */
internal class RelayConsumer(
    private val stills: com.dashplay.android.tesla.JpegVideoEncoder? = null,
    private val rtc: AndroidRtcChannel? = null,
) : com.playport.server.VideoFrameConsumer {
    @Volatile private var dimensions = 0 to 0
    override fun onDimensions(width: Int, height: Int) {
        dimensions = width to height
        stills?.onDimensions(width, height)
    }
    override fun onConfig(streamType: Int, codec: VideoCodec, codecData: ByteArray) {
        stills?.onConfig(streamType, codec, codecData)
        rtc?.onConfig(codec, dimensions, codecData, streamType)
    }

    override fun onFrame(streamType: Int, keyframe: Boolean, annexB: ByteArray) {
        rtc?.onFrame(annexB, keyframe, streamType)
        stills?.onFrame(streamType, keyframe, annexB)
    }

    override fun onStreamEnded(streamType: Int) {
        stills?.onStreamEnded(streamType)
        rtc?.onStreamEnded(streamType)
    }
}
