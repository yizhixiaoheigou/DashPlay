package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class ExternalWifiPolicyTest {
    @Test fun oldNetworkCannotBeInvitedOnCurrentWifi() {
        assertNotNull(ExternalWifiPolicy.problem(ExternalWifiChoice.OTHER, "old", "current", true))
        assertNull(ExternalWifiPolicy.problem(ExternalWifiChoice.CURRENT, "current", "current", true))
    }
    @Test fun legacyConfigurationNeedsExplicitConfirmation() {
        assertNotNull(ExternalWifiPolicy.problem(null, "old", null, true))
        assertNull(ExternalWifiPolicy.problem(ExternalWifiChoice.OTHER, "manual", null, true))
    }
    @Test fun differentNetworkNeverInheritsSavedPassword() {
        assertEquals("", ExternalWifiPolicy.password("old", "saved-secret", "new", null))
        assertEquals("saved-secret", ExternalWifiPolicy.password("same", "saved-secret", "same", null))
        assertEquals("new-secret", ExternalWifiPolicy.password("old", "saved-secret", "new", "new-secret"))
        assertEquals("", ExternalWifiPolicy.password("same", "saved-secret", "same", ""))
    }
    @Test fun ssidsAreCaseSensitiveAndEthernetDoesNotImplyWifiMismatch() {
        assertNotNull(ExternalWifiPolicy.problem(ExternalWifiChoice.OTHER, "Network", "network", true))
        assertEquals("", ExternalWifiPolicy.password("Network", "saved-secret", "network", null))
        assertNull(ExternalWifiPolicy.problem(ExternalWifiChoice.OTHER, "downstream", "upstream", false))
    }
}
