package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class ConnectionSupportTest {
    @Test fun wiredRequiresUsbButNotBluetooth() {
        for (network in Network.values()) {
            assertNull(ConnectionSupport.problem(network, IphoneLink.WIRED, true, false))
            assertNotNull(ConnectionSupport.problem(network, IphoneLink.WIRED, false, true))
        }
    }
    @Test fun wirelessRequiresBluetoothButNotUsb() {
        for (network in listOf(Network.MIFI, Network.THIS_HOTSPOT)) {
            assertNull(ConnectionSupport.problem(network, IphoneLink.WIRELESS, false, true))
            assertNotNull(ConnectionSupport.problem(network, IphoneLink.WIRELESS, true, false))
        }
    }
    @Test fun carHotspotCannotBecomeWirelessEvenWithBothRadios() {
        assertNotNull(ConnectionSupport.problem(Network.CAR_HOTSPOT, IphoneLink.WIRELESS, true, true))
    }
    @Test fun automaticCanUseEitherSupportedPath() {
        for (network in listOf(Network.MIFI, Network.THIS_HOTSPOT)) {
            assertNull(ConnectionSupport.problem(network, IphoneLink.ANY, true, false))
            assertNull(ConnectionSupport.problem(network, IphoneLink.ANY, false, true))
            assertNotNull(ConnectionSupport.problem(network, IphoneLink.ANY, false, false))
        }
    }
    @Test fun automaticOnCarHotspotStillNeedsUsb() {
        assertNotNull(ConnectionSupport.problem(Network.CAR_HOTSPOT, IphoneLink.ANY, false, true))
        assertNull(ConnectionSupport.problem(Network.CAR_HOTSPOT, IphoneLink.ANY, true, false))
    }
}
