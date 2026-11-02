package com.dashplay.android

import java.io.File
import java.util.UUID
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

@Serializable internal class AutomaticTlsUpdate(val enabled: Boolean, val provider: String = "duckdns",
    val domain: String = "", val zone: String = "", val token: String? = null, val termsAccepted: Boolean = false)

/** Configuration, cleanup ownership and retry deadlines survive process death as one private record. */
internal class AutomaticTlsStore(val directory: File, private val now: () -> Long = System::currentTimeMillis) {
    @Serializable internal class Profile(val id: String, val dns: CertificateDnsConfig)
    @Serializable internal data class Record(val revision: String = UUID.randomUUID().toString(),
        val enabled: Boolean = false, val termsAccepted: Boolean = false, val profile: Profile? = null,
        val retired: List<Profile> = emptyList(), val phase: String = "off", val reason: String = "",
        val failures: Int = 0, val nextAttempt: Long = 0, val dnsAddress: String = "", val dnsNext: Long = 0,
        val renewNext: Long = 0, val serverNotBefore: Long = 0)
    @Serializable data class State(val enabled: Boolean, val provider: String, val domain: String, val zone: String,
        val hasToken: Boolean, val termsAccepted: Boolean, val phase: String, val reason: String,
        val nextAttempt: Long, val pendingCleanups: Int, val message: String)
    private val file = File(directory, "automation.json")
    private var cached: Record? = null

    @Synchronized fun read(): Record {
        cached?.let { return it }
        val value = if (!file.exists()) Record() else try {
            require(file.length() in 1..32768)
            Json.decodeFromString<Record>(file.readText()).also { record ->
                require(record.retired.size <= 8)
                (record.retired + listOfNotNull(record.profile)).forEach {
                    require(it.id.matches(Regex("[a-f0-9-]{36}"))); it.dns.checked()
                }
                require(!record.enabled || record.profile != null && record.termsAccepted)
            }
        } catch (_: Exception) { throw CertificateFailure("automation_storage") }
        cached = value
        return value
    }
    @Synchronized fun state(): State {
        val r = read(); val p = r.profile?.dns
        return State(r.enabled, p?.provider ?: "duckdns", p?.domain.orEmpty(), p?.zone.orEmpty(),
            p != null, r.termsAccepted, r.phase, r.reason, r.nextAttempt,
            r.retired.count { journal(it).exists() } + if (r.profile?.let { journal(it).exists() } == true) 1 else 0,
            when (r.phase) {
                "off" -> "自动申请与续期已关闭，已保存的证书保留。"
                "waiting_network" -> "等待可用的外网与 DNS；本地 CarPlay 可继续使用。"
                "waiting_address" -> "等待车机可访问的本机地址，暂不修改域名解析。"
                "issuing" -> "正在验证域名并申请证书，请保持联网。"
                "ready" -> "自动维护已开启；证书更新后将在 CarPlay 空闲时加载。"
                "retry" -> "本次未完成（${reasonMessage(r.reason)}），稍后自动重试；原有证书保留。"
                else -> "等待自动维护。"
            })
    }
    @Synchronized fun configure(update: AutomaticTlsUpdate) {
        val old = read()
        if (!update.enabled) {
            write(old.copy(revision = UUID.randomUUID().toString(), enabled = false, phase = "off", reason = ""))
            return
        }
        val previous = old.profile?.dns
        val normalizedDomain = OwnTlsMaterial.domain(update.domain)
        val normalizedZone = if (update.provider == "desec") OwnTlsMaterial.domain(update.zone) else ""
        val sameName = previous?.provider == update.provider && previous.domain == normalizedDomain && previous.zone == normalizedZone
        val secret = update.token?.trim()?.takeIf { it.isNotEmpty() } ?: previous?.token?.takeIf { sameName }
            ?: throw IllegalArgumentException("请填写 DNS token；只有同一域名可以保留已保存的 token。")
        val dns = CertificateDnsConfig(update.provider, normalizedDomain, secret, normalizedZone).checked()
        val same = previous?.let { it.provider == dns.provider && it.domain == dns.domain && it.zone == dns.zone && it.token == dns.token } == true
        require(update.termsAccepted || same && old.termsAccepted) { "请先同意 Let's Encrypt 证书服务条款。" }
        val retired = if (!same && old.profile != null) old.retired + old.profile else old.retired
        require(retired.size <= 8) { "请先联网完成旧域名的验证记录清理，再更换配置。" }
        val profile = if (same) old.profile else Profile(UUID.randomUUID().toString(), dns)
        write(old.copy(revision = UUID.randomUUID().toString(), enabled = true, termsAccepted = true,
            profile = profile, retired = retired, phase = "waiting", reason = "",
            failures = if (same) old.failures else 0, nextAttempt = if (same) old.nextAttempt else 0,
            dnsAddress = if (same) old.dnsAddress else "", dnsNext = if (same) old.dnsNext else 0,
            renewNext = if (same) old.renewNext else 0))
    }
    @Synchronized fun retry() {
        val old = read()
        // A UI refresh/retry must not defeat a server's explicit Retry-After.
        if (now() < old.serverNotBefore) return
        write(old.copy(nextAttempt = 0, renewNext = 0))
    }
    @Synchronized fun current(revision: String): Boolean = read().revision == revision
    @Synchronized fun update(revision: String, transform: (Record) -> Record): Boolean {
        if (!current(revision)) return false
        write(transform(read())); return true
    }
    @Synchronized fun completeCleanup(id: String) {
        val old = read()
        write(old.copy(retired = old.retired.filterNot { it.id == id }))
    }
    /** Both manual and automatic certificate replacement arbitrate on this lock. */
    @Synchronized fun installIfCurrent(revision: String, action: () -> Unit): Boolean {
        if (!current(revision) || !read().enabled) return false
        action(); return true
    }
    @Synchronized fun manual(action: () -> Unit) {
        val old = read()
        if (old.enabled) write(old.copy(revision = UUID.randomUUID().toString(), enabled = false, phase = "off", reason = ""))
        action()
    }
    fun journal(profile: Profile) = File(directory, "profiles/${profile.id}/pending-txt.json")
    private fun write(value: Record) {
        try { privateAtomicWrite(file, Json.encodeToString(value).toByteArray(Charsets.UTF_8)); cached = value }
        catch (_: Exception) { throw CertificateFailure("automation_storage") }
    }
    companion object {
        fun reasonMessage(reason: String): String = when (reason) {
            "dns_denied" -> "DNS 授权或域名不正确"
            "dns_record", "dns_response", "dns_identifier" -> "DNS 记录未确认"
            "dns_cleanup", "dns_journal" -> "验证记录尚未清理"
            "rate_limited" -> "服务商要求等待"
            "tls_trust" -> "无法验证服务商的 HTTPS 证书"
            "account_storage", "automation_storage", "storage" -> "本机保存失败"
            "terms" -> "证书服务条款需要重新确认"
            "authorization", "challenge" -> "域名验证未通过"
            "certificate" -> "返回的证书未通过校验"
            "timeout", "pending" -> "服务暂未响应"
            else -> "网络或证书服务暂不可用"
        }
    }
}
