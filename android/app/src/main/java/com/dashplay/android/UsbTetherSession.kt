package com.dashplay.android

import java.io.Closeable
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean

/** Owns one opted-in USB Internet link. A consumer must explicitly install/withdraw its own routes. */
internal class UsbTetherSession(private val wire: Wire,
    private val changed: (UsbTetherProtocol.Lease?, String) -> Unit,
    private val incoming: (ByteArray) -> Unit,
) : Closeable {
    interface Wire : Closeable {
        val hostMac: ByteArray
        fun receive(timeoutMillis: Long): ByteArray?
        fun send(frame: ByteArray)
        fun carrier(): Boolean
    }
    private val running = AtomicBoolean(false)
    private val started = AtomicBoolean(false)
    private val lifecycle = Any()
    private val outgoing = ArrayBlockingQueue<ByteArray>(32)
    private var worker: Thread? = null
    @Volatile var lease: UsbTetherProtocol.Lease? = null; private set
    val isRunning: Boolean get() = running.get()

    fun start() {
        synchronized(lifecycle) {
            check(started.compareAndSet(false, true)) { "USB 热点会话不能重复启动" }
            running.set(true)
            worker = Thread(::run, "iphone-usb-internet").apply { isDaemon = true; start() }
        }
    }
    fun sendIpv4(packet: ByteArray): Boolean = running.get() && lease != null && packet.size in 20..1500 && outgoing.offer(packet.copyOf())
    private fun run() {
        val link = UsbTetherLink(wire.hostMac, wire::send, { value, message ->
            synchronized(lifecycle) {
                if (running.get()) { lease = value; changed(value, message) }
            }
        }, { synchronized(lifecycle) { if (running.get()) incoming(it) } })
        var nextCarrier = 0L
        var terminal = "USB 热点已关闭"
        try {
            while (running.get()) {
                val frame = wire.receive(50)
                val now = System.nanoTime() / 1_000_000
                if (!running.get()) break
                if (now >= nextCarrier) {
                    val available = wire.carrier()
                    if (!running.get()) break
                    link.carrier(available, now); nextCarrier = now + 1000
                }
                if (frame != null) link.receive(frame, now)
                link.tick(now)
                repeat(8) { outgoing.poll()?.let { link.sendIpv4(it, now) } }
            }
        } catch (_: Exception) {
            terminal = "USB 热点连接中断，请检查个人热点和数据线"
        } finally {
            stopped(terminal)
            link.close(); runCatching { wire.close() }
        }
    }
    private fun stopped(message: String) = synchronized(lifecycle) {
        started.set(true); lease = null; outgoing.clear()
        if (running.getAndSet(false)) runCatching { changed(null, message) }
    }
    override fun close() {
        // Also owns an opened wire if the caller cancels before start().
        stopped("USB 热点已关闭")
        runCatching { wire.close() }
        worker?.takeIf { it !== Thread.currentThread() }?.let {
            runCatching { it.join(1500) }
            if (it.isAlive) it.interrupt()
        }
    }
}
