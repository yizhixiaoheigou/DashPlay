package com.dashplay.android

import java.io.File
import java.net.URLEncoder
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.*

/** Explicit zone avoids guessing the registrable domain or listing all zones using a restricted token. */
@Serializable internal class CertificateDnsConfig(val provider: String, val domain: String, val token: String, val zone: String = "") {
    fun checked(): CertificateDnsConfig {
        val name = OwnTlsMaterial.domain(domain)
        val secret = token.trim()
        when (provider) {
            "duckdns" -> {
                require(name.endsWith(".duckdns.org") && name.removeSuffix(".duckdns.org").matches(Regex("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"))) {
                    "请填写为 DashPlay 单独使用的完整 DuckDNS 域名。"
                }
                require(secret.matches(Regex("[A-Za-z0-9-]{16,64}"))) { "DuckDNS token 格式不正确。" }
                return CertificateDnsConfig(provider, name, secret)
            }
            "desec" -> {
                val hosted = OwnTlsMaterial.domain(zone)
                require(name == hosted || name.endsWith(".$hosted")) { "域名不在填写的 deSEC DNS 区域内。" }
                require(secret.matches(Regex("[A-Za-z0-9_-]{20,128}"))) { "deSEC token 格式不正确。" }
                return CertificateDnsConfig(provider, name, secret, hosted)
            }
            else -> throw IllegalArgumentException("不支持这个 DNS 服务商。")
        }
    }
    // Deliberately not a data class: auto-generated toString/copy output must not expose tokens.
    override fun toString() = "CertificateDnsConfig($provider, $domain)"
}

/** Journal contains only the exact TXT value we own. Configurations use separate private directories. */
internal class CertificateDns(private val config: CertificateDnsConfig, private val http: CertificateHttp,
    private val journal: File) : AcmeDnsChallenge {
    @Serializable private data class Pending(val name: String, val value: String)
    private val c = config.checked()
    private val challengeName = "_acme-challenge.${c.domain}"
    private val collection = if (c.provider == "desec") "https://desec.io/api/v1/domains/${c.zone}/rrsets/" else ""
    private val subname = if (c.domain == c.zone) "" else c.domain.removeSuffix(".${c.zone}")
    private val txtName = if (subname.isEmpty()) "_acme-challenge" else "_acme-challenge.$subname"
    private val auth = mapOf("Authorization" to "Token ${c.token}", "Content-Type" to "application/json")
    private val minimumTtl: Int by lazy {
        val reply = http.request("GET", "https://desec.io/api/v1/domains/${c.zone}/", auth, null)
        checkReply(reply)
        try {
            val value = Json.parseToJsonElement(reply.text()).jsonObject
            val ttl = value["minimum_ttl"]?.jsonPrimitive?.intOrNull
            if (value.string("name") != c.zone || ttl == null || ttl !in 1..86400) throw CertificateFailure("dns_response")
            ttl
        } catch (_: Exception) { throw CertificateFailure("dns_response") }
    }

    /** Publishing IPv6 requires a separate firewall design; only an explicit IPv4 receiver address is accepted. */
    fun updateAddress(address: String) {
        require(address.matches(Regex("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}")) && address.split('.').all { it.toInt() in 0..255 } &&
            address.substringBefore('.').toInt() in 1..223 && address.substringBefore('.') != "127" && !address.startsWith("169.254.")) {
            "当前没有可发布的 IPv4 地址。"
        }
        if (c.provider == "duckdns") duck(mapOf("ip" to address))
        else writeRecord(subname, "A", listOf(address), null)
    }
    @Synchronized override fun present(name: String, value: String) {
        checkValue(name, value)
        // A previous interrupted attempt must be cleaned before a new value can replace its journal.
        recover()
        try { privateAtomicWrite(journal, Json.encodeToString(Pending(name, value)).toByteArray(Charsets.UTF_8)) }
        catch (_: Exception) { throw CertificateFailure("dns_journal") }
        if (c.provider == "duckdns") duck(mapOf("txt" to value))
        else {
            val record = txtRecord()
            val old = record?.get("records")?.jsonArray?.map { it.jsonPrimitive.content }.orEmpty()
            writeRecord(txtName, "TXT", (old + "\"$value\"").distinct(), record?.get("ttl")?.jsonPrimitive?.intOrNull)
        }
    }
    @Synchronized override fun remove(name: String, value: String) {
        checkValue(name, value)
        val pending = pending() ?: return
        // Caller never gets authority over any different or unjournaled record.
        if (pending.name != name || pending.value != value) throw CertificateFailure("dns_journal")
        if (c.provider == "duckdns") {
            // DuckDNS has one TXT slot shared by subdomains; configuration requires a dedicated DashPlay name.
            // Including txt is essential: clear=true without txt would delete A/AAAA instead.
            duck(mapOf("txt" to value, "clear" to "true"))
        } else {
            val record = txtRecord()
            if (record != null) {
                val old = record["records"]?.jsonArray?.map { it.jsonPrimitive.content } ?: throw CertificateFailure("dns_response")
                val remaining = old.filterNot { it == "\"$value\"" }
                if (remaining != old) writeRecord(txtName, "TXT", remaining, record["ttl"]?.jsonPrimitive?.intOrNull)
            }
        }
        if (!journal.delete() && journal.exists()) throw CertificateFailure("dns_journal")
    }
    @Synchronized fun recover() { pending()?.let { remove(it.name, it.value) } }
    fun cleanupPending(): Boolean = journal.exists()
    private fun pending(): Pending? {
        if (!journal.exists()) return null
        try {
            require(journal.length() in 1..2048)
            return Json.decodeFromString<Pending>(journal.readText()).also { checkValue(it.name, it.value) }
        } catch (_: Exception) { throw CertificateFailure("dns_journal") }
    }
    private fun checkValue(name: String, value: String) {
        if (name != challengeName || !value.matches(Regex("[A-Za-z0-9_-]{43}"))) throw CertificateFailure("dns_identifier")
    }
    private fun duck(fields: Map<String, String>) {
        val query = (mapOf("domains" to c.domain.removeSuffix(".duckdns.org"), "token" to c.token) + fields)
            .entries.joinToString("&") { encode(it.key) + "=" + encode(it.value) }
        val reply = http.request("GET", "https://www.duckdns.org/update?$query", emptyMap(), null)
        checkReply(reply)
        if (reply.text().trim() != "OK") throw CertificateFailure("dns_denied")
    }
    private fun txtRecord(): JsonObject? {
        val reply = http.request("GET", "$collection$txtName/TXT/", auth, null)
        if (reply.code == 404) return null
        checkReply(reply)
        return try {
            Json.parseToJsonElement(reply.text()).jsonObject.also {
                if (it.string("subname") != txtName || it.string("type") != "TXT" || it["records"] !is JsonArray ||
                    (it["ttl"]?.jsonPrimitive?.intOrNull ?: 0) !in 1..86400) throw CertificateFailure("dns_response")
                it["records"]!!.jsonArray.forEach { record ->
                    if (!record.jsonPrimitive.isString) throw CertificateFailure("dns_response")
                }
            }
        } catch (_: Exception) { throw CertificateFailure("dns_response") }
    }
    private fun writeRecord(sub: String, type: String, records: List<String>, ttl: Int?) {
        val body = buildJsonArray { add(buildJsonObject {
            put("subname", sub); put("type", type); put("ttl", maxOf(ttl ?: minimumTtl, minimumTtl))
            put("records", buildJsonArray { records.forEach { add(it) } })
        }) }.toString().toByteArray(Charsets.UTF_8)
        checkReply(http.request("PUT", collection, auth, body))
    }
    private fun checkReply(reply: CertificateReply) {
        if (reply.code !in 200..299) throw CertificateFailure(when (reply.code) {
            401, 403, 404 -> "dns_denied"
            400, 422 -> "dns_record"
            429 -> "rate_limited"
            else -> "dns_network"
        }, AcmeClient.retryAfter(reply.header("Retry-After"), System.currentTimeMillis()))
    }
    private fun encode(value: String): String = URLEncoder.encode(value, "UTF-8")
}
