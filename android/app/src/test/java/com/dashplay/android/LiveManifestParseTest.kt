package com.dashplay.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Parses the manifest that a release would really carry (signed with the release key, android section present).
 * Skips (loudly) when the artifact is absent, so a clean checkout does not fail.
 */
class LiveManifestParseTest {
    // Unit tests run with android/app as the working directory; the fixture lives in the repository root artifacts/.
    private val root = File(System.getProperty("dashplayRoot") ?: "../..").absoluteFile

    @Test fun signedManifestWithAndroidSectionParses() {
        val manifest = File(root, "artifacts/android-product/update-manifest-fixture/dashplay-update.json")
        val sig = File(root, "artifacts/android-product/update-manifest-fixture/dashplay-update.json.sig")
        org.junit.Assume.assumeTrue(
            "manifest fixture missing: ${manifest.absolutePath} (see docs/安卓发布验收清单.md)",
            manifest.isFile && sig.isFile)
        val info = OnlineUpdate.parseManifestOrApi(manifest.readBytes(), sig.readBytes(), null)
        assertEquals("0.3.0-beta.1", info.version)
        assertTrue(info.apkUrl.endsWith("DashPlay-android-0.3.0-beta.1.apk"))
        assertTrue(OnlineUpdate.newer(info.version, "0.2.0-dev.1"))
        assertEquals(false, OnlineUpdate.newer(info.version, "0.3.0-beta.1"))
        assertEquals(true, OnlineUpdate.newer("0.3.0", info.version))
    }
}
