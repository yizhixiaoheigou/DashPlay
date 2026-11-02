package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test
import java.util.PriorityQueue

class HotspotControllerTest {
    private class Clock {
        data class Task(val at: Long, val id: Long, val run: () -> Unit, var cancelled: Boolean = false)
        var now = 0L; private var ids = 0L
        private val tasks = PriorityQueue<Task>(compareBy<Task> { it.at }.thenBy { it.id })
        fun later(delay: Long, action: () -> Unit): HotspotController.Cancel {
            val task = Task(now + delay, ++ids, action); tasks += task
            return HotspotController.Cancel { task.cancelled = true }
        }
        fun advance(ms: Long) {
            val end = now + ms; var steps = 0
            while (tasks.isNotEmpty() && tasks.peek()!!.at <= end) {
                val task = tasks.remove(); now = task.at
                if (!task.cancelled) { check(++steps < 1000) { "unbounded retry" }; task.run() }
            }
            now = end
        }
    }
    private class Driver : HotspotController.Driver<String> {
        var group: HotspotController.Group<String>? = null
        var problem: String? = null
        var creates = 0; var removes = 0; var closes = 0
        var holdCreate = false; var holdInspect = false
        var createResult: ((Result<Unit>) -> Unit)? = null
        var inspectResult: ((Result<HotspotController.Group<String>?>) -> Unit)? = null
        override fun unavailable() = problem
        override fun inspect(done: (Result<HotspotController.Group<String>?>) -> Unit) {
            if (holdInspect) inspectResult = done else done(Result.success(group))
        }
        override fun create(done: (Result<Unit>) -> Unit) {
            creates++
            if (holdCreate) createResult = done else { group = HotspotController.Group(true, "wlan2/192.0.2.1/149"); done(Result.success(Unit)) }
        }
        override fun remove(done: (Result<Unit>) -> Unit) { check(group?.owned == true); removes++; group = null; done(Result.success(Unit)) }
        override fun close() { closes++ }
    }
    private fun fixture(test: (Clock, Driver, HotspotController<String>) -> Unit) {
        val clock = Clock(); val driver = Driver()
        test(clock, driver, HotspotController(driver, { clock.now }, clock::later) {})
    }
    @Test fun restartAdoptsMatchingGroupAndConfigurationRestartKeepsIt() = fixture { time, io, state ->
        io.group = HotspotController.Group(true, "saved")
        state.configure(true); time.advance(0)
        assertEquals("saved", state.connection); assertEquals(0, io.creates)
        state.configure(true, retry = true); time.advance(2000)
        assertEquals("saved", state.connection); assertEquals(0, io.removes)
        state.close(); time.advance(0)
        assertEquals(1, io.removes); assertEquals(HotspotController.Phase.OFF, state.phase)
    }
    @Test fun lostGroupAndWifiRecoveryRecreateWithoutUserRetry() = fixture { time, io, state ->
        state.configure(true); time.advance(1000); assertNotNull(state.connection)
        io.problem = "Wi-Fi off"; io.group = null; time.advance(2000)
        assertNull(state.connection); assertEquals(HotspotController.Phase.FAILED, state.phase)
        io.problem = null; time.advance(6000)
        assertEquals(2, io.creates); assertNotNull(state.connection)
        io.group = null; time.advance(3000)
        assertEquals(3, io.creates); assertNotNull(state.connection)
    }
    @Test fun otherAppsGroupIsNeitherAdoptedNorRemoved() = fixture { time, io, state ->
        io.group = HotspotController.Group(false, "foreign")
        state.configure(true); time.advance(15000)
        assertNull(state.connection); assertEquals(0, io.creates); assertEquals(0, io.removes)
        state.configure(false); time.advance(0)
        assertEquals("foreign", io.group?.connection); assertEquals(HotspotController.Phase.OFF, state.phase)
        io.group = null; state.configure(true); time.advance(1000)
        assertNotNull(state.connection)
    }
    @Test fun disableDuringCreateCleansLateOwnedGroup() = fixture { time, io, state ->
        io.holdCreate = true; state.configure(true); time.advance(0)
        state.configure(false); time.advance(0)
        io.group = HotspotController.Group(true, "late")
        io.createResult!!(Result.success(Unit)); time.advance(1000)
        assertEquals(1, io.removes); assertNull(io.group); assertNull(state.connection)
        assertEquals(HotspotController.Phase.OFF, state.phase)
    }
    @Test fun timedOutCreateStillCleansUpIfItSucceedsAfterDisable() = fixture { time, io, state ->
        io.holdCreate = true; state.configure(true); time.advance(0)
        val late = io.createResult!!
        time.advance(10000); state.configure(false); time.advance(0)
        assertEquals(HotspotController.Phase.OFF, state.phase)
        io.group = HotspotController.Group(true, "late after timeout")
        late(Result.success(Unit)); time.advance(0)
        assertEquals(1, io.removes); assertNull(io.group); assertEquals(HotspotController.Phase.OFF, state.phase)
    }
    @Test fun timedOutOldCallbackCannotOverwriteNewState() = fixture { time, io, state ->
        io.holdInspect = true; state.configure(true); time.advance(0)
        val old = io.inspectResult!!
        time.advance(10000); assertEquals(HotspotController.Phase.FAILED, state.phase)
        io.holdInspect = false; time.advance(3000); assertNotNull(state.connection)
        old(Result.success(HotspotController.Group(false, "stale"))); time.advance(0)
        assertNotNull(state.connection); assertEquals(1, io.creates); assertEquals(0, io.removes)
    }
    @Test fun channelLossReopensAndAdoptsWithoutRemovingWorkingGroup() = fixture { time, io, state ->
        state.configure(true); time.advance(1000)
        state.disconnected(); assertNull(state.connection)
        time.advance(2000)
        assertNotNull(state.connection); assertEquals(1, io.creates); assertEquals(0, io.removes)
    }
    @Test fun unreadyOwnedGroupIsRecreatedAfterBoundedWait() = fixture { time, io, state ->
        io.group = HotspotController.Group(true, null)
        state.configure(true); time.advance(35000)
        assertEquals(1, io.removes); assertEquals(1, io.creates); assertNotNull(state.connection)
    }
    @Test fun stoppingWithLostPermissionIsBoundedAndNotFalselyReportedOff() = fixture { time, io, state ->
        state.configure(true); time.advance(1000)
        io.problem = "permission denied"; state.close(); time.advance(35000)
        assertEquals(HotspotController.Phase.FAILED, state.phase); assertEquals(0, io.removes); assertTrue(io.closes > 0)
        val before = io.creates; io.problem = null; time.advance(60000)
        assertEquals(before, io.creates); assertNull(state.connection)
    }
    @Test fun nextGenerationWaitsForAsynchronousOwnedGroupCleanup() = fixture { time, io, state ->
        state.configure(true); time.advance(1000)
        io.holdInspect = true
        var nextStarted = false
        state.close { assertNull(io.group); nextStarted = true }
        time.advance(0)
        assertFalse(nextStarted)
        val pending = io.inspectResult!!
        io.holdInspect = false
        pending(Result.success(io.group)); time.advance(0)
        assertTrue(nextStarted); assertEquals(1, io.removes)
        var completedAgain = false
        state.close { completedAgain = true }
        assertTrue(completedAgain)
    }
    @Test fun repeatedCloseDoesNotExtendPermissionFailureDeadlineOrHoldNextGeneration() = fixture { time, io, state ->
        state.configure(true); time.advance(1000)
        io.problem = "permission denied"
        var completions = 0
        state.close { completions++ }
        time.advance(20000)
        state.close { completions++ }
        time.advance(10000)
        assertEquals(2, completions); assertEquals(HotspotController.Phase.FAILED, state.phase)
        assertEquals(0, io.removes)
    }
    @Test fun disabledStartupCleansOnlyPreviouslyOwnedGroup() = fixture { time, io, state ->
        io.group = HotspotController.Group(true, "previous process")
        state.configure(false); time.advance(0)
        assertEquals(1, io.removes); assertEquals(0, io.creates); assertEquals(HotspotController.Phase.OFF, state.phase)
    }
}
