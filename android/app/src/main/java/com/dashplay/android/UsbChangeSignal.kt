package com.dashplay.android

/** Generation checks prevent an attach between enumeration and waiting from being lost. */
internal class UsbChangeSignal {
    private val monitor = Object()
    private var generation = 0L
    private var closed = false

    fun snapshot(): Long = synchronized(monitor) { generation }
    fun changed() = synchronized(monitor) { generation++; monitor.notifyAll() }
    fun close() = synchronized(monitor) { closed = true; monitor.notifyAll() }

    fun awaitChange(previous: Long, timeoutMillis: Long) = synchronized(monitor) {
        val deadline = System.nanoTime() + timeoutMillis * 1_000_000
        while (!closed && generation == previous) {
            val remaining = deadline - System.nanoTime()
            if (remaining <= 0) break
            monitor.wait(remaining / 1_000_000, (remaining % 1_000_000).toInt())
        }
    }

    fun awaitReady(timeoutMillis: Long, ready: () -> Boolean): Boolean {
        val deadline = System.nanoTime() + timeoutMillis * 1_000_000
        while (!synchronized(monitor) { closed }) {
            val previous = snapshot()
            if (ready()) return true
            val remaining = (deadline - System.nanoTime()) / 1_000_000
            if (remaining <= 0) return false
            // Some OEMs omit USB broadcasts; retain bounded polling as a fallback.
            awaitChange(previous, minOf(remaining, 250))
        }
        return false
    }
}
