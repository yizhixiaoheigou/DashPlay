package com.dashplay.android

/** Compatibility for Android toolbox id and legacy xtables, without weakening Root checks. */
internal object RootShell {
    const val requireRoot = "root_identity=\$(id -u 2>/dev/null) || root_identity=\$(id 2>/dev/null) || exit 70\n" +
        "case \"\$root_identity\" in 0|uid=0\\(*|uid=0\\ *) ;; *) exit 70;; esac\n"

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
