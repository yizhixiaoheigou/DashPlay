package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class BrowserPortTest {
    @Test fun defaultViewerPortIs18743() {
        assertEquals(18743, BrowserEndpoint.DEFAULT_COMPAT_PORT)
    }
    @Test fun oldDefaultsMigrateAndCustomPortIsRetainedForBothProtocols() {
        for (old in listOf(null, 8080, 8081)) assertEquals(18743, BrowserPort.selected(null, old))
        assertEquals(19443, BrowserPort.selected(null, 19443))
        assertEquals(18743, BrowserPort.selected(null, 80))
        assertEquals(8081, BrowserPort.selected(8081, 19443))
        assertEquals(18744, BrowserPort.selected(18744, 19443))
    }
    @Test fun sharedPortAcceptsNonPrivilegedPortsOnly() {
        for (port in listOf(1024, 18743, 65535)) BrowserPort.validate(port)
        for (port in listOf(0, 80, 443, 65536)) assertThrows(IllegalArgumentException::class.java) { BrowserPort.validate(port) }
    }
}
