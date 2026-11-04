package com.dashplay.android

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import java.net.Inet4Address
import java.net.InetAddress
import java.net.NetworkInterface

/**
 * The address the car and the iPhone reach this device on. A phone has several: cellular, its Wi-Fi client and its
 * hotspot. The selected topology decides whether a local hotspot or an external client network is eligible.
 */
object LocalNetwork {
    private val HOTSPOT = Regex("^(ap\\d*|swlan\\d*|softap\\d*|wlan1|wigig\\d*)$")
    private val WIFI = Regex("^wlan\\d*$")
    private val ETHERNET = Regex("^eth\\d*$")

    enum class Kind { HOTSPOT, WIFI, ETHERNET }

    data class Choice(val name: String, val address: InetAddress, val kind: Kind)

    fun choose(context: Context? = null): Choice? {
        val all = runCatching { NetworkInterface.getNetworkInterfaces().toList() }.getOrDefault(emptyList())
            .filter { runCatching { it.isUp && !it.isLoopback }.getOrDefault(false) }
            .mapNotNull { iface ->
                iface.inetAddresses.toList()
                    .firstOrNull { it is Inet4Address && !it.isLoopbackAddress && !it.isLinkLocalAddress }
                    ?.let { iface.name to it }
            }
        // Tethering interfaces aren't app-facing upstream Networks. Modern Android may put an AP on
        // wlan2 with a randomized host address, while wlan0 remains connected to another Wi-Fi.
        val observation = context?.let {
            runCatching {
                val manager = it.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
                val names = mutableSetOf<String>()
                val kinds = mutableMapOf<String, Kind>()
                for (net in manager.allNetworks) {
                    val name = manager.getLinkProperties(net)?.interfaceName ?: continue
                    names += name
                    val caps = manager.getNetworkCapabilities(net) ?: continue
                    // A LAN does not need Internet/VALIDATED capability. VPN and cellular are not receiver LANs.
                    if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) continue
                    when {
                        caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> kinds[name] = Kind.WIFI
                        caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> kinds[name] = Kind.ETHERNET
                    }
                }
                names to kinds
            }.getOrNull()
        }
        val requireHotspot = context != null && Settings(context).network == Network.THIS_HOTSPOT
        return choose(all, observation?.first, context == null || requireHotspot, observation?.second,
            requireHotspot = requireHotspot)
    }

    internal fun choose(all: List<Pair<String, InetAddress>>, upstream: Set<String>?, preferHotspot: Boolean = true,
        clientKinds: Map<String, Kind>? = null, requireHotspot: Boolean = false): Choice? {
        // An explicitly selected system hotspot must not fall back to the uplink. If Android's
        // network observation failed, interface names and a .1 address alone cannot establish its role.
        if (requireHotspot && upstream == null) return null
        val hotspots = all.filter { (name, address) ->
            name !in upstream.orEmpty() && (HOTSPOT.matches(name) || upstream != null && WIFI.matches(name)) ||
                upstream == null && WIFI.matches(name) && address.hostAddress.orEmpty().endsWith(".1")
        }
        if (preferHotspot || requireHotspot) hotspots.firstOrNull()?.let { return Choice(it.first, it.second, Kind.HOTSPOT) }
        if (requireHotspot) return null
        val clients = all.filterNot { it in hotspots }.mapNotNull { (name, address) ->
            val kind = clientKinds?.get(name) ?: when {
                ETHERNET.matches(name) && (clientKinds == null || name !in upstream.orEmpty()) -> Kind.ETHERNET
                clientKinds == null && upstream != null && name in upstream && WIFI.matches(name) -> Kind.WIFI
                upstream == null && preferHotspot && WIFI.matches(name) -> Kind.WIFI
                else -> null
            }
            kind?.let { Choice(name, address, it) }
        }
        return clients.firstOrNull { it.kind == Kind.WIFI } ?: clients.firstOrNull { it.kind == Kind.ETHERNET }
    }
}
