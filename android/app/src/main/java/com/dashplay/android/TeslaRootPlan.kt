package com.dashplay.android

import kotlinx.serialization.Serializable

/** A narrowly scoped local-page redirect, not a replacement tethering stack or Internet gateway. */
@Serializable
data class TeslaRootPlan(val chain: String, val iface: String, val host: String, val port: Int,
    val dnsPort: Int? = null, val checkPort: Int? = null, val redirectAddress: Boolean = true) {
    init {
        require(chain.matches(Regex("DP_TESLA_[0-9a-f]{12}")))
        require(iface.matches(Regex("[A-Za-z][A-Za-z0-9_.:-]{0,31}")))
        val parts = host.split('.')
        require(parts.size == 4 && parts.all { it.matches(Regex("0|[1-9][0-9]{0,2}")) && it.toInt() in 0..255 })
        require(parts[0].toInt() in 1..223 && parts[0] != "127" && host != ADDRESS)
        require(port in 1024..65535)
        require(redirectAddress || dnsPort != null)
        require(dnsPort == null || dnsPort in 1024..65535 && dnsPort != port)
        require(checkPort == null || dnsPort != null && checkPort in 1024..65535 && checkPort != dnsPort && checkPort != port)
        require(host != TeslaCheckProtocol.ADDRESS)
    }

    private val ipt get() = "dashplay_iptables -t nat"
    private val tag get() = "-m comment --comment $chain"
    private val hook get() = "-i '$iface' -d $ADDRESS/32 -p tcp $tag -j $chain"
    private fun redirect(frontPort: Int) = "-p tcp --dport $frontPort $tag -j DNAT --to-destination $host:$port"
    private val hooks get() = (if (redirectAddress) listOf(hook) else emptyList()) +
        (if (dnsPort == null) emptyList() else listOf(
            "-i '$iface' -p udp --dport 53 $tag -j $chain",
            "-i '$iface' -p tcp --dport 53 $tag -j $chain")) +
        (if (checkPort == null) emptyList() else listOf(
            "-i '$iface' -d ${TeslaCheckProtocol.ADDRESS}/32 -p tcp --dport 80 $tag -j $chain"))
    private val redirects get() = (if (redirectAddress) ports.map(::redirect) else emptyList()) +
        (if (dnsPort == null) emptyList() else listOf(
            "-p udp --dport $53 $tag -j DNAT --to-destination $host:$dnsPort".replace("$53", "53"),
            "-p tcp --dport 53 $tag -j DNAT --to-destination $host:$dnsPort")) +
        (if (checkPort == null) emptyList() else listOf(
            "-d ${TeslaCheckProtocol.ADDRESS}/32 -p tcp --dport 80 $tag -j DNAT --to-destination $host:$checkPort"))
    private val ports get() = listOf(443, port).distinct()

    private fun cleanupFunction(): String = buildString {
        append("cleanup() {\n")
        // Never flush a chain: delete only exact owned rules, then remove it only if it is empty.
        append("  $ipt -S >/dev/null 2>&1 || return 1\n")
        for (rule in hooks) append("  if $ipt -C PREROUTING $rule 2>/dev/null; then $ipt -D PREROUTING $rule || return 1; fi\n")
        append("  if $ipt -S $chain >/dev/null 2>&1; then\n")
        for (rule in redirects) append("    if $ipt -C $chain $rule 2>/dev/null; then $ipt -D $chain $rule || return 1; fi\n")
        append("    $ipt -X $chain || return 1\n  fi\n}\n")
    }

    internal fun installScript(leasePath: String, process: RootProcessIdentity?): String = buildString {
        require(leasePath.startsWith('/') && leasePath.matches(Regex("[A-Za-z0-9_./-]+")))
        append("set -eu\n${RootShell.requireRoot}")
        append("command -v iptables >/dev/null || exit 71\n")
        append("echo 'DASHPLAY_ROOT_STARTED'\n")
        append(RootShell.firewallFunctions())
        append(cleanupFunction())
        process?.let { append(it.shellFunction()) }
        append("wanted() { ${if (process != null) "process_alive && " else ""}[ \"\$(cat '$leasePath' 2>/dev/null)\" = '$chain' ]; }\n")
        append("$ipt -S >/dev/null\n")
        // A collision is not permission to take over another chain. The caller recovers its journal first.
        append("wanted\n$ipt -N $chain\ntrap 'cleanup >/dev/null 2>&1' EXIT\necho 'DASHPLAY_ROOT_OWNED'\n")
        for (rule in redirects) append("wanted\n$ipt -A $chain $rule\n")
        for (rule in hooks) append("wanted\n$ipt -I PREROUTING 1 $rule\nwanted\n")
        for (rule in hooks) append("$ipt -C PREROUTING $rule\n")
        append("wanted\ntrap - EXIT\necho 'DASHPLAY_ROOT_ACTIVE'\n")
    }

    fun removeScript(): String = "set -eu\n${RootShell.requireRoot}echo 'DASHPLAY_ROOT_STARTED'\n" + cleanupFunction() +
        RootShell.firewallFunctions() + "cleanup\necho 'DASHPLAY_ROOT_REMOVED'\n"

    fun verifyScript(): String = buildString {
        append("set -eu\n${RootShell.requireRoot}echo 'DASHPLAY_ROOT_STARTED'\n")
        append(RootShell.firewallFunctions())
        for (rule in redirects) append("$ipt -C $chain $rule\n")
        for (rule in hooks) append("$ipt -C PREROUTING $rule\n")
        append("echo 'DASHPLAY_ROOT_ACTIVE'\n")
    }

    companion object { const val ADDRESS = "100.96.0.1" }
}
