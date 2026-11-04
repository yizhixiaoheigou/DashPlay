package com.dashplay.android

import com.shilapi.xcertplay.airplay.RtpAudioOrder
import org.junit.Assert.*
import org.junit.Test

class RtpAudioOrderTest {
    private var time = 0L
    private val played = mutableListOf<Int>()
    private fun queue() = RtpAudioOrder<Int>({ played += it }, { time })

    @Test fun contiguousPacketsAreNotDelayed() {
        val q = queue()
        repeat(100) { q.offer(it, it); assertEquals(it + 1, played.size) }
        assertEquals(0, q.queued)
    }
    @Test fun screenOffBurstsAreRestoredBeforePlayback() {
        val q = queue(); q.offer(0, 0)
        for (n in 18 downTo 2) q.offer(n, n)
        assertEquals(listOf(0), played)
        q.offer(1, 1)
        assertEquals((0..18).toList(), played)
        assertEquals(17L, q.reordered)
        assertEquals(0L, q.missing)
    }
    @Test fun repeatedAndLatePacketsAreNeverPlayedTwice() {
        val q = queue(); q.offer(10, 10); q.offer(12, 12); q.offer(12, 12)
        q.offer(11, 11); q.offer(10, 10)
        assertEquals(listOf(10, 11, 12), played)
        assertEquals(2L, q.lateDropped)
    }
    @Test fun sequenceWrapDoesNotLookLikeLoss() {
        val q = queue(); q.offer(65534, 65534); q.offer(0, 0); q.offer(65535, 65535); q.offer(1, 1)
        assertEquals(listOf(65534, 65535, 0, 1), played)
        assertEquals(0L, q.missing)
    }
    @Test fun missingPacketExpiresEvenWhenNoMorePacketsArrive() {
        val q = queue(); q.offer(0, 0); q.offer(2, 2)
        time = 79_000_000; q.flush(); assertEquals(listOf(0), played)
        time = 80_000_000; q.flush(); assertEquals(listOf(0, 2), played)
        q.offer(1, 1); assertEquals(listOf(0, 2), played)
        assertEquals(1L, q.missing)
    }
    @Test fun prolongedLossCannotGrowTheQueueWithoutBound() {
        val q = queue(); q.offer(0, 0)
        for (n in 2..200) { q.offer(n, n); assertTrue(q.queued <= 64) }
        time = 80_000_000; q.flush()
        assertEquals(listOf(0) + (2..200).toList(), played)
    }
    @Test fun independentStreamsDoNotShareTheirSequenceOrClock() {
        val q = queue(); val otherPlayed = mutableListOf<Int>()
        val other = RtpAudioOrder<Int>({otherPlayed += it}, {time})
        q.offer(100, 100); q.offer(102, 102); other.offer(900, 900); other.offer(901, 901)
        assertEquals(listOf(900, 901), otherPlayed)
        q.offer(101, 101); assertEquals(listOf(100, 101, 102), played)
    }
}
