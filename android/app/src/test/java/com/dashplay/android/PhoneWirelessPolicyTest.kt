package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class PhoneWirelessPolicyTest {
    @Test fun oldPhoneWiredAndAutomaticSettingsCannotEnableCable() {
        for (stored in IphoneLink.values()) {
            assertEquals(IphoneLink.WIRELESS, ConnectionSupport.effectiveLink(DeviceKind.PHONE, stored))
            assertEquals(stored, ConnectionSupport.effectiveLink(DeviceKind.BOX, stored))
        }
    }
    @Test fun phoneRejectsCableAndAutomaticEvenWithBothRadios() {
        for (network in Network.values()) for (link in listOf(IphoneLink.WIRED, IphoneLink.ANY)) {
            assertEquals(ConnectionSupport.PHONE_WIRELESS_ONLY,
                ConnectionSupport.problem(network, link, true, true, DeviceKind.PHONE))
        }
    }
    @Test fun phoneNeedsBluetoothAndWirelessTopologyNotUsbHost() {
        for (usb in listOf(false, true)) for (network in listOf(Network.MIFI, Network.THIS_HOTSPOT)) {
            assertNull(ConnectionSupport.problem(network, IphoneLink.WIRELESS, usb, true, DeviceKind.PHONE))
            assertEquals(ConnectionSupport.PHONE_NO_BLUETOOTH,
                ConnectionSupport.problem(network, IphoneLink.WIRELESS, usb, false, DeviceKind.PHONE))
        }
        assertEquals(ConnectionSupport.PHONE_NETWORK,
            ConnectionSupport.problem(Network.CAR_HOTSPOT, IphoneLink.WIRELESS, true, true, DeviceKind.PHONE))
    }
    @Test fun boxWithoutBluetoothStillSupportsWiredCarplay() {
        for (network in Network.values()) for (link in listOf(IphoneLink.WIRED, IphoneLink.ANY)) {
            assertNull(ConnectionSupport.problem(network, link, true, false, DeviceKind.BOX))
        }
    }
    @Test fun phoneCannotCompleteOldSameIphoneHotspotDraft() {
        val answers = mapOf(SetupGuide.SOURCE to "same_iphone")
        for (link in IphoneLink.values()) {
            assertNotNull(SetupGuide.connectionProblem(Network.MIFI, link, true, true, answers, DeviceKind.PHONE))
            assertNotNull(SetupGuide.problem(SetupGuide.SOURCE, Car.OTHER, Network.MIFI, link,
                true, true, false, answers, DeviceKind.PHONE))
        }
        assertNull(SetupGuide.connectionProblem(Network.MIFI, IphoneLink.WIRED, true, false, answers, DeviceKind.BOX))
        assertNull(SetupGuide.connectionProblem(Network.MIFI, IphoneLink.WIRELESS, false, true,
            mapOf(SetupGuide.SOURCE to "other"), DeviceKind.PHONE))
    }
    @Test fun phoneGuideDoesNotRequireUsbWhenInternetIsNotBorrowed() {
        val answers = mapOf(SetupGuide.INTERNET to "upstream")
        assertFalse(SetupGuide.included("usb", Car.OTHER, Network.MIFI, IphoneLink.WIRELESS, true, true, answers))
        assertTrue(SetupGuide.included("wifi", Car.OTHER, Network.MIFI, IphoneLink.WIRELESS, true, true, answers))
    }
}
