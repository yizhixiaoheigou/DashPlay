package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayOutputStream

/** Independent, explicitly synthetic DHCP server packets; no real phone or Internet claim. */
class UsbTetherTest {
    private val mac = byteArrayOf(2, 0, 0, 0, 0, 2)
    private val serverMac = byteArrayOf(2, 0, 0, 0, 0, 1)
    private val host = 0xc000020a.toInt()
    private val server = 0xc0000201.toInt()
    private val xid = 0x12345678
    private fun options(type: Int, serverId: Int = server, seconds: Int = 80): ByteArray {
        val out = ByteArrayOutputStream()
        fun option(id: Int, vararg value: Int) { out.write(id); out.write(value.size); value.forEach(out::write) }
        fun number(id: Int, value: Int) = option(id, value ushr 24, value ushr 16, value ushr 8, value)
        option(53, type); number(54, serverId); number(1, 0xffffff00.toInt()); number(3, server)
        number(6, 0xc0000235.toInt()); number(51, seconds); number(58, seconds / 2); number(59, seconds * 7 / 8)
        out.write(255); return out.toByteArray()
    }
    private fun body(type: Int = 2, transaction: Int = xid, address: Int = host, serverId: Int = server): ByteArray {
        val b = ByteArray(300); b[0] = 2; b[1] = 1; b[2] = 6
        fun number(p: Int, n: Int) { for (i in 0..3) b[p + i] = (n ushr (24 - i * 8)).toByte() }
        number(4, transaction); number(16, address); number(236, 0x63825363); mac.copyInto(b, 28)
        options(type, serverId).copyInto(b, 240); return b
    }
    private fun frame(body: ByteArray, fragmented: Boolean = false): ByteArray {
        // Deliberately independent IPv4 builder; UDP checksum zero is legal in IPv4.
        val b = ByteArray(14 + 28 + body.size)
        UsbTetherProtocol.broadcastMac.copyInto(b); serverMac.copyInto(b, 6); b[12] = 8
        b[14] = 0x45; val length = b.size - 14; b[16] = (length ushr 8).toByte(); b[17] = length.toByte()
        if (fragmented) b[20] = 0x20
        b[22] = 64; b[23] = 17
        byteArrayOf(192.toByte(), 0, 2, 1).copyInto(b, 26)
        byteArrayOf(-1, -1, -1, -1).copyInto(b, 30)
        var sum = 0
        for (p in 14 until 34 step 2) sum += ((b[p].toInt() and 255) shl 8) + (b[p + 1].toInt() and 255)
        while (sum ushr 16 != 0) sum = (sum and 65535) + (sum ushr 16)
        sum = sum.inv() and 65535; b[24] = (sum ushr 8).toByte(); b[25] = sum.toByte()
        b[35] = 67; b[37] = 68
        val size = body.size + 8; b[38] = (size ushr 8).toByte(); b[39] = size.toByte()
        body.copyInto(b, 42); return b
    }
    private fun client() = UsbTetherDhcp(mac) { xid }
    private fun bound(): UsbTetherDhcp = client().also {
        it.tick(0); it.receive(frame(body()), 100); it.receive(frame(body(5)), 200)
        assertEquals(UsbTetherDhcp.Phase.BOUND, it.phase)
    }

    @Test fun discoverAndSelectingRequestUseBroadcastAndExplicitRequestedAddress() {
        val c = client()
        val discover = (c.tick(0).single() as UsbTetherDhcp.Event.Send).packet
        assertEquals(0, UsbTetherProtocol.i32(discover, 12))
        assertEquals(-1, UsbTetherProtocol.i32(discover, 16))
        assertEquals(0, UsbTetherProtocol.checksum(discover, 0, 20))
        val request = (c.receive(frame(body()), 10).single() as UsbTetherDhcp.Event.Send).packet
        assertEquals(3.toByte(), request[28 + 242])
        assertEquals(0x8000, UsbTetherProtocol.u16(request, 28 + 10))
        assertEquals(0, UsbTetherProtocol.i32(request, 28 + 12))
        assertEquals(UsbTetherDhcp.Phase.REQUESTING, c.phase)
        assertNull(c.lease)
        val event = c.receive(frame(body(5)), 20).single() as UsbTetherDhcp.Event.Bound
        assertEquals("192.0.2.10", UsbTetherProtocol.address(event.lease.address))
        assertEquals(24, event.lease.prefix)
        assertEquals(listOf(0xc0000235.toInt()), event.lease.dns)
    }

    @Test fun foreignTransactionsClientsAndServersCannotSupplyLease() {
        val c = client(); c.tick(0)
        assertTrue(c.receive(frame(body(transaction = 7)), 1).isEmpty())
        assertTrue(c.receive(frame(body().also { it[28] = 4 }), 2).isEmpty())
        c.receive(frame(body()), 3)
        assertTrue(c.receive(frame(body(5, serverId = server + 1)), 4).isEmpty())
        assertTrue(c.receive(frame(body(5, address = host + 1)), 5).isEmpty())
        assertNull(c.lease)
    }

    @Test fun renewalIsUnicastRebindingBroadcastAndExpiryWithdrawsAddress() {
        val c = bound()
        assertTrue(c.tick(40_199).isEmpty())
        val renew = c.tick(40_200).single() as UsbTetherDhcp.Event.Send
        assertEquals(server, renew.destination)
        assertEquals(host, UsbTetherProtocol.i32(renew.packet, 12))
        assertEquals(host, UsbTetherProtocol.i32(renew.packet, 28 + 12))
        assertEquals(0, UsbTetherProtocol.u16(renew.packet, 28 + 10))
        val rebind = c.tick(70_200).single() as UsbTetherDhcp.Event.Send
        assertEquals(-1, rebind.destination)
        val expired = c.tick(80_200)
        assertTrue(expired.first() is UsbTetherDhcp.Event.Lost)
        assertTrue(expired.last() is UsbTetherDhcp.Event.Send)
        assertNull(c.lease)
        assertEquals(UsbTetherDhcp.Phase.SELECTING, c.phase)
    }

    @Test fun renewAckExtendsLeaseButNakOrCarrierLossInvalidatesImmediately() {
        val c = bound(); c.tick(40_200)
        assertTrue(c.receive(frame(body(5)), 40_300).single() is UsbTetherDhcp.Event.Bound)
        assertTrue(c.tick(80_200).isEmpty())
        c.tick(80_300)
        assertTrue(c.receive(frame(body(6)), 80_400).single() is UsbTetherDhcp.Event.Lost)
        assertNull(c.lease); assertTrue(c.tick(80_401).isEmpty())
        val d = bound(); assertTrue(d.reset(300).single() is UsbTetherDhcp.Event.Lost); assertNull(d.lease)
        d.close(); assertTrue(d.tick(1_000_000).isEmpty()); assertTrue(d.receive(frame(body()), 1_000_001).isEmpty())
    }

    @Test fun wrongLengthsFragmentsChecksumsAndUnterminatedOptionsAreRejected() {
        val valid = frame(body())
        for (size in valid.indices) assertNull(UsbTetherProtocol.reply(valid.copyOf(size), mac, xid))
        assertNull(UsbTetherProtocol.reply(frame(body(), true), mac, xid))
        assertNull(UsbTetherProtocol.reply(valid.clone().also { it[24] = (it[24].toInt() xor 1).toByte() }, mac, xid))
        assertNull(UsbTetherProtocol.reply(valid.clone().also { it[40] = 1 }, mac, xid))
        assertNull(UsbTetherProtocol.reply(frame(body().also { for (i in 240 until it.size) it[i] = 0 }), mac, xid))
        assertNull(UsbTetherProtocol.reply(frame(body().also { it[299] = 42; it.fill(0, 240, 299) }), mac, xid))
    }

    @Test fun overloadedAndSplitDhcpOptionsAreParsedWithoutReadingBeyondFields() {
        val b = body(); b.fill(0, 240)
        byteArrayOf(52, 1, 1, -1).copyInto(b, 240)
        options(2).copyInto(b, 108)
        val reply = UsbTetherProtocol.reply(frame(b), mac, xid)!!
        assertNotNull(UsbTetherProtocol.lease(reply))
        b[242] = 4; assertNull(UsbTetherProtocol.reply(frame(b), mac, xid))
        val split = body(); val opts = options(2)
        // Split the six-byte DNS TLV into two parts as permitted by RFC 3396.
        val dnsOffset = opts.indices.first { it + 5 < opts.size && opts[it] == 6.toByte() && opts[it + 1] == 4.toByte() }
        val newOpts = opts.copyOfRange(0, dnsOffset) + byteArrayOf(6, 2) + opts.copyOfRange(dnsOffset + 2, dnsOffset + 4) +
            byteArrayOf(6, 2) + opts.copyOfRange(dnsOffset + 4, opts.size)
        newOpts.copyInto(split, 240)
        assertEquals(reply.options[6]!!.toList(), UsbTetherProtocol.reply(frame(split), mac, xid)!!.options[6]!!.toList())
    }

    @Test fun invalidSubnetRouterDnsAndLeaseTimersNeverBecomeUsable() {
        val reply = UsbTetherProtocol.reply(frame(body()), mac, xid)!!
        fun changed(id: Int, number: Int) = reply.copy(options = reply.options +
            (id to ByteArray(4).also { UsbTetherProtocol.put32(it, 0, number) }))
        for (bad in listOf(changed(1, 0xff00ff00.toInt()), changed(3, 0xcb007101.toInt()), changed(6, 0),
            changed(51, 0), changed(58, 79), changed(59, 90), reply.copy(address = 0xc00002ff.toInt())))
            assertNull(UsbTetherProtocol.lease(bad))
    }

    @Test fun arpAndLegacyUsbFramingRemainSeparateFromCarPlayNcm() {
        val arp = UsbTetherProtocol.arp(mac, host, serverMac, server, true)
        val parsed = UsbTetherProtocol.parseArp(arp)!!
        assertEquals(host, parsed.address); assertEquals(server, parsed.target); assertEquals(2, parsed.operation)
        assertArrayEquals(arp, UsbTetherProtocol.legacyFrame(byteArrayOf(0, 0) + arp))
        assertNull(UsbTetherProtocol.legacyFrame(byteArrayOf(0, 1, 0, 0)))
        assertNull(UsbTetherProtocol.parseArp(arp.clone().also { it[22] = 4 }))
    }

    @Test fun retriesAreBoundedAndSelectingRestartsAfterUnansweredRequest() {
        val c = client(); c.tick(0)
        assertTrue(c.tick(1).isEmpty())
        c.receive(frame(body()), 2)
        val later = c.tick(60_002)
        assertEquals(1, later.size)
        assertEquals(UsbTetherDhcp.Phase.SELECTING, c.phase)
        assertNull(c.lease)
    }

    @Test fun lateAckCannotResurrectExpiredLeaseEvenWhenTickWasDelayed() {
        val c = bound(); c.tick(40_200)
        val events = c.receive(frame(body(5)), 80_200)
        assertTrue(events.first() is UsbTetherDhcp.Event.Lost)
        assertFalse(events.any { it is UsbTetherDhcp.Event.Bound })
        assertNull(c.lease)
    }

    @Test fun addressProbeArpGatewayAndIpv4WorkTogetherBeforeAndAfterCarrierLoss() {
        val sent = mutableListOf<ByteArray>(); val incoming = mutableListOf<ByteArray>()
        val changes = mutableListOf<UsbTetherProtocol.Lease?>()
        val link = UsbTetherLink(mac, { sent += it }, { l, _ -> changes += l }, { incoming += it }) { xid }
        link.carrier(true, 0); link.receive(frame(body()), 10); link.receive(frame(body(5)), 20)
        assertNull(link.lease)
        for (now in 1000L..9000L step 1000) link.tick(now)
        assertEquals(host, link.lease!!.address)
        val probeFrames = sent.mapNotNull(UsbTetherProtocol::parseArp).filter { it.address == 0 }
        assertEquals(3, probeFrames.size)
        val packet = UsbTetherProtocol.udp(host, 0xcb007105.toInt(), 1234, 443, byteArrayOf(1, 2, 3))
        assertTrue(link.sendIpv4(packet, 9001))
        assertEquals(server, UsbTetherProtocol.parseArp(sent.last())!!.target)
        val answer = UsbTetherProtocol.arp(serverMac, server, mac, host, true)
        link.receive(answer, 9002)
        val transmitted = sent.last()
        assertArrayEquals(serverMac, transmitted.copyOfRange(0, 6))
        assertArrayEquals(packet, transmitted.copyOfRange(14, 14 + packet.size))
        val reply = UsbTetherProtocol.udp(0xcb007105.toInt(), host, 443, 1234, byteArrayOf(4, 5, 6))
        link.receive(UsbTetherProtocol.ethernet(serverMac, mac, 0x800, reply), 9003)
        assertArrayEquals(reply, incoming.single())
        link.carrier(false, 9004)
        assertNull(link.lease); assertNull(changes.last()); assertFalse(link.sendIpv4(packet, 9005))
        val count = sent.size; link.tick(20_000); assertEquals(count, sent.size)
        link.close(); link.carrier(true, 30_000); assertEquals(count, sent.size)
    }

    @Test fun conflictingArpDeclinesLeaseAndWaitsInsteadOfAdvertisingDuplicateAddress() {
        val sent = mutableListOf<ByteArray>()
        val link = UsbTetherLink(mac, { sent += it }, { _, _ -> }, {}) { xid }
        link.carrier(true, 0); link.receive(frame(body()), 10); link.receive(frame(body(5)), 20)
        link.receive(UsbTetherProtocol.arp(serverMac, host, mac, host, true), 30)
        assertNull(link.lease)
        val decline = sent.last()
        assertEquals(4.toByte(), decline[14 + 28 + 242])
        val count = sent.size; link.tick(10_029); assertEquals(count, sent.size)
        link.tick(10_030); assertTrue(sent.size > count)
    }

    @Test fun packetsForAnotherHostAndForgedSourceCannotEnterOrLeaveLink() {
        val incoming = mutableListOf<ByteArray>()
        val link = UsbTetherLink(mac, {}, { _, _ -> }, { incoming += it }) { xid }
        link.carrier(true, 0); link.receive(frame(body()), 10); link.receive(frame(body(5)), 20)
        for (now in 1000L..9000L step 1000) link.tick(now)
        val packet = UsbTetherProtocol.udp(server, host + 1, 443, 1234, byteArrayOf(1))
        link.receive(UsbTetherProtocol.ethernet(serverMac, mac, 0x800, packet), 9001)
        assertTrue(incoming.isEmpty())
        assertFalse(link.sendIpv4(packet, 9002))
        assertFalse(link.sendIpv4(ByteArray(10), 9003))
    }

    @Test fun sessionCloseWakesBlockedUsbAndWithdrawsOnceWithoutLateCallbacks() {
        val entered = java.util.concurrent.CountDownLatch(1)
        val released = java.util.concurrent.CountDownLatch(1)
        val terminal = java.util.concurrent.CountDownLatch(1)
        val callbacks = java.util.concurrent.atomic.AtomicInteger()
        val wire = object : UsbTetherSession.Wire {
            override val hostMac = mac
            override fun receive(timeoutMillis: Long): ByteArray? {
                entered.countDown(); check(released.await(3, java.util.concurrent.TimeUnit.SECONDS)); return null
            }
            override fun carrier() = error("must not query after close")
            override fun send(frame: ByteArray) = error("must not send after close")
            override fun close() { released.countDown() }
        }
        val session = UsbTetherSession(wire, { lease, _ -> assertNull(lease); callbacks.incrementAndGet(); terminal.countDown() }, {})
        session.start(); assertTrue(entered.await(2, java.util.concurrent.TimeUnit.SECONDS))
        session.close(); session.close()
        assertTrue(terminal.await(2, java.util.concurrent.TimeUnit.SECONDS))
        assertEquals(1, callbacks.get()); assertNull(session.lease)
        assertFalse(session.sendIpv4(ByteArray(30)))
        try { session.start(); fail("closed session restarted") } catch (_: IllegalStateException) { }
    }

    @Test fun usbReadFailureClosesOwnedInterfaceAndReportsNoUsableLease() {
        val closed = java.util.concurrent.CountDownLatch(1)
        val failed = java.util.concurrent.CountDownLatch(1)
        val wire = object : UsbTetherSession.Wire {
            override val hostMac = mac
            override fun receive(timeoutMillis: Long): ByteArray? = throw java.io.IOException("fixture detach")
            override fun carrier() = true
            override fun send(frame: ByteArray) { }
            override fun close() { closed.countDown() }
        }
        val session = UsbTetherSession(wire, { lease, message ->
            assertNull(lease); assertTrue(message.contains("中断")); failed.countDown()
        }, {})
        session.start()
        assertTrue(failed.await(2, java.util.concurrent.TimeUnit.SECONDS))
        assertTrue(closed.await(2, java.util.concurrent.TimeUnit.SECONDS)); assertNull(session.lease)
        session.close()
    }
}
