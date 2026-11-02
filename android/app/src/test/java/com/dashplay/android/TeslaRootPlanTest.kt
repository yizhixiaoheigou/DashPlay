package com.dashplay.android

import java.io.File
import java.nio.file.Files
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.*
import org.junit.Test

class TeslaRootPlanTest {
    private val plan = TeslaRootPlan("DP_TESLA_012345abcdef", "wlan2", "192.168.43.1", 8080)
    private fun fixture(test: (File) -> Unit) {
        val dir = Files.createTempDirectory("dashplay-iptables-test-").toFile()
        try {
            File(dir, "iptables").apply {
                writeBytes(TeslaRootPlanTest::class.java.getResourceAsStream("/iptables-fixture.py")!!.use { it.readBytes() }); setExecutable(true)
            }
            File(dir, "id").apply { writeText("#!/bin/sh\nprintf '0\\n'\n"); setExecutable(true) }
            test(dir)
        } finally { dir.deleteRecursively() }
    }
    private fun install(dir: File): String {
        val lease = File(dir, "lease").apply { writeText(plan.chain) }
        return plan.installScript(lease.path, null)
    }
    private fun run(dir: File, script: String, failAt: Int = 0, cancelAt: Int = 0, cancelFile: File? = null, legacy: Boolean = false): Int {
        val builder = ProcessBuilder("sh", "-c", script).redirectErrorStream(true)
        builder.environment()["PATH"] = dir.absolutePath + File.pathSeparator + System.getenv("PATH")
        builder.environment()["FAKE_IPTABLES_STATE"] = File(dir, "rules.json").path
        builder.environment()["FAKE_FAIL_AT"] = failAt.toString()
        builder.environment()["FAKE_CANCEL_AT"] = cancelAt.toString()
        builder.environment()["FAKE_LEASE"] = File(dir, "lease").path
        cancelFile?.let { builder.environment()["FAKE_CANCEL_FILE"] = it.path }
        builder.environment()["FAKE_LEGACY_WAIT"] = if (legacy) "1" else "0"
        val process = builder.start()
        process.inputStream.bufferedReader().use { it.readText() }
        return process.waitFor()
    }
    private fun rules(dir: File) = Json.parseToJsonElement(File(dir, "rules.json").readText()).jsonObject.getValue("chains").jsonObject

    @Test fun normalInstallAndRepeatedCleanupPreserveUnrelatedRules() = fixture { dir ->
        assertEquals(0, run(dir, "iptables -w 2 -t nat -S"))
        val before = rules(dir)
        assertEquals(0, run(dir, install(dir)))
        assertEquals(0, run(dir, plan.verifyScript()))
        assertEquals(2, rules(dir).getValue(plan.chain).let { (it as kotlinx.serialization.json.JsonArray).size })
        assertEquals(0, run(dir, plan.removeScript()))
        assertEquals(before, rules(dir))
        assertNotEquals(0, run(dir, plan.verifyScript()))
        // The emulator integration test consumes these exact production-generated commands.
        val output = File("build/test-artifacts/tesla-root").apply { mkdirs() }
        val emulator = plan.copy(iface = "dp-tesla0", host = "192.0.2.1", port = 18080)
        File(output, "install.sh").writeText(emulator.installScript("/data/local/tmp/dashplay-tesla-root/lease", null))
        File(output, "remove.sh").writeText(emulator.removeScript())
        assertEquals(0, run(dir, plan.removeScript()))
        assertEquals(before, rules(dir))
    }
    @Test fun eachPartialInstallationFailureRollsBackOnlyItsOwnChanges() {
        for (step in 1..4) fixture { dir ->
            assertEquals(0, run(dir, "iptables -w 2 -t nat -S"))
            val before = rules(dir)
            assertNotEquals(0, run(dir, install(dir), step))
            assertEquals("failed installation step $step", before, rules(dir))
        }
    }
    @Test fun existingChainCollisionCannotBeTakenOverOrDeleted() = fixture { dir ->
        assertEquals(0, run(dir, "iptables -w 2 -t nat -N ${plan.chain}\niptables -w 2 -t nat -A ${plan.chain} -j RETURN"))
        val before = rules(dir)
        assertNotEquals(0, run(dir, install(dir)))
        assertEquals(before, rules(dir))
    }
    @Test fun foreignRuleAddedToOwnedChainSurvivesCleanupAndRequiresAttention() = fixture { dir ->
        assertEquals(0, run(dir, install(dir)))
        assertEquals(0, run(dir, "iptables -w 2 -t nat -A ${plan.chain} -p udp -j RETURN"))
        assertNotEquals(0, run(dir, plan.removeScript()))
        assertEquals("[[\"-p\",\"udp\",\"-j\",\"RETURN\"]]", rules(dir).getValue(plan.chain).toString())
        assertEquals("[[\"-j\",\"OTHER\"]]", rules(dir).getValue("PREROUTING").toString())
    }
    @Test fun connectivityRulesRollbackAtEveryMutationAndPreserveExistingRules() {
        val checks = plan.copy(dnsPort = 15353, checkPort = 18081)
        for (cancel in listOf(false, true)) for (step in 1..10) fixture { dir ->
            assertEquals(0, run(dir, "iptables -w 2 -t nat -S"))
            val before = rules(dir)
            val lease = File(dir, "lease").apply { writeText(checks.chain) }
            assertNotEquals(0, run(dir, checks.installScript(lease.path, null), failAt = if (cancel) 0 else step, cancelAt = if (cancel) step else 0))
            assertEquals("step $step", before, rules(dir))
        }
        fixture { dir ->
            assertEquals(0, run(dir, "iptables -w 2 -t nat -S"))
            val before = rules(dir)
            val lease = File(dir, "lease").apply { writeText(checks.chain) }
            assertEquals(0, run(dir, checks.installScript(lease.path, null)))
            assertEquals(0, run(dir, checks.verifyScript()))
            assertEquals(0, run(dir, checks.removeScript()))
            assertEquals(before, rules(dir))
        }
    }
    @Test fun connectivityJournalRemainsCompatibleWithOldPlans() {
        val old = "{\"chain\":\"${plan.chain}\",\"iface\":\"wlan2\",\"host\":\"192.168.43.1\",\"port\":8080}"
        assertEquals(plan, Json.decodeFromString<TeslaRootPlan>(old))
        assertThrows(IllegalArgumentException::class.java) { plan.copy(dnsPort = 53, checkPort = 18081) }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(dnsPort = null, checkPort = 18081) }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(redirectAddress = false, dnsPort = null) }
    }

    @Test fun dnsOnlyPlanRedirects53WithoutTeslaAddressOrHttpChecks() = fixture { dir ->
        val dnsOnly = plan.copy(dnsPort = 15353, checkPort = null, redirectAddress = false)
        assertEquals(0, run(dir, "iptables -w 2 -t nat -S"))
        val before = rules(dir)
        val lease = File(dir, "lease").apply { writeText(dnsOnly.chain) }
        assertEquals(0, run(dir, dnsOnly.installScript(lease.path, null)))
        assertEquals(0, run(dir, dnsOnly.verifyScript()))
        val chainRules = rules(dir).getValue(dnsOnly.chain).toString()
        assertTrue(chainRules.contains("15353"))
        assertFalse(chainRules.contains("100.96.0."))
        assertEquals(0, run(dir, dnsOnly.removeScript()))
        assertEquals(before, rules(dir))
    }
    @Test fun rootDenialCannotReachAnyFirewallCommand() = fixture { dir ->
        File(dir, "id").writeText("#!/bin/sh\nprintf '2000\\n'\n")
        assertEquals(70, run(dir, install(dir)))
        assertFalse(File(dir, "rules.json").exists())
    }
    @Test fun toolboxIdentityAcceptsOnlyUidZeroAndHandlesUnsupportedOption() {
        for (value in listOf("0", "uid=0(root) gid=0(root)", "uid=0 gid=0", "2000", "uid=2000(shell) gid=0(root)", "uid=00(root)", "uid=01(root)", "", "unexpected")) {
            fixture { dir ->
                File(dir, "id").writeText("#!/bin/sh\nprintf '%s\\n' '$value'\n")
                val allowed = value in listOf("0", "uid=0(root) gid=0(root)", "uid=0 gid=0")
                assertEquals(value, allowed, run(dir, "set -eu\n" + RootShell.requireRoot) == 0)
            }
        }
        fixture { dir ->
            File(dir, "id").writeText("#!/bin/sh\n[ \"\$#\" = 0 ] || exit 1\nprintf 'uid=0(root) gid=0(root)\\n'\n")
            assertEquals(0, run(dir, "set -eu\n" + RootShell.requireRoot))
        }
    }
    @Test fun journalInputsCannotInjectShellOrTargetOtherChains() {
        for (iface in listOf("wlan0;id", "wlan0\nreboot", "-x", "wlan'0"))
            assertThrows(IllegalArgumentException::class.java) { plan.copy(iface = iface) }
        for (host in listOf("127.0.0.1", "256.0.0.1", "1.2.3.4;id", "100.96.0.1", "1.2.3.04"))
            assertThrows(IllegalArgumentException::class.java) { plan.copy(host = host) }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(chain = "PREROUTING") }
        assertThrows(IllegalArgumentException::class.java) { plan.copy(port = 80) }
    }
    @Test fun cancellationDuringAnyMutationPreventsLateActivationAndRemovesPartialRules() {
        for (step in 1..4) fixture { dir ->
            assertEquals(0, run(dir, "iptables -w 2 -t nat -S"))
            val before = rules(dir)
            assertNotEquals(0, run(dir, install(dir), cancelAt = step))
            assertEquals(before, rules(dir))
        }
    }

    private fun processStat(dir: File, state: String = "S", start: String = "42"): File =
        File(dir, "proc/123/stat").apply {
            parentFile!!.mkdirs()
            writeText("123 (name ) with spaces) $state " + (4..21).joinToString(" ") { "0" } + " $start 0 0\n")
        }

    @Test fun processIdentityParsesKernelFieldsAndRejectsMalformedInput() = fixture { dir ->
        val stat = processStat(dir)
        val identity = RootProcessIdentity.fromStat(123, stat.readText(), File(dir, "proc").path)
        assertEquals(123, identity.pid); assertEquals(42L, identity.startTicks)
        for (value in listOf("", "123 broken", stat.readText().replace("42 0 0", "bad 0 0"),
            processStat(dir, state = "Z").readText(), processStat(dir, start = "-1").readText())) {
            assertThrows(IllegalArgumentException::class.java) { RootProcessIdentity.fromStat(123, value) }
        }
        assertThrows(IllegalArgumentException::class.java) { RootProcessIdentity.fromStat(124, processStat(dir).readText()) }
        assertThrows(IllegalArgumentException::class.java) { RootProcessIdentity(123, 42, "/tmp/';exit") }
    }

    @Test fun deadOrReusedProcessCannotInstallEvenWhenItsLeaseStillExists() {
        for (condition in listOf("missing", "reused", "Z", "X", "x")) fixture { dir ->
            val stat = processStat(dir)
            val identity = RootProcessIdentity.fromStat(123, stat.readText(), File(dir, "proc").path)
            val lease = File(dir, "lease").apply { writeText(plan.chain) }
            assertEquals(0, run(dir, "iptables -w 2 -t nat -S")); val before = rules(dir)
            when (condition) {
                "missing" -> stat.delete()
                "reused" -> processStat(dir, start = "43")
                else -> processStat(dir, state = condition)
            }
            assertNotEquals(condition, 0, run(dir, plan.installScript(lease.path, identity)))
            assertEquals(before, rules(dir)); assertTrue(lease.exists())
        }
    }

    @Test fun legacyWaitSyntaxUsesFailFastInstallVerifyAndCleanup() = fixture { dir ->
        val checks = plan.copy(dnsPort = 15353, checkPort = 18081)
        val lease = File(dir, "lease").apply { writeText(checks.chain) }
        assertEquals(0, run(dir, "iptables -t nat -S", legacy = true)); val before = rules(dir)
        assertEquals(0, run(dir, checks.installScript(lease.path, null), legacy = true))
        assertEquals(0, run(dir, checks.verifyScript(), legacy = true))
        assertEquals(0, run(dir, checks.removeScript(), legacy = true)); assertEquals(before, rules(dir))
    }

    @Test fun liveProcessCanInstallAndCleanup() = fixture { dir ->
        val stat = processStat(dir)
        val identity = RootProcessIdentity.fromStat(123, stat.readText(), File(dir, "proc").path)
        val lease = File(dir, "lease").apply { writeText(plan.chain) }
        assertEquals(0, run(dir, "iptables -w 2 -t nat -S")); val before = rules(dir)
        assertEquals(0, run(dir, plan.installScript(lease.path, identity)))
        assertEquals(0, run(dir, plan.verifyScript()))
        assertEquals(0, run(dir, plan.removeScript())); assertEquals(before, rules(dir))
    }

    @Test fun deathDuringEachInstallationMutationRollsBackWithTheLeaseIntact() {
        val checks = plan.copy(dnsPort = 15353, checkPort = 18081)
        for (step in 1..10) fixture { dir ->
            val stat = processStat(dir)
            val identity = RootProcessIdentity.fromStat(123, stat.readText(), File(dir, "proc").path)
            val lease = File(dir, "lease").apply { writeText(checks.chain) }
            assertEquals(0, run(dir, "iptables -w 2 -t nat -S")); val before = rules(dir)
            assertNotEquals("step $step", 0, run(dir, checks.installScript(lease.path, identity), cancelAt = step, cancelFile = stat))
            assertEquals(before, rules(dir)); assertTrue(lease.exists())
        }
    }
}
