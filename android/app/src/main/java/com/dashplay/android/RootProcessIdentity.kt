package com.dashplay.android

import java.io.File

/** A persisted lease alone cannot revoke a delayed Root request when Android kills the caller. */
internal data class RootProcessIdentity(val pid: Int, val startTicks: Long, private val procRoot: String = "/proc") {
    init {
        require(pid > 0 && startTicks >= 0)
        require(procRoot.matches(Regex("/[A-Za-z0-9_./-]+")))
    }

    fun shellFunction(): String = """
        process_alive() {
          owner_stat=${'$'}(cat '$procRoot/$pid/stat' 2>/dev/null) || return 1
          owner_fields=${'$'}{owner_stat##*) }
          set -- ${'$'}owner_fields
          [ ${'$'}# -ge 20 ] || return 1
          case "${'$'}1" in Z|X|x) return 1;; esac
          shift 19
          [ "${'$'}1" = '$startTicks' ]
        }
    """.trimIndent() + "\n"

    companion object {
        fun current(): RootProcessIdentity = fromStat(android.os.Process.myPid(), File("/proc/self/stat").readText())

        /** procRoot is a JVM fixture path only; this identity is never deserialized from a journal. */
        fun fromStat(pid: Int, stat: String, procRoot: String = "/proc"): RootProcessIdentity {
            require(stat.startsWith("$pid ("))
            // comm can contain spaces and closing parentheses; fields start after its final ') '.
            val fields = stat.substringAfterLast(") ", "").trim().split(Regex("\\s+"))
            require(fields.size >= 20 && fields[0].length == 1 && fields[0] !in listOf("Z", "X", "x"))
            return RootProcessIdentity(pid, fields[19].toLong(), procRoot)
        }
    }
}
