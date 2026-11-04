package com.dashplay.android

/**
 * Watches one wired CarPlay attempt after iAP2 is ready.
 *
 * Before the first session: give the iPhone [startWaitNanos] to open CarPlay, then start the cable over.
 * After it was live: a session that goes away (the owner toggled the phone's personal hotspot, iOS reset the
 * USB network) is not reopened by the iPhone while iAP2 stays up, so the picture would wait forever. If it
 * stays gone for [graceNanos], start the cable over; if it comes back sooner, keep it.
 */
internal class WiredSessionWatch(
    startedAt: Long,
    private val startWaitNanos: Long = 60_000_000_000L,
    private val graceNanos: Long = 10_000_000_000L,
    private val startRetryNanos: Long = 3_000_000_000L,
) {
    enum class Step { WAITING, RETRY_START, LIVE, GONE, RESTART }

    private val deadline = startedAt + startWaitNanos
    private var nextStartRetry = startedAt + startRetryNanos
    private var live = false
    private var goneSince: Long? = null

    fun step(active: Boolean, now: Long, startWasSent: Boolean = false): Step {
        if (active) { live = true; goneSince = null; return Step.LIVE }
        if (!live) {
            if (now >= deadline) return Step.RESTART
            // iAP2 authenticates before NCM is ready. Repeat the accepted start request on
            // the same connection; rebuilding USB here can restart network setup indefinitely.
            if (startWasSent && now >= nextStartRetry) {
                nextStartRetry = now + startRetryNanos
                return Step.RETRY_START
            }
            return Step.WAITING
        }
        val since = goneSince ?: now.also { goneSince = it }
        return if (now - since >= graceNanos) Step.RESTART else Step.GONE
    }
}
