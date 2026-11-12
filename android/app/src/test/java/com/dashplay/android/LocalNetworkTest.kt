package com.dashplay.android

import java.net.InetAddress
import org.junit.Assert.*
import org.junit.Test

class LocalNetworkTest {
    private fun address(name: String, ip: String) = name to InetAddress.getByName(ip)

    @Test fun xiaomiConcurrentHotspotHasRandomHostAndWlan2() {
        val choice = LocalNetwork.choose(listOf(address("wlan0", "192.168.20.45"), address("wlan2", "10.123.45.162")), setOf("wlan0"))!!
        assertEquals("wlan2", choice.name)
        assertEquals(LocalNetwork.Kind.HOTSPOT, choice.kind)
    }

    @Test fun secondClientInterfaceIsNotAHotspot() {
        val choice = LocalNetwork.choose(listOf(address("wlan1", "192.168.20.1")), setOf("wlan1"))!!
        assertEquals(LocalNetwork.Kind.WIFI, choice.kind)
    }

    @Test fun standaloneHotspotDoesNotNeedDotOne() {
        val choice = LocalNetwork.choose(listOf(address("wlan0", "10.123.45.162")), emptySet())!!
        assertEquals(LocalNetwork.Kind.HOTSPOT, choice.kind)
    }

    @Test fun ethernetAndCellularRemainDistinct() {
        val choice = LocalNetwork.choose(listOf(address("rmnet_data0", "10.2.3.4"), address("eth0", "192.168.20.4")), setOf("rmnet_data0", "eth0"))!!
        assertEquals(LocalNetwork.Kind.ETHERNET, choice.kind)
        assertNull(LocalNetwork.choose(listOf(address("rmnet_data0", "10.2.3.4")), setOf("rmnet_data0")))
    }

    @Test fun unavailableNetworkMetadataKeepsLegacyFallback() {
        assertEquals(LocalNetwork.Kind.WIFI, LocalNetwork.choose(listOf(address("wlan0", "192.168.20.45")), null)!!.kind)
        assertEquals(LocalNetwork.Kind.HOTSPOT, LocalNetwork.choose(listOf(address("ap0", "10.123.45.162")), null)!!.kind)
    }

    @Test fun externalWifiSelectionIgnoresAnActiveConcurrentHotspot() {
        val all = listOf(address("wlan2", "10.123.45.162"), address("wlan0", "192.168.20.45"))
        assertEquals("wlan0", LocalNetwork.choose(all, setOf("wlan0"), preferHotspot = false)!!.name)
        assertEquals("wlan2", LocalNetwork.choose(all, setOf("wlan0"), preferHotspot = true)!!.name)
    }
    @Test fun externalModeWaitsWhenOnlyTheLocalHotspotRemains() {
        assertNull(LocalNetwork.choose(listOf(address("wlan2", "10.123.45.162")), emptySet(), preferHotspot = false))
    }
    @Test fun externalEthernetWinsOverAnUnselectedHotspotWithoutCellular() {
        val all = listOf(address("ap0", "192.168.43.1"), address("eth0", "192.168.20.4"))
        val selected = LocalNetwork.choose(all, setOf("eth0"), preferHotspot = false)!!
        assertEquals("eth0", selected.name); assertEquals(LocalNetwork.Kind.ETHERNET, selected.kind)
    }
    @Test fun platformTransportRecognizesUsbEthernetWithoutAssumingItsInterfaceName() {
        val all = listOf(address("rndis7", "192.168.20.4"), address("tun0", "100.64.0.2"))
        val selected = LocalNetwork.choose(all, setOf("rndis7", "tun0"), preferHotspot = false,
            clientKinds = mapOf("rndis7" to LocalNetwork.Kind.ETHERNET))!!
        assertEquals("rndis7", selected.name); assertEquals(LocalNetwork.Kind.ETHERNET, selected.kind)
    }
    @Test fun externalModeDoesNotGuessAWifiRoleWhenNetworkMetadataIsUnavailable() {
        assertNull(LocalNetwork.choose(listOf(address("wlan0", "10.123.45.162")), null, preferHotspot = false))
        assertEquals("eth0", LocalNetwork.choose(listOf(address("wlan0", "10.123.45.162"),
            address("eth0", "192.168.20.4")), null, preferHotspot = false)!!.name)
    }
    @Test fun interfaceNameCannotOverrideAnExcludedPlatformTransport() {
        assertNull(LocalNetwork.choose(listOf(address("eth0", "100.64.0.2")), setOf("eth0"),
            preferHotspot = false, clientKinds = emptyMap()))
    }
}
