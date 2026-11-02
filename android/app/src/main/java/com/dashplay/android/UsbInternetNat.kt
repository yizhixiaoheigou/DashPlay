package com.dashplay.android

/** One-to-one IPv4 mapping keeps the VPN address stable across DHCP changes, preserving CarPlay's TUN. */
internal class UsbInternetNat(private val local: Int, private val dns: Int) {
    private data class DnsKey(val protocol: Int, val port: Int)
    private data class DnsValue(val server: Int, val at: Long)
    private data class FragmentKey(val incoming: Boolean, val source: Int, val target: Int, val protocol: Int, val id: Int)
    private data class FragmentValue(val source: Int, val target: Int, val at: Long)
    private val queries = linkedMapOf<DnsKey, DnsValue>()
    private val fragments = linkedMapOf<FragmentKey, FragmentValue>()
    private var lease: UsbTetherProtocol.Lease? = null

    @Synchronized fun configure(value: UsbTetherProtocol.Lease?) {
        if (lease?.address != value?.address || lease?.dns != value?.dns) { queries.clear(); fragments.clear() }
        lease = value
    }
    @Synchronized fun outgoing(packet: ByteArray, now: Long): ByteArray? = translate(packet, false, now)
    @Synchronized fun incoming(packet: ByteArray, now: Long): ByteArray? = translate(packet, true, now)

    private fun translate(packet: ByteArray, incoming: Boolean, now: Long): ByteArray? {
        val active = lease ?: return null
        val b = valid(packet) ?: return null
        queries.entries.removeAll { now - it.value.at >= 300_000 }
        fragments.entries.removeAll { now - it.value.at >= 30_000 }
        val source = word32(b, 12); val target = word32(b, 16)
        if (if (incoming) target != active.address else source != local) return null
        var newSource = if (incoming) source else active.address
        var newTarget = if (incoming) local else target
        val ihl = (b[0].toInt() and 15) * 4
        val proto = b[9].toInt() and 255
        val offset = word(b, 6) and 8191
        val more = word(b, 6) and 0x2000 != 0
        val key = FragmentKey(incoming, source, target, proto, word(b, 4))
        if (offset == 0 && proto in listOf(6, 17)) {
            if (b.size < ihl + if (proto == 6) 20 else 8) return null
            val localPort = word(b, ihl + if (incoming) 2 else 0)
            val remotePort = word(b, ihl + if (incoming) 0 else 2)
            val dnsKey = DnsKey(proto, localPort)
            if (!incoming && target == dns) {
                if (remotePort != 53) return null
                newTarget = active.dns.firstOrNull() ?: return null
                if (queries.size >= 256 && dnsKey !in queries) queries.remove(queries.keys.first())
                queries[dnsKey] = DnsValue(newTarget, now)
            } else if (incoming && remotePort == 53 && queries[dnsKey]?.server == source) newSource = dns
        } else if (!incoming && target == dns && offset == 0) return null
        if (offset != 0) {
            fragments[key]?.let { newSource = it.source; newTarget = it.target }
            if (!incoming && target == dns && newTarget == dns) return null
        }
        if (more && offset == 0) {
            if (fragments.size >= 256 && key !in fragments) fragments.remove(fragments.keys.first())
            fragments[key] = FragmentValue(newSource, newTarget, now)
        }
        if (offset == 0 && proto == 1 && b.size >= ihl + 8 && b[ihl].toInt() and 255 in listOf(3, 11, 12)) {
            val original = b.copyOfRange(ihl, b.size)
            val quoteStart = ihl + 8
            if (b.size >= quoteStart + 20) {
                val quote = b.copyOfRange(quoteStart, b.size)
                val header = (quote[0].toInt() and 15) * 4
                if (quote[0].toInt() ushr 4 == 4 && header in 20..quote.size && checksum(quote, 0, header) == 0) {
                    var qs = word32(quote, 12); var qt = word32(quote, 16)
                    val qp = quote[9].toInt() and 255
                    if (incoming && qs == active.address) {
                        qs = local
                        if (qp in listOf(6, 17) && quote.size >= header + 4 && word(quote, header + 2) == 53 &&
                            queries[DnsKey(qp, word(quote, header))]?.server == qt) qt = dns
                    } else if (!incoming && qt == local) {
                        qt = active.address
                        if (qs == dns && qp in listOf(6, 17) && quote.size >= header + 4 && word(quote, header) == 53)
                            qs = queries[DnsKey(qp, word(quote, header + 2))]?.server ?: return null
                    }
                    replaceAddresses(quote, qs, qt)
                    quote.copyInto(b, quoteStart)
                    // Incremental update works even when an ICMP error is itself fragmented.
                    put16(b, ihl + 2, adjusted(word(original, 2), original, b.copyOfRange(ihl, b.size), 2))
                }
            }
        }
        replaceAddresses(b, newSource, newTarget)
        return b
    }

    private fun replaceAddresses(b: ByteArray, source: Int, target: Int) {
        val old = b.copyOfRange(12, 20)
        put32(b, 12, source); put32(b, 16, target)
        val new = b.copyOfRange(12, 20)
        val ihl = (b[0].toInt() and 15) * 4
        val proto = b[9].toInt() and 255
        if (word(b, 6) and 8191 == 0 && proto in listOf(6, 17)) {
            val at = ihl + if (proto == 6) 16 else 6
            if (at + 2 <= b.size && (proto != 17 || word(b, at) != 0)) {
                val changed = adjusted(word(b, at), old, new)
                put16(b, at, if (proto == 17 && changed == 0) 65535 else changed)
            }
        }
        put16(b, 10, 0); put16(b, 10, checksum(b, 0, ihl))
    }
    private fun valid(bytes: ByteArray): ByteArray? {
        if (bytes.size !in 20..1500 || bytes[0].toInt() ushr 4 != 4) return null
        val ihl = (bytes[0].toInt() and 15) * 4
        if (ihl !in 20..bytes.size || checksum(bytes, 0, ihl) != 0 || word(bytes, 6) and 0x8000 != 0) return null
        val size = word(bytes, 2)
        if (size !in ihl..bytes.size) return null
        return bytes.copyOf(size)
    }
    private fun adjusted(check: Int, old: ByteArray, new: ByteArray, skip: Int = -1): Int {
        var sum = (check.inv() and 65535).toLong()
        for (p in old.indices step 2) {
            if (p == skip) continue
            fun value(b: ByteArray) = (b[p].toInt() and 255 shl 8) or if (p + 1 < b.size) b[p + 1].toInt() and 255 else 0
            sum += value(old).inv() and 65535; sum += value(new)
        }
        while (sum ushr 16 != 0L) sum = (sum and 65535) + (sum ushr 16)
        return sum.toInt().inv() and 65535
    }
    private fun word(b: ByteArray, p: Int) = UsbTetherProtocol.u16(b, p)
    private fun word32(b: ByteArray, p: Int) = UsbTetherProtocol.i32(b, p)
    private fun put16(b: ByteArray, p: Int, n: Int) = UsbTetherProtocol.put16(b, p, n)
    private fun put32(b: ByteArray, p: Int, n: Int) = UsbTetherProtocol.put32(b, p, n)
    private fun checksum(b: ByteArray, p: Int, n: Int) = UsbTetherProtocol.checksum(b, p, n)
}
