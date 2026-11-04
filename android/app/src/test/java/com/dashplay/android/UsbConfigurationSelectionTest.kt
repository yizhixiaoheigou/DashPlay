package com.dashplay.android

import java.io.IOException
import org.junit.Assert.*
import org.junit.Test

class UsbConfigurationSelectionTest {
    @Test fun matchingConfigurationIsNotReset() {
        var writes = 0
        val result = UsbConfigurationSelection.ensure(6, { 6 }, { writes++; true })
        assertEquals("an already configured phone must not receive SET_CONFIGURATION", 0, writes)
        assertEquals(6, result.before)
        assertNull(result.selectionResult)
    }

    @Test fun differentConfigurationIsSelectedAndVerified() {
        var actual = 4
        val result = UsbConfigurationSelection.ensure(6, { actual }, { actual = 6; true })
        assertEquals(4, result.before); assertEquals(6, result.after); assertEquals(true, result.selectionResult)
    }

    @Test fun unavailableQueryRetainsInterfaceBasedFallback() {
        var writes = 0
        val result = UsbConfigurationSelection.ensure(6, { null }, { writes++; false })
        assertEquals(1, writes); assertNull(result.after); assertEquals(false, result.selectionResult)
    }

    @Test fun oemFalseReturnDoesNotOverrideSuccessfulReadback() {
        var actual = 0
        val result = UsbConfigurationSelection.ensure(6, { actual }, { actual = 6; false })
        assertEquals(6, result.after); assertEquals(false, result.selectionResult)
    }

    @Test(expected = IOException::class) fun knownWrongConfigurationCannotBeUsed() {
        UsbConfigurationSelection.ensure(6, { 4 }, { false })
    }
}
