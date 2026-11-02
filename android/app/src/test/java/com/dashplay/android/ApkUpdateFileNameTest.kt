package com.dashplay.android

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ApkUpdateFileNameTest {
    @Test fun legacyNegativeTemporaryNamesCanBeRestored() {
        assertTrue(ApkUpdates.validFileName("update--1134195570.apk"))
        assertTrue(ApkUpdates.validFileName("update-574627463.apk"))
    }
    @Test fun pathsAndUnrelatedCacheFilesRemainRejected() {
        for (name in listOf("../update-1.apk", "/update-1.apk", "update-1.apk/../secret", "update---1.apk",
            "update-1.apk.bak", "secret", "update-1.apk\u0000", "update-%2e%2e.apk"))
            assertFalse(name, ApkUpdates.validFileName(name))
    }
}
