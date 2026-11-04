package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class WifiRecoveryPolicyTest {
    @Test fun temporaryFailureMustPersistBeforeARequest() {
        val p=WifiRecoveryPolicy()
        assertFalse(p.retry(0,true)); assertFalse(p.retry(29_999,true)); assertTrue(p.retry(30_000,true))
    }
    @Test fun retriesAreBoundedAndSpaced() {
        val p=WifiRecoveryPolicy();p.retry(0,true)
        assertTrue(p.retry(30_000,true));assertFalse(p.retry(89_999,true));assertTrue(p.retry(90_000,true))
        assertFalse(p.retry(209_999,true));assertTrue(p.retry(210_000,true))
        assertFalse(p.retry(999_999,true));assertEquals(3,p.attempts)
    }
    @Test fun manualDisableUnknownStateAndOtherNetworksNeverConsumeAttempts() {
        val p=WifiRecoveryPolicy();p.retry(0,true);assertFalse(p.retry(30_000,false))
        assertFalse(p.retry(99_999,false));assertEquals(0,p.attempts)
        assertFalse(p.retry(100_000,true));assertTrue(p.retry(130_000,true))
    }
    @Test fun flappingDoesNotResetTheRetryBudget() {
        val p=WifiRecoveryPolicy();p.retry(0,true);p.retry(30_000,true)
        p.connected(31_000);p.connected(32_000);p.retry(33_000,true)
        assertFalse(p.retry(63_000,true));assertEquals(1,p.attempts)
        p.connected(100_000);p.connected(160_000);assertEquals(0,p.attempts)
    }
}
