package com.dashplay.android

import java.security.SecureRandom

/** Single-owner DHCP state machine. A lease is usable only after ACK and never beyond its deadline. */
internal class UsbTetherDhcp(mac: ByteArray, private val random: () -> Int = SecureRandom()::nextInt) {
    enum class Phase { SELECTING, REQUESTING, BOUND, RENEWING, REBINDING, CLOSED }
    sealed class Event {
        data class Send(val packet: ByteArray, val destination: Int) : Event()
        data class Bound(val lease: UsbTetherProtocol.Lease) : Event()
        data class Lost(val reason: String) : Event()
    }
    private val mac = mac.copyOf().also { require(UsbTetherProtocol.validMac(it)) }
    var phase = Phase.SELECTING; private set
    var lease: UsbTetherProtocol.Lease? = null; private set
    private var offer: UsbTetherProtocol.Reply? = null
    private var xid = random()
    private var started = -1L
    private var next = 0L
    private var delay = 4_000L
    private var boundAt = 0L
    private var conflicts = 0

    fun tick(now: Long): List<Event> {
        require(now >= 0)
        if (phase == Phase.CLOSED) return emptyList()
        val events = mutableListOf<Event>()
        val current = lease
        if (current != null) {
            if (now - boundAt >= current.seconds * 1000) {
                events += Event.Lost("USB 热点地址已到期")
                restart(now)
            } else if (now - boundAt >= current.rebindSeconds * 1000 && phase != Phase.REBINDING) {
                if (phase == Phase.BOUND) { xid = random(); started = now }
                phase = Phase.REBINDING; next = now; delay = 4_000
            } else if (phase == Phase.BOUND && now - boundAt >= current.renewSeconds * 1000) {
                phase = Phase.RENEWING; xid = random(); started = now; next = now; delay = 4_000
            }
        }
        if (phase == Phase.BOUND || now < next) return events
        if (started < 0) started = now
        if (phase == Phase.REQUESTING && now - started >= 60_000) restart(now)
        val old = lease
        val packet = UsbTetherProtocol.request(mac, xid, if (phase == Phase.SELECTING) 1 else 3,
            ((now - started) / 1000).coerceAtMost(65535).toInt(), offer, old, phase == Phase.REBINDING)
        events += Event.Send(packet, UsbTetherProtocol.i32(packet, 16))
        // RFC 2131: exponential retry with +/- 1 second jitter, bounded at 64 seconds.
        var wait = delay + ((random().toLong() and 0xffffffffL) % 2001) - 1000
        old?.let {
            val deadline = boundAt + (if (phase == Phase.RENEWING) it.rebindSeconds else it.seconds) * 1000
            wait = minOf(wait, deadline - now)
        }
        next = now + wait.coerceAtLeast(1)
        delay = (delay * 2).coerceAtMost(64_000)
        return events
    }

    fun receive(frame: ByteArray, now: Long): List<Event> {
        val events = tick(now)
        return events + accept(frame, now)
    }

    private fun accept(frame: ByteArray, now: Long): List<Event> {
        if (phase in listOf(Phase.CLOSED, Phase.BOUND)) return emptyList()
        val response = UsbTetherProtocol.reply(frame, mac, xid) ?: return emptyList()
        if (phase == Phase.SELECTING) {
            if (response.type != 2 || UsbTetherProtocol.lease(response) == null) return emptyList()
            offer = response; phase = Phase.REQUESTING; started = now; next = now; delay = 4_000
            return tick(now)
        }
        val expected = offer?.server ?: lease?.server ?: return emptyList()
        if (phase != Phase.REBINDING && response.server != expected) return emptyList()
        if (response.type == 6) {
            val hadLease = lease != null
            restart(now)
            // Allow the server to settle instead of looping rapidly on NAK.
            next = now + 1000
            return if (hadLease) listOf(Event.Lost("iPhone 已撤销 USB 热点地址")) else emptyList()
        }
        if (response.type != 5) return emptyList()
        val accepted = UsbTetherProtocol.lease(response) ?: return emptyList()
        if (accepted.address != (offer?.address ?: lease?.address)) return emptyList()
        lease = accepted; offer = null; phase = Phase.BOUND; boundAt = now
        return listOf(Event.Bound(accepted))
    }

    /** Carrier loss/replug discards the old lease before requesting a fresh one. */
    fun reset(now: Long): List<Event> {
        if (phase == Phase.CLOSED) return emptyList()
        val hadLease = lease != null; restart(now)
        return if (hadLease) listOf(Event.Lost("USB 热点连接中断")) else emptyList()
    }
    fun conflict(now: Long): List<Event> {
        val old = lease ?: return emptyList()
        val declined = UsbTetherProtocol.Reply(2, old.address, old.server, emptyMap())
        val packet = UsbTetherProtocol.request(mac, xid, 4, selected = declined)
        conflicts++
        restart(now); next = now + if (conflicts >= 10) 60_000 else 10_000
        return listOf(Event.Lost("USB 热点地址冲突，正在重新申请"), Event.Send(packet, UsbTetherProtocol.BROADCAST))
    }
    private fun restart(now: Long) {
        lease = null; offer = null; phase = Phase.SELECTING; xid = random()
        started = now; next = now; delay = 4_000
    }
    fun close() { lease = null; offer = null; phase = Phase.CLOSED }
}
