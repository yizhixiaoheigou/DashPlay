package com.dashplay.android

import java.io.File
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit
import kotlinx.serialization.encodeToString
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json

/** Revocation can arrive on the UI/VPN thread while the receiver worker is waiting for su. */
internal class UsbShareToken(private val file: File) {
    private var revision = 0L
    @Synchronized fun version() = revision
    @Synchronized fun revoke(): Boolean { revision++; return !file.exists() || file.delete() }
    @Synchronized fun revokeIf(expected: Long) { if (revision == expected) revoke() }
    @Synchronized fun grant(expected: Long, owner: String): Boolean {
        if (revision != expected) return false
        file.outputStream().use { it.write(owner.toByteArray()); it.fd.sync() }
        return true
    }
    @Synchronized fun valid(expected: Long) = revision == expected && file.exists()
    val path get() = file.absolutePath
}

internal data class UsbShareRootResult(val success: Boolean, val finished: Boolean, val granted: Boolean,
    val output: List<String> = emptyList())

internal object UsbShareRoot {
    fun run(script: String, marker: String, timeout: () -> Unit = {}): UsbShareRootResult {
        val process = try { RootShell.open(script) }
        catch (_: Exception) { return UsbShareRootResult(false, true, false) }
        val task = FutureTask {
            val output = mutableListOf<String>()
            var success = false; var granted = false
            process.inputStream.bufferedReader().useLines { lines -> lines.forEach { line ->
                if (line == marker) success = true
                if (line == "DASHPLAY_SHARE_STARTED") granted = true
                if (line.startsWith("DASHPLAY_SHARE_LINKS ") && output.size < 4) output += line.take(256)
            } }
            UsbShareRootResult(process.waitFor() == 0 && success, true, granted, output)
        }
        Thread(task, "dashplay-usb-share-root").apply { isDaemon = true; start() }
        return try { task.get(30, TimeUnit.SECONDS) } catch (_: Exception) {
            timeout(); process.destroy(); UsbShareRootResult(false, false, false)
        }
    }
}

/** Serialized worker operations; durable ownership survives process death and unsuccessful cleanup. */
internal class UsbShareRuntime(val token: UsbShareToken, private val read: () -> String?,
    private val write: (String?) -> Boolean,
    private val root: (String, String, () -> Unit) -> UsbShareRootResult = UsbShareRoot::run) {
    var installed: UsbShareSessionPlan? = null; private set
    var error: String? = null; private set
    fun recover(): Boolean {
        installed = null
        if (!token.revoke()) { error = "无法撤销共享规则凭据，请重试关闭。"; return false }
        val raw = read() ?: run { error = null; return true }
        val old = runCatching { Json.decodeFromString<UsbShareSessionPlan>(raw) }.getOrNull()
        if (old == null) { error = "共享恢复记录无法读取，已保留原记录。"; return false }
        if (!root(old.removeScript(liveLease = token.path), "DASHPLAY_SHARE_SESSION_REMOVED") { token.revoke() }.success) {
            error = "尚未确认共享规则已清理，请恢复 Root 授权后重试关闭。"; return false
        }
        if (!write(null)) { error = "规则已移除，恢复记录尚未清除，请重试。"; return false }
        error = null; return true
    }
    fun install(plan: UsbShareSessionPlan, expected: Long): Boolean {
        check(installed == null && read() == null)
        if (!write(Json.encodeToString(plan))) { error = "无法保存共享恢复记录，未修改网络。"; return false }
        if (!runCatching { token.grant(expected, plan.rules.owner) }.getOrDefault(false)) {
            token.revokeIf(expected)
            if (!write(null)) error = "共享已取消，恢复记录待清理。" else error = "共享已取消。"
            return false
        }
        val result = root(plan.installScript(token.path), "DASHPLAY_SHARE_SESSION_ACTIVE") { token.revoke() }
        if (result.success && token.valid(expected)) { installed = plan; error = null; return true }
        token.revoke()
        if (!result.finished) {
            error = "Root 请求超时，已撤销共享凭据；请重试关闭以核对清理。"
            return false
        }
        // No root code started: no kernel or interface mutation. A timeout may still run later, so retain its journal.
        if (result.finished && !result.granted && write(null)) error = "Root 未授权或不可用，未修改网络。"
        else if (recover()) error = "共享未能启用，已清理本次规则。"
        return false
    }
    fun verify(): UsbShareRootResult? = installed?.let { root(it.verifyScript(), "DASHPLAY_SHARE_VERIFIED") { token.revoke() } }
}
