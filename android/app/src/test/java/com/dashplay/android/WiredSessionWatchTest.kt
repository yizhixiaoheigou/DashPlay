package com.dashplay.android

import com.dashplay.android.WiredSessionWatch.Step
import org.junit.Assert.assertEquals
import org.junit.Test

class WiredSessionWatchTest {
    private val s = 1_000_000_000L
    private fun watch() = WiredSessionWatch(startedAt = 0L)

    @Test fun waitsForTheFirstSessionThenGivesUpAfterTheStartTimeout() {
        val w = watch()
        assertEquals(Step.WAITING, w.step(active = false, now = 59 * s))
        assertEquals(Step.RESTART, w.step(active = false, now = 60 * s))
    }

    @Test fun retriesAcceptedStartWithoutTearingDownThePendingUsbNetwork() {
        val w = watch()
        assertEquals(Step.WAITING, w.step(false, 2 * s, true))
        assertEquals(Step.RETRY_START, w.step(false, 3 * s, true))
        assertEquals(Step.WAITING, w.step(false, 4 * s, true))
        assertEquals(Step.RETRY_START, w.step(false, 6 * s, true))
        assertEquals(Step.RESTART, w.step(false, 60 * s, true))
    }

    @Test fun neverRetriesBeforeThePhonesAvailabilityOrAfterASessionWasLive() {
        val w = watch()
        assertEquals(Step.WAITING, w.step(false, 20 * s, false))
        assertEquals(Step.RETRY_START, w.step(false, 25 * s, true))
        assertEquals(Step.LIVE, w.step(true, 26 * s, true))
        assertEquals(Step.GONE, w.step(false, 50 * s, true))
        assertEquals(Step.RESTART, w.step(false, 60 * s, true))
    }

    @Test fun displayReinviteFallsBackWithoutWaitingTheFullInitialConnectTimeout() {
        val w = WiredSessionWatch(startedAt = 0L, startWaitNanos = 10 * s)
        assertEquals(Step.RETRY_START, w.step(false, 3 * s, true))
        assertEquals(Step.RETRY_START, w.step(false, 6 * s, true))
        assertEquals(Step.RETRY_START, w.step(false, 9 * s, true))
        assertEquals(Step.RESTART, w.step(false, 10 * s, true))
    }

    @Test fun staysLiveWhileTheSessionLastsWellPastTheStartTimeout() {
        val w = watch()
        assertEquals(Step.LIVE, w.step(active = true, now = 2 * s))
        assertEquals(Step.LIVE, w.step(active = true, now = 3600 * s))
    }

    /** dp-0017 2026-10-31: the phone's personal hotspot toggled, the session ended, iAP2 stayed up, the page waited forever. */
    @Test fun aSessionThatEndsAndStaysGoneIsStartedOverAfterTheGrace() {
        val w = watch()
        w.step(active = true, now = 2 * s)
        assertEquals(Step.GONE, w.step(active = false, now = 100 * s))
        assertEquals(Step.GONE, w.step(active = false, now = 109 * s))
        assertEquals(Step.RESTART, w.step(active = false, now = 110 * s))
    }

    @Test fun aSessionThatComesBackWithinTheGraceIsKeptAndTheGraceStartsAgain() {
        val w = watch()
        w.step(active = true, now = 2 * s)
        assertEquals(Step.GONE, w.step(active = false, now = 100 * s))
        assertEquals(Step.LIVE, w.step(active = true, now = 108 * s))
        assertEquals(Step.GONE, w.step(active = false, now = 200 * s))
        assertEquals(Step.GONE, w.step(active = false, now = 209 * s))
        assertEquals(Step.RESTART, w.step(active = false, now = 210 * s))
    }
}
