package com.dashplay.android

/** A second NCM peer's multicast must not replace the MAC used for an existing CarPlay peer. */
internal class Ipv6PeerTable {
    private val peers = linkedMapOf<List<Byte>, ByteArray>()
    @Synchronized fun learn(packet: ByteArray, offset: Int, length: Int, mac: ByteArray) {
        if (length < 40 || offset < 0 || offset + length > packet.size || mac.size != 6) return
        val address = packet.copyOfRange(offset + 8, offset + 24)
        if (address.all { it == 0.toByte() } || address[0] == 0xff.toByte()) return
        val key = address.toList()
        if (key !in peers && peers.size >= 128) peers.remove(peers.keys.first())
        peers[key] = mac.copyOf()
    }
    @Synchronized fun destination(packet: ByteArray): ByteArray? =
        if (packet.size < 40) null else peers[packet.copyOfRange(24, 40).toList()]?.copyOf()
}
