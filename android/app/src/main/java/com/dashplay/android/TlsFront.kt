package com.dashplay.android

import android.util.Log
import android.os.Build
import java.io.Closeable
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.KeyStore
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Semaphore
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLServerSocket
import javax.net.ssl.X509KeyManager

/**
 * HTTPS for the browser page: accepts TLS on [host]:[port] and passes the plain bytes to Ktor CIO on 127.0.0.1.
 * CIO has no TLS of its own, and Netty (which has) needs Android 8.0.
 */
class TlsFront(
    keyStore: KeyStore,
    password: CharArray,
    private val host: InetAddress,
    private val port: Int,
    private val backendPort: Int,
) : Closeable {
    private val context = SSLContext.getInstance("TLS").apply {
        val keys = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply { init(keyStore, password) }
        val managers = keys.keyManagers.map {
            if (Build.VERSION.SDK_INT <= 22 && it is X509KeyManager) LegacyEcKeyManager(it) else it
        }.toTypedArray()
        init(managers, null, null)
    }
    @Volatile private var listener: ServerSocket? = null
    private val connections: MutableSet<Socket> = ConcurrentHashMap.newKeySet()
    private val slots = Semaphore(32)
    val localPort: Int get() = checkNotNull(listener).localPort

    fun start() {
        val socket = context.serverSocketFactory.createServerSocket() as SSLServerSocket
        if (Build.VERSION.SDK_INT <= 22) {
            // Avoid using the EC_EC alias workaround for static ECDH, whose issuer constraints differ.
            socket.enabledCipherSuites = socket.enabledCipherSuites.filterNot {
                it.startsWith("TLS_ECDH_") || it.startsWith("SSL_ECDH_")
            }.toTypedArray()
        }
        socket.reuseAddress = true
        socket.bind(InetSocketAddress(host, port), BACKLOG)
        listener = socket
        Thread({ acceptLoop(socket) }, "tls-front").apply { isDaemon = true }.start()
    }

    private fun acceptLoop(socket: ServerSocket) {
        while (!socket.isClosed) {
            val client = try {
                socket.accept()
            } catch (error: Exception) {
                if (!socket.isClosed) Log.w(TAG, "accept failed", error)
                continue
            }
            if (!slots.tryAcquire()) {
                runCatching { client.close() }
                continue
            }
            connections.add(client)
            if (socket.isClosed) runCatching { client.close() }
            Thread({
                try { serve(client) } finally {
                    connections.remove(client)
                    runCatching { client.close() }
                    slots.release()
                }
            }, "tls-front-conn").apply { isDaemon = true }.start()
        }
    }

    private fun serve(client: Socket) {
        try {
            client.soTimeout = 10_000
            (client as javax.net.ssl.SSLSocket).startHandshake()
            client.soTimeout = 0
        } catch (error: Exception) {
            Log.w(TAG, "TLS handshake from ${client.inetAddress?.hostAddress} failed", error)
            runCatching { client.close() }
            return
        }
        val backend = try {
            // A small send buffer keeps seconds of picture from piling up on a slow car Wi-Fi (as Main.kt does for Netty).
            client.sendBufferSize = SEND_BUFFER
            client.tcpNoDelay = true
            Socket(InetAddress.getByName(LOOPBACK), backendPort).apply { tcpNoDelay = true }
        } catch (error: Exception) {
            Log.w(TAG, "page backend 127.0.0.1:$backendPort unreachable: $error")
            runCatching { client.close() }
            return
        }
        val upstream = Thread({ pump(client.getInputStream(), backend.getOutputStream(), client, backend) }, "tls-front-up")
        upstream.isDaemon = true
        upstream.start()
        pump(backend.getInputStream(), client.getOutputStream(), client, backend)
    }

    private fun pump(input: InputStream, output: OutputStream, a: Socket, b: Socket) {
        val buffer = ByteArray(BUFFER)
        try {
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                output.write(buffer, 0, read)
                output.flush()
            }
        } catch (error: Exception) {
            // Either side went away.
            Log.d(TAG, "page connection ended: $error")
        } finally {
            runCatching { a.close() }
            runCatching { b.close() }
        }
    }

    override fun close() {
        runCatching { listener?.close() }
        connections.forEach { runCatching { it.close() } }
    }

    companion object {
        /** Not InetAddress.getLoopbackAddress(): Android 5 answers ::1 there, and CIO listens on IPv4. */
        const val LOOPBACK = "127.0.0.1"
        private const val TAG = "DashPlay"
        private const val BACKLOG = 16
        private const val BUFFER = 64 * 1024
        private const val SEND_BUFFER = 128 * 1024
    }
}
