package com.dashplay.android

/** Compatibility for Android toolbox id and legacy xtables, without weakening Root checks. */
internal object RootShell {
    const val requireRoot = "root_identity=\$(id -u 2>/dev/null) || root_identity=\$(id 2>/dev/null) || exit 70\n" +
        "case \"\$root_identity\" in 0|uid=0\\(*|uid=0\\ *) ;; *) exit 70;; esac\n"

    /** Whether this app may run `su` at all. Some ROMs ship it as `-rwsr-x--- root shell`, which only the shell user
     *  may execute: no app can get Root there, whatever syntax it uses (SY910, 2026-11-03). `canExecute` answers for
     *  this app's own uid, so it is the honest question to ask. */
    fun available(): Boolean = listOf("/system/bin/su", "/system/xbin/su", "/sbin/su", "/su/bin/su", "/vendor/bin/su")
        .any { path -> runCatching { java.io.File(path).let { it.exists() && it.canExecute() } }.getOrDefault(false) }

    /** How this device's `su` takes a script. SuperSU/Magisk read `su -c <script>`; the toybox `su` some boxes ship
     *  (SY910, Android 9) only knows `su [UID] COMMAND` and answers `su: invalid uid/gid '-c'` to the first form, which
     *  made every Root feature report "Root 权限、iptables 或规则链不可用" (2026-11-03). Probed once, then cached. */
    private val suForms = listOf(listOf("su", "0", "sh", "-c"), listOf("su", "-c"))
    @Volatile private var suForm: List<String>? = null

    /** Starts [script] as root with the dialect this device accepts; the caller reads stdout and the exit code. */
    fun open(script: String): Process {
        suForm?.let { return ProcessBuilder(it + script).redirectErrorStream(true).start() }
        for (candidate in suForms) {
            val probe = try {
                ProcessBuilder(candidate + "echo DASHPLAY_SU_OK").redirectErrorStream(true).start()
            } catch (_: Exception) {
                continue
            }
            val accepted = runCatching {
                val text = probe.inputStream.bufferedReader().readText()
                probe.waitFor() == 0 && text.contains("DASHPLAY_SU_OK")
            }.getOrDefault(false)
            if (!accepted) continue
            suForm = candidate
            return ProcessBuilder(candidate + script).redirectErrorStream(true).start()
        }
        // Nothing answered the probe (root denied, or no su): use the classic form so the caller sees the real error.
        return ProcessBuilder(suForms.last() + script).redirectErrorStream(true).start()
    }

    /** Older iptables accepts -w but not its timeout argument; use fail-fast locking instead. */
    fun firewallFunctions(ipv6: Boolean = false): String = buildString {
        for ((binary, table) in listOf("iptables" to "nat") + if (ipv6) listOf("ip6tables" to "filter") else emptyList()) {
            val flag = "dashplay_${binary}_wait"
            append("dashplay_$binary() {\n")
            append("  if [ \"\${$flag+x}\" != x ]; then\n")
            // Read-only feature detection. Never retry a failed mutation using different arguments.
            append("    if command $binary -w 2 -t $table -S >/dev/null 2>&1; then $flag='-w 2'; else $flag=''; fi\n")
            append("  fi\n")
            append("  command $binary \$$flag \"\$@\"\n}\n")
        }
    }
}
