package com.dashplay.android

import kotlinx.serialization.Serializable

/** Owned IPv4 forwarding rules; caller journals the plan and supplies a live VPN plus forwarding-capable AP. */
@Serializable
internal data class UsbSharePlan(val owner: String, val hotspot: String, val tunnel: String,
    val subnet: String, val address: String, val dns: String, val routes: List<String>,
    val table: Int, val priority: Int, val dnsHost: String = dns, val dnsPort: Int = 53, val guarded: Boolean = false) {
    init {
        require(owner.matches(Regex("DP_USB_[0-9a-f]{12}")))
        for (name in listOf(hotspot, tunnel)) require(name.matches(Regex("[A-Za-z][A-Za-z0-9_-]{0,14}"))) { "Invalid share interface: $name" }
        require(hotspot != tunnel && hotspot != "lo" && tunnel != "lo")
        require(table in 40000..60000 && priority in 9000..9999)
        val lan = cidr(subnet)
        require(lan.prefix in 8..30)
        val local = ipv4(address); val resolver = ipv4(dns)
        require(local != resolver && !lan.contains(local) && !lan.contains(resolver))
        require(if (dnsHost == dns) dnsPort == 53 else lan.contains(ipv4(dnsHost)) && dnsPort in 1024..65535)
        require(routes.isNotEmpty() && routes.size <= 200 && routes.distinct().size == routes.size)
        val parsed = routes.map(::cidr)
        require(parsed.none { it.contains(lan.address) || lan.contains(it.address) || it.contains(local) })
        require(parsed.any { it.contains(resolver) })
    }
    private val tag get() = "-m comment --comment $owner"
    private val nat get() = "dashplay_iptables -t nat"
    private val filter get() = "dashplay_iptables -t filter"
    private val ipv6 get() = "dashplay_ip6tables -t filter"
    private val ipv6Rules get() = if (guarded) listOf("$ipv6 FORWARD -i $hotspot $tag -j REJECT", "$ipv6 FORWARD -o $hotspot $tag -j REJECT") else emptyList()
    private val outbound get() = "-i $hotspot -o $tunnel -s $subnet $tag -j ACCEPT"
    private val inbound get() = "-i $tunnel -o $hotspot -d $subnet -m conntrack --ctstate ESTABLISHED,RELATED $tag -j ACCEPT"
    private val snat get() = "-o $tunnel -s $subnet $tag -j SNAT --to-source $address"
    private fun dnsRule(protocol: String) = "-i $hotspot -s $subnet -p $protocol --dport 53 $tag -j DNAT --to-destination $dnsHost:$dnsPort"
    private val rules get() = listOf(
        "$nat PREROUTING ${dnsRule("udp")}", "$nat PREROUTING ${dnsRule("tcp")}",
        "$nat POSTROUTING $snat", "$filter FORWARD $outbound", "$filter FORWARD $inbound") + ipv6Rules
    private val policy get() = listOf("pref $priority from $subnet iif $hotspot lookup $table",
        "pref $priority to $subnet iif $tunnel lookup $table") +
        if (dnsHost == dns) emptyList() else listOf("pref $priority from $dnsHost/32 to $subnet lookup $table")
    private val routeCommands get() = routes.map { "$it dev $tunnel" to table } + listOf("$subnet dev $hotspot" to table) +
        if (guarded) routes.map { "unreachable $it" to table + 1 } else emptyList()
    private fun ruleCommand(rule: String, operation: String): String {
        val split = rule.split(' ', limit = 4)
        // command prefix is exactly: dashplay_iptables -t TABLE; chain follows it.
        return split.take(3).joinToString(" ") + " $operation " + split[3]
    }
    private fun cleanupFunction(): String = buildString {
        append("cleanup() {\n  failed=0\n")
        append("  ip -4 rule show >/dev/null || return 1\n  ip -4 route show table all >/dev/null || return 1\n")
        append("  $nat -S >/dev/null || return 1\n  $filter -S >/dev/null || return 1\n")
        if (guarded) append("  $ipv6 -S >/dev/null || return 1\n")
        // Disconnect policy first. Never flush a shared chain/table or delete by priority alone.
        for (p in policy) append("  while ip -4 rule del $p 2>/dev/null; do :; done\n")
        for (r in rules.reversed()) append("  if ${ruleCommand(r, "-C")} 2>/dev/null; then ${ruleCommand(r, "-D")} || failed=1; fi\n")
        for ((r, metric) in routeCommands) append("  ip -4 route del $r table $table proto 186 metric $metric 2>/dev/null || true\n")
        append("  if ip -4 route show table all | grep -E ' table $table( |$)' >/dev/null; then failed=1; fi\n")
        append("  if ip -4 rule show | grep -E 'lookup $table( |$)' >/dev/null; then failed=1; fi\n")
        append("  if $nat -S $owner >/dev/null 2>&1; then $nat -X $owner || failed=1; fi\n")
        append("  [ \"\$failed\" = 0 ]\n}\n")
    }
    fun installScript(lease: String): String = buildString {
        require(lease.startsWith('/') && lease.matches(Regex("[A-Za-z0-9_./-]+")))
        append("set -eu\n${RootShell.requireRoot}")
        append("command -v ip >/dev/null\ncommand -v iptables >/dev/null\n")
        if (guarded) append("command -v ip6tables >/dev/null\n")
        append(RootShell.firewallFunctions(guarded))
        append("wanted() { [ \"\$(cat '$lease' 2>/dev/null)\" = '$owner' ]; }\n")
        append(cleanupFunction())
        append("wanted\n")
        // This layer does not toggle global/per-interface sysctls or change system hotspot configuration.
        for (iface in listOf(hotspot, tunnel)) append("[ \"\$(cat /proc/sys/net/ipv4/conf/$iface/forwarding)\" = 1 ] || exit 72\n")
        append("ip -4 route show table all >/dev/null\nip -4 rule show >/dev/null\n")
        append("if ip -4 route show table all | grep -E ' table $table( |$)' >/dev/null; then exit 73; fi\n")
        append("if ip -4 rule show | grep -E '^$priority:|lookup $table( |$)' >/dev/null; then exit 73; fi\n")
        append("$nat -N $owner\ntrap 'cleanup >/dev/null 2>&1' EXIT\necho 'DASHPLAY_SHARE_OWNED'\n")
        for ((r, metric) in routeCommands) append("wanted\nip -4 route add $r table $table proto 186 metric $metric\n")
        for (r in rules) append("wanted\n${ruleCommand(r, "-I")}\n")
        for (p in policy) append("wanted\nip -4 rule add $p\n")
        append("wanted\ntrap - EXIT\necho 'DASHPLAY_SHARE_ACTIVE'\n")
    }
    /** Read-only maintenance; the owner chain and every exact firewall rule must still exist. */
    fun verifyScript(): String = buildString {
        append("set -eu\n${RootShell.requireRoot}echo 'DASHPLAY_SHARE_STARTED'\n")
        append(RootShell.firewallFunctions(guarded))
        append("$nat -S $owner >/dev/null\n")
        for (r in rules) append("${ruleCommand(r, "-C")}\n")
        val expected = listOf("from $subnet iif $hotspot", "from all to $subnet iif $tunnel") +
            if (dnsHost == dns) emptyList() else listOf("from $dnsHost to $subnet")
        for (p in expected) append("ip -4 rule show | grep -E '^$priority:.*${p.replace(".", "[.]")} lookup $table( |$)' >/dev/null\n")
        for ((r, metric) in routeCommands) append("ip -4 route show table all | grep -E '^${r.replace("/32", "").replace(".", "[.]")} table $table .*metric $metric( |$)' >/dev/null\n")
        append("echo 'DASHPLAY_SHARE_VERIFIED'\n")
    }
    fun removeScript(): String = "set -eu\n${RootShell.requireRoot}" + cleanupFunction() +
        RootShell.firewallFunctions(guarded) + "cleanup\necho 'DASHPLAY_SHARE_REMOVED'\n"

    companion object {
        private fun ipv4(text: String): Int {
            val parts = text.split('.')
            require(parts.size == 4 && parts.all { it.matches(Regex("0|[1-9][0-9]{0,2}")) && it.toInt() in 0..255 })
            require(parts[0].toInt() in 1..223 && parts[0] != "127")
            return parts.fold(0) { value, part -> (value shl 8) or part.toInt() }
        }
        private fun cidr(text: String): UsbInternetPlan.Cidr {
            val parts = text.split('/'); require(parts.size == 2 && parts[1].matches(Regex("0|[1-9][0-9]?")))
            return UsbInternetPlan.Cidr(ipv4(parts[0]), parts[1].toInt())
        }
    }
}
