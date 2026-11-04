package com.dashplay.android

import android.content.Context
import kotlinx.serialization.encodeToString
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import org.json.JSONObject
import java.io.File
import java.util.UUID
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit

/** Runs only after device-local opt-in. All calls are on the receiver worker, never the UI thread. */
class TeslaRootAccess internal constructor(context: Context, private val rootOverride: ((String, String) -> Result)?) {
    constructor(context: Context) : this(context, null)
    private val app = context.applicationContext
    private var checks: TeslaCheckServer? = null
    private val settings = Settings(context.applicationContext)
    private val prefs = context.applicationContext.getSharedPreferences("dashplay", Context.MODE_PRIVATE)
    private val lease = File(context.filesDir, "tesla-root-lease")
    private var installed: TeslaRootPlan? = null
    private var nextCheck = 0L

    fun start(network: LocalNetwork.Choice, port: Int) {
        if (!recover()) return
        val wantsTesla = settings.teslaRootAccess && settings.car == Car.TESLA
        val ownDomain = if (settings.ownNameOnHotspot) OwnTls.store(app).selected()?.domain?.takeIf { it.isNotBlank() } else null
        val wantsOwnDns = settings.hotspotOwnNameRoot && ownDomain != null
        if (!wantsTesla && !wantsOwnDns) {
            phase = "OFF"; message = "Root 访问地址未启用"; return
        }
        if (settings.network != Network.THIS_HOTSPOT || network.kind != LocalNetwork.Kind.HOTSPOT) {
            phase = "WAITING_HOTSPOT"; message = "请让车机连接这台安卓设备的热点；外部 Wi-Fi 的网关不由本应用控制。"; return
        }
        val wantsChecks = wantsTesla && settings.teslaOfflineChecks
        if (wantsChecks || wantsOwnDns) {
            val targetIp = if (wantsTesla) TeslaRootPlan.ADDRESS else network.address.hostAddress.orEmpty()
            checks = runCatching {
                TeslaCheckServer(network.address, { if (settings.ownNameOnHotspot) OwnTls.store(app).selected()?.domain else null },
                    TeslaCheckDns(app, network.name)::exchange,
                    { q -> TeslaCheckProtocol.local(q, if (settings.ownNameOnHotspot) OwnTls.store(app).selected()?.domain else null, wantsChecks, targetIp) })
            }.getOrNull()
            if (checks == null) { phase = "FAILED"; message = "无法启动热点 DNS / 联网检查服务，请关闭后重试。"; return }
        }
        val plan = runCatching {
            TeslaRootPlan("DP_TESLA_" + UUID.randomUUID().toString().replace("-", "").take(12),
                network.name, network.address.hostAddress.orEmpty(), port,
                checks?.dnsPort, if (wantsChecks) checks?.httpPort else null, redirectAddress = wantsTesla)
        }.getOrNull()
        if (plan == null) { closeChecks(); phase = "FAILED"; message = "当前热点地址不适用于此方式。"; return }
        val process = runCatching { RootProcessIdentity.current() }.getOrNull()
        if (process == null) { closeChecks(); phase = "FAILED"; message = "无法确认应用进程状态，未修改访问规则。"; return }
        if (!prefs.edit().putString(JOURNAL, Json.encodeToString(plan)).commit()) {
            closeChecks()
            phase = "FAILED"; message = "无法保存恢复记录，未修改访问规则。"; return
        }
        try { lease.outputStream().use { it.write(plan.chain.toByteArray()); it.fd.sync() } }
        catch (_: Exception) {
            prefs.edit().remove(JOURNAL).commit()
            closeChecks()
            phase = "FAILED"; message = "无法保存访问规则的生效凭据，未修改网络。"; return
        }
        phase = "REQUESTING_ROOT"; message = "请在设备上允许 DashPlay 的 Root 请求"
        val result = runRoot(plan.installScript(lease.absolutePath, process), "DASHPLAY_ROOT_ACTIVE")
        if (result.success) {
            installed = plan; nextCheck = android.os.SystemClock.elapsedRealtime() + 15_000
            active = wantsTesla; checksActive = wantsChecks; ownDnsActive = checks != null && settings.ownNameOnHotspot
            phase = "READY"
            message = if (wantsTesla) {
                "访问地址已启用：${TeslaRootPlan.ADDRESS}:$port" +
                    (if (wantsChecks) "；热点联网检查已开启（不提供互联网）" else "") +
                    (if (ownDnsActive && ownDomain != null) "；本机回答域名 $ownDomain" else "")
            } else {
                "热点域名解析已启用：$ownDomain → ${network.address.hostAddress}"
            }
            return
        }
        // A completed denial/collision before chain creation cannot have installed our rules.
        if (result.finished && !result.owned && prefs.edit().remove(JOURNAL).commit()) {
            lease.delete()
            closeChecks()
            phase = "FAILED"
            message = if (!RootShell.available()) "本机 Root 不允许应用使用（su 只有 shell 组可执行），无法启用访问地址。"
            else "Root 权限、iptables 或规则链不可用，未接管已有网络规则。"
            return
        }
        if (recover()) { phase = "FAILED"; message = "系统未能启用访问地址，已移除本次规则。" }
    }

    /** A durable journal is written before the first command and retained until cleanup is verified. */
    fun recover(): Boolean {
        active = false
        installed = null
        // Revoke first: a delayed su prompt/command must not install rules after cleanup completes.
        if (lease.exists() && !lease.delete()) {
            phase = "CLEANUP_REQUIRED"; message = "无法撤销访问规则的生效凭据，请重试关闭。"; return false
        }
        val raw = prefs.getString(JOURNAL, null) ?: run { closeChecks(); return true }
        phase = "CLEANING"; message = "正在移除上次的特斯拉访问规则"
        val old = runCatching { Json.decodeFromString<TeslaRootPlan>(raw) }.getOrNull()
        if (old == null) { phase = "FAILED"; message = "访问规则记录无法读取，已保留原记录。"; return false }
        if (!runRoot(old.removeScript(), "DASHPLAY_ROOT_REMOVED").success) {
            phase = "CLEANUP_REQUIRED"; message = "尚未确认访问规则已清理。请恢复 Root 授权后重试关闭。"; return false
        }
        if (!prefs.edit().remove(JOURNAL).commit()) {
            phase = "CLEANUP_REQUIRED"; message = "规则已移除，清理记录未保存，请重试。"; return false
        }
        closeChecks()
        return true
    }

    private fun closeChecks() { checks?.close(); checks = null; checksActive = false; ownDnsActive = false }

    fun close() {
        if (recover()) { phase = "OFF"; message = "Root 访问地址未启用" }
    }

    fun waitingForNetwork() {
        val wantsTesla = settings.teslaRootAccess && settings.car == Car.TESLA
        val wantsOwnDns = settings.hotspotOwnNameRoot && settings.ownNameOnHotspot
        if (phase == "OFF" && (wantsTesla || wantsOwnDns)) {
            phase = "WAITING_HOTSPOT"; message = "等待这台安卓设备的热点开启"
        }
    }

    fun maintain(network: LocalNetwork.Choice, port: Int) {
        val plan = installed ?: return
        if (phase != "READY" || android.os.SystemClock.elapsedRealtime() < nextCheck) return
        nextCheck = android.os.SystemClock.elapsedRealtime() + 15_000
        if (checks?.healthy == false) {
            if (recover()) { phase = "FAILED"; message = "热点联网检查已停止，已清理本次访问规则，请重试启用。" }
            return
        }
        dnsQueries = checks?.queries?.get() ?: 0
        httpChecks = checks?.checks?.get() ?: 0
        val result = runRoot(plan.verifyScript(), "DASHPLAY_ROOT_ACTIVE")
        if (result.success) return
        active = false
        if (result.rootGranted && result.finished) start(network, port)
        else { phase = "FAILED"; message = "无法复核 Root 访问规则，请在设备上重新授权或关闭清理。" }
    }

    internal data class Result(val success: Boolean, val owned: Boolean, val finished: Boolean, val rootGranted: Boolean = false)
    private fun runRoot(script: String, success: String): Result {
        rootOverride?.let { return it(script, success) }
        val process = try { RootShell.open(script) }
            catch (_: Exception) { return Result(false, false, true) }
        val task = FutureTask<Result> {
            var matched = false
            var owned = false
            var rootGranted = false
            process.inputStream.bufferedReader().useLines { lines ->
                lines.forEach {
                    if (it.trim() == success) matched = true
                    if (it.trim() == "DASHPLAY_ROOT_OWNED") owned = true
                    if (it.trim() == "DASHPLAY_ROOT_STARTED") rootGranted = true
                }
            }
            Result(process.waitFor() == 0 && matched, owned, true, rootGranted)
        }
        Thread(task, "dashplay-root-result").apply { isDaemon = true; start() }
        return try { task.get(30, TimeUnit.SECONDS) }
        catch (_: Exception) { process.destroy(); Result(false, false, false) }
    }

    companion object {
        private const val JOURNAL = "tesla_root_journal"
        @Volatile var checksActive = false; private set
        @Volatile var ownDnsActive = false; private set
        @Volatile var dnsQueries = 0L; private set
        @Volatile var httpChecks = 0L; private set
        @Volatile var active = false; private set
        @Volatile var phase = "OFF"; private set
        @Volatile var message = "Root 访问地址未启用"; private set
        fun describe() = JSONObject().put("active", active).put("state", phase).put("message", message).put("checksActive", checksActive).put("ownDnsActive", ownDnsActive).put("dnsQueries", dnsQueries).put("httpChecks", httpChecks)
    }
}
