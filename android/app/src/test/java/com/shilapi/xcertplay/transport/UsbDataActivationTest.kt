package com.shilapi.xcertplay.transport

import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import org.junit.Assert.*
import org.junit.Test

class UsbDataActivationTest {
    @Test fun inactiveDataCannotBeReadBeforePreparationCompletes() {
        val gate = UsbDataActivation(false)
        assertFalse(gate.await(1))
        gate.activate { assertFalse(gate.isActive()) }
        assertTrue(gate.await(1))
    }

    @Test fun repeatedReadyCallbacksDoNotResetAnActiveInterface() {
        val gate = UsbDataActivation(false)
        var selections = 0
        repeat(3) { gate.activate { selections++ } }
        assertEquals(1, selections)
    }

    @Test fun compatibilityPathDoesNotSelectAnAlreadyActiveAlternate() {
        val gate = UsbDataActivation(true)
        gate.activate { fail("must not reset an active interface") }
        assertTrue(gate.await(1))
    }

    @Test fun rejectedAlternateDoesNotReleaseReaders() {
        val gate = UsbDataActivation(false)
        try { gate.activate { throw IOException("rejected") }; fail() } catch (_: IOException) { }
        assertFalse(gate.await(1))
    }

    @Test fun closeWakesPendingReaderAndPreventsLaterUsbCalls() {
        val gate = UsbDataActivation(false)
        val started = CountDownLatch(1)
        val ended = CountDownLatch(1)
        val cancelled = AtomicBoolean(false)
        val reader = Thread {
            started.countDown()
            try { gate.await(10_000) } catch (_: IOException) { cancelled.set(true) }
            finally { ended.countDown() }
        }.apply { isDaemon = true; start() }
        assertTrue(started.await(1, TimeUnit.SECONDS))
        gate.close()
        assertTrue(ended.await(1, TimeUnit.SECONDS))
        assertTrue(cancelled.get())
        try { gate.activate { fail("closed USB must not be touched") }; fail() } catch (_: IOException) { }
        reader.join(100)
    }
}
