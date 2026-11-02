package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

class UsbCarPlayWireGroupTest {
    private class Port : UsbCarPlayWire {
        val input = LinkedBlockingQueue<ByteArray>()
        val output = mutableListOf<ByteArray>()
        @Volatile var closed = false
        override fun receive(timeoutMillis: Long) = input.poll(timeoutMillis, TimeUnit.MILLISECONDS)
        override fun send(frame: ByteArray) { output += frame.copyOf() }
        override fun close() { closed = true }
    }
    private val a = byteArrayOf(2, 0, 0, 0, 0, 1)
    private val b = byteArrayOf(2, 0, 0, 0, 0, 2)
    private fun frame(source: ByteArray, target: ByteArray) = ByteArray(14).also {
        target.copyInto(it); source.copyInto(it, 6); it[12] = 0x86.toByte(); it[13] = 0xdd.toByte()
    }

    @Test fun receivesBothFunctionsAndRepliesOnTheLearnedPort() {
        val first = Port(); val second = Port()
        UsbCarPlayWireGroup(listOf(first, second)).use { group ->
            // The formerly ignored second function carries the CarPlay peer.
            val carplay = frame(b, a); second.input.add(carplay)
            assertArrayEquals(carplay, group.receive(250))
            val remotePairing = frame(a, b); first.input.add(remotePairing)
            assertArrayEquals(remotePairing, group.receive(250))
            group.send(frame(a, b))
            assertTrue(first.output.isEmpty())
            assertArrayEquals(frame(a, b), second.output.single())
            group.send(frame(b, a))
            assertArrayEquals(frame(b, a), first.output.single())
        }
        assertTrue(first.closed && second.closed)
    }

    @Test fun floodsMulticastAndUnknownUnicastAndLearnsMovedPeers() {
        val first = Port(); val second = Port()
        UsbCarPlayWireGroup(listOf(first, second)).use { group ->
            group.send(frame(a, b))
            val multicast = frame(a, byteArrayOf(0x33, 0x33, 0, 0, 0, 1))
            group.send(multicast)
            assertEquals(2, first.output.size); assertEquals(2, second.output.size)
            first.input.add(frame(b, a)); assertNotNull(group.receive(250))
            second.input.add(frame(b, a)); assertNotNull(group.receive(250))
            group.send(frame(a, b))
            assertEquals(2, first.output.size); assertEquals(3, second.output.size)
        }
    }
}
