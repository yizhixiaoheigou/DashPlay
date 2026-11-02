package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class UsbInternetTest {
    private val local = 0xc0000002.toInt()
    private val dns = local - 1
    private val assigned = 0xac140a02.toInt()
    private val remote = 0xcb007108.toInt()
    private val resolver = 0xac140a01.toInt()
    private val lease = UsbTetherProtocol.Lease(assigned, 28, resolver, listOf(resolver), resolver, 3600, 1800, 3150)
    private fun nat() = UsbInternetNat(local, dns).also { it.configure(lease) }
    private fun word(b: ByteArray, p: Int) = ((b[p].toInt() and 255) shl 8) or (b[p + 1].toInt() and 255)
    private fun number(b: ByteArray, p: Int) = (word(b, p) shl 16) or word(b, p + 2)
    private fun put16(b: ByteArray, p: Int, n: Int) { b[p] = (n ushr 8).toByte(); b[p + 1] = n.toByte() }
    private fun sum(b: ByteArray, from: Int, to: Int, start: Int = 0): Int {
        var sum = start
        for (p in from until to step 2) sum += ((b[p].toInt() and 255) shl 8) + if (p + 1 < to) b[p + 1].toInt() and 255 else 0
        while (sum ushr 16 != 0) sum = (sum and 65535) + (sum ushr 16)
        return sum
    }
    private fun header(b: ByteArray) { put16(b, 10, 0); put16(b, 10, sum(b, 0, 20).inv()) }
    private fun valid(b: ByteArray) {
        assertEquals(65535, sum(b, 0, 20))
        when (b[9].toInt()) {
            6, 17 -> assertEquals(65535, sum(b, 20, b.size,
                sum(b, 12, 20) + (b[9].toInt() and 255) + b.size - 20))
            1 -> assertEquals(65535, sum(b, 20, b.size))
        }
    }
    private fun packet(source: Int, target: Int, proto: Int, payload: ByteArray): ByteArray {
        val b = ByteArray(20 + payload.size); b[0] = 0x45; b[8] = 64; b[9] = proto.toByte()
        put16(b, 2, b.size)
        for (i in 0..3) { b[12 + i] = (source ushr (24 - i * 8)).toByte(); b[16 + i] = (target ushr (24 - i * 8)).toByte() }
        payload.copyInto(b, 20); header(b); return b
    }
    private fun tcp(source: Int, target: Int, sport: Int = 12345, dport: Int = 443): ByteArray {
        val body = ByteArray(23); put16(body, 0, sport); put16(body, 2, dport); body[12] = 0x50; body[13] = 0x18
        body[20] = 1; body[21] = 2; body[22] = 3
        val b = packet(source, target, 6, body)
        put16(b, 36, sum(b, 20, b.size, sum(b, 12, 20) + 6 + body.size).inv())
        return b
    }
    private fun udp(source: Int, target: Int, sport: Int = 12345, dport: Int = 443, size: Int = 5) =
        UsbTetherProtocol.udp(source, target, sport, dport, ByteArray(size) { (it + 1).toByte() })

    @Test fun tcpUdpAndEchoChecksumsSurviveBothDirections() {
        val nat = nat()
        val echo = byteArrayOf(8, 0, 0xf7.toByte(), 0xff.toByte(), 0, 0, 0, 0)
        for (original in listOf(tcp(local, remote), udp(local, remote), packet(local, remote, 1, echo))) {
            valid(original)
            val out = nat.outgoing(original, 0)!!; valid(out)
            assertEquals(assigned, number(out, 12)); assertEquals(remote, number(out, 16))
            assertEquals(local, number(original, 12))
        }
        for (original in listOf(tcp(remote, assigned), udp(remote, assigned))) {
            val input = nat.incoming(original, 0)!!; valid(input)
            assertEquals(local, number(input, 16)); assertEquals(remote, number(input, 12))
        }
    }

    @Test fun virtualDnsRewritesOnlyTrackedUdpAndTcpResponsesAndExpiresOnLeaseChange() {
        for (proto in listOf(6, 17)) {
            val n = nat()
            val request = if (proto == 6) tcp(local, dns, 23456, 53) else udp(local, dns, 23456, 53)
            val sent = n.outgoing(request, 10)!!; valid(sent); assertEquals(resolver, number(sent, 16))
            val reply = if (proto == 6) tcp(resolver, assigned, 53, 23456) else udp(resolver, assigned, 53, 23456)
            val received = n.incoming(reply, 20)!!; valid(received); assertEquals(dns, number(received, 12))
            assertEquals(resolver, number(n.incoming(reply, 300_010)!!, 12))
            n.outgoing(request, 400_000)
            n.configure(lease.copy(dns = listOf(remote)))
            assertEquals(resolver, number(n.incoming(reply, 400_001)!!, 12))
        }
        assertNull(nat().outgoing(udp(local, dns, dport = 123), 0))
    }

    @Test fun udpZeroChecksumIsPreservedAndLeaseLossStopsBothDirections() {
        val n = nat(); val original = udp(local, remote).also { put16(it, 26, 0) }
        assertEquals(0, word(n.outgoing(original, 0)!!, 26))
        n.configure(null)
        assertNull(n.outgoing(original, 1)); assertNull(n.incoming(udp(remote, assigned), 1))
        n.configure(lease.copy(address = assigned + 1))
        assertNull(n.incoming(udp(remote, assigned), 2))
        assertNull(n.outgoing(udp(assigned, remote), 2))
    }

    @Test fun fragmentedDnsKeepsAddressMappingAndFullDatagramChecksum() {
        val n = nat(); val whole = udp(local, dns, dport = 53, size = 100)
        val first = whole.copyOf(52).also { put16(it, 2, it.size); put16(it, 6, 0x2000); header(it) }
        val last = (whole.copyOf(20) + whole.copyOfRange(52, whole.size)).also {
            put16(it, 2, it.size); put16(it, 6, 4); header(it)
        }
        assertNull(n.outgoing(last, 0))
        val a = n.outgoing(first, 1)!!; val b = n.outgoing(last, 2)!!
        assertEquals(resolver, number(a, 16)); assertEquals(resolver, number(b, 16))
        val assembled = (a + b.copyOfRange(20, b.size)).also { put16(it, 2, it.size); put16(it, 6, 0); header(it) }
        valid(assembled)
    }

    @Test fun icmpErrorsRestoreQuotedAddressesPortsAndChecksums() {
        val n = nat(); val original = udp(local, dns, dport = 53)
        val sent = n.outgoing(original, 0)!!
        val error = ByteArray(8) + sent.copyOf(28); error[0] = 3; error[1] = 4
        put16(error, 2, sum(error, 0, error.size).inv())
        val received = n.incoming(packet(remote, assigned, 1, error), 1)!!
        valid(received)
        assertEquals(local, number(received, 16))
        assertEquals(local, number(received, 28 + 12)); assertEquals(dns, number(received, 28 + 16))
        assertEquals(65535, sum(received, 28, 48))
        assertArrayEquals(original.copyOf(28), received.copyOfRange(28, received.size))
    }

    @Test fun malformedPacketsDoNotThrowOrProduceTranslatedTraffic() {
        val n = nat(); val bytes = udp(local, remote)
        for (i in bytes.indices) assertNull(n.outgoing(bytes.copyOf(i), i.toLong()))
        assertNull(n.outgoing(bytes.clone().also { it[10] = (it[10].toInt() xor 1).toByte() }, 0))
        val random = java.util.Random(19)
        repeat(1000) { val b = ByteArray(random.nextInt(1600)); random.nextBytes(b); n.incoming(b, it.toLong()); n.outgoing(b, it.toLong()) }
    }

    @Test fun routesPreservePrivateAndRealPublicLansWhileDnsUsesOnlyItsHostRoute() {
        val lan = UsbInternetPlan.Cidr(0xc6336400.toInt(), 24)
        val plan = UsbInternetPlan.create(listOf(lan))
        fun routed(ip: Int) = plan.routes.any { it.contains(ip) }
        for (ip in listOf(0x0a000001, 0xac100001.toInt(), 0xc0a80101.toInt(), 0x64600001, 0x7f000001,
            0xa9fe0001.toInt(), 0xc6336411.toInt(), 0xe0000001.toInt())) assertFalse(routed(ip))
        for (ip in listOf(0x08080808, 0x01010101, 0xc0000201.toInt(), remote, plan.dns)) assertTrue(routed(ip))
        assertFalse(routed(plan.address)); assertTrue(plan.routes.size < 200)
        for (i in plan.routes.indices) for (j in 0 until i) {
            assertFalse(plan.routes[i].contains(plan.routes[j].address))
            assertFalse(plan.routes[j].contains(plan.routes[i].address))
        }
        val fallback = UsbInternetPlan.create(listOf(UsbInternetPlan.Cidr(0xc0000000.toInt(), 29)))
        assertEquals(0xc6120002.toInt(), fallback.address)
    }
}
