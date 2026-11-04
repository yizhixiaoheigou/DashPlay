package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test
import kotlinx.serialization.encodeToString
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json

class PhonePolicyTest {
    private val a = "02:00:00:00:00:01"
    private val b = "02:00:00:00:00:02"
    private val bonded = setOf(a, b)
    private fun policy() = PhonePolicy().apply { add(a, "First"); add(b, "Second") }

    @Test fun pausedPhonesStayPausedAcrossReceiverAndProcessRestarts() {
        val first = policy()
        first.pause(b)
        val restarted = PhonePolicy(first.state)
        restarted.boot()
        assertEquals(a, restarted.select(bonded, 0))
        restarted.pause(a)
        assertNull(PhonePolicy(restarted.state).select(bonded, 100_000))
    }
    @Test fun explicitSelectionResumesOnlyThatPhone() {
        val p = policy(); p.pause(a); p.pause(b)
        assertTrue(p.choose(a, bonded))
        assertEquals(a, p.select(bonded, 0)); assertFalse(p.allowed(b, bonded))
        assertFalse(p.choose(b, setOf(a)))
    }
    @Test fun removedPhoneCannotReturnThroughAutomaticDiscoveryOrAnotherSelection() {
        val p = policy(); assertTrue(p.authorize("controller-b", b, true, bonded))
        p.pause(b, remove = true); p.discover(b, "Second renamed")
        assertNull(p.state.preferred)
        assertNull(p.state.last)
        assertFalse(p.choose(b, bonded))
        assertFalse(p.authorize("controller-b", a, true, bonded))
        assertEquals(a, p.select(bonded, 0))
        p.add(b, "Second"); assertTrue(p.authorize("controller-b", b, false, bonded))
    }
    @Test fun knownControllerCannotStealTheNewlySelectedPhonesSession() {
        val p = policy(); assertTrue(p.authorize("controller-a", a, true, bonded))
        p.choose(b, bonded)
        assertFalse(p.authorize("controller-a", b, true, bonded))
        assertFalse(p.authorize(null, b, true, bonded))
        assertFalse(p.authorize("unknown", b, false, bonded))
        assertTrue(p.authorize("controller-b", b, true, bonded))
    }
    @Test fun declinedPreferredPhoneAllowsFallbackThenRetriesAfterBackoff() {
        val p = policy(); p.decline(b, 100)
        assertEquals(a, p.select(bonded, 101))
        assertEquals(b, p.select(bonded, 60_100))
        assertNull(p.select(emptySet(), 100_000))
    }
    @Test fun serializedPauseAndRemovalSurviveReloadAndSuccessBecomesTheNextDefault() {
        val p = policy(); p.pause(b, remove = true)
        assertTrue(p.authorize("controller-a", a, true, bonded))
        p.pause(a)
        val restored = PhonePolicy(Json.decodeFromString<PhoneState>(Json.encodeToString(p.state)))
        assertNull(restored.select(bonded, 100_000))
        assertFalse(restored.choose(b, bonded))
        restored.choose(a, bonded)
        assertTrue(restored.authorize("controller-a", a, false, bonded))
        assertEquals(a, restored.state.preferred)
    }
    @Test fun failedDurableWriteDoesNotChangeThePolicy() {
        val p = PhonePolicy(policy().state) { throw IllegalStateException("storage unavailable") }
        assertThrows(IllegalStateException::class.java) { p.pause(b) }
        assertTrue(p.allowed(b, bonded)); assertNull(p.disconnect)
    }
}
