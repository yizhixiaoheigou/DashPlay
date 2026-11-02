package com.dashplay.android

import android.app.Instrumentation
import android.os.Build
import android.os.Bundle
import java.io.File
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.net.InetAddress

/** Disposable-emulator integration fixture. The upstream result is explicitly local test data. */
internal object TeslaChecksProbe {
    fun query(name: String): ByteArray = ByteArrayOutputStream().also { bytes ->
        DataOutputStream(bytes).apply {
            writeShort(123); writeShort(0x0100); writeShort(1); writeShort(0); writeShort(0); writeShort(0)
            for (label in name.split('.')) { writeByte(label.length); write(label.toByteArray()) }
            writeByte(0); writeShort(1); writeShort(1)
        }
    }.toByteArray()

    fun local(results: Bundle) {
        check(Build.HARDWARE in listOf("ranchu", "goldfish"))
        val host = InetAddress.getByName("127.0.0.1")
        TeslaCheckServer(host, { null }, { null }).use { server ->
            java.net.Socket(host, server.dnsPort).use { socket ->
                socket.soTimeout = 3000
                repeat(2) {
                    val q = query("connman.vn.cloud.tesla.cn")
                    DataOutputStream(socket.getOutputStream()).apply { writeShort(q.size); write(q); flush() }
                    val input = java.io.DataInputStream(socket.getInputStream())
                    val response = ByteArray(input.readUnsignedShort()).also { input.readFully(it) }
                    check(TeslaCheckProtocol.validResponse(q, response))
                    check(response.takeLast(4).toByteArray().contentEquals(byteArrayOf(100, 96, 0, 2)))
                }
            }
            java.net.Socket(host, server.httpPort).use { socket ->
                socket.soTimeout = 3000
                socket.getOutputStream().write("GET / HTTP/1.1\r\nHost: connman.vn.tesla.services\r\n\r\n".toByteArray())
                check(socket.getInputStream().bufferedReader().readText().contains("X-ConnMan-Status: online"))
            }
            check(server.healthy)
        }
        results.putString("local_check_sockets", "ok (TCP reuse, HTTP, close; no Root)")
    }
    fun run(instrumentation: Instrumentation, results: Bundle, iface: String = "dp-tesla0") {
        check(Build.HARDWARE in listOf("ranchu", "goldfish"))
        require(iface in listOf("dp-tesla0", "eth0"))
        val base = File(instrumentation.targetContext.cacheDir, "tesla-check-probe").apply { mkdirs() }
        File(base, "done").delete(); File(base, "ready").delete()
        TeslaCheckServer(InetAddress.getByName("192.0.2.1"), { "box.example.org" }, { q -> TeslaCheckProtocol.reply(q, "192.0.2.20") }).use { server ->
            val plan = TeslaRootPlan("DP_TESLA_012345abcdef", iface, "192.0.2.1", 18080, server.dnsPort, server.httpPort)
            File(base, "lease").writeText(plan.chain)
            File(base, "install.sh").writeText(plan.installScript(File(base, "lease").path, RootProcessIdentity.current()))
            File(base, "remove.sh").writeText(plan.removeScript())
            File(base, "verify.sh").writeText(plan.verifyScript())
            for ((label, name) in listOf("check" to "connman.vn.cloud.tesla.cn", "own" to "box.example.org", "forward" to "other.example.org")) {
                val q = query(name)
                val r = TeslaCheckProtocol.local(q, "box.example.org") ?: TeslaCheckProtocol.reply(q, "192.0.2.20")!!
                File(base, "$label.query").writeBytes(q); File(base, "$label.response").writeBytes(r)
                File(base, "$label.tcp-query").writeBytes(byteArrayOf((q.size shr 8).toByte(), q.size.toByte()) + q)
                File(base, "$label.tcp-response").writeBytes(byteArrayOf((r.size shr 8).toByte(), r.size.toByte()) + r)
            }
            File(base, "ready").writeText("ready")
            val deadline = System.nanoTime() + 90_000_000_000L
            while (!File(base, "done").exists() && System.nanoTime() < deadline) Thread.sleep(100)
            check(File(base, "done").exists() && server.healthy)
            results.putLong("dns_queries", server.queries.get()); results.putLong("http_checks", server.checks.get())
        }
        results.putBoolean("success", true)
    }
}
