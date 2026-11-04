package com.dashplay.android

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The address plan's rules, on the JVM: the states may not outrun the evidence, and ownership is exclusive. */
class TeslaVpnAddressTest {
    private val plan = TeslaVpnAddress()

    @Test
    fun `an address must be unicast and never loopback`() {
        assertTrue(runCatching { TeslaVpnAddress("10.0.0.1") }.isSuccess)
        assertTrue(runCatching { TeslaVpnAddress("127.0.0.1") }.isFailure)
        assertTrue(runCatching { TeslaVpnAddress("0.0.0.0") }.isFailure)
        assertTrue(runCatching { TeslaVpnAddress("300.1.1.1") }.isFailure)
        assertTrue(runCatching { TeslaVpnAddress("1.2.3") }.isFailure)
    }

    @Test
    fun `address present is never claimed without the system creating the tunnel`() {
        assertTrue(plan.mayMove(TeslaVpnAddress.State.CREATING, TeslaVpnAddress.State.ADDRESS_PRESENT))
        assertFalse(plan.mayMove(TeslaVpnAddress.State.OFF, TeslaVpnAddress.State.ADDRESS_PRESENT))
        assertFalse(plan.mayMove(TeslaVpnAddress.State.WAITING_LAN, TeslaVpnAddress.State.PEER_VERIFIED))
    }

    @Test
    fun `peer verified only follows an established address`() {
        assertTrue(plan.mayMove(TeslaVpnAddress.State.ADDRESS_PRESENT, TeslaVpnAddress.State.PEER_VERIFIED))
        assertFalse(plan.mayMove(TeslaVpnAddress.State.CREATING, TeslaVpnAddress.State.PEER_VERIFIED))
    }

    @Test
    fun `revocation and failure release the descriptor and may be retried`() {
        for (state in TeslaVpnAddress.State.values()) {
            if (!plan.holdsDescriptor(state)) continue
            assertTrue("$state must allow REVOKED", plan.mayMove(state, TeslaVpnAddress.State.REVOKED))
            assertTrue("$state must allow OFF", plan.mayMove(state, TeslaVpnAddress.State.OFF))
        }
        assertTrue(plan.mayMove(TeslaVpnAddress.State.FAILED, TeslaVpnAddress.State.CREATING))
        assertFalse(plan.holdsDescriptor(TeslaVpnAddress.State.REVOKED))
    }

    @Test
    fun `root redirect and the vpn address never own the same address together`() {
        assertTrue(TeslaVpnAddress.conflictsWithRoot(rootEnabled = true, car = Car.TESLA))
        assertFalse(TeslaVpnAddress.conflictsWithRoot(rootEnabled = false, car = Car.TESLA))
        assertFalse(TeslaVpnAddress.conflictsWithRoot(rootEnabled = true, car = Car.OTHER))
    }

    @Test
    fun `states never claim success in their words`() {
        assertTrue(plan.describe(TeslaVpnAddress.State.ADDRESS_PRESENT).contains("尚未"))
        assertTrue(plan.describe(TeslaVpnAddress.State.PEER_VERIFIED).contains("客户端验证"))
    }

    @Test
    fun `a challenge is bound to its marker, network generation and age`() {
        val challenge = AddressChallenge("0123456789abcdef", 1_000)
        assertTrue(challenge.verified("0123456789abcdef", 7, 7, 1_000))
        assertFalse("another marker", challenge.verified("ffffffffffffffff", 7, 7, 1_000))
        assertFalse("another network generation", challenge.verified("0123456789abcdef", 8, 7, 1_000))
        assertFalse("expired", challenge.verified("0123456789abcdef", 7, 7, AddressChallenge.VERIFY_TTL_MS + 1))
        assertFalse("nothing answered", challenge.verified(null, 7, 7, 0))
    }
}
