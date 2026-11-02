package com.dashplay.android

import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.util.Locale

/** Fixed check responses shared with scripts/onecloud/setup_wizard.py; never a general HTTP proxy. */
internal object TeslaCheckProtocol {
    const val ADDRESS = "100.96.0.2"
    val connman = setOf("connman.vn.cloud.tesla.cn", "connman.vn.tesla.services")
    val names = connman + setOf("www.baidu.cn", "www.qq.com", "www.tesla.cn", "www.baidu.com", "connectivitycheck.gstatic.com")
    data class Question(val name: String, val type: Int, val klass: Int, val end: Int)
    fun word(bytes: ByteArray, at: Int): Int = ((bytes[at].toInt() and 255) shl 8) or (bytes[at + 1].toInt() and 255)
    fun question(bytes: ByteArray): Question? = runCatching {
        require(bytes.size in 17..8192 && word(bytes, 2) and 0xf800 == 0 && word(bytes, 4) == 1)
        var p = 12
        val labels = mutableListOf<String>()
        while (true) {
            val n = bytes[p++].toInt() and 255
            if (n == 0) break
            // Standard one-question requests use literal labels. Reject compression cycles and oversized names.
            require(n in 1..63 && p + n < bytes.size)
            val label = bytes.copyOfRange(p, p + n)
            require(label.all { (it.toInt() and 255) in 33..126 && it != '.'.code.toByte() })
            labels += String(label, Charsets.US_ASCII); p += n
            require(p - 12 <= 254)
        }
        require(labels.isNotEmpty() && p + 4 <= bytes.size)
        Question(labels.joinToString(".").lowercase(Locale.ROOT), word(bytes, p), word(bytes, p + 2), p + 4)
    }.getOrNull()

    fun reply(query: ByteArray, address: String? = null, error: Int = 0): ByteArray? {
        val q = question(query) ?: return null
        val answer = address != null && q.type == 1 && q.klass == 1 && error == 0
        val bytes = ByteArrayOutputStream()
        DataOutputStream(bytes).use { out ->
            out.writeShort(word(query, 0)); out.writeShort(0x8080 or (word(query, 2) and 0x0100) or error)
            out.writeShort(1); out.writeShort(if (answer) 1 else 0); out.writeShort(0); out.writeShort(0)
            out.write(query, 12, q.end - 12)
            if (answer) {
                out.writeShort(0xc00c); out.writeShort(1); out.writeShort(1); out.writeInt(30); out.writeShort(4)
                address!!.split('.').forEach { out.writeByte(it.toInt()) }
            }
        }
        return bytes.toByteArray()
    }

    fun local(query: ByteArray, ownDomain: String?, checksEnabled: Boolean = true,
        ownAddress: String = TeslaRootPlan.ADDRESS): ByteArray? {
        val q = question(query) ?: return null
        val address = when {
            q.name in names && checksEnabled -> ADDRESS
            !ownDomain.isNullOrEmpty() && q.name == ownDomain.lowercase(Locale.ROOT) -> ownAddress
            else -> return null
        }
        return reply(query, address) // AAAA/other types: NODATA, never invent an IPv6 route.
    }
    fun validResponse(query: ByteArray, response: ByteArray): Boolean {
        val q = question(query) ?: return false
        if (response.size !in q.end..65535 || word(response, 0) != word(query, 0) || word(response, 2) and 0x8000 == 0 || word(response, 4) != 1) return false
        return response.copyOfRange(12, q.end).contentEquals(query.copyOfRange(12, q.end))
    }

    fun http(request: String): ByteArray {
        val lines = request.split("\r\n")
        val first = lines.firstOrNull()?.split(' ').orEmpty()
        val hosts = lines.drop(1).filter { it.substringBefore(':').equals("Host", true) }
        val host = hosts.singleOrNull()?.substringAfter(':')?.trim()?.lowercase(Locale.ROOT)
            ?.removeSuffix(":80")?.trimEnd('.')
        val allowed = first.size == 3 && first[0] in listOf("GET", "HEAD") && first[1].startsWith('/') && host in names
        val noContent = allowed && host == "connectivitycheck.gstatic.com" && first[1].substringBefore('?') in listOf("/generate_204", "/gen_204")
        val code = if (!allowed) "404 Not Found" else if (noContent) "204 No Content" else "200 OK"
        val body = when {
            !allowed -> "Not found\n"
            noContent -> ""
            host in connman -> "<html>\n<head>\n</head>\n<body>\n</body>\n</html>\n\n"
            else -> "<html><body>online</body></html>\n"
        }
        val headers = if (allowed && host in connman) "X-ConnMan-Status: online\r\nX-Cache: Hit from cloudfront\r\n" else ""
        return ("HTTP/1.1 $code\r\n${headers}Content-Type: text/html; charset=utf-8\r\nContent-Length: ${body.toByteArray().size}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n" +
            if (first.firstOrNull() == "HEAD") "" else body).toByteArray(Charsets.UTF_8)
    }
}
