package com.dashplay.android

/** A bounded retry budget for a known temporary association failure, not a general network selector. */
internal class WifiRecoveryPolicy {
    private var failureSince: Long? = null
    private var stableSince: Long? = null
    private var retryAt = 0L
    var attempts = 0; private set

    fun connected(now: Long) {
        failureSince = null
        val since = stableSince ?: now.also { stableSince = it }
        if (now - since >= 60_000) { attempts = 0; retryAt = 0 }
    }
    fun retry(now: Long, eligible: Boolean): Boolean {
        stableSince = null
        if (!eligible) { failureSince = null; return false }
        val since = failureSince ?: now.also { failureSince = it }
        if (attempts >= 3 || now - since < 30_000 || now < retryAt) return false
        retryAt = now + longArrayOf(60_000, 120_000, 240_000)[attempts]
        attempts++
        return true
    }
}
