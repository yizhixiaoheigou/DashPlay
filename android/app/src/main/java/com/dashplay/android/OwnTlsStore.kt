package com.dashplay.android

import java.io.File
import java.io.FileOutputStream
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** One private atomic record prevents a new certificate from ever being paired with an old key after a crash. */
class OwnTlsStore(private val directory: File, private val clock: () -> Long = System::currentTimeMillis) {
    @Serializable private data class Record(val domain: String, val chain: String, val key: String, val enabled: Boolean)
    @Serializable data class State(val enabled: Boolean, val usable: Boolean, val domain: String,
        val expires: Long, val fingerprint: String, val systemTrusted: Boolean, val message: String)
    private val file = File(directory, "certificate.json")
    private var initialized = false
    private var record: Record? = null
    private var loaded: OwnTlsMaterial.Loaded? = null
    private var invalid = false

    @Synchronized private fun read() {
        if (initialized) return
        if (file.exists()) try {
            require(file.length() in 1..100_000)
            val saved = Json.decodeFromString<Record>(file.readText())
            record = saved
            loaded = OwnTlsMaterial.parse(saved.domain, saved.chain, saved.key, clock(), minimumRemaining = 0)
        } catch (_: Exception) { invalid = true }
        initialized = true
    }

    @Synchronized fun state(): State {
        read()
        val saved = record
        val material = loaded
        val valid = material?.validAt(clock()) == true
        val enabled = saved?.enabled == true
        val message = when {
            invalid -> "保存的证书无法使用，当前回退到本地证书。请重新导入；原文件已保留。"
            saved == null -> "尚未配置自有证书。"
            !enabled -> "自有证书已停用，材料保留；当前使用本地证书。"
            !valid -> "自有证书已过期或尚未生效，请更新。下次空闲重建接收服务时使用本地证书。"
            material?.systemTrusted == true -> "自有证书可用；请确认车机能把域名解析到本机地址。"
            else -> "自有证书可用，但本机系统不信任其签发链；车机也可能提示证书警告。"
        }
        return State(enabled, enabled && valid, saved?.domain.orEmpty(), material?.expires ?: 0,
            material?.fingerprint.orEmpty(), material?.systemTrusted ?: false, message)
    }

    @Synchronized fun selected(): OwnTlsMaterial.Loaded? {
        read()
        return loaded?.takeIf { record?.enabled == true && it.validAt(clock()) }
    }

    @Synchronized fun install(domain: String, chain: String, key: String) {
        val material = OwnTlsMaterial.parse(domain, chain, key, clock())
        val next = Record(material.domain, chain, key, true)
        write(next)
        record = next; loaded = material; invalid = false; initialized = true
    }

    @Synchronized fun enable(enabled: Boolean) {
        read()
        val saved = record ?: throw IllegalArgumentException("请先导入证书。")
        require(!enabled || loaded?.validAt(clock()) == true) { "证书已失效，请重新导入。" }
        write(saved.copy(enabled = enabled))
        record = saved.copy(enabled = enabled)
    }

    private fun write(next: Record) {
        check(directory.isDirectory || directory.mkdirs()) { "证书未保存，请重试。" }
        val scratch = File.createTempFile(".certificate-", ".json", directory)
        try {
            check(scratch.setReadable(false, false) && scratch.setWritable(false, false) &&
                scratch.setReadable(true, true) && scratch.setWritable(true, true)) { "证书未保存，请重试。" }
            FileOutputStream(scratch).use { output ->
                output.write(Json.encodeToString(next).toByteArray(Charsets.UTF_8)); output.fd.sync()
            }
            check(scratch.renameTo(file)) { "证书未保存，请重试。" }
        } finally { scratch.delete() }
    }
}
