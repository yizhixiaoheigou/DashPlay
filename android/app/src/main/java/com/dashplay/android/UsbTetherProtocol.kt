package com.dashplay.android

import java.io.ByteArrayOutputStream

/** Wire formats for the iPhone's separate Personal Hotspot interface, never the CarPlay NCM link. */
internal object UsbTetherProtocol {
    val broadcastMac = ByteArray(6) { 0xff.toByte() }
    const val BROADCAST = -1
    data class Lease(val address: Int, val prefix: Int, val router: Int, val dns: List<Int>,
        val server: Int, val seconds: Long, val renewSeconds: Long, val rebindSeconds: Long)
    data class Reply(val type: Int, val address: Int, val server: Int, val options: Map<Int, ByteArray>)
    data class Arp(val operation: Int, val mac: ByteArray, val address: Int, val target: Int)

    fun u16(b: ByteArray, p: Int) = (b[p].toInt() and 255 shl 8) or (b[p + 1].toInt() and 255)
    fun i32(b: ByteArray, p: Int) = (u16(b, p) shl 16) or u16(b, p + 2)
    fun put16(b: ByteArray, p: Int, n: Int) { b[p] = (n ushr 8).toByte(); b[p + 1] = n.toByte() }
    fun put32(b: ByteArray, p: Int, n: Int) { put16(b, p, n ushr 16); put16(b, p + 2, n) }
    fun address(n: Int) = (3 downTo 0).joinToString(".") { ((n ushr (it * 8)) and 255).toString() }
    fun unicast(n: Int) = (n ushr 24) in 1..223 && (n ushr 24) != 127 && (n ushr 16) != 0xa9fe
    fun validMac(mac: ByteArray) = mac.size == 6 && mac[0].toInt() and 1 == 0 && mac.any { it != 0.toByte() }
    fun checksum(b: ByteArray, start: Int = 0, size: Int = b.size - start, initial: Long = 0): Int {
        var sum = initial; var p = start
        while (p + 1 < start + size) { sum += u16(b, p); p += 2 }
        if (p < start + size) sum += (b[p].toInt() and 255) shl 8
        while (sum ushr 16 != 0L) sum = (sum and 65535) + (sum ushr 16)
        return sum.toInt().inv() and 65535
    }

    fun ethernet(source: ByteArray, destination: ByteArray, type: Int, payload: ByteArray): ByteArray {
        require(validMac(source) && destination.size == 6 && payload.size <= 1500)
        return ByteArray(maxOf(60, 14 + payload.size)).also {
            destination.copyInto(it); source.copyInto(it, 6); put16(it, 12, type); payload.copyInto(it, 14)
        }
    }

    /** Legacy ipheth RX has two prefix bytes. Its four-byte notification is not an Ethernet frame. */
    fun legacyFrame(bytes: ByteArray): ByteArray? {
        if (bytes.size !in 16..1516) return null
        return bytes.copyOfRange(2, bytes.size).takeIf { validMac(it.copyOfRange(6, 12)) }
    }

    fun arp(sourceMac: ByteArray, source: Int, targetMac: ByteArray, target: Int, reply: Boolean): ByteArray {
        val b = ByteArray(28)
        put16(b, 0, 1); put16(b, 2, 0x800); b[4] = 6; b[5] = 4; put16(b, 6, if (reply) 2 else 1)
        sourceMac.copyInto(b, 8); put32(b, 14, source); targetMac.copyInto(b, 18); put32(b, 24, target)
        return ethernet(sourceMac, if (reply) targetMac else broadcastMac, 0x806, b)
    }
    fun parseArp(frame: ByteArray): Arp? {
        if (frame.size < 42 || u16(frame, 12) != 0x806 || u16(frame, 14) != 1 || u16(frame, 16) != 0x800 ||
            frame[18] != 6.toByte() || frame[19] != 4.toByte() || u16(frame, 20) !in 1..2) return null
        val mac = frame.copyOfRange(22, 28)
        if (!validMac(mac) || !mac.contentEquals(frame.copyOfRange(6, 12))) return null
        return Arp(u16(frame, 20), mac, i32(frame, 28), i32(frame, 38))
    }

    fun request(mac: ByteArray, xid: Int, type: Int, elapsedSeconds: Int = 0,
        selected: Reply? = null, current: Lease? = null, rebind: Boolean = false): ByteArray {
        require(validMac(mac) && type in listOf(1, 3, 4))
        require(type != 4 || selected != null && current == null)
        require(type != 3 || ((selected != null) xor (current != null)))
        val options = ByteArrayOutputStream()
        fun option(id: Int, value: ByteArray) { options.write(id); options.write(value.size); options.write(value) }
        fun ipOption(id: Int, value: Int) = option(id, ByteArray(4).also { put32(it, 0, value) })
        option(53, byteArrayOf(type.toByte())); option(61, byteArrayOf(1) + mac)
        option(55, byteArrayOf(1, 3, 6, 51, 58, 59)); option(57, byteArrayOf(5, 0xdc.toByte()))
        if (selected != null) { ipOption(50, selected.address); ipOption(54, selected.server) }
        options.write(255)
        val body = ByteArray(maxOf(300, 240 + options.size()))
        body[0] = 1; body[1] = 1; body[2] = 6; put32(body, 4, xid)
        put16(body, 8, elapsedSeconds.coerceIn(0, 65535))
        if (current == null || rebind) put16(body, 10, 0x8000)
        put32(body, 12, current?.address ?: 0); mac.copyInto(body, 28)
        put32(body, 236, 0x63825363); options.toByteArray().copyInto(body, 240)
        return udp(current?.address ?: 0, if (current != null && !rebind) current.server else BROADCAST, 68, 67, body)
    }

    /** An IPv4 UDP packet, with both IP and UDP checksums, for the Ethernet transport. */
    fun udp(source: Int, target: Int, sourcePort: Int, targetPort: Int, payload: ByteArray): ByteArray {
        require(payload.size <= 1472)
        val b = ByteArray(28 + payload.size)
        b[0] = 0x45; put16(b, 2, b.size); b[8] = 64; b[9] = 17
        put32(b, 12, source); put32(b, 16, target); put16(b, 10, checksum(b, 0, 20))
        put16(b, 20, sourcePort); put16(b, 22, targetPort); put16(b, 24, b.size - 20)
        payload.copyInto(b, 28)
        val pseudo = u16(b, 12).toLong() + u16(b, 14) + u16(b, 16) + u16(b, 18) + 17 + b.size - 20
        val check = checksum(b, 20, b.size - 20, pseudo)
        put16(b, 26, if (check == 0) 65535 else check)
        return b
    }

    fun reply(frame: ByteArray, mac: ByteArray, xid: Int): Reply? {
        if (frame.size !in (14 + 28 + 240)..1514 || u16(frame, 12) != 0x800) return null
        val dest = frame.copyOfRange(0, 6)
        if (!dest.contentEquals(mac) && !dest.contentEquals(broadcastMac)) return null
        val ip = frame.copyOfRange(14, frame.size)
        val ihl = (ip[0].toInt() and 15) * 4
        if (ip[0].toInt() ushr 4 != 4 || ihl < 20 || ihl + 8 + 240 > ip.size ||
            ip[9] != 17.toByte() || u16(ip, 6) and 0xbfff != 0 || checksum(ip, 0, ihl) != 0) return null
        val total = u16(ip, 2); val udpLength = u16(ip, ihl + 4)
        if (total > ip.size || total < ihl + 8 + 240 || udpLength != total - ihl ||
            u16(ip, ihl) != 67 || u16(ip, ihl + 2) != 68) return null
        if (u16(ip, ihl + 6) != 0) {
            val pseudo = u16(ip, 12).toLong() + u16(ip, 14) + u16(ip, 16) + u16(ip, 18) + 17 + udpLength
            if (checksum(ip, ihl, udpLength, pseudo) != 0) return null
        }
        val b = ip.copyOfRange(ihl + 8, total)
        if (b[0] != 2.toByte() || b[1] != 1.toByte() || b[2] != 6.toByte() || i32(b, 4) != xid ||
            !mac.contentEquals(b.copyOfRange(28, 34)) || i32(b, 236) != 0x63825363) return null
        val options = mutableMapOf<Int, ByteArray>()
        fun parse(start: Int, end: Int): Boolean {
            var p = start
            while (p < end) {
                val id = b[p++].toInt() and 255
                if (id == 255) return true
                if (id == 0) continue
                if (p >= end) return false
                val size = b[p++].toInt() and 255
                if (size > end - p) return false
                options[id] = (options[id] ?: byteArrayOf()) + b.copyOfRange(p, p + size)
                p += size
            }
            return false
        }
        if (!parse(240, b.size)) return null
        val overload = options[52]
        if (overload != null) {
            if (overload.size != 1 || overload[0].toInt() !in 1..3) return null
            if (overload[0].toInt() and 1 != 0 && !parse(108, 236)) return null
            if (overload[0].toInt() and 2 != 0 && !parse(44, 108)) return null
        }
        val type = options[53]?.takeIf { it.size == 1 }?.get(0)?.toInt() ?: return null
        val server = options[54]?.takeIf { it.size == 4 }?.let { i32(it, 0) } ?: return null
        if (type !in listOf(2, 5, 6) || !unicast(server)) return null
        return Reply(type, i32(b, 16), server, options)
    }

    fun lease(reply: Reply): Lease? {
        if (reply.type !in listOf(2, 5) || !unicast(reply.address)) return null
        fun ip(id: Int) = reply.options[id]?.takeIf { it.size == 4 }?.let { i32(it, 0) }
        fun seconds(id: Int) = ip(id)?.toLong()?.and(0xffffffffL)
        val mask = ip(1) ?: return null
        val prefix = Integer.bitCount(mask)
        if (prefix !in 1..30 || mask != (-1 shl (32 - prefix))) return null
        fun host(n: Int) = unicast(n) && (n and mask) == (reply.address and mask) &&
            (n and mask.inv()) != 0 && (n or mask) != -1
        if (!host(reply.address) || !host(reply.server)) return null
        val routers = reply.options[3]?.takeIf { it.isNotEmpty() && it.size % 4 == 0 } ?: return null
        val router = (routers.indices step 4).map { i32(routers, it) }.firstOrNull { host(it) && it != reply.address } ?: return null
        val dnsBytes = reply.options[6]?.takeIf { it.isNotEmpty() && it.size % 4 == 0 } ?: return null
        val dns = (dnsBytes.indices step 4).map { i32(dnsBytes, it) }.filter(::unicast).distinct().take(4)
        if (dns.isEmpty()) return null
        val duration = seconds(51)?.takeIf { it >= 4 } ?: return null
        val renew = seconds(58) ?: (duration / 2)
        val rebind = seconds(59) ?: (duration * 7 / 8)
        if (renew !in 1 until rebind || rebind >= duration) return null
        return Lease(reply.address, prefix, router, dns, reply.server, duration, renew, rebind)
    }
}
