package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class PhoneConnectionEventTest {
    @Test fun retainsConnectionStateAndErrorCode() {
        val event = PhoneConnectionEvent.summarize("carkitd: CarPlay wired connection failed error=-1009")!!
        assertTrue(event.contains("wired connection failed")); assertTrue(event.contains("codes=-1009"))
    }
    @Test fun arbitraryValuesAreNeverCopied() {
        val event = PhoneConnectionEvent.summarize("accessoryd: CarPlay session rejected token=abcd1234secret contact=Alice ssid=HomeWifi device=112233445566")!!
        assertEquals("accessoryd words=carplay session rejected", event)
    }
    @Test fun recognizesErrorFormatsWithoutEqualsAndInterfaceNames() {
        val event = PhoneConnectionEvent.summarize("airplayd: CarPlay connection failed (-6722), status 60, interface en2 ENETUNREACH")!!
        assertTrue(event.contains("codes=60,-6722"))
        assertTrue(event.contains("interfaces=en2")); assertTrue(event.contains("errors=ENETUNREACH"))
    }
    @Test fun unrelatedProcessesAndMessagesAreIgnored() {
        assertNull(PhoneConnectionEvent.summarize("Music: CarPlay song started"))
        assertNull(PhoneConnectionEvent.summarize("accessoryd: unrelated service available"))
    }
}
