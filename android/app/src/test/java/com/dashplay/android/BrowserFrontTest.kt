package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

class BrowserFrontTest {
    private val host = InetAddress.getByName("127.0.0.1")
    private fun exchange(port: Int, payload: ByteArray): ByteArray = Socket(host, port).use {
        it.soTimeout = 3000
        it.getOutputStream().write(payload)
        it.getInputStream().readBytes()
    }

    @Test(timeout=10000) fun protocolPrefaceAndPayloadArriveUnmodifiedAtOnlyTheSelectedBackend() {
        ServerSocket(0, 8, host).use { tls -> ServerSocket(0, 8, host).use { http ->
            BrowserFront(host, 0, tls.localPort, http.localPort).use { front ->
                front.start()
                val tlsPayload = byteArrayOf(0x16, 3, 3, 0, 6, 0, 1, 2, 3, 4, 5)
                val httpPayload = "POST /api/android/settings HTTP/1.1\r\nHost: local\r\n\r\n".toByteArray()
                val received = java.util.concurrent.CopyOnWriteArrayList<ByteArray>()
                val done = CountDownLatch(2)
                for ((backend, payload, reply) in listOf(Triple(tls, tlsPayload, "TLS"), Triple(http, httpPayload, "VIEWER"))) {
                    thread(isDaemon=true) {
                        backend.accept().use { socket ->
                            socket.soTimeout = 3000
                            val bytes = ByteArray(payload.size)
                            java.io.DataInputStream(socket.getInputStream()).readFully(bytes)
                            received.add(bytes)
                            socket.getOutputStream().write(reply.toByteArray())
                        }; done.countDown()
                    }
                }
                assertEquals("TLS", String(exchange(front.localPort, tlsPayload)))
                assertEquals("VIEWER", String(exchange(front.localPort, httpPayload)))
                assertTrue(done.await(3, TimeUnit.SECONDS))
                assertTrue(received.any { it.contentEquals(tlsPayload) })
                assertTrue(received.any { it.contentEquals(httpPayload) })
            }
        } }
    }

    @org.junit.Rule @JvmField val tmp = org.junit.rules.TemporaryFolder()

    @Test(timeout=10000) fun realTlsHandshakeAndHttpUseTheSamePublicPort() {
        val material = com.playport.server.TlsCertificate.loadOrCreate(tmp.root.toPath(), listOf(host))
        val keys = javax.net.ssl.KeyManagerFactory.getInstance(javax.net.ssl.KeyManagerFactory.getDefaultAlgorithm())
        keys.init(material.keyStore, material.password)
        // Trust only this generated test certificate, not arbitrary network certificates.
        val trusted = java.security.KeyStore.getInstance("PKCS12").apply {
            load(null, null); setCertificateEntry("fixture", material.keyStore.getCertificate(material.alias))
        }
        val trust = javax.net.ssl.TrustManagerFactory.getInstance(javax.net.ssl.TrustManagerFactory.getDefaultAlgorithm())
        trust.init(trusted)
        val context = javax.net.ssl.SSLContext.getInstance("TLS").apply { init(keys.keyManagers, trust.trustManagers, null) }
        (context.serverSocketFactory.createServerSocket(0, 8, host) as javax.net.ssl.SSLServerSocket).use { tls ->
            ServerSocket(0, 8, host).use { http ->
                val errors = java.util.concurrent.CopyOnWriteArrayList<Throwable>()
                val done = CountDownLatch(2)
                for ((server, text) in listOf(tls to "TLS-MANAGE", http to "HTTP-VIEWER")) thread(isDaemon=true) {
                    try {
                        server.accept().use { socket ->
                            socket.soTimeout = 3000
                            val reader = socket.getInputStream().bufferedReader()
                            while (reader.readLine()?.isNotEmpty() == true) { }
                            socket.getOutputStream().write(("HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: ${text.length}\r\n\r\n$text").toByteArray())
                        }
                    } catch (error: Throwable) { errors.add(error) } finally { done.countDown() }
                }
                BrowserFront(host, 0, tls.localPort, http.localPort).use { front ->
                    front.start()
                    val request = "GET / HTTP/1.1\r\nHost: localhost:${front.localPort}\r\n\r\n".toByteArray()
                    assertTrue(String(exchange(front.localPort, request)).endsWith("HTTP-VIEWER"))
                    (context.socketFactory.createSocket(host, front.localPort) as javax.net.ssl.SSLSocket).use { client ->
                        client.soTimeout = 3000
                        client.startHandshake()
                        client.getOutputStream().write(request)
                        assertTrue(String(client.getInputStream().readBytes()).endsWith("TLS-MANAGE"))
                    }
                    assertTrue(done.await(3, TimeUnit.SECONDS))
                    assertTrue(errors.toString(), errors.isEmpty())
                }
            }
        }
    }

    @Test(timeout=5000) fun disabledHttpNeverReachesTlsBackend() {
        ServerSocket(0, 8, host).use { tls ->
            tls.soTimeout = 200
            BrowserFront(host, 0, tls.localPort, null).use { front ->
                front.start()
                Socket(host, front.localPort).use { client ->
                    client.soTimeout = 2000
                    client.getOutputStream().write("GET /manage HTTP/1.1\r\n\r\n".toByteArray())
                    assertEquals("HTTP/1.1 426 Upgrade Required", client.getInputStream().bufferedReader().readLine())
                }
                assertThrows(java.net.SocketTimeoutException::class.java) { tls.accept() }
            }
        }
    }

    @Test(timeout=5000) fun invalidProtocolDoesNotReachEitherBackend() {
        ServerSocket(0, 8, host).use { backend ->
            backend.soTimeout = 200
            BrowserFront(host, 0, backend.localPort, backend.localPort).use { front ->
                front.start()
                val response = runCatching { exchange(front.localPort, byteArrayOf(0, 1, 2)) }
                assertTrue(response.exceptionOrNull() is java.net.SocketException || response.getOrNull()?.isEmpty() == true)
                assertThrows(java.net.SocketTimeoutException::class.java) { backend.accept() }
            }
        }
    }

    @Test(timeout=5000) fun closingListenerClosesIdleConnectionsAndReleasesPort() {
        val front = BrowserFront(host, 0, 1, null)
        front.start()
        val port = front.localPort
        Socket(host, port).use { idle ->
            idle.soTimeout = 2000
            front.close()
            val result = runCatching { idle.getInputStream().read() }
            assertTrue(result.isFailure || result.getOrNull() == -1)
        }
        ServerSocket().use { it.reuseAddress = true; it.bind(java.net.InetSocketAddress(host, port)) }
    }

    @Test(timeout=5000) fun occupiedPortFailsWithoutReplacingExistingListener() {
        ServerSocket(0, 8, host).use { occupied ->
            BrowserFront(host, occupied.localPort, 1, null).use { front ->
                assertThrows(java.net.BindException::class.java) { front.start() }
            }
            assertFalse(occupied.isClosed)
        }
    }
}
