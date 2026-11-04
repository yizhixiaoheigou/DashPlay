package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class UsbChangeSignalTest {
    @Test(timeout = 1000) fun attachBeforeWaitIsNotLost() {
        val signal = UsbChangeSignal()
        val previous = signal.snapshot()
        signal.changed()
        signal.awaitChange(previous, 5000)
    }

    @Test(timeout = 1000) fun alreadyEnumeratedReplacementContinuesImmediately() {
        assertTrue(UsbChangeSignal().awaitReady(3500) { true })
    }

    @Test(timeout = 2000) fun oldDeviceAndDetachAloneDoNotCountAsReady() {
        val signal = UsbChangeSignal()
        val replacement = AtomicBoolean(false)
        val checking = CountDownLatch(1)
        val done = CountDownLatch(1)
        val result = AtomicBoolean(false)
        val worker = Thread {
            result.set(signal.awaitReady(3500) { checking.countDown(); replacement.get() })
            done.countDown()
        }
        worker.start()
        try {
            assertTrue(checking.await(1, TimeUnit.SECONDS))
            signal.changed() // detach, but no replacement yet
            assertFalse(done.await(30, TimeUnit.MILLISECONDS))
            replacement.set(true)
            signal.changed()
            assertTrue(done.await(1, TimeUnit.SECONDS))
            assertTrue(result.get())
        } finally { signal.close(); worker.join(500) }
    }

    @Test(timeout = 2000) fun missingBroadcastFallsBackToEnumeration() {
        val signal = UsbChangeSignal()
        var checks = 0
        assertTrue(signal.awaitReady(1000) { ++checks >= 2 })
    }

    @Test(timeout = 1000) fun unchangedDeviceTimesOut() {
        assertFalse(UsbChangeSignal().awaitReady(30) { false })
    }

    @Test(timeout = 1000) fun closeCancelsWaitIncludingCloseBeforeWait() {
        val signal = UsbChangeSignal()
        signal.close()
        signal.awaitChange(signal.snapshot(), 5000)
        assertFalse(signal.awaitReady(3500) { true })
    }
}
