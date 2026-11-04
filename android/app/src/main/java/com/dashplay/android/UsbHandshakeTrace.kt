package com.dashplay.android

import java.net.InetAddress

/** Bounded handshake metadata only. Never records TCP payloads or application credentials. */
internal object UsbHandshakeTrace {
    fun describe(frame: ByteArray): String? {
        if (frame.size < 54 || frame[12] != 0x86.toByte() || frame[13] != 0xdd.toByte() ||
            frame[14].toInt() and 0xf0 != 0x60) return null
        val length = ((frame[18].toInt() and 255) shl 8) or (frame[19].toInt() and 255)
        if (length > frame.size - 54) return null
        val protocol = frame[20].toInt() and 255
        val kind = when {
            protocol == 58 && length >= 4 -> {
                val type = frame[54].toInt() and 255
                val target = if (type == 135 || type == 136) {
                    if (length < 24) return null
                    " target=${address(frame, 62)}"
                } else ""
                "icmp6 type=$type$target"
            }
            protocol == 6 && length >= 20 && frame[67].toInt() and 0x07 != 0 ->
                "tcp flags=${frame[67].toInt() and 255}"
            else -> return null
        }
        return "$kind src=${address(frame, 22)} dst=${address(frame, 38)} hop=${frame[21].toInt() and 255} bytes=$length"
    }

    private fun address(frame: ByteArray, offset: Int): String =
        InetAddress.getByAddress(frame.copyOfRange(offset, offset + 16)).hostAddress.orEmpty()
}
