package com.dashplay.android

import java.io.File
import java.nio.file.Files
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class UsbSharePlanTest {
    private val plan = UsbSharePlan("DP_USB_012345abcdef", "dp-ap0", "dp-usb0", "192.0.2.0/24",
        "192.0.0.2", "192.0.0.1", listOf("192.0.0.1/32", "198.51.100.0/24"), 45123, 9234)
    private fun fixture(body: (File) -> Unit) {
        val dir = Files.createTempDirectory("dashplay-share-").toFile()
        try {
            val bytes = javaClass.getResourceAsStream("/usb-share-fixture.py")!!.use { it.readBytes() }
            for (name in listOf("ip", "iptables", "ip6tables", "cat", "id")) File(dir, name).apply { writeBytes(bytes); setExecutable(true) }
            File(dir, "lease").writeText(plan.owner)
            body(dir)
        } finally { dir.deleteRecursively() }
    }
    private fun run(dir: File, script: String, fail: Int = 0, cancel: Int = 0, forwarding: Boolean = true, legacy: Boolean = false): Int {
        val builder = ProcessBuilder("sh", "-c", script).redirectErrorStream(true)
        builder.environment().apply {
            put("PATH", dir.path + File.pathSeparator + System.getenv("PATH"))
            put("FAKE_SHARE_STATE", File(dir, "state.json").path)
            put("FAKE_LEASE", File(dir, "lease").path)
            put("FAKE_FAIL_AT", fail.toString()); put("FAKE_CANCEL_AT", cancel.toString())
            put("FAKE_FORWARDING", if (forwarding) "1" else "0")
            put("FAKE_LEGACY_WAIT", if (legacy) "1" else "0")
        }
        val p = builder.start()
        // Drain the pipe while the process runs: reading to EOF first deadlocks whenever the script leaves a
        // descendant holding stdout open (its EXIT trap re-enters sh), and the test then hangs for ever.
        val output = StringBuilder()
        val reader = Thread {
            runCatching { p.inputStream.bufferedReader().use { input ->
                val buffer = CharArray(8192)
                while (true) {
                    val n = input.read(buffer)
                    if (n < 0) break
                    synchronized(output) { output.append(buffer, 0, n) }
                }
            } }
        }.apply { isDaemon = true; start() }
        if (!p.waitFor(30, java.util.concurrent.TimeUnit.SECONDS)) {
            p.destroyForcibly(); reader.interrupt()
            File(dir, "last-output").writeText(output.toString())
            throw AssertionError("fixture script did not finish in 30 s")
        }
        reader.join(2000)
        File(dir, "last-output").writeText(output.toString())
        return p.exitValue()
    }
    private fun state(dir: File): Map<String, JsonElement> = Json.parseToJsonElement(File(dir, "state.json").readText()).jsonObject.filterKeys { it != "mutations" }
    private fun baseline(dir: File): Map<String, JsonElement> {
        assertEquals(0, run(dir, "ip -4 rule show")); return state(dir)
    }
    @Test fun installationAndRepeatedRemovalPreserveExistingFirewallAndPolicy() = fixture { dir ->
        val original = baseline(dir)
        assertEquals(File(dir, "last-output").takeIf { it.exists() }?.readText(), 0, run(dir, plan.installScript(File(dir, "lease").path)))
        val installed = state(dir)
        assertEquals(2, (installed["rules"] as JsonArray).size)
        assertEquals(3, (installed["routes"] as JsonArray).size)
        assertEquals(0, run(dir, plan.removeScript())); assertEquals(original, state(dir))
        assertEquals(0, run(dir, plan.removeScript())); assertEquals(original, state(dir))
        val out = File("build/test-artifacts/usb-share").apply { mkdirs() }
        File(out, "install.sh").writeText(plan.installScript("/data/local/tmp/dashplay-usb-share/lease"))
        File(out, "remove.sh").writeText(plan.removeScript())
    }
    @Test fun everyMutationFailureAndCancellationRollsBack() {
        // Claim, three routes, five firewall rules, two policy rules.
        for (cancel in listOf(false, true)) for (step in 1..11) fixture { dir ->
            val original = baseline(dir)
            assertNotEquals(0, run(dir, plan.installScript(File(dir, "lease").path),
                fail = if (cancel) 0 else step, cancel = if (cancel) step else 0))
            assertEquals("cancel=$cancel step=$step", original, state(dir))
        }
    }
    @Test fun collisionsAndDisabledForwardingCauseNoMutation() {
        for (collision in listOf(
            "iptables -w 2 -t nat -N ${plan.owner}",
            "ip -4 route add 203.0.113.0/24 dev other table ${plan.table}",
            "ip -4 rule add pref ${plan.priority} from all lookup 100",
            "ip -4 rule add pref 8000 from all lookup ${plan.table}")) fixture { dir ->
            baseline(dir); assertEquals(0, run(dir, collision)); val before = state(dir)
            assertNotEquals(0, run(dir, plan.installScript(File(dir, "lease").path)))
            assertEquals(before, state(dir))
        }
        fixture { dir ->
            val before = baseline(dir)
            assertEquals(72, run(dir, plan.installScript(File(dir, "lease").path), forwarding = false))
            assertEquals(before, state(dir))
        }
    }
    @Test fun unrelatedRoutesAddedToOwnedTableArePreservedAndReported() = fixture { dir ->
        baseline(dir); assertEquals(0, run(dir, plan.installScript(File(dir, "lease").path)))
        assertEquals(0, run(dir, "ip -4 route add 203.0.113.0/24 dev other table ${plan.table}"))
        assertNotEquals(0, run(dir, plan.removeScript()))
        val routes = state(dir)["routes"] as JsonArray
        assertEquals(1, routes.size); assertTrue(routes.toString().contains("other"))
    }
    @Test fun journalRejectsInjectionAndOverlappingOrSharedTargets() {
        for (name in listOf("lo", "wlan0;id", "wlan0\nexit", "wlan'0"))
            assertThrows(IllegalArgumentException::class.java) { plan.copy(hotspot = name) }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(table = 254) }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(priority = 0) }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(routes = listOf("192.0.0.0/16")) }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(subnet = "192.0.2.1/24") }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(address = "192.0.2.3") }
        assertThrows(IllegalArgumentException::class.java) { plan.installScript("/tmp/x';id") }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(dnsHost = "8.8.8.8", dnsPort = 5353) }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(dnsHost = "192.0.2.1", dnsPort = 53) }
    }
    @Test fun localDnsProxyGetsItsOwnReturnRouteAndRollsBackEveryStep() {
        val proxy = plan.copy(dnsHost = "192.0.2.1", dnsPort = 15353)
        fixture { dir ->
            val before = baseline(dir)
            assertEquals(0, run(dir, proxy.installScript(File(dir, "lease").path)))
            assertEquals(3, (state(dir)["rules"] as JsonArray).size)
            assertEquals(0, run(dir, proxy.removeScript())); assertEquals(before, state(dir))
        }
        for (step in 1..12) fixture { dir ->
            val before = baseline(dir)
            assertNotEquals(0, run(dir, proxy.installScript(File(dir, "lease").path), fail = step))
            assertEquals("proxy step=$step", before, state(dir))
        }
    }
    @Test fun guardedRulesSurviveTunnelRouteLossWithoutSelectingAnotherUplink() = fixture { dir ->
        val guarded = plan.copy(guarded = true)
        val before = baseline(dir)
        assertEquals(0, run(dir, guarded.installScript(File(dir, "lease").path)))
        assertEquals(0, run(dir, guarded.verifyScript()))
        // Model the kernel removing link routes on TUN teardown; unreachable routes remain independent of a link.
        for (route in plan.routes) assertEquals(0, run(dir, "ip -4 route del $route dev ${plan.tunnel} table ${plan.table} proto 186 metric ${plan.table}"))
        val routes = (state(dir)["routes"] as JsonArray).toString()
        assertTrue(routes.contains("unreachable")); assertFalse(routes.contains(plan.tunnel))
        assertNotEquals(0, run(dir, guarded.verifyScript()))
        assertEquals(0, run(dir, guarded.removeScript())); assertEquals(before, state(dir))
    }
    @Test fun legacyIpv4AndIpv6WaitSyntaxStillInstallsVerifiesAndCleans() = fixture { dir ->
        val guarded = plan.copy(guarded = true)
        val before = baseline(dir)
        assertEquals(0, run(dir, guarded.installScript(File(dir, "lease").path), legacy = true))
        assertEquals(0, run(dir, guarded.verifyScript(), legacy = true))
        assertEquals(0, run(dir, guarded.removeScript(), legacy = true)); assertEquals(before, state(dir))
    }

    @Test fun ownedForwardingRestoresAfterEveryGuardedInstallationFailure() {
        val guarded = plan.copy(guarded = true, dnsHost = "192.0.2.1", dnsPort = 15353)
        // Claim, five routes, seven IPv4/IPv6 rules, three policy rules.
        for (step in 1..16) fixture { dir ->
            val paths = UsbShareSessionPlan.Paths(File(dir, "sys").path, File(dir, "proc").path, File(dir, "boot").path)
            val boot = "01234567-89ab-cdef-0123-456789abcdef"
            File(paths.boot).writeText(boot)
            for ((name, index) in listOf(guarded.hotspot to 11, guarded.tunnel to 12)) {
                File(paths.sys, "$name/ifindex").apply { parentFile!!.mkdirs(); writeText(index.toString()) }
                File(paths.proc, "$name/forwarding").apply { parentFile!!.mkdirs(); writeText("0") }
            }
            val session = UsbShareSessionPlan(guarded, 11, 12, 0, 0, true, boot)
            val before = baseline(dir)
            assertNotEquals(0, run(dir, session.installScript(File(dir, "lease").path, paths), fail = step))
            assertEquals("step=$step", before, state(dir))
            for (name in listOf(guarded.hotspot, guarded.tunnel)) assertEquals("0", File(paths.proc, "$name/forwarding").readText().trim())
        }
    }
    @Test fun lateCleanupCannotDisableForwardingOwnedByANewerLease() = fixture { dir ->
        val guarded = plan.copy(guarded = true)
        val paths = UsbShareSessionPlan.Paths(File(dir, "sys").path, File(dir, "proc").path, File(dir, "boot").path)
        val boot = "01234567-89ab-cdef-0123-456789abcdef"
        File(paths.boot).writeText(boot)
        for ((name, index) in listOf(guarded.hotspot to 11, guarded.tunnel to 12)) {
            File(paths.sys, "$name/ifindex").apply { parentFile!!.mkdirs(); writeText(index.toString()) }
            File(paths.proc, "$name/forwarding").apply { parentFile!!.mkdirs(); writeText("0") }
        }
        val session = UsbShareSessionPlan(guarded, 11, 12, 0, 0, true, boot)
        baseline(dir)
        val lease = File(dir, "lease")
        assertEquals(0, run(dir, session.installScript(lease.path, paths)))
        lease.writeText("DP_USB_abcdef012345")
        assertEquals(0, run(dir, session.removeScript(paths, lease.path)))
        for (name in listOf(guarded.hotspot, guarded.tunnel)) assertEquals("1", File(paths.proc, "$name/forwarding").readText().trim())
        assertEquals("DP_USB_abcdef012345", lease.readText())
    }
    @Test fun systemOffloadGuardRejectsEnabledAndUnknownAccelerationWithoutChangingIt() = fixture { dir ->
        val settings = File(dir, "settings").apply { writeText("#!/bin/sh\nprintf '1\\n'\n"); setExecutable(true) }
        val dumpsys = File(dir, "dumpsys").apply { writeText("#!/bin/sh\nprintf '  mIsBpfEnabled: false\\n'\n"); setExecutable(true) }
        assertEquals(0, run(dir, UsbShareSessionPlan.offloadCheck(30)))
        dumpsys.writeText("#!/bin/sh\nprintf '  mIsBpfEnabled: true\\n'\n")
        assertEquals(74, run(dir, UsbShareSessionPlan.offloadCheck(30)))
        assertEquals(0, run(dir, UsbShareSessionPlan.offloadCheck(27)))
        settings.writeText("#!/bin/sh\nprintf 'null\\n'\n")
        assertEquals(74, run(dir, UsbShareSessionPlan.offloadCheck(27)))
        assertEquals(0, run(dir, UsbShareSessionPlan.offloadCheck(26)))
        assertFalse(File(dir, "state.json").exists())
    }
    @Test fun interfaceRestorationDoesNotTouchReusedIndicesOrAnotherBoot() = fixture { dir ->
        val guarded = plan.copy(guarded = true)
        val paths = UsbShareSessionPlan.Paths(File(dir, "sys").path, File(dir, "proc").path, File(dir, "boot").path)
        val boot = "01234567-89ab-cdef-0123-456789abcdef"
        File(paths.boot).writeText(boot)
        for ((name, index) in listOf(guarded.hotspot to 11, guarded.tunnel to 12)) {
            File(paths.sys, "$name/ifindex").apply { parentFile!!.mkdirs(); writeText(index.toString()) }
            File(paths.proc, "$name/forwarding").apply { parentFile!!.mkdirs(); writeText("0") }
        }
        val session = UsbShareSessionPlan(guarded, 11, 12, 0, 0, true, boot)
        baseline(dir)
        assertEquals(0, run(dir, session.installScript(File(dir, "lease").path, paths)))
        assertEquals(0, run(dir, session.verifyScript(paths)))
        File(paths.sys, "${guarded.tunnel}/ifindex").writeText("42")
        assertEquals(0, run(dir, session.removeScript(paths)))
        assertEquals("1", File(paths.proc, "${guarded.tunnel}/forwarding").readText().trim())
        assertEquals("0", File(paths.proc, "${guarded.hotspot}/forwarding").readText().trim())
        File(paths.proc, "${guarded.hotspot}/forwarding").writeText("1")
        File(paths.boot).writeText("abcdef01-2345-6789-abcd-ef0123456789")
        assertEquals(0, run(dir, session.removeScript(paths)))
        assertEquals("1", File(paths.proc, "${guarded.hotspot}/forwarding").readText().trim())
        assertThrows(IllegalArgumentException::class.java) { session.copy(manageHotspot = false) }
    }
}
