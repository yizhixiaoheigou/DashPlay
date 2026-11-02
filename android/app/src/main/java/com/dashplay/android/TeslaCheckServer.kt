package com.dashplay.android

import java.io.Closeable
import java.io.DataInputStream
import java.io.DataOutputStream
import java.net.*
import java.util.Collections
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.ScheduledThreadPoolExecutor

/** Bound exclusively to the receiver's hotspot address. Bounded sockets, queue, requests and idle time. */
internal class TeslaCheckServer(host: InetAddress, private val ownDomain: () -> String?,
    private val forward: (ByteArray) -> ByteArray?, private val local: ((ByteArray) -> ByteArray?)? = null) : Closeable {
    private val clients = Collections.synchronizedSet(mutableSetOf<Socket>())
    private val workers = ThreadPoolExecutor(4, 4, 0, TimeUnit.SECONDS, ArrayBlockingQueue(32),
        { r -> Thread(r, "dashplay-check-request").apply { isDaemon = true } }, ThreadPoolExecutor.AbortPolicy())
    private val http = ServerSocket()
    private val tcp = ServerSocket()
    private val udp = DatagramSocket(null)
    @Volatile private var closed = false
    @Volatile private var failed = false
    val queries = AtomicLong()
    val checks = AtomicLong()
    val httpPort get() = http.localPort
    val dnsPort get() = tcp.localPort
    val healthy get() = !closed && !failed
    init {
        try {
            http.bind(InetSocketAddress(host, 0), 16)
            tcp.bind(InetSocketAddress(host, 0), 16)
            udp.bind(InetSocketAddress(host, tcp.localPort))
            loop("http") { accept(http, false) }
            loop("dns-tcp") { accept(tcp, true) }
            loop("dns-udp") {
                val packet = DatagramPacket(ByteArray(8193), 8193)
                udp.receive(packet)
                val data = packet.data.copyOf(packet.length)
                submit(null) {
                    val reply = answer(data) ?: return@submit
                    // Ordinary UDP DNS limit; ask the client to retry with TCP for larger answers.
                    val bounded = if (reply.size <= 512) reply else TeslaCheckProtocol.reply(data, error = 0)?.also { it[2] = (it[2].toInt() or 2).toByte() }
                    if (bounded != null) udp.send(DatagramPacket(bounded, bounded.size, packet.socketAddress))
                }
            }
        } catch (error: Exception) { close(); throw error }
    }
    private fun loop(name: String, body: () -> Unit) {
        Thread({
            try { while (!closed) body() }
            catch (_: Exception) { if (!closed) failed = true }
        }, "dashplay-check-$name").apply { isDaemon = true; start() }
    }
    private fun submit(socket: Socket?, work: () -> Unit) {
        val expiry = socket?.let { TeslaCheckIo.deadline(it, 5000) }
        try { workers.execute { try { work() } catch (_: Exception) {} finally { expiry?.cancel(false); socket?.close(); clients.remove(socket) } } }
        catch (_: java.util.concurrent.RejectedExecutionException) { expiry?.cancel(false); socket?.close(); clients.remove(socket) }
    }
    private fun accept(server: ServerSocket, dns: Boolean) {
        val socket = server.accept()
        socket.soTimeout = 3000
        clients.add(socket)
        submit(socket) {
            if (dns) {
                val input = DataInputStream(socket.getInputStream())
                val output = DataOutputStream(socket.getOutputStream())
                repeat(16) {
                    val n = input.readUnsignedShort()
                    if (n !in 17..8192) return@submit
                    val query = ByteArray(n).also { input.readFully(it) }
                    val reply = answer(query) ?: return@submit
                    output.apply { writeShort(reply.size); write(reply); flush() }
                }
            } else {
                val input = socket.getInputStream()
                val bytes = java.io.ByteArrayOutputStream()
                val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3)
                while (bytes.size() < 8192 && System.nanoTime() < deadline) {
                    val byte = input.read(); if (byte < 0) return@submit
                    bytes.write(byte)
                    if (byte == 10 && bytes.toString("US-ASCII").endsWith("\r\n\r\n")) {
                        checks.incrementAndGet()
                        socket.getOutputStream().write(TeslaCheckProtocol.http(bytes.toString("US-ASCII")))
                        return@submit
                    }
                }
            }
        }
    }
    private fun answer(query: ByteArray): ByteArray? {
        TeslaCheckProtocol.question(query) ?: return null
        queries.incrementAndGet()
        val localReply = if (local == null) TeslaCheckProtocol.local(query, ownDomain()) else local.invoke(query)
        localReply?.let { return it }
        val response = runCatching { forward(query) }.getOrNull()
        return response?.takeIf { TeslaCheckProtocol.validResponse(query, it) } ?: TeslaCheckProtocol.reply(query, error = 2)
    }
    override fun close() {
        closed = true
        runCatching { http.close() }; runCatching { tcp.close() }; udp.close()
        synchronized(clients) { clients.toList().forEach { runCatching { it.close() } }; clients.clear() }
        workers.shutdownNow()
    }
}

internal object TeslaCheckIo {
    private val timer = ScheduledThreadPoolExecutor(1) { r -> Thread(r, "dashplay-check-deadline").apply { isDaemon = true } }
        .apply { removeOnCancelPolicy = true }
    fun deadline(socket: Socket, milliseconds: Long) = timer.schedule(Runnable { runCatching { socket.close() } }, milliseconds, TimeUnit.MILLISECONDS)
}
