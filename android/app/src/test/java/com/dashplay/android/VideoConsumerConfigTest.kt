package com.dashplay.android

import com.playport.server.*
import com.shilapi.xcertplay.airplay.VideoCodec
import com.dashplay.android.tesla.DecodeQueue
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class VideoConsumerConfigTest {
    @get:Rule val tmp = TemporaryFolder()
    private class Consumer : VideoFrameConsumer {
        var configs = 0
        val queue = DecodeQueue(12)
        override fun onConfig(streamType: Int, codec: VideoCodec, codecData: ByteArray) { configs++; queue.reset() }
        override fun onFrame(streamType: Int, keyframe: Boolean, annexB: ByteArray) { queue.offer(annexB, keyframe) }
        override fun onStreamEnded(streamType: Int) { queue.reset() }
    }
    @Test fun `repeated parameter sets do not clear a queued IDR or restart the consumer`() {
        val consumer = Consumer()
        val hub = WebHub(ServerConfig(identityDir=tmp.root.toPath(),stateDir=tmp.root.toPath()),DisplayState(1280,720,30,100))
        val bridge = WebBridge(hub,consumer)
        val csd = byteArrayOf(0,0,0,1,0x67,1,0,0,0,1,0x68,2)
        bridge.onVideoConfig(110,csd)
        bridge.onVideoFrame(110,byteArrayOf(0,0,0,1,0x65,3))
        repeat(100) { bridge.onVideoConfig(110,csd.copyOf()) }
        assertEquals("resends must not reset the decoder",1,consumer.configs)
        assertNotNull("the first keyframe must survive repeated config",consumer.queue.poll())
    }
    @Test fun `real changes and an ended stream reinitialize the consumer`() {
        val consumer=Consumer();val display=DisplayState(1280,720,30,100)
        val bridge=WebBridge(WebHub(ServerConfig(identityDir=tmp.root.toPath(),stateDir=tmp.root.toPath()),display),consumer)
        val csd=byteArrayOf(0,0,0,1,0x67,1)
        bridge.onVideoConfig(110,csd)
        csd[5]=2;bridge.onVideoConfig(110,csd)
        assertEquals("remember a copy because input buffers are reused",2,consumer.configs)
        display.update(1920,1080,30,100,false);bridge.onVideoConfig(110,csd)
        assertEquals(3,consumer.configs)
        bridge.onVideoCodec(110,VideoCodec.H265);bridge.onVideoConfig(110,csd)
        assertEquals(4,consumer.configs)
        bridge.onScreenStreamActive(110,false);bridge.onVideoConfig(110,csd)
        assertEquals(5,consumer.configs)
        bridge.onVideoConfig(111,csd);bridge.onVideoConfig(110,csd)
        assertEquals("switching streams must not reuse another decoder state",7,consumer.configs)
    }
}
