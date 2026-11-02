package com.dashplay.android

import java.io.DataInputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket

/** Use only the VPN's virtual DNS address; no fallback to an unrelated physical uplink. */
internal object UsbInternetDns {
    fun exchange(query: ByteArray, server: InetAddress): ByteArray? {
        TeslaCheckProtocol.question(query) ?: return null
        return try {
            Socket().use { socket ->
                val expiry = TeslaCheckIo.deadline(socket, 5000)
                try {
                    socket.soTimeout = 2000
                    socket.connect(InetSocketAddress(server, 53), 2000)
                    socket.getOutputStream().write(byteArrayOf((query.size ushr 8).toByte(), query.size.toByte()) + query)
                    val input = DataInputStream(socket.getInputStream())
                    val size = input.readUnsignedShort()
                    if (size !in 12..65535) return null
                    val response = ByteArray(size).also { input.readFully(it) }
                    response.takeIf { TeslaCheckProtocol.validResponse(query, it) }
                } finally { expiry.cancel(false) }
            }
        } catch (_: Exception) { null }
    }
}
