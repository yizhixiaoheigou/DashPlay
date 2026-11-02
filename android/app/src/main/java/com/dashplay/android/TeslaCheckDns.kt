package com.dashplay.android

import android.content.Context
import android.net.ConnectivityManager
import android.os.Build
import java.io.DataInputStream
import java.io.DataOutputStream
import java.net.InetSocketAddress

/** Forward through Android's selected default network, including its VPN; never silently choose a different uplink. */
internal class TeslaCheckDns(context: Context, private val hotspot: String) {
    private val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
    @Suppress("DEPRECATION")
    fun exchange(query: ByteArray): ByteArray? {
        // A selected Network's socketFactory can bypass an app VPN. During USB borrowing, target
        // its virtual DNS explicitly and fail closed when the lease is unavailable.
        UsbVpnService.internetDns?.let { dns ->
            return if (UsbVpnService.internetReady) UsbInternetDns.exchange(query, dns) else null
        }
        val deadline = System.nanoTime() + 5_000_000_000L
        val network = if (Build.VERSION.SDK_INT >= 23) manager.activeNetwork else {
            val active = manager.activeNetworkInfo?.takeIf { it.isConnected } ?: return null
            // API 21/22 have no activeNetwork. Require one unambiguous connected network of the active type.
            manager.allNetworks.filter { manager.getNetworkInfo(it)?.let { info -> info.isConnected && info.type == active.type } == true }.singleOrNull()
        } ?: return null
        val links = manager.getLinkProperties(network) ?: return null
        if (links.interfaceName == hotspot || links.routes.none { it.isDefaultRoute }) return null
        // TCP works through Network.socketFactory on API 21 too; remote DNS errors stay DNS errors.
        for (server in links.dnsServers.take(2)) {
            if (System.nanoTime() >= deadline) return null
            try {
                network.socketFactory.createSocket().use { socket ->
                    val expiry = TeslaCheckIo.deadline(socket, ((deadline - System.nanoTime()) / 1_000_000).coerceAtLeast(1))
                    try {
                        socket.soTimeout = 2000
                        socket.connect(InetSocketAddress(server, 53), 2000)
                        DataOutputStream(socket.getOutputStream()).apply { writeShort(query.size); write(query); flush() }
                        val input = DataInputStream(socket.getInputStream())
                        val n = input.readUnsignedShort()
                        if (n !in 12..65535) return@use
                        val response = ByteArray(n).also { input.readFully(it) }
                        if (TeslaCheckProtocol.validResponse(query, response)) return response
                    } finally { expiry.cancel(false) }
                }
            } catch (_: Exception) { /* Try another DNS address on the same selected network. */ }
        }
        return null
    }
}
