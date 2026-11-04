package com.shilapi.xcertplay.transport

/** CDC NCM capability lookup, scoped to the selected configuration and control alternate. */
internal object NcmPacketFilter {
    // Directed + broadcast + all multicast (IPv6 neighbour discovery needs multicast).
    // This is not promiscuous mode and does not modify Android's IP/firewall configuration.
    const val RECEIVE_MASK = 0x000e

    fun receiveMask(raw: ByteArray, configurationId: Int, controlId: Int): Int? {
        var offset = 0
        var configuration = -1
        var configurationEnd = -1
        var control = false
        while (offset + 2 <= raw.size) {
            val length = raw[offset].toInt() and 0xff
            val type = raw[offset + 1].toInt() and 0xff
            if (length < 2 || offset + length > raw.size) return null
            if (type == 2) {
                if (length < 9) return null
                val total = (raw[offset + 2].toInt() and 0xff) or
                    ((raw[offset + 3].toInt() and 0xff) shl 8)
                if (total < length || offset + total > raw.size) return null
                configuration = raw[offset + 5].toInt() and 0xff
                configurationEnd = offset + total
                control = false
            } else if (configuration == configurationId && offset < configurationEnd) {
                if (offset + length > configurationEnd) return null
                if (type == 4) {
                    if (length < 9) return null
                    control = (raw[offset + 2].toInt() and 0xff) == controlId &&
                        raw[offset + 3].toInt() == 0 &&
                        raw[offset + 5].toInt() == 2 && raw[offset + 6].toInt() == 0x0d
                } else if (control && type == 0x24 && length >= 3 && raw[offset + 2].toInt() == 0x1a) {
                    if (length < 6) return null
                    return if (raw[offset + 5].toInt() and 1 != 0) RECEIVE_MASK else null
                }
            }
            offset += length
        }
        return null
    }
}
