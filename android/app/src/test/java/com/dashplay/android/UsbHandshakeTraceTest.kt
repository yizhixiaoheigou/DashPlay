package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class UsbHandshakeTraceTest {
    private fun packet(protocol: Int, length: Int) = ByteArray(54 + length).apply {
        this[12] = 0x86.toByte(); this[13] = 0xdd.toByte(); this[14] = 0x60
        this[18] = (length ushr 8).toByte(); this[19] = length.toByte()
        this[20] = protocol.toByte(); this[21] = 255.toByte()
    }

    @Test fun distinguishesDadTargetFromReceiverAddressWithoutDumpingPayload() {
        val dad = packet(58, 32).apply { this[54] = 135.toByte(); this[62] = 0xfe.toByte(); this[63] = 0x80.toByte(); this[77] = 2 }
        val text = UsbHandshakeTrace.describe(dad)!!
        assertTrue(text.contains("icmp6 type=135 target=fe80:"))
        assertTrue(text.contains("src=0:0:0:0:0:0:0:0"))
        assertTrue(text.contains("hop=255"))
        assertEquals(text, UsbHandshakeTrace.describe(dad.copyOf().apply { this[80] = 123 }))
    }

    @Test fun tcpPayloadAndOrdinaryDataAreNeverLogged() {
        val syn = packet(6, 40).apply { this[67] = 2; "secret-payload".toByteArray().copyInto(this, 74) }
        val text = UsbHandshakeTrace.describe(syn)!!
        assertTrue(text.startsWith("tcp flags=2"))
        assertFalse(text.contains("secret"))
        syn[67] = 0x18
        assertNull(UsbHandshakeTrace.describe(syn))
    }

    @Test fun malformedAndUnsupportedPacketsHaveNoTrace() {
        val ns = packet(58, 24).apply { this[54] = 135.toByte() }
        for (size in 0 until ns.size) assertNull(UsbHandshakeTrace.describe(ns.copyOf(size)))
        assertNull(UsbHandshakeTrace.describe(packet(58, 4).apply { this[54] = 135.toByte() }))
        assertNull(UsbHandshakeTrace.describe(packet(17, 24)))
        assertNull(UsbHandshakeTrace.describe(ns.copyOf().apply { this[14] = 0x40 }))
    }
}
