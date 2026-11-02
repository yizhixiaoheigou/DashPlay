package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class HotspotPolicyTest {
    @Test fun actualFrequencyProducesInvitationChannel() {
        assertEquals(1, HotspotPolicy.channel(2412)); assertEquals(14, HotspotPolicy.channel(2484))
        assertEquals(48, HotspotPolicy.channel(5240)); assertEquals(149, HotspotPolicy.channel(5745))
    }
    @Test fun unknownAndUnsupportedBandsAreNotAdvertisedAsAChannel() {
        for (value in listOf(0, -1, 2413, 5000, 5955, 6000)) assertNull(HotspotPolicy.channel(value))
    }
    @Test fun anotherAppsGroupMustNeverBeRemoved() {
        assertFalse(HotspotPolicy.owns(true, "DIRECT-ab-Other", "password1", "DIRECT-ab-DashPlay", "password1"))
        assertFalse(HotspotPolicy.owns(true, "DIRECT-ab-DashPlay", "password2", "DIRECT-ab-DashPlay", "password1"))
        assertFalse(HotspotPolicy.owns(false, "DIRECT-ab-DashPlay", "password1", "DIRECT-ab-DashPlay", "password1"))
        assertFalse(HotspotPolicy.owns(true, "DIRECT-ab-DashPlay", null, "DIRECT-ab-DashPlay", "password1"))
        assertTrue(HotspotPolicy.owns(true, "DIRECT-ab-DashPlay", "password1", "DIRECT-ab-DashPlay", "password1"))
    }
}
