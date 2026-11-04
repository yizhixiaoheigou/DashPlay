package com.dashplay.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The address's cleanup paths (design doc section 5.2, "VPN 被撤销、另一应用取得 VPN、进程退出、接口建立失败都关闭本功能
 * 拥有的 fd/socket；保留显式关闭记忆").
 *
 * These are the rules, not the Android calls: the state machine decides what may be claimed after a teardown, and
 * the ownership table decides which resource a teardown is allowed to touch. A device test is still needed for the
 * real descriptors; what must never happen is a leaked address or a cable dropped by mistake.
 */
class TeslaAddressCleanupTest {
    private val plan = TeslaVpnAddress()

    @Test
    fun `revocation is a real state and never leaves a descriptor claimed`() {
        assertTrue(plan.mayMove(TeslaVpnAddress.State.ADDRESS_PRESENT, TeslaVpnAddress.State.REVOKED))
        assertTrue(plan.mayMove(TeslaVpnAddress.State.PEER_VERIFIED, TeslaVpnAddress.State.REVOKED))
        assertFalse("a revoked address holds nothing", plan.holdsDescriptor(TeslaVpnAddress.State.REVOKED))
        assertFalse("and never claims client evidence", plan.describe(TeslaVpnAddress.State.REVOKED).contains("客户端验证"))
    }

    @Test
    fun `after another app takes the VPN only OFF or CREATING is reachable`() {
        assertTrue(plan.mayMove(TeslaVpnAddress.State.REVOKED, TeslaVpnAddress.State.OFF))
        assertTrue(plan.mayMove(TeslaVpnAddress.State.REVOKED, TeslaVpnAddress.State.CREATING))
        assertFalse("never straight back to a claimed address", plan.mayMove(TeslaVpnAddress.State.REVOKED, TeslaVpnAddress.State.ADDRESS_PRESENT))
        assertFalse(plan.mayMove(TeslaVpnAddress.State.REVOKED, TeslaVpnAddress.State.PEER_VERIFIED))
    }

    @Test
    fun `a failed establishment releases its resource and may be retried`() {
        assertTrue(plan.mayMove(TeslaVpnAddress.State.CREATING, TeslaVpnAddress.State.FAILED))
        assertFalse(plan.holdsDescriptor(TeslaVpnAddress.State.FAILED))
        assertTrue(plan.mayMove(TeslaVpnAddress.State.FAILED, TeslaVpnAddress.State.CREATING))
    }

    @Test
    fun `tearing the address down never touches the cable lease`() {
        val ownership = VpnResourceOwnership()
        ownership.acquire(VpnResourceOwnership.Kind.CABLE, "cable-1")
        ownership.acquire(VpnResourceOwnership.Kind.ADDRESS, "address-1")
        // Every address cleanup path ends in releasing exactly its own kind.
        val left = ownership.release(VpnResourceOwnership.Kind.ADDRESS, "address-1")
        assertEquals(setOf(VpnResourceOwnership.Kind.CABLE), left)
        assertTrue(ownership.owns(VpnResourceOwnership.Kind.CABLE, "cable-1"))
    }

    @Test
    fun `a stale address release cannot close a successor's tunnel`() {
        val ownership = VpnResourceOwnership()
        ownership.acquire(VpnResourceOwnership.Kind.ADDRESS, "address-1")
        ownership.release(VpnResourceOwnership.Kind.ADDRESS, "address-1")
        ownership.acquire(VpnResourceOwnership.Kind.ADDRESS, "address-2")
        val left = ownership.release(VpnResourceOwnership.Kind.ADDRESS, "address-1")
        assertTrue("address-2 still owns the tunnel", ownership.owns(VpnResourceOwnership.Kind.ADDRESS, "address-2"))
        assertEquals(setOf(VpnResourceOwnership.Kind.ADDRESS), left)
    }

    @Test
    fun `process shutdown closes the cable before the address and leaves nothing`() {
        val ownership = VpnResourceOwnership()
        ownership.acquire(VpnResourceOwnership.Kind.ADDRESS, "address-1")
        ownership.acquire(VpnResourceOwnership.Kind.CABLE, "cable-1")
        assertEquals(
            "the busier resource goes first so its threads stop before the address disappears",
            listOf(VpnResourceOwnership.Kind.CABLE, VpnResourceOwnership.Kind.ADDRESS),
            ownership.releaseAll(),
        )
        assertTrue(ownership.isEmpty())
    }

    @Test
    fun `evidence from before a teardown never counts after it`() {
        val challenge = AddressChallenge("0011223344556677", System.currentTimeMillis())
        // Something answered, but the network generation moved on (revocation or a new address): not evidence.
        assertFalse(challenge.verified("0011223344556677", generation = 9, answeredGeneration = 8, ageMillis = 10))
    }

    @Test
    fun `an address is never accepted as its own root redirect`() {
        val address = TeslaVpnAddress()
        assertEquals(TeslaRootPlan.ADDRESS, address.address)
        assertTrue(TeslaVpnAddress.conflictsWithRoot(rootEnabled = true, car = Car.TESLA))
    }
}
