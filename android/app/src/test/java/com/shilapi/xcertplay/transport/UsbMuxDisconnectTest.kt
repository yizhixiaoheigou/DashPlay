package com.shilapi.xcertplay.transport

import java.io.IOException
import java.nio.ByteBuffer
import java.util.concurrent.CountDownLatch
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.*
import org.junit.Test

/** Real mux reader thread and pending TCP connect; only the physical bulk pipe is a fixture. */
class UsbMuxDisconnectTest {
    @Test fun unplugIOExceptionClosesMuxAndWakesPendingConnectWithoutUncaughtException() {
        readerFailure(IOException("USB disconnected"), false)
    }

    @Test fun failedCleanupDoesNotTurnTransportFailureIntoAnUncaughtThreadException() {
        readerFailure(IphoneUsbException.DeviceUnavailable("USB disconnected"), true)
    }

    @Test fun existingTransportFailureKeepsItsTypeAndIdentity() {
        readerFailure(IphoneUsbException.PermissionDenied("USB permission revoked"), false)
    }

    @Test fun writeIOExceptionClosesMuxAndIsReportedAsDeviceUnavailable() {
        val original = IOException("USB write disconnected")
        val pipe = Pipe(writeFailure = original)
        val host = Iap2UsbMuxHost.open(pipe, 30)
        try {
            assertTrue(pipe.reading.await(2, TimeUnit.SECONDS))
            val failure = assertThrows(IphoneUsbException.DeviceUnavailable::class.java) { host.connect(timeoutMillis = 3000) }
            assertSame(original, failure.cause)
            assertTrue(pipe.closed.get())
            assertSame(failure, assertThrows(IphoneUsbException.DeviceUnavailable::class.java) { host.connect() })
        } finally { host.close() }
        pipe.reader.get()?.join(1000)
        assertNull(pipe.uncaught.get())
    }

    private fun readerFailure(original: IOException, closeFails: Boolean) {
        val pipe = Pipe(original, closeFails)
        val host = Iap2UsbMuxHost.open(pipe, 30)
        val waiting = FutureTask<Throwable?> {
            try { host.connect(timeoutMillis = 5000); null } catch (error: Exception) { error }
        }
        val connector = Thread(waiting, "test-pending-usbmux-connect").apply { isDaemon = true; start() }
        try {
            assertTrue(pipe.reading.await(2, TimeUnit.SECONDS))
            assertTrue(pipe.syn.await(2, TimeUnit.SECONDS))
            pipe.release.countDown()
            pipe.reader.get()!!.join(2000)
            assertFalse("reader must terminate", pipe.reader.get()!!.isAlive)
            assertNull("unplug must not reach Android's uncaught exception handler", pipe.uncaught.get())
            assertTrue("failed pipe must be closed", pipe.closed.get())
            val failure = waiting.get(1, TimeUnit.SECONDS)
            if (original is IphoneUsbException) assertSame(original, failure)
            else {
                assertTrue(failure is IphoneUsbException.DeviceUnavailable)
                assertSame(original, failure!!.cause)
            }
            assertSame(failure, assertThrows(IphoneUsbException::class.java) { host.connect() })
        } finally {
            pipe.release.countDown()
            runCatching { host.close() }
            connector.join(1000)
        }
    }

    private class Pipe(
        val readFailure: IOException? = null,
        val closeFails: Boolean = false,
        val writeFailure: IOException? = null,
    ) : Iap2UsbSession {
        private val handshake = AtomicBoolean(true)
        val closed = AtomicBoolean(false)
        val reading = CountDownLatch(1)
        val release = CountDownLatch(1)
        val syn = CountDownLatch(1)
        val reader = AtomicReference<Thread>()
        val uncaught = AtomicReference<Throwable>()
        override fun read(timeoutMillis: Long): ByteArray? {
            if (handshake.compareAndSet(true, false)) return ByteBuffer.allocate(20).putInt(0).putInt(20).putInt(2).array()
            reader.set(Thread.currentThread())
            Thread.currentThread().uncaughtExceptionHandler = Thread.UncaughtExceptionHandler { _, error -> uncaught.set(error) }
            reading.countDown()
            release.await()
            if (!closed.get()) readFailure?.let { throw it }
            return null
        }
        override fun write(data: ByteArray, timeoutMillis: Int) {
            if (ByteBuffer.wrap(data).int == 6) {
                syn.countDown()
                writeFailure?.let { throw it }
            }
        }
        override fun close() {
            closed.set(true)
            release.countDown()
            if (closeFails) throw IOException("USB close disconnected")
        }
    }
}
