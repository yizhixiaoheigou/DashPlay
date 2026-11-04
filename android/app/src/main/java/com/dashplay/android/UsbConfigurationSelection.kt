package com.dashplay.android

import java.io.IOException

/** SET_CONFIGURATION on an already selected configuration resets interface state. */
internal object UsbConfigurationSelection {
    data class Result(val before: Int?, val after: Int?, val selectionResult: Boolean?)

    fun ensure(target: Int, current: () -> Int?, select: () -> Boolean): Result {
        require(target in 1..255)
        val before = current()
        if (before == target) return Result(before, before, null)
        val selected = select()
        val after = current()
        if (after != null && after != target) throw IOException("iPhone USB configuration did not change to the required value")
        // Some OEMs return false even though selection succeeded. Unknown readback retains
        // the previous interface-claim/endpoint checks as the authoritative fallback.
        return Result(before, after, selected)
    }
}
