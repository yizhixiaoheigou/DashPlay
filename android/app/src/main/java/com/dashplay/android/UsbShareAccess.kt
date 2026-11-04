package com.dashplay.android

import android.content.Context
import android.os.SystemClock
import java.io.Closeable
import java.io.File
import java.net.Inet4Address
import java.net.InetAddress
import java.net.NetworkInterface
import java.util.UUID
import org.json.JSONObject

/** All maintenance/recovery runs on DashPlayService's process-wide serial worker; revoke is thread-safe. */
internal class UsbShareAccess internal constructor(context: Context,
    private val root: (String, String, () -> Unit) -> UsbShareRootResult = UsbShareRoot::run,
    private val now: () -> Long = SystemClock::elapsedRealtime) {
    private val app = context.applicationContext
    private val prefs = app.getSharedPreferences("dashplay", Context.MODE_PRIVATE)
    private val settings = Settings(app)
    private val token = UsbShareToken(File(app.filesDir, "usb-share-lease"))
    private val runtime = UsbShareRuntime(token, { prefs.getString(JOURNAL, null) }, { value ->
        prefs.edit().apply { if (value == null) remove(JOURNAL) else putString(JOURNAL, value) }.commit()
    }, root)
    private var dns: TeslaCheckServer? = null
    private var closing: Closeable? = null
    private data class Target(val network: LocalNetwork.Choice, val tunnel: UsbVpnService.Lease, val managed: Boolean)
    private var target: Target? = null
    private var failedTarget: Target? = null
    private var generation = 0L
    private var nextCheck = 0L
    @Volatile private var phase = "OFF"
    @Volatile private var message = "热点共享未启用"
    @Volatile private var active = false

    fun revoke() { token.revoke() }
    private fun disposeSockets() { closing?.close(); closing = null; dns?.close(); dns = null; target = null }
    fun recover(): Boolean {
        active = false
        val clean = runtime.recover()
        if (clean) { disposeSockets(); phase = "OFF"; message = "热点共享未启用" }
        else { phase = "CLEANUP_REQUIRED"; message = runtime.error.orEmpty() }
        return clean
    }
    fun close() { failedTarget = null; recover() }

    fun maintain(network: LocalNetwork.Choice?, managed: Boolean) {
        val wanted = settings.usbHotspotShare && settings.usesUsb && settings.usbInternet
        val tunnel = UsbVpnService.internetLease
        val next = if (wanted && settings.network == Network.THIS_HOTSPOT && network?.kind == LocalNetwork.Kind.HOTSPOT && tunnel != null)
            Target(network, tunnel, managed) else null
        // A failed cleanup needs an explicit retry; do not open another su request on every worker tick.
        if (runtime.installed == null && prefs.contains(JOURNAL)) {
            phase = "CLEANUP_REQUIRED"; message = runtime.error ?: "上次的共享规则尚待清理，请重试关闭。"; return
        }
        if (target != null && (target != next || !token.valid(generation))) {
            if (!recover()) return
        }
        if (!wanted) { phase = "OFF"; message = if (settings.usbHotspotShare) "等待启用通过数据线使用 iPhone 网络" else "热点共享未启用"; return }
        if (network?.kind != LocalNetwork.Kind.HOTSPOT || settings.network != Network.THIS_HOTSPOT) {
            phase = "WAITING_HOTSPOT"; message = "请让车机连接这台安卓设备的热点；外部 Wi-Fi 的网关不由本应用控制。"; return
        }
        if (next == null || runtime.installed == null && !UsbVpnService.internetReady) {
            phase = "WAITING_USB"; message = "等待通过数据线建立 iPhone 网络连接"; return
        }
        if (runtime.installed == null) {
            if (failedTarget == next) return
            start(next)
            return
        }
        if (now() >= nextCheck) {
            nextCheck = now() + 15_000
            val result = runtime.verify()
            if (result?.success != true || dns?.healthy != true) {
                active = false
                if (result?.granted == true && result.finished && recover()) start(next)
                else { token.revoke(); phase = "CLEANUP_REQUIRED"; message = "无法复核共享规则，请恢复 Root 授权后重试关闭。" }
                return
            }
        }
        active = true
        phase = if (UsbVpnService.internetReady) "READY" else "WAITING_USB"
        message = if (UsbVpnService.internetReady) "热点 IPv4 已转接至 iPhone；外网取决于 iPhone 连接" else "iPhone 网络暂不可用，热点互联网已暂停"
    }

    private fun start(next: Target) {
        failedTarget = next
        if (!recover()) return
        generation = token.version()
        try {
            val vpn = next.tunnel.internetPlan ?: error("尚未启用通过数据线使用 iPhone 网络")
            val local = next.network.address as Inet4Address
            val prefix = NetworkInterface.getNetworkInterfaces().toList().flatMap { it.interfaceAddresses }
                .single { it.address == local }.networkPrefixLength.toInt()
            require(prefix in 8..30) { "当前热点网段不支持共享。" }
            val number = UsbTetherProtocol.i32(local.address, 0)
            val subnet = UsbTetherProtocol.address(number and UsbInternetPlan.Cidr.mask(prefix)) + "/$prefix"
            val resolver = InetAddress.getByName(UsbTetherProtocol.address(vpn.dns))
            val ownedGeneration = generation
            closing = next.tunnel.onClosing { token.revokeIf(ownedGeneration) }
            dns = TeslaCheckServer(local, { if (settings.ownNameOnHotspot) OwnTls.store(app).selected()?.domain else null }, { query ->
                if (UsbVpnService.internetLease === next.tunnel && UsbVpnService.internetReady) UsbInternetDns.exchange(query, resolver) else null
            }, { query ->
                TeslaCheckProtocol.local(query, if (settings.ownNameOnHotspot) OwnTls.store(app).selected()?.domain else null, TeslaRootAccess.checksActive,
                    if (TeslaRootAccess.active) TeslaRootPlan.ADDRESS else local.hostAddress.orEmpty())
            })
            val uuid = UUID.randomUUID()
            val seed = uuid.leastSignificantBits and Long.MAX_VALUE
            val rules = UsbSharePlan("DP_USB_" + uuid.toString().replace("-", "").take(12), next.network.name,
                next.tunnel.interfaceName, subnet, UsbTetherProtocol.address(vpn.address), UsbTetherProtocol.address(vpn.dns),
                vpn.routes.map { "${it.text}/${it.prefix}" }, 40000 + (seed % 20000).toInt(), 9000 + (seed % 1000).toInt(),
                local.hostAddress.orEmpty(), dns!!.dnsPort, guarded = true)
            phase = "REQUESTING_ROOT"; message = "请在设备上允许 DashPlay 的 Root 请求"
            val probe = root(UsbShareSessionPlan.probeScript(rules), "DASHPLAY_SHARE_PROBED") { token.revoke() }
            check(probe.success) { "Root 未授权或接口信息不可用，请重新启用后重试。" }
            val plan = UsbShareSessionPlan.fromProbe(rules, probe.output, next.managed).copy(
                systemOffloadApi = if (!next.managed && android.os.Build.VERSION.SDK_INT >= 27) android.os.Build.VERSION.SDK_INT else 0)
            if (plan.systemOffloadApi != 0) check(root(
                "set -eu\n" + UsbShareSessionPlan.offloadCheck(plan.systemOffloadApi) + "echo 'DASHPLAY_SHARE_OFFLOAD_OK'\n",
                "DASHPLAY_SHARE_OFFLOAD_OK") { token.revoke() }.success) {
                "当前系统热点的加速转发方式不兼容，请改用本应用热点后重试。"
            }
            check(plan.tunnelIndex == next.tunnel.address.scopeId && token.version() == generation) { "共享已取消或 VPN 已改变。" }
            target = next
            if (!runtime.install(plan, generation)) {
                phase = if (prefs.contains(JOURNAL)) "CLEANUP_REQUIRED" else "FAILED"
                message = runtime.error.orEmpty()
                if (!prefs.contains(JOURNAL)) disposeSockets()
                return
            }
            failedTarget = null
            active = true; phase = "READY"; nextCheck = now() + 15_000
            message = "热点 IPv4 已转接至 iPhone；外网取决于 iPhone 连接"
        } catch (error: Exception) {
            token.revoke()
            if (!prefs.contains(JOURNAL)) disposeSockets()
            phase = if (prefs.contains(JOURNAL)) "CLEANUP_REQUIRED" else "FAILED"
            message = if (error is IllegalArgumentException) "当前热点未开启系统转发或地址不适用；可使用本应用热点后重试。" else error.message ?: "热点共享未能启用，请重试。"
        }
    }

    internal fun status() = JSONObject().put("active", active).put("state", phase).put("message", message)

    companion object {
        private const val JOURNAL = "usb_share_journal"
        @Volatile private var instance: UsbShareAccess? = null
        @Synchronized fun get(context: Context): UsbShareAccess = instance ?: UsbShareAccess(context).also { instance = it }
        fun describe(): JSONObject {
            val current = instance
            return current?.status() ?: JSONObject().put("active", false).put("state", "OFF").put("message", "热点共享未启用")
        }
    }
}
