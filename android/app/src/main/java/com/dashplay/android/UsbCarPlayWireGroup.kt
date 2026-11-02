package com.dashplay.android

import java.io.IOException
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** iPhone can select either NCM function for CarPlay. Learn each Ethernet peer's actual port. */
internal class UsbCarPlayWireGroup(private val ports: List<UsbCarPlayWire>) : UsbCarPlayWire {
    private val open = AtomicBoolean(true)
    private val incoming = ArrayBlockingQueue<ByteArray>(256)
    private val peers = linkedMapOf<Long, Int>()
    @Volatile private var failure: Exception? = null
    private val readers: List<Thread>

    init {
        require(ports.isNotEmpty())
        readers = ports.mapIndexed { index, port ->
            Thread({
                try {
                    while (open.get()) {
                        val frame = port.receive(250) ?: continue
                        if (frame.size < 14) continue
                        synchronized(peers) {
                            val key = mac(frame, 6)
                            if (key !in peers && peers.size >= 128) peers.remove(peers.keys.first())
                            peers[key] = index
                        }
                        if (!incoming.offer(frame, 250, TimeUnit.MILLISECONDS) && open.get()) {
                            throw IOException("USB NCM receive queue full")
                        }
                    }
                } catch (error: Exception) { if (open.get()) failure = error }
            }, "usb-ncm-$index").apply { isDaemon = true; start() }
        }
    }

    override fun receive(timeoutMillis: Long): ByteArray? {
        checkOpen()
        return incoming.poll(timeoutMillis.coerceIn(1, 250), TimeUnit.MILLISECONDS).also { checkOpen() }
    }

    override fun send(frame: ByteArray) {
        checkOpen()
        require(frame.size >= 14)
        val target = if (frame[0].toInt() and 1 != 0) null else synchronized(peers) { peers[mac(frame, 0)] }
        if (target != null) ports[target].send(frame) else ports.forEach { it.send(frame) }
    }

    private fun checkOpen() {
        failure?.let { throw IOException("USB NCM receive failed", it) }
        if (!open.get()) throw IOException("USB NCM closed")
    }

    override fun close() {
        if (!open.compareAndSet(true, false)) return
        ports.forEach { runCatching { it.close() } }
        readers.forEach { it.interrupt() }
        readers.filter { it !== Thread.currentThread() }.forEach { runCatching { it.join(500) } }
        incoming.clear()
    }

    private fun mac(frame: ByteArray, offset: Int): Long = (offset until offset + 6).fold(0L) { n, i ->
        (n shl 8) or (frame[i].toLong() and 255)
    }
}
