package com.dashplay.android

import org.junit.Assert.assertThrows
import org.junit.Test

class ApkUpdatePolicyTest {
    private val current = ApkUpdatePolicy.Package("com.dashplay.android", 2, setOf("publisher"))
    @Test fun samePublisherNewerBuildIsAccepted() { ApkUpdatePolicy.check(current, current.copy(version = 3)) }
    @Test fun otherApplicationOrSplitIsRejected() {
        for (candidate in listOf(current.copy(name = "other.app", version = 3), current.copy(version = 3, split = true)))
            assertThrows(IllegalArgumentException::class.java) { ApkUpdatePolicy.check(current, candidate) }
    }
    @Test fun MissingDifferentOrPartialSignaturesAreRejected() {
        for (signers in listOf(emptySet(), setOf("other"), setOf("publisher", "other")))
            assertThrows(IllegalArgumentException::class.java) { ApkUpdatePolicy.check(current, current.copy(version = 3, signers = signers)) }
        assertThrows(IllegalArgumentException::class.java) { ApkUpdatePolicy.check(current.copy(signers = emptySet()), current.copy(version = 3, signers = emptySet())) }
    }
    @Test fun reinstallAndDowngradeAreRejected() {
        for (version in listOf(-1L, 0L, 1L, 2L))
            assertThrows(IllegalArgumentException::class.java) { ApkUpdatePolicy.check(current, current.copy(version = version)) }
    }

    @Test fun onlineMirrorCandidateUrlsAndValidationMatchBoxRules() {
        // Direct first, then each built-in mirror in order: one hard-coded mirror broke when ghfast.top stopped
        // answering (2026-11-01), and a dead mirror must not be the only fallback.
        org.junit.Assert.assertEquals(
            listOf("https://github.com/a/b") + OnlineUpdate.DEFAULT_MIRRORS.map { it + "https://github.com/a/b" },
            OnlineUpdate.candidateUrls("https://github.com/a/b", ""),
        )
        org.junit.Assert.assertTrue(OnlineUpdate.DEFAULT_MIRRORS.size >= 2)
        org.junit.Assert.assertTrue(OnlineUpdate.DEFAULT_MIRRORS.all { it.endsWith("/") })
        org.junit.Assert.assertEquals(
            listOf("https://github.com/a/b"),
            OnlineUpdate.candidateUrls("https://github.com/a/b", "none"),
        )
        org.junit.Assert.assertEquals(
            listOf("https://github.com/a/b", "https://m1.example.com/https://github.com/a/b", "https://m2.example.com/https://github.com/a/b"),
            OnlineUpdate.candidateUrls("https://github.com/a/b", "https://m1.example.com https://m2.example.com/"),
        )
        SettingValidation.updateMirror("")
        SettingValidation.updateMirror("none")
        SettingValidation.updateMirror("https://ghfast.top/")
        assertThrows(IllegalArgumentException::class.java) { SettingValidation.updateMirror("ftp://bad.example.com") }
    }

    @Test fun versionComparisonAndEd25519VerificationWork() {
        org.junit.Assert.assertTrue(OnlineUpdate.newer("2026.10.31.2", "2026.10.31"))
        org.junit.Assert.assertTrue(OnlineUpdate.newer("2026.10.31", "0.2.0-dev.1"))
        org.junit.Assert.assertFalse(OnlineUpdate.newer("2026.10.29", "2026.10.31"))
        org.junit.Assert.assertFalse(OnlineUpdate.newer("2026.10.31", "2026.10.31"))

        if (java.security.Security.getProvider("BC") == null) {
            java.security.Security.addProvider(org.bouncycastle.jce.provider.BouncyCastleProvider())
        }
        val kpg = java.security.KeyPairGenerator.getInstance("Ed25519", "BC")
        val kp = kpg.generateKeyPair()
        val pubB64 = java.util.Base64.getEncoder().encodeToString(kp.public.encoded)
        val payload = """{"format":1,"version":"2026.10.31.2","notes":"test","online":true}""".toByteArray()
        val sig = java.security.Signature.getInstance("Ed25519", "BC").run {
            initSign(kp.private); update(payload); sign()
        }
        org.junit.Assert.assertTrue(OnlineUpdate.verifyEd25519(payload, sig, pubB64))
        org.junit.Assert.assertFalse(OnlineUpdate.verifyEd25519(payload + byteArrayOf(1), sig, pubB64))
    }
}
