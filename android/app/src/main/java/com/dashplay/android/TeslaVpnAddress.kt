package com.dashplay.android

import kotlinx.serialization.Serializable

/**
 * The address a page is reachable at when no Root is available (docs/安卓免Root特斯拉与WebRTC接入开发方案-20261103.md
 * section 5). Deliberately a plan, not a promise: whether the ROM keeps an address reachable from the hotspot client
 * after the active tunnel changes has to be measured on the device (TeslaAddressProbe), and until it is, the state
 * stays [State.CANDIDATE].
 *
 * Ownership rules this type encodes, so the service cannot get them wrong:
 *  - the address never carries a default route and never enables USB tethering: it is one /32 on a tunnel;
 *  - it is exclusive with the Root redirect for the same address: both must never own it at once;
 *  - every state but OFF/FAILED means a descriptor is held and must be closed on revoke, process death or replacement.
 */
@Serializable
data class TeslaVpnAddress(
    val address: String = TeslaRootPlan.ADDRESS,
    /** Purpose only, for the UI; routing never reads it. */
    val purpose: String = "VPN 兼容地址",
) {
    init {
        val parts = address.split('.')
        require(parts.size == 4 && parts.all { it.matches(Regex("0|[1-9][0-9]{0,2}")) && it.toInt() in 0..255 }) {
            "地址格式无效"
        }
        require(parts[0].toInt() in 1..223 && parts[0] != "127") { "地址不能是回环或保留段" }
    }

    enum class State {
        /** Nothing held. */
        OFF,
        /** The system has not allowed this app's VPN yet; the person has to confirm once on the device. */
        NEEDS_CONSENT,
        /** No real hotspot or LAN to carry clients yet. */
        WAITING_LAN,
        /** The system is establishing the tunnel. */
        CREATING,
        /** The tunnel exists and owns the address. NOT proof that a client reached it. */
        ADDRESS_PRESENT,
        /** A real client completed the challenge on this network and configuration. */
        PEER_VERIFIED,
        /** Revoked by the system or another app; a descriptor must be closed. */
        REVOKED,
        /** The route cannot work on this device/ROM: stop claiming it, do not work around it with Root. */
        FAILED,
    }

    /** What the page/status line may claim in [state]; keeps UI text from outrunning the evidence. */
    fun describe(state: State): String = when (state) {
        State.OFF -> "未启用"
        State.NEEDS_CONSENT -> "等待在设备上允许 VPN"
        State.WAITING_LAN -> "等待热点或局域网"
        State.CREATING -> "正在建立地址"
        State.ADDRESS_PRESENT -> "地址已建立（尚未由客户端验证）"
        State.PEER_VERIFIED -> "已由客户端验证"
        State.REVOKED -> "VPN 已被系统或另一应用撤销"
        State.FAILED -> "此设备不支持免 Root 地址"
    }

    /**
     * Transitions that need no external evidence. Anything else (ADDRESS_PRESENT, PEER_VERIFIED) must come from the
     * system actually establishing the tunnel, or from a client answering the challenge.
     */
    fun mayMove(from: State, to: State): Boolean = when (from) {
        State.OFF -> to in setOf(State.NEEDS_CONSENT, State.WAITING_LAN, State.CREATING)
        State.NEEDS_CONSENT -> to in setOf(State.OFF, State.CREATING, State.FAILED)
        State.WAITING_LAN -> to in setOf(State.OFF, State.CREATING, State.FAILED)
        State.CREATING -> to in setOf(State.ADDRESS_PRESENT, State.FAILED, State.REVOKED, State.OFF)
        State.ADDRESS_PRESENT -> to in setOf(State.PEER_VERIFIED, State.REVOKED, State.FAILED, State.OFF, State.WAITING_LAN)
        State.PEER_VERIFIED -> to in setOf(State.REVOKED, State.FAILED, State.OFF, State.WAITING_LAN)
        State.REVOKED, State.FAILED -> to in setOf(State.OFF, State.CREATING)
    }

    /** True when a descriptor may be held in [state]: revoking or failing must close it. */
    fun holdsDescriptor(state: State): Boolean = state in setOf(State.CREATING, State.ADDRESS_PRESENT, State.PEER_VERIFIED)

    companion object {
        /** Never the same address as the Root redirect: both would fight over one /32. */
        fun conflictsWithRoot(rootEnabled: Boolean, car: Car): Boolean = rootEnabled && car == Car.TESLA
    }
}

/** The outcome of one attempt to reach the address from a real client: evidence, not a status string. */
internal data class AddressChallenge(val marker: String, val atMillis: Long) {
    init {
        require(marker.matches(Regex("[0-9a-f]{16}"))) { "marker must be 16 hex characters" }
    }

    /** A client answered with this marker on the current network generation; anything older has expired. */
    fun verified(answered: String?, generation: Long, answeredGeneration: Long, ageMillis: Long): Boolean =
        answered != null && answered == marker && answeredGeneration == generation && ageMillis in 0..VERIFY_TTL_MS

    companion object {
        /** Verification is bound to the network and configuration it was measured on. */
        const val VERIFY_TTL_MS = 10 * 60 * 1000L
    }
}
