package com.dashplay.android

import android.os.ParcelFileDescriptor
import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import android.system.StructPollfd
import java.io.Closeable
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean

/**
 * A blocking read holds a kernel TUN reference even after another thread closes its descriptor.
 * Use nonblocking I/O with bounded readiness waits, then close only after both I/O paths have exited.
 * The caller obtains a nonblocking TUN with VpnService.Builder.setBlocking(false) (API 21).
 * Separate locks retain full duplex and prevent a closing/reused descriptor from reaching a syscall.
 */
internal class TunPacketIo(private val tun: ParcelFileDescriptor) : Closeable {
    private val open = AtomicBoolean(true)
    private val reads = Any()
    private val writes = Any()
    private val fd = tun.fileDescriptor

    /** null means local shutdown, not a transport failure. */
    fun read(buffer: ByteArray): Int? = synchronized(reads) {
        while (open.get()) {
            if (!ready(OsConstants.POLLIN)) continue
            try {
                val count = Os.read(fd, buffer, 0, buffer.size)
                if (count <= 0) throw IOException("USB VPN 已关闭")
                return@synchronized count
            } catch (error: ErrnoException) {
                if (!retry(error)) throw IOException("USB VPN 读取失败", error)
            }
        }
        null
    }

    fun write(bytes: ByteArray, offset: Int, length: Int) = synchronized(writes) {
        while (open.get()) {
            try {
                // Try immediately; poll only when the kernel cannot accept the packet yet.
                val count = Os.write(fd, bytes, offset, length)
                if (count != length) throw IOException("USB VPN 数据包写入不完整")
                return@synchronized
            } catch (error: ErrnoException) {
                if (!retry(error)) throw IOException("USB VPN 写入失败", error)
                ready(OsConstants.POLLOUT)
            }
        }
    }

    private fun ready(events: Int): Boolean {
        if (!open.get()) return false
        val poll = StructPollfd().also { it.fd = fd; it.events = events.toShort() }
        try {
            Os.poll(arrayOf(poll), 100)
        } catch (error: ErrnoException) {
            if (error.errno != OsConstants.EINTR) throw IOException("USB VPN 等待失败", error)
            return false
        }
        if (!open.get()) return false
        if (poll.revents.toInt() and (OsConstants.POLLERR or OsConstants.POLLNVAL) != 0) {
            throw IOException("USB VPN 接口已关闭")
        }
        return poll.revents.toInt() and (events or OsConstants.POLLHUP) != 0
    }

    private fun retry(error: ErrnoException) = error.errno == OsConstants.EAGAIN || error.errno == OsConstants.EINTR

    override fun close() {
        if (!open.compareAndSet(true, false)) return
        synchronized(reads) { synchronized(writes) { tun.close() } }
    }
}
