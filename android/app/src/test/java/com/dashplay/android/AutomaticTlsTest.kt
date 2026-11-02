package com.dashplay.android

import java.io.File
import java.io.StringWriter
import java.math.BigInteger
import java.nio.file.Files
import java.security.KeyPairGenerator
import java.util.Date
import kotlinx.serialization.json.Json
import org.bouncycastle.asn1.x500.X500Name
import org.bouncycastle.asn1.x509.*
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder
import org.bouncycastle.openssl.jcajce.JcaPEMWriter
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder
import org.bouncycastle.jce.provider.BouncyCastleProvider
import org.junit.Assert.*
import org.junit.Test

class AutomaticTlsTest {
    private val secret = "fixture-token-1234567890"
    @Test fun accountReloadWorksWithLegacyProviderWithoutCrtInterface() {
        val dir = Files.createTempDirectory("legacy-account").toFile()
        val created = AcmeAccountStore(dir).loadOrCreate()
        val reopened = AcmeAccountStore(dir) { encoded ->
            val key = java.security.KeyFactory.getInstance("RSA").generatePrivate(java.security.spec.PKCS8EncodedKeySpec(encoded)) as java.security.interfaces.RSAPrivateKey
            object : java.security.interfaces.RSAPrivateKey {
                override fun getAlgorithm() = key.algorithm
                override fun getFormat() = key.format
                override fun getEncoded() = key.encoded
                override fun getModulus() = key.modulus
                override fun getPrivateExponent() = key.privateExponent
            }
        }.loadOrCreate()
        assertArrayEquals(created.public.encoded, reopened.public.encoded)
        assertFalse(reopened.private is java.security.interfaces.RSAPrivateCrtKey)
        val message = "legacy account fixture".toByteArray()
        val signature = java.security.Signature.getInstance("SHA256withRSA").run { initSign(reopened.private); update(message); sign() }
        assertTrue(java.security.Signature.getInstance("SHA256withRSA").run { initVerify(created.public); update(message); verify(signature) })
    }
    private fun config(domain: String = "fixture.duckdns.org", token: String? = secret, enabled: Boolean = true) =
        AutomaticTlsUpdate(enabled, "duckdns", domain, token = token, termsAccepted = true)
    private fun issued(now: Long, domain: String = "fixture.duckdns.org", days: Int = 90): AcmeClient.Issued {
        val pair = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
        val dn = X500Name("CN=$domain")
        val cert = JcaX509v3CertificateBuilder(dn, BigInteger.ONE, Date(now - 60000), Date(now + days * 86400_000L), dn, pair.public)
            .addExtension(Extension.basicConstraints, true, BasicConstraints(false))
            .addExtension(Extension.keyUsage, true, KeyUsage(KeyUsage.digitalSignature))
            .addExtension(Extension.extendedKeyUsage, false, ExtendedKeyUsage(KeyPurposeId.id_kp_serverAuth))
            .addExtension(Extension.subjectAlternativeName, false, GeneralNames(GeneralName(GeneralName.dNSName, domain)))
            .build(JcaContentSignerBuilder("SHA256withRSA").setProvider(BouncyCastleProvider()).build(pair.private))
        fun pem(value: Any) = StringWriter().also { out -> JcaPEMWriter(out).use { it.writeObject(value) } }.toString()
        return AcmeClient.Issued(domain, pem(cert), pem(pair.private))
    }
    private inner class Fixture {
        val directory = Files.createTempDirectory("automatic-tls").toFile()
        var time = System.currentTimeMillis()
        val store = AutomaticTlsStore(File(directory, "auto")) { time }
        val certs = OwnTlsStore(File(directory, "cert")) { time }
        var online = true
        var address: String? = "192.0.2.1"
        var requests = mutableListOf<String>()
        var failDns = false
        var installed = 0
        var issueCount = 0
        var issuer: (AutomaticTlsStore.Profile, CertificateDns, () -> Boolean) -> AcmeClient.Issued = { _, _, _ -> issued(time) }
        val http = CertificateHttp { _, url, _, _ ->
            requests += url
            if (failDns) throw CertificateFailure("dns_network")
            CertificateReply(200, emptyMap(), "OK".toByteArray())
        }
        val worker = AutomaticTlsWorker(store, certs, { online }, { address }, { installed++ }, now = { time },
            dnsFactory = { CertificateDns(it.dns, http, store.journal(it)) },
            issue = { p, d, c -> issueCount++; issuer(p,d,c) })
        fun enable() = store.configure(config())
        fun advance(millis: Long = 15_001) { time += millis; worker.tick() }
    }

    @Test fun offlineAndNoReceiverAddressWaitWithoutIssuingOrIncreasingFailures() {
        val f = Fixture(); f.enable(); f.online = false
        repeat(4) { f.advance() }
        assertEquals("waiting_network", f.store.state().phase); assertEquals(0, f.store.read().failures)
        assertEquals(0, f.requests.size); assertEquals(0, f.issueCount)
        f.online = true; f.address = null; f.advance()
        assertEquals("waiting_address", f.store.state().phase); assertEquals(0, f.requests.size)
        f.address = "192.0.2.1"; f.advance()
        assertEquals("ready", f.store.state().phase); assertEquals(1, f.installed)
        assertEquals("fixture.duckdns.org", f.certs.selected()?.domain)
    }
    @Test fun retryStartsAfterFailureFinishesAndExplicitServerDelaySurvivesDisableRestartAndRetry() {
        val f = Fixture(); f.enable()
        f.issuer = { _, _, _ -> f.time += 120_000; throw CertificateFailure("rate_limited", 3600) }
        f.worker.tick()
        val deadline = f.time + 3600_000
        assertEquals(deadline, f.store.read().nextAttempt)
        f.store.configure(config(enabled = false)); f.store.configure(config(token = null)); f.store.retry()
        assertEquals(deadline, f.store.read().serverNotBefore)
        val reopened = AutomaticTlsStore(f.store.directory) { f.time }
        assertEquals(deadline, reopened.read().serverNotBefore)
        f.advance(3599_000); assertEquals(1, f.issueCount)
        f.issuer = { _, _, _ -> issued(f.time) }; f.advance(1001)
        assertEquals(1, f.installed); assertEquals(0, f.store.read().failures)
        assertEquals(listOf(60_000L,120_000L,300_000L,900_000L,3_600_000L,3_600_000L),
            (1..6).map(AutomaticTlsWorker::backoff))
    }
    @Test fun changedConfigurationDiscardsCompletedOldRequestAndCleansItsJournalUsingOldCredentials() {
        val f = Fixture(); f.enable()
        f.issuer = { p, dns, cancelled ->
            dns.present("_acme-challenge.${p.dns.domain}", "x".repeat(43))
            f.store.configure(config("replacement.duckdns.org", "new-fixture-token-123456"))
            assertTrue(cancelled()); issued(f.time)
        }
        f.worker.tick()
        assertEquals(0, f.installed); assertNull(f.certs.selected()); assertEquals(1, f.store.read().retired.size)
        f.store.configure(config(enabled = false))
        f.requests.clear(); f.worker.tick()
        assertEquals(0, f.store.read().retired.size)
        assertEquals(1, f.requests.size)
        assertTrue(f.requests.single().contains("token=$secret")); assertTrue(f.requests.single().contains("clear=true"))
        assertFalse(f.requests.single().contains("new-fixture"))
        assertEquals("off", f.store.state().phase)
    }
    @Test fun failedCleanupSurvivesProcessRestartAndDisabledAutomationStillCleansOwnedTxt() {
        val f = Fixture(); f.enable(); val p = f.store.read().profile!!
        CertificateDns(p.dns, f.http, f.store.journal(p)).present("_acme-challenge.${p.dns.domain}", "x".repeat(43))
        f.store.configure(config(enabled = false)); f.failDns = true; f.worker.tick()
        assertEquals("retry", f.store.state().phase); assertTrue(f.store.journal(p).exists())
        val reopened = AutomaticTlsStore(f.store.directory) { f.time }
        assertFalse(reopened.state().enabled); assertEquals(1, reopened.state().pendingCleanups)
        f.failDns = false; f.advance(60_001)
        assertFalse(f.store.journal(p).exists()); assertEquals(0, f.issueCount)
    }
    @Test fun manualReplacementWinsOverInFlightAutomaticResult() {
        val f = Fixture(); f.enable(); val manual = issued(f.time, "manual.test")
        f.issuer = { _, _, cancelled ->
            f.store.manual { f.certs.install(manual.domain, manual.chain, manual.key) }
            assertTrue(cancelled()); issued(f.time)
        }
        f.worker.tick()
        assertFalse(f.store.state().enabled); assertEquals("manual.test", f.certs.selected()?.domain)
        assertEquals(0, f.installed); f.advance(86400_000); assertEquals(1, f.issueCount)
    }
    @Test fun addressChangesRefreshDnsWithoutReissuingAndRenewalDeadlineDoesNotSlideForever() {
        val f = Fixture(); f.enable(); f.worker.tick()
        assertEquals(1, f.issueCount); f.requests.clear(); f.advance()
        assertEquals(0, f.requests.size)
        f.address = "192.0.2.2"; f.advance()
        assertTrue(f.requests.single().contains("ip=192.0.2.2")); assertEquals(1, f.issueCount)
        // Enter the renewal window, then require a second renewal check while that window stays open.
        f.issuer = { _, _, _ -> issued(f.time, days = 10) }
        f.advance(61L * 86400_000); assertEquals(2, f.issueCount)
        val next = f.store.read().renewNext
        repeat(10) { f.advance() }
        assertEquals(next, f.store.read().renewNext); assertEquals(2, f.issueCount)
        f.advance(next - f.time + 1); assertEquals(3, f.issueCount)
    }
    @Test fun stateNeverReturnsTokenAndInvalidConfigurationOrCorruptFileIsNotSilentlyReplaced() {
        val f = Fixture(); f.enable(); val before = File(f.store.directory, "automation.json").readBytes()
        assertFalse(Json.encodeToString(f.store.state()).contains(secret)); assertFalse(f.store.read().toString().contains(secret))
        try { f.store.configure(config("other.duckdns.org", token = null)); fail() } catch (_: IllegalArgumentException) {}
        assertArrayEquals(before, File(f.store.directory, "automation.json").readBytes())
        File(f.store.directory, "automation.json").writeText("broken-private-fixture")
        val broken = AutomaticTlsStore(f.store.directory)
        try { broken.configure(config()); fail() } catch (error: CertificateFailure) { assertEquals("automation_storage", error.reason) }
        assertEquals("broken-private-fixture", File(f.store.directory, "automation.json").readText())
    }
}
