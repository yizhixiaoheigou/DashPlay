package com.dashplay.android

import java.security.SecureRandom

/** Single-thread Ethernet/IP adapter. Its callbacks do not themselves install routes or enable VPN. */
internal class UsbTetherLink(
    mac: ByteArray,
    private val send: (ByteArray) -> Unit,
    private val changed: (UsbTetherProtocol.Lease?, String) -> Unit,
    private val incoming: (ByteArray) -> Unit,
    private val random: () -> Int = SecureRandom()::nextInt,
) {
    private val mac = mac.copyOf()
    private val dhcp = UsbTetherDhcp(mac, random)
    private var carrier = false
    private var closed = false
    private var offered: UsbTetherProtocol.Lease? = null
    var lease: UsbTetherProtocol.Lease? = null; private set
    private var probeCount = 0
    private var probeAt = 0L
    private var announceAt = Long.MAX_VALUE
    private data class Neighbor(val mac: ByteArray, val at: Long)
    private data class Pending(val packet: ByteArray, val target: Int, val at: Long)
    private val neighbors = linkedMapOf<Int, Neighbor>()
    private val pending = ArrayDeque<Pending>()
    private val asked = mutableMapOf<Int, Long>()

    fun carrier(available: Boolean, now: Long) {
        if (closed || available == carrier) return
        carrier = available
        if (!available) { dhcp.reset(now); withdraw("等待 iPhone 开启个人热点") }
        else { dhcp.reset(now); changed(null, "正在申请 iPhone USB 热点地址"); tick(now) }
    }
    fun tick(now: Long) {
        if (closed || !carrier) return
        events(dhcp.tick(now), now)
        val candidate = offered
        if (candidate != null && now >= probeAt) {
            if (probeCount < 3) {
                send(UsbTetherProtocol.arp(mac, 0, ByteArray(6), candidate.address, false))
                probeCount++
                probeAt = now + if (probeCount == 3) 2000 else 1000 + jitter(1001)
            } else {
                offered = null; lease = candidate
                send(UsbTetherProtocol.arp(mac, candidate.address, ByteArray(6), candidate.address, false))
                announceAt = now + 2000
                changed(candidate, "USB 热点地址已就绪")
            }
        }
        lease?.let { active ->
            if (now >= announceAt) {
                announceAt = Long.MAX_VALUE
                send(UsbTetherProtocol.arp(mac, active.address, ByteArray(6), active.address, false))
            }
            while (pending.isNotEmpty() && now - pending.first().at >= 5000) pending.removeFirst()
            asked.entries.removeAll { now - it.value >= 5000 && pending.none { packet -> packet.target == it.key } }
            neighbors.entries.removeAll { now - it.value.at >= 60_000 }
            for (target in pending.map { it.target }.distinct()) resolve(target, now)
        }
    }
    fun receive(frame: ByteArray, now: Long) {
        if (closed || !carrier || frame.size !in 14..1514) return
        // Check expiry even when an incoming burst delays the next scheduled tick.
        events(dhcp.receive(frame, now), now)
        val destination = frame.copyOfRange(0, 6)
        if (!destination.contentEquals(mac) && !destination.contentEquals(UsbTetherProtocol.broadcastMac)) return
        val arp = UsbTetherProtocol.parseArp(frame)
        val local = lease ?: offered
        if (arp != null && local != null && !arp.mac.contentEquals(mac)) {
            if (arp.address == local.address || offered != null && arp.address == 0 && arp.target == local.address) {
                events(dhcp.conflict(now), now); return
            }
            val active = lease ?: return
            if (arp.operation == 1 && arp.target == active.address) {
                send(UsbTetherProtocol.arp(mac, active.address, arp.mac, arp.address, true))
            }
            if (arp.target == active.address && UsbTetherProtocol.unicast(arp.address)) {
                if (neighbors.size >= 32) neighbors.remove(neighbors.keys.first())
                neighbors[arp.address] = Neighbor(arp.mac, now)
                asked.remove(arp.address)
                val ready = pending.filter { it.target == arp.address }
                pending.removeAll { it.target == arp.address }
                ready.filter { now - it.at < 5000 }.forEach { send(UsbTetherProtocol.ethernet(mac, arp.mac, 0x800, it.packet)) }
            }
            return
        }
        val active = lease ?: return
        if (UsbTetherProtocol.u16(frame, 12) != 0x800) return
        val packet = ipv4(frame.copyOfRange(14, frame.size)) ?: return
        if (UsbTetherProtocol.i32(packet, 16) != active.address) return
        // DHCP stays inside this adapter; it is not delivered to an application's TUN.
        val ihl = (packet[0].toInt() and 15) * 4
        if (packet[9] == 17.toByte() && packet.size >= ihl + 8 &&
            UsbTetherProtocol.u16(packet, ihl) == 67 && UsbTetherProtocol.u16(packet, ihl + 2) == 68) return
        incoming(packet)
    }
    fun sendIpv4(bytes: ByteArray, now: Long): Boolean {
        tick(now)
        val active = lease ?: return false
        val packet = ipv4(bytes) ?: return false
        if (UsbTetherProtocol.i32(packet, 12) != active.address) return false
        val destination = UsbTetherProtocol.i32(packet, 16)
        if (!UsbTetherProtocol.unicast(destination)) return false
        val mask = -1 shl (32 - active.prefix)
        val target = if ((destination and mask) == (active.address and mask)) destination else active.router
        transmit(packet, target, now)
        return true
    }
    private fun transmit(packet: ByteArray, target: Int, now: Long) {
        if (target == UsbTetherProtocol.BROADCAST) {
            send(UsbTetherProtocol.ethernet(mac, UsbTetherProtocol.broadcastMac, 0x800, packet)); return
        }
        val neighbor = neighbors[target]?.takeIf { now - it.at < 60_000 }
        if (neighbor != null) send(UsbTetherProtocol.ethernet(mac, neighbor.mac, 0x800, packet))
        else {
            if (pending.size >= 32) {
                val removed = pending.removeFirst()
                if (pending.none { it.target == removed.target }) asked.remove(removed.target)
            }
            pending.addLast(Pending(packet.copyOf(), target, now)); resolve(target, now)
        }
    }
    private fun resolve(target: Int, now: Long) {
        val active = lease ?: return
        if (asked[target]?.let { now - it < 1000 } == true) return
        asked[target] = now
        send(UsbTetherProtocol.arp(mac, active.address, ByteArray(6), target, false))
    }
    private fun events(events: List<UsbTetherDhcp.Event>, now: Long) {
        for (event in events) when (event) {
            is UsbTetherDhcp.Event.Send -> transmit(event.packet, event.destination, now)
            is UsbTetherDhcp.Event.Lost -> withdraw(event.reason)
            is UsbTetherDhcp.Event.Bound -> {
                if (lease?.address == event.lease.address) {
                    lease = event.lease; changed(lease, "USB 热点地址已续期")
                } else if (offered?.address == event.lease.address) {
                    offered = event.lease
                } else {
                    withdraw("正在检查 USB 热点地址")
                    offered = event.lease; probeCount = 0; probeAt = now + jitter(1001)
                }
            }
        }
    }
    private fun withdraw(message: String) {
        lease = null; offered = null; neighbors.clear(); pending.clear(); asked.clear()
        announceAt = Long.MAX_VALUE; changed(null, message)
    }
    fun close() { if (!closed) { closed = true; carrier = false; dhcp.close(); withdraw("USB 热点已关闭") } }
    private fun jitter(bound: Int) = (random().toLong() and 0xffffffffL) % bound
    private fun ipv4(b: ByteArray): ByteArray? {
        if (b.size !in 20..1500 || b[0].toInt() ushr 4 != 4) return null
        val ihl = (b[0].toInt() and 15) * 4
        if (ihl !in 20..b.size || UsbTetherProtocol.checksum(b, 0, ihl) != 0) return null
        val length = UsbTetherProtocol.u16(b, 2)
        if (length !in ihl..b.size) return null
        return b.copyOf(length)
    }
}
