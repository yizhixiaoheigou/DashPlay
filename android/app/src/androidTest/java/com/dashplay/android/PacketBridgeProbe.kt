package com.dashplay.android

import android.os.Bundle
import android.os.ParcelFileDescriptor
import android.system.Os
import android.system.OsConstants
import java.io.FileDescriptor
import java.io.FileOutputStream
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/** Real idle kernel reads, without VPN routes, USB devices or application settings. */
internal object PacketBridgeProbe {
    private fun readers() = Thread.getAllStackTraces().keys.filter { it.isAlive && it.name == "dashplay-usb-out" }.toSet()

    fun run(result: Bundle) {
        val baseline = readers()
        repeat(6) { round ->
            val pipe = ParcelFileDescriptor.createPipe()
            val peer = FileOutputStream(pipe[1].fileDescriptor)
            val closed = AtomicInteger()
            val failed = AtomicInteger()
            val wire = object : UsbCarPlayWire {
                override fun receive(timeoutMillis: Long): ByteArray? { Thread.sleep(25); return null }
                override fun send(frame: ByteArray) = Unit
                override fun close() { closed.incrementAndGet() }
            }
            val bridge = PacketBridge(wire, pipe[0], ByteArray(6), null, null, { _, _ -> }, { failed.incrementAndGet() })
            try {
                bridge.start()
                val deadline = System.nanoTime() + 2_000_000_000L
                while ((readers() - baseline).none { thread -> thread.stackTrace.any { it.methodName in listOf("read", "poll") } }) {
                    check(System.nanoTime() < deadline) { "reader did not enter kernel I/O" }
                    Thread.sleep(10)
                }
                val start = System.nanoTime()
                bridge.close()
                bridge.close()
                val elapsed = (System.nanoTime() - start) / 1_000_000
                check((readers() - baseline).isEmpty()) { "idle reader leaked after close (round=$round, closeMs=$elapsed)" }
                check(closed.get() == 1 && failed.get() == 0) { "close must be idempotent and must not signal failure" }
                check(elapsed < 1500) { "idle close took ${elapsed}ms" }
                result.putLong("idle_close_${round}_ms", elapsed)
            } finally {
                bridge.close()
                // A failing pre-fix reader still holds the pipe after close. Wake it for test cleanup only.
                runCatching { peer.write(0) }
                runCatching { pipe[1].close() }
            }
        }
        packetIo(result)
        result.putBoolean("success", true)
    }

    private fun packetIo(result: Bundle) {
        val local = FileDescriptor()
        val peer = FileDescriptor()
        // Linux defines SOCK_NONBLOCK as O_NONBLOCK; only the latter is public on old Android.
        Os.socketpair(OsConstants.AF_UNIX, OsConstants.SOCK_DGRAM or OsConstants.O_NONBLOCK, 0, local, peer)
        val owned = ParcelFileDescriptor.dup(local)
        Os.close(local)
        val packets = TunPacketIo(owned)
        val sending = AtomicBoolean(true)
        val failure = AtomicReference<Throwable?>()
        var writer: Thread? = null
        try {
            val packet = ByteArray(1400) { (it % 251).toByte() }
            val buffer = ByteArray(1600)
            Os.write(peer, packet, 0, packet.size)
            check(packets.read(buffer) == packet.size && buffer.copyOf(packet.size).contentEquals(packet))
            packets.write(packet, 0, packet.size)
            check(Os.read(peer, buffer, 0, buffer.size) == packet.size && buffer.copyOf(packet.size).contentEquals(packet))
            writer = Thread({
                try { while (sending.get()) packets.write(packet, 0, packet.size) }
                catch (error: Throwable) { failure.set(error) }
            }, "test-tun-backpressure").also { it.start() }
            val deadline = System.nanoTime() + 3_000_000_000L
            while (writer.stackTrace.none { it.methodName == "poll" }) {
                check(System.nanoTime() < deadline && failure.get() == null) { "writer did not reach backpressure" }
                Thread.sleep(10)
            }
            sending.set(false)
            val start = System.nanoTime()
            packets.close()
            writer.join(1000)
            check(!writer.isAlive && failure.get() == null) { "backpressured writer did not stop cleanly" }
            val elapsed = (System.nanoTime() - start) / 1_000_000
            check(elapsed < 1500)
            result.putLong("backpressure_close_ms", elapsed)
            result.putString("packet_integrity", "bidirectional datagrams unchanged")
        } finally {
            sending.set(false)
            packets.close()
            Os.close(peer)
            writer?.join(1000)
        }
    }
}

/** Can run in a separate app_process on vendor Android without stopping the real receiver. */
object PacketBridgeProbeMain {
    @JvmStatic fun main(args: Array<String>) {
        try {
            val result = Bundle()
            PacketBridgeProbe.run(result)
            println(result)
        } catch (error: Throwable) {
            println("PROBE_FAILED ${error.javaClass.simpleName}: ${error.message}")
            kotlin.system.exitProcess(1)
        }
    }
}
