package com.dashplay.android

import java.io.Closeable
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Semaphore

/** One public TCP port. TLS bytes go only to TLS; cleartext goes to the HTTP management/viewer backend. */
internal class BrowserFront(
    private val host: InetAddress,
    private val port: Int,
    private val tlsPort: Int?,
    private val viewerPort: Int?,
) : Closeable {
    @Volatile private var listener: ServerSocket? = null
    private val connections = ConcurrentHashMap.newKeySet<Socket>()
    private val slots = Semaphore(32)
    val localPort: Int get() = checkNotNull(listener).localPort

    fun start() {
        check(listener == null)
        val socket = ServerSocket()
        try {
            socket.reuseAddress = true
            socket.bind(InetSocketAddress(host, port), 16)
        } catch (error: Exception) { socket.close(); throw error }
        listener = socket
        Thread({
            while (!socket.isClosed) {
                val client = try { socket.accept() } catch (_: Exception) { break }
                if (!slots.tryAcquire()) { client.close(); continue }
                connections.add(client)
                if (socket.isClosed) runCatching { client.close() }
                Thread({
                    try { serve(client) } finally {
                        connections.remove(client)
                        runCatching { client.close() }
                        slots.release()
                    }
                }, "browser-front-conn").apply { isDaemon = true }.start()
            }
        }, "browser-front").apply { isDaemon = true }.start()
    }

    private fun serve(client: Socket) {
        var backend: Socket? = null
        try {
            client.soTimeout = 10_000
            val input = client.getInputStream()
            val first = input.read()
            val target = when (first) {
                0x16 -> tlsPort // TLS ClientHello record; not decrypted or rewritten here.
                'G'.code, 'H'.code, 'P'.code, 'D'.code, 'O'.code, 'T'.code, 'C'.code -> viewerPort
                else -> return
            }
            if (target == null) {
                if (first == 0x16) return
                client.getOutputStream().write("HTTP/1.1 426 Upgrade Required\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".toByteArray(Charsets.US_ASCII))
                return
            }
            backend = Socket()
            connections.add(backend)
            if (listener?.isClosed != false) return
            backend.connect(InetSocketAddress(InetAddress.getByName("127.0.0.1"), target), 2000)
            backend.tcpNoDelay = true
            client.tcpNoDelay = true
            client.sendBufferSize = 128 * 1024
            backend.getOutputStream().write(first)
            client.soTimeout = 0
            val upstream = backend
            Thread({ pump(client, upstream) }, "browser-front-up").apply { isDaemon = true }.start()
            pump(upstream, client)
        } catch (_: Exception) {
            // Connection ended, unavailable backend, or incomplete protocol preface.
        } finally {
            backend?.let { connections.remove(it); runCatching { it.close() } }
        }
    }

    private fun pump(source: Socket, destination: Socket) {
        try {
            source.getInputStream().copyTo(destination.getOutputStream(), 64 * 1024)
        } catch (_: Exception) {
            // Closing either side releases both directions, including WebSocket streams.
        } finally {
            runCatching { source.close() }
            runCatching { destination.close() }
        }
    }

    override fun close() {
        runCatching { listener?.close() }
        connections.forEach { runCatching { it.close() } }
    }
}

internal object BrowserPort {
    const val DEFAULT = 18743
    fun selected(current: Int?, legacy: Int?): Int = current ?: legacy
        ?.takeIf { it in 1024..65535 && it != 8080 && it != 8081 } ?: DEFAULT
    fun validate(value: Int) {
        require(value in 1024..65535) { "网页端口需在 1024–65535 之间。" }
    }
}
