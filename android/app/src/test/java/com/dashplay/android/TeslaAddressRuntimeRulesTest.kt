package com.dashplay.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The address runtime's evidence rules, without a device: what may be claimed, when old evidence expires, and which
 * state transitions are legal. `TeslaAddressRuntime` itself binds to the VPN service, so this exercises the same
 * state machine and challenge rules it delegates to.
 */
class TeslaAddressRuntimeRulesTest {
    private val plan = TeslaVpnAddress()

    @Test
    fun `starting moves through creating to an established address`() {
        assertTrue(plan.mayMove(TeslaVpnAddress.State.OFF, TeslaVpnAddress.State.CREATING))
        assertTrue(plan.mayMove(TeslaVpnAddress.State.CREATING, TeslaVpnAddress.State.ADDRESS_PRESENT))
    }

    @Test
    fun `an established address is described as unverified until a client answers`() {
        assertTrue(plan.describe(TeslaVpnAddress.State.ADDRESS_PRESENT).contains("尚未"))
        assertFalse(plan.describe(TeslaVpnAddress.State.ADDRESS_PRESENT).contains("已由客户端验证"))
        assertTrue(plan.describe(TeslaVpnAddress.State.PEER_VERIFIED).contains("客户端验证"))
    }

    @Test
    fun `a failure may be retried but never jumps straight to verified`() {
        assertTrue(plan.mayMove(TeslaVpnAddress.State.FAILED, TeslaVpnAddress.State.CREATING))
        assertFalse(plan.mayMove(TeslaVpnAddress.State.FAILED, TeslaVpnAddress.State.PEER_VERIFIED))
        assertFalse(plan.mayMove(TeslaVpnAddress.State.REVOKED, TeslaVpnAddress.State.ADDRESS_PRESENT))
    }

    @Test
    fun `a revocation closes the descriptor before any retry`() {
        assertTrue(plan.mayMove(TeslaVpnAddress.State.ADDRESS_PRESENT, TeslaVpnAddress.State.REVOKED))
        assertTrue(plan.mayMove(TeslaVpnAddress.State.PEER_VERIFIED, TeslaVpnAddress.State.REVOKED))
        assertFalse(plan.holdsDescriptor(TeslaVpnAddress.State.REVOKED))
        assertFalse(plan.holdsDescriptor(TeslaVpnAddress.State.FAILED))
    }

    @Test
    fun `evidence expires when the network generation changes or the marker is not the one asked`() {
        val challenge = AddressChallenge("0011223344556677", System.currentTimeMillis())
        assertTrue(challenge.verified("0011223344556677", 4, 4, 10))
        assertFalse("new generation invalidates old evidence", challenge.verified("0011223344556677", 5, 4, 10))
        assertFalse("a wrong marker is not evidence", challenge.verified("ffffffffffffffff", 4, 4, 10))
        assertFalse("stale evidence", challenge.verified("0011223344556677", 4, 4, AddressChallenge.VERIFY_TTL_MS + 1))
    }

    @Test
    fun `an address never doubles as the Root redirect`() {
        assertTrue(TeslaVpnAddress.conflictsWithRoot(rootEnabled = true, car = Car.TESLA))
        assertFalse(TeslaVpnAddress.conflictsWithRoot(rootEnabled = true, car = Car.OTHER))
    }

    @Test
    fun `only an established address holds a descriptor`() {
        assertTrue(plan.holdsDescriptor(TeslaVpnAddress.State.CREATING))
        assertTrue(plan.holdsDescriptor(TeslaVpnAddress.State.ADDRESS_PRESENT))
        assertTrue(plan.holdsDescriptor(TeslaVpnAddress.State.PEER_VERIFIED))
        assertFalse(plan.holdsDescriptor(TeslaVpnAddress.State.OFF))
        assertFalse(plan.holdsDescriptor(TeslaVpnAddress.State.WAITING_LAN))
    }
}
