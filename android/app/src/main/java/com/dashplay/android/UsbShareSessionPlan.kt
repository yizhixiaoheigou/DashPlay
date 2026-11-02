package com.dashplay.android

import kotlinx.serialization.Serializable

/** Persisted before any mutation. Interface restoration is restricted to the captured boot and ifindex. */
@Serializable
internal data class UsbShareSessionPlan(val rules: UsbSharePlan, val hotspotIndex: Int, val tunnelIndex: Int,
    val hotspotForwarding: Int, val tunnelForwarding: Int, val manageHotspot: Boolean, val boot: String,
    val systemOffloadApi: Int = 0) {
    init {
        require(rules.guarded && hotspotIndex > 0 && tunnelIndex > 0 && hotspotIndex != tunnelIndex)
        require(hotspotForwarding in 0..1 && tunnelForwarding in 0..1)
        require(manageHotspot || hotspotForwarding == 1)
        require(systemOffloadApi == 0 || !manageHotspot && systemOffloadApi in 27..100)
        require(boot.matches(Regex("[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")))
    }
    /** Paths are injected only by isolated JVM fixtures, never deserialized from a journal. */
    data class Paths(val sys: String = "/sys/class/net", val proc: String = "/proc/sys/net/ipv4/conf",
        val boot: String = "/proc/sys/kernel/random/boot_id") {
        init { for (path in listOf(sys, proc, boot)) require(path.matches(Regex("/[A-Za-z0-9_./-]+"))) }
    }
    private fun restore(paths: Paths, liveLease: String? = null) = buildString {
        if (liveLease != null) require(liveLease.matches(Regex("/[A-Za-z0-9_./-]+")))
        append("restore_interfaces() {\n  [ \"\$(cat '${paths.boot}')\" = '$boot' ] || return 0\n")
        // A delayed old su shell must never undo forwarding that a newer live owner now relies on.
        if (liveLease != null) append("  live=\$(cat '$liveLease' 2>/dev/null || true)\n  [ -z \"\$live\" ] || [ \"\$live\" = '${rules.owner}' ] || return 0\n")
        append("  failed=0\n")
        for ((name, index, original) in listOf(Triple(rules.tunnel, tunnelIndex, tunnelForwarding)) +
            if (manageHotspot) listOf(Triple(rules.hotspot, hotspotIndex, hotspotForwarding)) else emptyList()) {
            if (original == 0) append("  if [ \"\$(cat '${paths.sys}/$name/ifindex' 2>/dev/null)\" = '$index' ] && [ \"\$(cat '${paths.proc}/$name/forwarding' 2>/dev/null)\" = 1 ]; then\n    echo '0' > '${paths.proc}/$name/forwarding' || failed=1\n  fi\n")
        }
        append("  [ \"\$failed\" = 0 ]\n}\n")
    }
    private fun identity(paths: Paths) = "[ \"\$(cat '${paths.boot}')\" = '$boot' ]\n" +
        "[ \"\$(cat '${paths.sys}/${rules.hotspot}/ifindex')\" = '$hotspotIndex' ]\n" +
        "[ \"\$(cat '${paths.sys}/${rules.tunnel}/ifindex')\" = '$tunnelIndex' ]\n"
    fun installScript(lease: String, paths: Paths = Paths()) = buildString {
        require(lease.matches(Regex("/[A-Za-z0-9_./-]+")))
        append("set -eu\n${RootShell.requireRoot}echo 'DASHPLAY_SHARE_STARTED'\n")
        append(identity(paths))
        append(offloadCheck(systemOffloadApi))
        for ((name, original) in listOf(rules.hotspot to hotspotForwarding, rules.tunnel to tunnelForwarding))
            append("[ \"\$(cat '${paths.proc}/$name/forwarding')\" = '$original' ]\n")
        append(restore(paths, lease)); append("trap 'restore_interfaces' EXIT\n")
        for (name in listOf(rules.tunnel) + if (manageHotspot) listOf(rules.hotspot) else emptyList()) {
            append("[ \"\$(cat '$lease' 2>/dev/null)\" = '${rules.owner}' ]\n")
            append(identity(paths))
            append("echo '1' > '${paths.proc}/$name/forwarding'\n")
        }
        append("(\n${rules.installScript(lease)}\n)\n")
        append("trap - EXIT\necho 'DASHPLAY_SHARE_SESSION_ACTIVE'\n")
    }
    fun removeScript(paths: Paths = Paths(), liveLease: String? = null) = "set -eu\n${RootShell.requireRoot}echo 'DASHPLAY_SHARE_STARTED'\n" +
        restore(paths, liveLease) + "if [ \"\$(cat '${paths.boot}')\" = '$boot' ]; then\n(\n${rules.removeScript()}\n)\nrestore_interfaces\nfi\n" +
        "echo 'DASHPLAY_SHARE_SESSION_REMOVED'\n"
    fun verifyScript(paths: Paths = Paths()) = "set -eu\n${RootShell.requireRoot}echo 'DASHPLAY_SHARE_STARTED'\n" + identity(paths) +
        offloadCheck(systemOffloadApi) +
        "[ \"\$(cat '${paths.proc}/${rules.hotspot}/forwarding')\" = 1 ]\n" +
        "[ \"\$(cat '${paths.proc}/${rules.tunnel}/forwarding')\" = 1 ]\n" + rules.verifyScript()

    companion object {
        /** System tether acceleration can bypass netfilter. Never change its global configuration silently. */
        fun offloadCheck(api: Int): String = if (api < 27) "" else
            "[ \"\$(settings get global tether_offload_disabled)\" = 1 ] || exit 74\n" +
            if (api >= 30) "dumpsys tethering | grep -E '^ *mIsBpfEnabled: false *$' >/dev/null || exit 74\n" else ""
        fun probeScript(rules: UsbSharePlan): String = "set -eu\n${RootShell.requireRoot}echo 'DASHPLAY_SHARE_STARTED'\n" +
            "echo 'DASHPLAY_SHARE_LINKS' \"\$(cat /sys/class/net/${rules.hotspot}/ifindex)\" \"\$(cat /sys/class/net/${rules.tunnel}/ifindex)\" \"\$(cat /proc/sys/net/ipv4/conf/${rules.hotspot}/forwarding)\" \"\$(cat /proc/sys/net/ipv4/conf/${rules.tunnel}/forwarding)\" \"\$(cat /proc/sys/kernel/random/boot_id)\"\n" +
            "echo 'DASHPLAY_SHARE_PROBED'\n"
        fun fromProbe(rules: UsbSharePlan, output: List<String>, managed: Boolean): UsbShareSessionPlan {
            val fields = output.single { it.startsWith("DASHPLAY_SHARE_LINKS ") }.split(' ')
            require(fields.size == 6)
            return UsbShareSessionPlan(rules, fields[1].toInt(), fields[2].toInt(), fields[3].toInt(), fields[4].toInt(), managed, fields[5])
        }
    }
}
