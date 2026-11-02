package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test
import java.io.*
import java.net.*
import java.util.concurrent.atomic.AtomicInteger

class TeslaChecksTest {
    private fun query(name: String, type: Int = 1): ByteArray = ByteArrayOutputStream().also { b ->
        DataOutputStream(b).apply {
            writeShort(123); writeShort(0x0100); writeShort(1); writeShort(0); writeShort(0); writeShort(0)
            for (label in name.split('.')) { writeByte(label.length); write(label.toByteArray()) }
            writeByte(0); writeShort(type); writeShort(1)
        }
    }.toByteArray()
    @Test fun fixedNamesAndOwnNameAnswerLocallyWithoutInventingIPv6() {
        for (name in TeslaCheckProtocol.names) {
            val q = query(name.uppercase())
            val response = TeslaCheckProtocol.local(q, null)!!
            assertTrue(TeslaCheckProtocol.validResponse(q, response))
            assertArrayEquals(byteArrayOf(100, 96, 0, 2), response.takeLast(4).toByteArray())
            assertEquals(0, TeslaCheckProtocol.word(TeslaCheckProtocol.local(query(name, 28), null)!!, 6))
        }
        assertArrayEquals(byteArrayOf(100, 96, 0, 1), TeslaCheckProtocol.local(query("box.example.org"), "box.example.org")!!.takeLast(4).toByteArray())
        assertNull(TeslaCheckProtocol.local(query("evil.connman.vn.cloud.tesla.cn"), null))
        assertNull(TeslaCheckProtocol.local(query("other.example.org"), "box.example.org"))
    }
    @Test fun malformedDnsCannotLoopOrBeReflected() {
        val q = query("www.qq.com")
        for (i in 0 until q.size) assertNull(TeslaCheckProtocol.question(q.copyOf(i)))
        assertNull(TeslaCheckProtocol.question(q.copyOf().also { it[12] = 0xc0.toByte(); it[13] = 12 }))
        assertNull(TeslaCheckProtocol.question(q.copyOf().also { it[5] = 2 }))
        assertFalse(TeslaCheckProtocol.validResponse(q, TeslaCheckProtocol.reply(query("www.tesla.cn"), "100.96.0.2")!!))
        assertFalse(TeslaCheckProtocol.validResponse(q, TeslaCheckProtocol.reply(q, "100.96.0.2")!!.also { it[1] = 9 }))
    }
    @Test fun sharedDnsCanDisableOfflineChecksWhileKeepingOwnDomainLocal() {
        val own = query("box.example.org")
        val check = query("connman.vn.cloud.tesla.cn")
        assertNull(TeslaCheckProtocol.local(check, "box.example.org", false, "192.0.2.1"))
        assertArrayEquals(byteArrayOf(-64, 0, 2, 1), TeslaCheckProtocol.local(own, "box.example.org", false, "192.0.2.1")!!.takeLast(4).toByteArray())
        val count = AtomicInteger()
        TeslaCheckServer(InetAddress.getByName("127.0.0.1"), { "box.example.org" },
            { q -> count.incrementAndGet(); TeslaCheckProtocol.reply(q, "192.0.2.20") },
            { q -> TeslaCheckProtocol.local(q, "box.example.org", false, "192.0.2.1") }).use { server ->
            for ((q, last) in listOf(own to 1, check to 20)) Socket("127.0.0.1", server.dnsPort).use { socket ->
                socket.soTimeout = 2000
                DataOutputStream(socket.getOutputStream()).apply { writeShort(q.size); write(q); flush() }
                val input = DataInputStream(socket.getInputStream())
                val response = ByteArray(input.readUnsignedShort()).also { input.readFully(it) }
                assertEquals(last, response.last().toInt() and 255)
            }
            assertEquals(1, count.get())
        }
    }
    @Test fun httpMatchesBoxResponsesAndRejectsOtherHosts() {
        fun response(host: String, path: String = "/", method: String = "GET") = String(TeslaCheckProtocol.http("$method $path HTTP/1.1\r\nHost: $host\r\n\r\n"))
        for (name in TeslaCheckProtocol.connman) {
            val r = response(name)
            assertTrue(r.startsWith("HTTP/1.1 200")); assertTrue(r.contains("X-ConnMan-Status: online\r\n")); assertTrue(r.endsWith("</html>\n\n"))
        }
        assertTrue(response("connectivitycheck.gstatic.com", "/generate_204?x=1").startsWith("HTTP/1.1 204"))
        assertTrue(response("www.qq.com", method = "HEAD").endsWith("\r\n\r\n"))
        assertTrue(response("other.example.org").startsWith("HTTP/1.1 404"))
        assertTrue(response("www.qq.com", method = "POST").startsWith("HTTP/1.1 404"))
        assertTrue(String(TeslaCheckProtocol.http("GET / HTTP/1.1\r\nHost: www.qq.com\r\nHost: www.qq.com\r\n\r\n")).startsWith("HTTP/1.1 404"))
    }
    @Test fun actualUdpTcpHttpForwardingAndShutdown() {
        val forwarded = AtomicInteger()
        val host = InetAddress.getByName("127.0.0.1")
        val server = TeslaCheckServer(host, { "box.example.org" }, { q -> forwarded.incrementAndGet(); TeslaCheckProtocol.reply(q, "192.0.2.20") })
        try {
            val q = query("connman.vn.cloud.tesla.cn")
            DatagramSocket().use { socket ->
                socket.soTimeout = 2000
                socket.send(DatagramPacket(q, q.size, host, server.dnsPort))
                val response = DatagramPacket(ByteArray(1024), 1024); socket.receive(response)
                assertTrue(TeslaCheckProtocol.validResponse(q, response.data.copyOf(response.length)))
            }
            assertEquals(0, forwarded.get())
            Socket(host, server.dnsPort).use { socket ->
                socket.soTimeout = 2000
                val other = query("other.example.org")
                repeat(2) {
                    DataOutputStream(socket.getOutputStream()).apply { writeShort(other.size); write(other); flush() }
                    val input = DataInputStream(socket.getInputStream()); val response = ByteArray(input.readUnsignedShort()).also { input.readFully(it) }
                    assertArrayEquals(byteArrayOf(-64, 0, 2, 20), response.takeLast(4).toByteArray())
                }
            }
            assertEquals(2, forwarded.get())
            Socket(host, server.httpPort).use { socket ->
                socket.soTimeout = 2000
                socket.getOutputStream().write("GET / HTTP/1.1\r\nHost: connman.vn.tesla.services\r\n\r\n".toByteArray())
                assertTrue(socket.getInputStream().bufferedReader().readText().contains("X-ConnMan-Status: online"))
            }
            assertTrue(server.healthy)
        } finally { server.close() }
        assertFalse(server.healthy)
    }
    @Test fun offlineAndInvalidUpstreamReturnServfailButChecksStillWork() {
        val q = query("other.example.org")
        for (forward in listOf<(ByteArray) -> ByteArray?>({ null }, { byteArrayOf(1, 2) })) {
            TeslaCheckServer(InetAddress.getByName("127.0.0.1"), { null }, forward).use { server ->
                Socket("127.0.0.1", server.dnsPort).use { socket ->
                    socket.soTimeout = 2000
                    DataOutputStream(socket.getOutputStream()).apply { writeShort(q.size); write(q); flush() }
                    val input = DataInputStream(socket.getInputStream()); val response = ByteArray(input.readUnsignedShort()).also { input.readFully(it) }
                    assertEquals(2, TeslaCheckProtocol.word(response, 2) and 15)
                }
            }
        }
    }
}
