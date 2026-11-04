package com.dashplay.android

import com.shilapi.xcertplay.airplay.VideoCodec
import org.junit.Assert.*
import org.junit.Test

class RtcRelayTest {
    private fun field(channel: AndroidRtcChannel, name: String): Any? =
        channel.javaClass.getDeclaredField(name).apply { isAccessible = true }.get(channel)

    @Test fun `RTC receives real dimensions and stream end clears retained configuration`() {
        val channel = AndroidRtcChannel()
        val relay = RelayConsumer(rtc = channel)
        relay.onDimensions(1280, 742)
        relay.onConfig(110, VideoCodec.H264, byteArrayOf(0,0,0,1,0x67,0x42,0,0x1f,0,0,0,1,0x68,2))
        assertEquals(1280, field(channel, "width"))
        assertEquals(742, field(channel, "height"))
        assertNotNull(field(channel, "retainedHeader"))
        relay.onStreamEnded(111)
        assertEquals(110, field(channel, "streamType"))
        relay.onStreamEnded(110)
        assertEquals(-1, field(channel, "streamType"))
        assertNull(field(channel, "retainedHeader"))
        relay.onDimensions(800, 1200)
        relay.onConfig(110, VideoCodec.H264, byteArrayOf(0,0,0,1,0x67,0x42,0,0x1f,0,0,0,1,0x68,2))
        assertEquals(800, field(channel, "width"))
        assertEquals(1200, field(channel, "height"))
    }
}
