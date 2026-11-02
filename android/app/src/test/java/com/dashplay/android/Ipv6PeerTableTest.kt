package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class Ipv6PeerTableTest {
    private fun packet(source: Int, target: Int) = ByteArray(40).also {
        it[0] = 0x60; it[8] = 0xfe.toByte(); it[9] = 0x80.toByte(); it[23] = source.toByte()
        it[24] = 0xfe.toByte(); it[25] = 0x80.toByte(); it[39] = target.toByte()
    }
    @Test fun anotherFunctionsMulticastCannotRedirectCarPlayReplies() {
        val peers = Ipv6PeerTable()
        val carplayMac = byteArrayOf(2, 0, 0, 0, 0, 3)
        val pairingMac = byteArrayOf(2, 0, 0, 0, 0, 5)
        peers.learn(packet(3, 2), 0, 40, carplayMac)
        peers.learn(packet(5, 2).also { it[24] = 0xff.toByte(); it[25] = 2 }, 0, 40, pairingMac)
        assertArrayEquals(carplayMac, peers.destination(packet(2, 3)))
        assertArrayEquals(pairingMac, peers.destination(packet(2, 5)))
        assertNull(peers.destination(packet(2, 6)))
    }
    @Test fun handlesEthernetOffsetAndDoesNotLearnUnspecifiedDadSource() {
        val peers = Ipv6PeerTable(); val mac = ByteArray(6) { 2 }
        val ethernet = ByteArray(14) + packet(3, 2)
        peers.learn(ethernet, 14, 40, mac)
        mac[0] = 4
        assertEquals(2, peers.destination(packet(2, 3))!![0].toInt())
        peers.learn(ByteArray(40), 0, 40, mac)
        assertNull(peers.destination(ByteArray(40)))
    }
}
