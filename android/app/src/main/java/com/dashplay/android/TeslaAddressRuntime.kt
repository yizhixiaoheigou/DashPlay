package com.dashplay.android

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Owns the Root-free address for the lifetime of the compatibility entry (2026-11-03).
 *
 * Why a separate object: the receiver worker starts and stops the page many times (a network change, a settings
 * change, a session restart). The address belongs to the VPN service, not to one page generation, so it must survive
 * a page refresh and be released exactly when the entry that asked for it is turned off. Binding locally also means
 * no second `VpnService` exists — the design's rule that one service owns every VPN resource.
 *
 * The state machine ([TeslaVpnAddress]) is the only thing allowed to describe what is known:
 * `ADDRESS_PRESENT` after `establish()` returned a descriptor, `PEER_VERIFIED` only when a client proved it.
 */
internal class TeslaAddressRuntime(private val context: Context) {
    @Volatile var state: TeslaVpnAddress.State = TeslaVpnAddress.State.OFF
        private set
    @Volatile var message: String = "免 Root 地址未启用"
        private set
    /** Generation of the network/configuration the current evidence belongs to; bumping it expires verification. */
    @Volatile var generation: Long = 0
        private var challenge: AddressChallenge? = null
        private var lease: UsbVpnService.AddressLease? = null
        private var service: UsbVpnService? = null
        private var bound = false
        private var boundOnce = CountDownLatch(1)
        private val plan = TeslaVpnAddress()

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            service = (binder as? UsbVpnService.LocalBinder)?.service
            bound = true
            boundOnce.countDown()
        }
        override fun onServiceDisconnected(name: ComponentName?) {
            service = null
            bound = false
            // The service (and every descriptor it owned) is gone: release ours so a later start makes a new one
            // instead of handing back a lease whose tunnel no longer exists.
            runCatching { lease?.release() }
            lease = null
            // A revoked VPN takes the descriptor with it: never keep saying the address is present.
            if (plan.mayMove(state, TeslaVpnAddress.State.REVOKED)) {
                state = TeslaVpnAddress.State.REVOKED
                message = plan.describe(state)
            }
        }
    }

    /** Establishes the address; returns false when the system or the service refuses (the reason is in [message]). */
    fun start(): Boolean {
        if (lease?.isOpen == true) return true
        if (lease?.isOpen != true && state in setOf(TeslaVpnAddress.State.ADDRESS_PRESENT, TeslaVpnAddress.State.PEER_VERIFIED)) {
            state = TeslaVpnAddress.State.REVOKED
        }
        if (!move(TeslaVpnAddress.State.CREATING)) return false
        if (!bind()) {
            fail("无法连接本应用的 VPN 服务，地址未建立。")
            return false
        }
        val host = service ?: run { fail("本应用的 VPN 服务已断开，地址未建立。"); return false }
        val created = host.attachAddress(plan)
        if (created == null) {
            fail("系统未建立免 Root 地址通道；可能未允许 VPN 或被其他 VPN 占用。")
            return false
        }
        lease = created
        created.onChanged { if (lease === created) invalidate() }
        created.onClosed {
            if (lease === created) {
                lease = null; challenge = null; generation++
                state = TeslaVpnAddress.State.REVOKED
                message = plan.describe(state)
            }
        }
        challenge = newChallenge()
        generation++
        state = TeslaVpnAddress.State.ADDRESS_PRESENT
        message = plan.describe(state) + "；请用另一台设备打开 http://${plan.address}:${Settings(context).compatHttpPort}/ 验证，请保留 http:// 开头，不要改成 https://。"
        if (!created.isOpen) { state = TeslaVpnAddress.State.REVOKED; message = plan.describe(state); return false }
        DriveLog.event("tesla address present ${plan.address} generation=$generation")
        return true
    }

    /** The page/challenge flow answered: real client evidence, bound to the current generation. */
    fun peerVerified(answered: String?): Boolean {
        val current = challenge ?: return false
        val ok = current.verified(answered, generation, generation, System.currentTimeMillis() - current.atMillis)
        if (ok) {
            state = TeslaVpnAddress.State.PEER_VERIFIED
            message = plan.describe(state)
            DriveLog.event("tesla address peer verified ${plan.address} generation=$generation")
        }
        return ok
    }

    /** Network, port or topology changed: old evidence no longer applies to the new configuration. */
    fun invalidate() {
        generation++
        challenge = newChallenge()
        if (state == TeslaVpnAddress.State.PEER_VERIFIED) {
            state = TeslaVpnAddress.State.ADDRESS_PRESENT
            message = plan.describe(state)
        }
    }

    /** The entry was turned off or the receiver is stopping: close the address only, never the cable lease. */
    fun stop() {
        lease?.release()
        lease = null
        // Release only this runtime's lease; a probe may own the service now.
        if (bound) {
            runCatching { context.unbindService(connection) }
            bound = false
        }
        service = null
        challenge = null
        boundOnce = CountDownLatch(1)
        if (state != TeslaVpnAddress.State.FAILED) state = TeslaVpnAddress.State.OFF
        message = plan.describe(state)
    }

    fun describe(): String = message

    private fun fail(reason: String) {
        state = TeslaVpnAddress.State.FAILED
        message = reason
        DriveLog.event("ERROR tesla address: $reason")
        runCatching { stop() }
        state = TeslaVpnAddress.State.FAILED
        message = reason
    }

    private fun move(next: TeslaVpnAddress.State): Boolean {
        if (!plan.mayMove(state, next)) return false
        state = next
        message = plan.describe(next)
        return true
    }

    private fun bind(): Boolean {
        if (service != null) return true
        val intent = Intent(context, UsbVpnService::class.java).setAction(UsbVpnService.LOCAL_BIND)
        // A latch counts down once: a fresh one per attempt, or the second try after a failed first would return at once.
        boundOnce = CountDownLatch(1)
        bound = context.bindService(intent, connection, Context.BIND_AUTO_CREATE)
        if (!bound) return false
        return boundOnce.await(10, TimeUnit.SECONDS) && service != null
    }

    private fun newChallenge(): AddressChallenge =
        AddressChallenge(java.util.UUID.randomUUID().toString().replace("-", "").take(16), System.currentTimeMillis())
}
