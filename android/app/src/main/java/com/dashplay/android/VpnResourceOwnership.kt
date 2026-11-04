package com.dashplay.android

/**
 * Ownership bookkeeping for the one VPN service that now carries two kinds of resource (2026-11-03):
 *
 *  - the cable/NCM tunnel with its Personal-Hotspot pipe ([UsbVpnService.Lease]), and
 *  - the Root-free address tunnel (one `/32` for the Tesla compatibility route, [TeslaVpnAddress]).
 *
 * The service must never let one teardown close the other: unplugging the cable must not drop the address, and
 * revoking the address must not stop CarPlay on the cable. This class holds only that rule — which resources exist,
 * which one a given close refers to, and what is left — so it can be unit-tested without a device.
 *
 * It deliberately has no Android types: the service asks it who owns what, then closes exactly those descriptors.
 */
internal class VpnResourceOwnership {
    enum class Kind { CABLE, ADDRESS }

    private val owned = LinkedHashMap<Kind, String>()

    /** Records a resource as owned by [holder] (a lease id or a generation string). Replaces only the same kind. */
    @Synchronized fun acquire(kind: Kind, holder: String): Boolean {
        if (owned.containsKey(kind)) return false
        owned[kind] = holder
        return true
    }

    /** True when this exact holder still owns [kind]: a stale release must not close a successor's descriptor. */
    @Synchronized fun owns(kind: Kind, holder: String): Boolean = owned[kind] == holder

    /**
     * Releases [kind] only when [holder] is its current owner. Returns the kinds that remain, so the caller knows
     * whether the VPN service itself can stop (no resource left) or must stay for the other one.
     */
    @Synchronized fun release(kind: Kind, holder: String): Set<Kind> {
        if (owned[kind] == holder) owned.remove(kind)
        return owned.keys.toSet()
    }

    /** Everything on shutdown, in the order the service should close them (cable first: it owns the busiest threads). */
    @Synchronized fun releaseAll(): List<Kind> = owned.keys.sortedBy { if (it == Kind.CABLE) 0 else 1 }
        .also { owned.clear() }

    @Synchronized fun holds(kind: Kind): Boolean = owned.containsKey(kind)

    @Synchronized fun isEmpty(): Boolean = owned.isEmpty()
}
