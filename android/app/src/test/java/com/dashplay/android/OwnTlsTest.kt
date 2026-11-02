package com.dashplay.android

import java.io.File
import java.io.StringWriter
import java.math.BigInteger
import java.net.InetAddress
import java.net.Socket
import java.nio.file.Files
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.cert.X509Certificate
import java.util.Date
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLServerSocket
import javax.net.ssl.SSLSocket
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509KeyManager
import java.security.Principal
import org.bouncycastle.asn1.x500.X500Name
import org.bouncycastle.asn1.pkcs.PrivateKeyInfo
import org.bouncycastle.asn1.x509.*
import org.bouncycastle.cert.jcajce.JcaX509CertificateConverter
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder
import org.bouncycastle.jce.provider.BouncyCastleProvider
import org.bouncycastle.openssl.jcajce.JcaPEMWriter
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder
import org.junit.Assert.*
import org.junit.Test

class OwnTlsTest {
    private val now = System.currentTimeMillis()
    private val provider = BouncyCastleProvider()
    private fun keys(ec: Boolean = false) = (if (ec) KeyPairGenerator.getInstance("EC", provider)
        else KeyPairGenerator.getInstance("RSA")).apply {
        initialize(if (ec) 256 else 2048)
    }.generateKeyPair()
    private val rootKey by lazy { keys() }
    private val root by lazy { cert(rootKey, "Fixture CA", rootKey, "Fixture CA", ca = true) }
    private fun cert(key: KeyPair, subject: String, signer: KeyPair = rootKey, issuer: String = "Fixture CA",
        ca: Boolean = false, san: String? = "dashplay.test", starts: Long = now - 60_000,
        ends: Long = now + 10 * OwnTlsMaterial.DAY_MS): X509Certificate {
        val builder = JcaX509v3CertificateBuilder(X500Name("CN=$issuer"), BigInteger(120, java.security.SecureRandom()),
            Date(starts), Date(ends), X500Name("CN=$subject"), key.public)
        builder.addExtension(Extension.basicConstraints, true, BasicConstraints(ca))
        builder.addExtension(Extension.keyUsage, true, KeyUsage(if (ca) KeyUsage.keyCertSign else KeyUsage.digitalSignature))
        if (!ca) builder.addExtension(Extension.extendedKeyUsage, false, ExtendedKeyUsage(KeyPurposeId.id_kp_serverAuth))
        if (san != null) builder.addExtension(Extension.subjectAlternativeName, false, GeneralNames(GeneralName(GeneralName.dNSName, san)))
        return JcaX509CertificateConverter().setProvider(provider).getCertificate(builder.build(
            JcaContentSignerBuilder(if (signer.private.algorithm == "RSA") "SHA256withRSA" else "SHA256withECDSA")
                .setProvider(provider).build(signer.private)))
    }
    private fun pem(vararg values: Any): String = StringWriter().also { out ->
        JcaPEMWriter(out).use { w -> values.forEach(w::writeObject) }
    }.toString()
    private fun fails(block: () -> Unit) {
        try { block(); fail("expected rejection") } catch (_: IllegalArgumentException) { }
    }

    @Test fun domainAndSanRulesDoNotAcceptUrlsIpsOrMultiLabelWildcards() {
        assertEquals("box.example.com", OwnTlsMaterial.domain(" Box.Example.Com. "))
        for (bad in listOf("https://box.example.com", "192.168.1.2", "a..test", "*.example.com", "a.test:8080", "a.test/path", "a.-test"))
            fails { OwnTlsMaterial.domain(bad) }
        assertTrue(OwnTlsMaterial.matches("box.example.com", "*.example.com"))
        assertFalse(OwnTlsMaterial.matches("a.box.example.com", "*.example.com"))
        assertFalse(OwnTlsMaterial.matches("example.com", "*.example.com"))
        assertFalse(OwnTlsMaterial.matches("box.example.com", "b*.example.com"))
    }
    @Test fun certificateServiceTrustAddsOnlyVerifiedAnchorAndStillRejectsUnknownOrExpiredChains() {
        val bundled = CertificateTrust.bundledRoot()
        assertEquals("CN=ISRG Root X1,O=Internet Security Research Group,C=US", bundled.subjectX500Principal.name)
        bundled.verify(bundled.publicKey)
        for (host in listOf("example.com", "www.duckdns.org.attacker.test", "127.0.0.1")) assertNull(CertificateTrust.forHost(host))
        assertNotNull(CertificateTrust.forHost("acme-v02.api.letsencrypt.org"))
        val system = CertificateTrust.manager(null)
        val extraKeys = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null); setCertificateEntry("fixture", root) }
        val trusted = CertificateTrust.combine(system, CertificateTrust.manager(extraKeys))
        val key = keys()
        trusted.checkServerTrusted(arrayOf(cert(key, "dashplay.test"), root), "ECDHE_RSA")
        val unrelatedKeys = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null); setCertificateEntry("public", bundled) }
        val notTrusted = CertificateTrust.combine(system, CertificateTrust.manager(unrelatedKeys))
        try { notTrusted.checkServerTrusted(arrayOf(cert(key, "dashplay.test"), root), "ECDHE_RSA"); fail() }
        catch (_: java.security.cert.CertificateException) {}
        try { trusted.checkServerTrusted(arrayOf(cert(key, "dashplay.test", ends = now - 1), root), "ECDHE_RSA"); fail() }
        catch (_: java.security.cert.CertificateException) {}
    }
    @Test fun rejectWrongDomainWrongKeyExpiredFutureAndCnOnly() {
        val key = keys(); val good = cert(key, "dashplay.test")
        fails { OwnTlsMaterial.parse("wrong.test", pem(good, root), pem(key.private), now) }
        fails { OwnTlsMaterial.parse("dashplay.test", pem(good, root), pem(keys().private), now) }
        for (leaf in listOf(cert(key, "dashplay.test", ends = now - 1), cert(key, "dashplay.test", starts = now + 60_000),
            cert(key, "dashplay.test", ends = now + 60_000), cert(key, "dashplay.test", san = null))) {
            fails { OwnTlsMaterial.parse("dashplay.test", pem(leaf, root), pem(key.private), now) }
        }
        fails { OwnTlsMaterial.parse("dashplay.test", pem(good, cert(keys(), "wrong CA", ca = true)), pem(key.private), now) }
        fails { OwnTlsMaterial.parse("dashplay.test", "secret-invalid-chain", "secret-invalid-key", now) }
    }
    @Test fun privateCaRsaAndEcKeysPerformRealHostnameVerifiedTlsHandshake() {
        for (ec in listOf(false, true)) {
            val key = keys(ec); val leaf = cert(key, "dashplay.test")
            val loaded = OwnTlsMaterial.parse("dashplay.test", pem(leaf, root), pem(key.private), now)
            assertEquals(loaded.fingerprint, OwnTlsMaterial.parse("dashplay.test", pem(leaf, root),
                pem(PrivateKeyInfo.getInstance(key.private.encoded)), now).fingerprint)
            assertFalse(loaded.systemTrusted)
            val km = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply {
                init(loaded.material.keyStore, loaded.material.password)
            }
            val serverContext = SSLContext.getInstance("TLS").apply { init(km.keyManagers, null, null) }
            val listener = serverContext.serverSocketFactory.createServerSocket(0, 1, InetAddress.getLoopbackAddress()) as SSLServerSocket
            listener.soTimeout = 5000
            var serverFailure: Throwable? = null
            val thread = Thread {
                try { (listener.accept() as SSLSocket).use { it.soTimeout = 5000; it.startHandshake(); it.outputStream.write(42) } }
                catch (failure: Throwable) { serverFailure = failure }
            }.apply { start() }
            val trust = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null); setCertificateEntry("fixture-root", root) }
            val tm = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(trust) }
            val client = SSLContext.getInstance("TLS").apply { init(null, tm.trustManagers, null) }
            try {
                val raw = Socket(InetAddress.getLoopbackAddress(), listener.localPort)
                (client.socketFactory.createSocket(raw, "dashplay.test", listener.localPort, true) as SSLSocket).use {
                    it.soTimeout = 5000
                    it.sslParameters = it.sslParameters.apply { endpointIdentificationAlgorithm = "HTTPS" }
                    it.startHandshake(); assertEquals(42, it.inputStream.read())
                }
            } finally { listener.close(); thread.join(6000) }
            assertFalse(thread.isAlive); assertNull(serverFailure)
            val out = File("build/test-artifacts/own-tls").apply { mkdirs() }
            File(out, if (ec) "ec-chain.pem" else "rsa-chain.pem").writeText(pem(leaf, root))
            File(out, if (ec) "ec-key.pem" else "rsa-key.pem").writeText(pem(PrivateKeyInfo.getInstance(key.private.encoded)))
            File(out, "root.pem").writeText(pem(root))
        }
    }
    @Test fun legacyEcSelectionFallsBackWithoutChangingOtherAlgorithmsOrClientSelection() {
        val key = keys(true)
        val loaded = OwnTlsMaterial.parse("dashplay.test", pem(cert(key, "dashplay.test"), root), pem(key.private), now)
        val factory = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply {
            init(loaded.material.keyStore, loaded.material.password)
        }
        val delegate = factory.keyManagers.filterIsInstance<X509KeyManager>().first()
        val requests = mutableListOf<String?>()
        val legacy = object : X509KeyManager by delegate {
            override fun chooseServerAlias(type: String?, issuers: Array<out Principal>?, socket: Socket?): String? {
                requests += type
                return if (type == "EC_EC") null else delegate.chooseServerAlias(type, issuers, socket)
            }
        }
        val compatible = LegacyEcKeyManager(legacy)
        val alias = compatible.chooseServerAlias("EC_EC", null, null)
        assertNotNull(alias)
        assertEquals(listOf("EC_EC", "EC"), requests)
        assertArrayEquals(loaded.chain.first().encoded, compatible.getCertificateChain(alias)[0].encoded)
        requests.clear()
        assertNull(compatible.chooseServerAlias("RSA", null, null))
        assertEquals(listOf("RSA"), requests)
        assertEquals(delegate.chooseClientAlias(arrayOf("EC"), null, null), compatible.chooseClientAlias(arrayOf("EC"), null, null))
    }
    @Test fun invalidReplacementKeepsOldMaterialAcrossRestartAndDisable() {
        val dir = Files.createTempDirectory("dashplay-own-tls-").toFile()
        try {
            val key = keys(); val store = OwnTlsStore(dir) { now }
            store.install("dashplay.test", pem(cert(key, "dashplay.test"), root), pem(key.private))
            val fingerprint = store.selected()!!.fingerprint
            val original = File(dir, "certificate.json").readBytes()
            fails { store.install("wrong.test", pem(cert(key, "dashplay.test"), root), pem(key.private)) }
            assertArrayEquals(original, File(dir, "certificate.json").readBytes())
            assertEquals(fingerprint, OwnTlsStore(dir) { now }.selected()!!.fingerprint)
            store.enable(false)
            val reloaded = OwnTlsStore(dir) { now }
            assertNull(reloaded.selected()); assertFalse(reloaded.state().enabled)
            reloaded.enable(true); assertEquals(fingerprint, reloaded.selected()!!.fingerprint)
        } finally { dir.deleteRecursively() }
    }
    @Test fun expiryAndCorruptionFallBackWithoutDeletingTheSavedRecord() {
        val dir = Files.createTempDirectory("dashplay-own-tls-").toFile()
        try {
            var clock = now
            val key = keys(); val store = OwnTlsStore(dir) { clock }
            store.install("dashplay.test", pem(cert(key, "dashplay.test"), root), pem(key.private))
            clock += 20 * OwnTlsMaterial.DAY_MS
            assertNull(store.selected()); assertFalse(store.state().usable)
            fails { store.enable(true) }
            File(dir, "certificate.json").writeText("damaged-private-material")
            val damaged = OwnTlsStore(dir) { clock }
            assertNull(damaged.selected())
            assertFalse(damaged.state().message.contains("damaged-private-material"))
            assertEquals("damaged-private-material", File(dir, "certificate.json").readText())
        } finally { dir.deleteRecursively() }
    }
}
