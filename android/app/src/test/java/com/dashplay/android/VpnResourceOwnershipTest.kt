package com.dashplay.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** One VPN service, two resources: closing either must never close the other. */
class VpnResourceOwnershipTest {
    @Test
    fun `the same kind cannot be taken twice`() {
        val own = VpnResourceOwnership()
        assertTrue(own.acquire(VpnResourceOwnership.Kind.CABLE, "lease-1"))
        assertFalse("a second cable lease must wait, not take the first one's place", own.acquire(VpnResourceOwnership.Kind.CABLE, "lease-2"))
        assertTrue(own.owns(VpnResourceOwnership.Kind.CABLE, "lease-1"))
    }

    @Test
    fun `a stale release never closes the successor`() {
        val own = VpnResourceOwnership()
        own.acquire(VpnResourceOwnership.Kind.CABLE, "lease-1")
        own.release(VpnResourceOwnership.Kind.CABLE, "lease-1")
        own.acquire(VpnResourceOwnership.Kind.CABLE, "lease-2")
        val left = own.release(VpnResourceOwnership.Kind.CABLE, "lease-1")
        assertTrue("lease-2 still owns the cable", own.owns(VpnResourceOwnership.Kind.CABLE, "lease-2"))
        assertEquals(setOf(VpnResourceOwnership.Kind.CABLE), left)
    }

    @Test
    fun `dropping the cable keeps the address and the other way round`() {
        val own = VpnResourceOwnership()
        own.acquire(VpnResourceOwnership.Kind.CABLE, "cable")
        own.acquire(VpnResourceOwnership.Kind.ADDRESS, "address")
        assertEquals(setOf(VpnResourceOwnership.Kind.ADDRESS), own.release(VpnResourceOwnership.Kind.CABLE, "cable"))
        assertTrue(own.holds(VpnResourceOwnership.Kind.ADDRESS))
        assertEquals(setOf(VpnResourceOwnership.Kind.CABLE), VpnResourceOwnership().apply {
            acquire(VpnResourceOwnership.Kind.CABLE, "cable")
            acquire(VpnResourceOwnership.Kind.ADDRESS, "address")
        }.release(VpnResourceOwnership.Kind.ADDRESS, "address"))
    }

    @Test
    fun `the service may stop only when nothing is left`() {
        val own = VpnResourceOwnership()
        own.acquire(VpnResourceOwnership.Kind.ADDRESS, "address")
        assertFalse(own.isEmpty())
        own.release(VpnResourceOwnership.Kind.ADDRESS, "address")
        assertTrue(own.isEmpty())
    }

    @Test
    fun `an address revoked while the cable is live leaves the cable exactly as it was`() {
        val own = VpnResourceOwnership()
        own.acquire(VpnResourceOwnership.Kind.CABLE, "lease-1")
        own.acquire(VpnResourceOwnership.Kind.ADDRESS, "address-1")
        // The compatibility entry is turned off mid-session: only its own resource goes.
        val left = own.release(VpnResourceOwnership.Kind.ADDRESS, "address-1")
        assertEquals(setOf(VpnResourceOwnership.Kind.CABLE), left)
        assertTrue(own.owns(VpnResourceOwnership.Kind.CABLE, "lease-1"))
        assertFalse(own.isEmpty())
    }

    @Test
    fun `a second address attempt while one is held is refused`() {
        val own = VpnResourceOwnership()
        assertTrue(own.acquire(VpnResourceOwnership.Kind.ADDRESS, "address-1"))
        assertFalse("one address tunnel at a time", own.acquire(VpnResourceOwnership.Kind.ADDRESS, "address-2"))
        assertTrue(own.owns(VpnResourceOwnership.Kind.ADDRESS, "address-1"))
    }

    @Test
    fun `shutdown closes the cable first`() {
        val own = VpnResourceOwnership()
        own.acquire(VpnResourceOwnership.Kind.ADDRESS, "address")
        own.acquire(VpnResourceOwnership.Kind.CABLE, "cable")
        assertEquals(listOf(VpnResourceOwnership.Kind.CABLE, VpnResourceOwnership.Kind.ADDRESS), own.releaseAll())
        assertTrue(own.isEmpty())
    }
}
