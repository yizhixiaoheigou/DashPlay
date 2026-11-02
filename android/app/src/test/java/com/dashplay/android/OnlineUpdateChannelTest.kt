package com.dashplay.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.KeyPairGenerator
import java.security.Signature
import java.util.Base64

/**
 * The box version (2026.10.31.3) and the app version (0.3.0-beta.1) are separate channels: comparing them made every
 * check say "a new version is available" while pointing at an APK that was never published.
 */
class OnlineUpdateChannelTest {

    private fun signedManifest(json: String): Pair<ByteArray, ByteArray> {
        if (java.security.Security.getProvider("BC") == null) {
            java.security.Security.addProvider(org.bouncycastle.jce.provider.BouncyCastleProvider())
        }
        val kp = KeyPairGenerator.getInstance("Ed25519", "BC").generateKeyPair()
        keyPair = kp
        val payload = json.toByteArray()
        val sig = Signature.getInstance("Ed25519", "BC").run { initSign(kp.private); update(payload); sign() }
        return payload to sig
    }

    private var keyPair: java.security.KeyPair? = null
    private fun publicKey(): String = Base64.getEncoder().encodeToString(keyPair!!.public.encoded)

    @Test fun boxVersionIsNeverComparedWithAppVersion() {
        assertTrue(OnlineUpdate.looksLikeBoxVersion("2026.10.31.3"))
        assertTrue(OnlineUpdate.looksLikeBoxVersion("v2026.10.31"))
        assertFalse(OnlineUpdate.looksLikeBoxVersion("0.3.0-beta.1"))
        assertFalse(OnlineUpdate.looksLikeBoxVersion("1.0.0"))
        assertFalse(OnlineUpdate.looksLikeBoxVersion(""))
    }

    @Test fun preReleaseSortsBeforeItsRelease() {
        assertTrue(OnlineUpdate.newer("0.3.0", "0.3.0-beta.1"))
        assertFalse(OnlineUpdate.newer("0.3.0-beta.1", "0.3.0"))
        assertTrue(OnlineUpdate.newer("0.3.0-beta.2", "0.3.0-beta.1"))
        assertFalse(OnlineUpdate.newer("0.3.0-beta.1", "0.3.0-beta.2"))
        assertFalse(OnlineUpdate.newer("0.3.0-beta.1", "0.3.0-beta.1"))
        assertTrue(OnlineUpdate.newer("0.3.1-beta.1", "0.3.0"))
        assertFalse(OnlineUpdate.newer("0.2.9", "0.3.0-beta.1"))
    }

    /** Same order as scripts/android_version.py, which turns these into versionCodes that grow the same way. */
    @Test fun preReleaseStagesAndNumbersCompareAsNumbersNotText() {
        assertTrue(OnlineUpdate.newer("0.3.0-beta.10", "0.3.0-beta.9"))
        assertTrue(OnlineUpdate.newer("0.3.0-rc.1", "0.3.0-beta.10"))
        assertTrue(OnlineUpdate.newer("0.3.0-beta.1", "0.3.0-alpha.3"))
        assertTrue(OnlineUpdate.newer("0.3.0-alpha.1", "0.3.0-dev.1"))
        assertFalse(OnlineUpdate.newer("0.3.0-dev.9", "0.3.0-beta.1"))
        assertTrue(OnlineUpdate.newer("0.3.0", "0.3.0-rc.2"))
    }

    @Test fun manifestWithoutAndroidSectionIsNotAnUpdate() {
        val (payload, sig) = signedManifest("""{"format":1,"version":"2026.10.31.3","notes":"box only","online":true}""")
        val error = assertThrows(IllegalArgumentException::class.java) {
            OnlineUpdate.parseManifestOrApi(payload, sig, null, publicKey())
        }
        assertTrue(error.message.orEmpty().contains("安卓"))
    }

    @Test fun signedManifestAndroidSectionWinsOverTheBoxVersion() {
        val json = """
            {"format":1,"version":"2026.10.31.3","notes":"box notes","online":true,
             "android":{"version":"0.3.0-beta.1","notes":"app notes",
               "apk":{"file":"DashPlay-android-0.3.0-beta.1.apk","sha256":"abc","size":42}}}
        """.trimIndent()
        val (payload, sig) = signedManifest(json)
        val info = OnlineUpdate.parseManifestOrApi(payload, sig, null, publicKey())
        assertEquals("0.3.0-beta.1", info.version)
        assertEquals("app notes", info.notes)
        assertEquals("abc", info.apkSha256)
        assertEquals(42L, info.apkSize)
        assertEquals(
            "https://github.com/yizhixiaoheigou/DashPlay/releases/download/v2026.10.31.3/DashPlay-android-0.3.0-beta.1.apk",
            info.apkUrl,
        )
    }

    @Test fun apkNameWithATraversalOrPathIsRejected() {
        for (file in listOf("../evil.apk", "sub/dir/app.apk", "app.bin", "")) {
            val json = """{"version":"2026.10.31.3","android":{"version":"0.3.0","apk":{"file":"$file"}}}"""
            val (payload, sig) = signedManifest(json)
            assertThrows(IllegalArgumentException::class.java) {
                OnlineUpdate.parseManifestOrApi(payload, sig, null, publicKey())
            }
        }
    }

    @Test fun tamperedManifestStaysAHardStop() {
        val (payload, sig) = signedManifest("""{"version":"2026.10.31.3","android":{"version":"0.3.0","apk":{"file":"a.apk"}}}""")
        val tampered = String(payload).replace("0.3.0", "9.9.9").toByteArray()
        val error = assertThrows(IllegalArgumentException::class.java) {
            OnlineUpdate.parseManifestOrApi(tampered, sig, null, publicKey())
        }
        assertTrue(error.message.orEmpty().contains("签名"))
    }

    @Test fun releaseListPicksTheNewestApkAndSkipsBoxPackages() {
        val body = """
            [
              {"tag_name":"v2026.10.31.3","draft":false,"body":"box release","assets":[
                {"name":"dashplay-bundle-2026.10.31.3.tar","browser_download_url":"https://x/b.tar"},
                {"name":"dashplay-update.json","browser_download_url":"https://x/u.json"}]},
              {"tag_name":"v2026.10.31.2","draft":false,"body":"older","assets":[
                {"name":"DashPlay-android-0.2.0-beta.1.apk","browser_download_url":"https://x/a2.apk","size":11,
                 "digest":"sha256:aaaa"},
                {"name":"DashPlay-android-0.3.0-beta.1.apk","browser_download_url":"https://x/a3.apk","size":22,
                 "digest":"sha256:bbbb"}]},
              {"tag_name":"v2026.11.1","draft":true,"assets":[
                {"name":"DashPlay-android-9.9.9.apk","browser_download_url":"https://x/draft.apk"}]}
            ]
        """.trimIndent()
        val info = OnlineUpdate.parseReleaseList(body.toByteArray())
        assertEquals("0.3.0-beta.1", info.version)
        assertEquals("https://x/a3.apk", info.apkUrl)
        assertEquals("bbbb", info.apkSha256)
        assertEquals(22L, info.apkSize)
    }

    @Test fun releaseListWithoutAnyApkIsReportedNotGuessed() {
        val body = """[{"tag_name":"v2026.10.31.3","assets":[{"name":"dashplay-bundle-2026.10.31.3.tar"}]}]"""
        val error = assertThrows(IllegalStateException::class.java) {
            OnlineUpdate.parseReleaseList(body.toByteArray())
        }
        assertTrue(error.message.orEmpty().contains("安卓"))
    }

    @Test fun versionComesFromTheApkName() {
        assertEquals("0.3.0-beta.1", OnlineUpdate.versionFromApkName("DashPlay-android-0.3.0-beta.1.apk"))
        assertEquals("0.3.0", OnlineUpdate.versionFromApkName("DashPlay-android-0.3.0.apk"))
        assertEquals("1.2.3", OnlineUpdate.versionFromApkName("dashplay-android-v1.2.3.apk"))
        assertNull(OnlineUpdate.versionFromApkName("dashplay-bundle-2026.10.31.3.tar"))
        assertNull(OnlineUpdate.versionFromApkName("app.apk"))
    }

    /**
     * Android ships its own provider called "BC" that has no Ed25519. Registering the bundled BouncyCastle only when
     * "BC" is absent (the old code) left the phone verifying with a provider that cannot do Ed25519 at all, so every
     * signed manifest looked tampered with. This pins the lookup to the provider the app registers itself.
     */
    @Test fun platformProviderCalledBcDoesNotShadowTheBundledOne() {
        val fake = object : java.security.Provider("BC", 1.0, "android platform stand-in") {}
        java.security.Security.addProvider(fake)
        try {
            val (payload, sig) = signedManifest("""{"version":"2026.10.31.3","android":{"version":"0.3.0","apk":{"file":"a.apk"}}}""")
            assertTrue(OnlineUpdate.verifyEd25519(payload, sig, publicKey()))
        } finally {
            java.security.Security.removeProvider("BC")
        }
    }
}
