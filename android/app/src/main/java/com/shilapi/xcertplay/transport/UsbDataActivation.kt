package com.shilapi.xcertplay.transport

import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Keeps bulk I/O off an inactive alternate and serializes activation against teardown. */
internal class UsbDataActivation(initiallyActive: Boolean) {
    private val lock = Any()
    private val ready = CountDownLatch(if (initiallyActive) 0 else 1)
    @Volatile private var active = initiallyActive
    @Volatile private var closed = false

    fun activate(select: () -> Unit) = synchronized(lock) {
        if (closed) throw IOException("NCM activation was cancelled")
        if (!active) {
            select()
            active = true
            ready.countDown()
        }
    }

    fun isActive(): Boolean {
        if (closed) throw IOException("NCM activation was cancelled")
        return active
    }

    fun await(timeoutMillis: Long): Boolean {
        ready.await(timeoutMillis, TimeUnit.MILLISECONDS)
        return isActive()
    }

    fun close() = synchronized(lock) {
        closed = true
        ready.countDown()
    }
}
