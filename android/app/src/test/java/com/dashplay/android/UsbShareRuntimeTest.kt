package com.dashplay.android

import java.io.File
import java.nio.file.Files
import org.junit.Assert.*
import org.junit.Test

class UsbShareRuntimeTest {
    private val rules = UsbSharePlan("DP_USB_012345abcdef", "ap0", "tun0", "192.0.2.0/24", "192.0.0.2", "192.0.0.1",
        listOf("192.0.0.1/32", "198.51.100.0/24"), 45123, 9234, guarded = true)
    private val plan = UsbShareSessionPlan(rules, 11, 12, 1, 0, false, "01234567-89ab-cdef-0123-456789abcdef")
    private fun fixture(body: (UsbShareToken) -> Unit) {
        val dir = Files.createTempDirectory("share-runtime-").toFile()
        try { body(UsbShareToken(File(dir, "lease"))) } finally { dir.deleteRecursively() }
    }
    @Test fun journalPrecedesMutationAndOnlyVerifiedCleanupRemovesIt() = fixture { token ->
        var journal: String? = null
        var removeAllowed = false
        val runtime = UsbShareRuntime(token, { journal }, { journal = it; true }, { _, marker, _ ->
            assertNotNull(journal)
            if (marker.endsWith("ACTIVE")) assertTrue(File(token.path).exists()) else assertFalse(File(token.path).exists())
            UsbShareRootResult(marker.endsWith("ACTIVE") || removeAllowed, true, true)
        })
        assertTrue(runtime.recover())
        assertTrue(runtime.install(plan, token.version()))
        assertFalse(runtime.recover()); assertNotNull(journal); assertNull(runtime.installed)
        removeAllowed = true
        assertTrue(runtime.recover()); assertNull(journal)
        assertTrue(runtime.recover())
    }
    @Test fun cancellationDuringRootWaitCannotBecomeActiveAndStaleCallbackCannotRevokeNewOwner() = fixture { token ->
        var journal: String? = null
        val runtime = UsbShareRuntime(token, { journal }, { journal = it; true }, { _, marker, _ ->
            if (marker.endsWith("ACTIVE")) token.revoke()
            UsbShareRootResult(true, true, true)
        })
        assertTrue(runtime.recover())
        val old = token.version()
        assertFalse(runtime.install(plan, old)); assertNull(runtime.installed); assertNull(journal)
        val current = token.version()
        assertTrue(token.grant(current, "DP_USB_abcdef012345"))
        token.revokeIf(old)
        assertTrue(token.valid(current))
    }
    @Test fun lateRootTimeoutKeepsRecoveryRecordAndRevokesItsInstallationToken() = fixture { token ->
        var journal: String? = null
        val runtime = UsbShareRuntime(token, { journal }, { journal = it; true }, { _, marker, timeout ->
            if (marker.endsWith("ACTIVE")) timeout()
            UsbShareRootResult(false, false, false)
        })
        assertFalse(runtime.install(plan, token.version()))
        assertNotNull(journal); assertFalse(File(token.path).exists()); assertNull(runtime.installed)
    }
    @Test fun invalidJournalAndFailedDurableWriteNeverRunRootMutations() = fixture { token ->
        var calls = 0
        val invalid = UsbShareRuntime(token, { "invalid" }, { false }, { _, _, _ -> calls++; error("must not run") })
        assertFalse(invalid.recover()); assertEquals(0, calls)
        val failed = UsbShareRuntime(token, { null }, { false }, { _, _, _ -> calls++; error("must not run") })
        assertFalse(failed.install(plan, token.version())); assertEquals(0, calls)
        assertFalse(File(token.path).exists())
    }
    @Test fun revokedGenerationCannotWriteANewLiveTokenOrInstall() = fixture { token ->
        var journal: String? = null; var calls = 0
        val runtime = UsbShareRuntime(token, { journal }, { journal = it; true }, { _, _, _ -> calls++; error("must not run") })
        val before = token.version(); token.revoke()
        assertFalse(runtime.install(plan, before)); assertNull(journal); assertEquals(0, calls)
    }
}
