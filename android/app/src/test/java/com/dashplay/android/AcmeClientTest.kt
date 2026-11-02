package com.dashplay.android

import java.io.File
import java.io.StringWriter
import java.math.BigInteger
import java.net.URI
import java.nio.file.Files
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.RSAPublicKeySpec
import java.util.Date
import java.util.Base64
import kotlinx.serialization.json.*
import org.bouncycastle.asn1.pkcs.PKCSObjectIdentifiers
import org.bouncycastle.asn1.x500.X500Name
import org.bouncycastle.asn1.x509.*
import org.bouncycastle.cert.X509v3CertificateBuilder
import org.bouncycastle.cert.jcajce.JcaX509CertificateConverter
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder
import org.bouncycastle.jce.provider.BouncyCastleProvider
import org.bouncycastle.openssl.jcajce.JcaPEMWriter
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder
import org.bouncycastle.operator.jcajce.JcaContentVerifierProviderBuilder
import org.bouncycastle.pkcs.PKCS10CertificationRequest
import org.junit.Assert.*
import org.junit.Test

/** Local ACME peer verifies actual JWS signatures/nonces and signs the submitted CSR with a generated test CA.
 * This is protocol integration evidence, not a public Let's Encrypt/DNS issuance claim.
 */
class AcmeClientTest {
    private fun failure(reason: String, action: () -> Unit): CertificateFailure {
        try { action(); fail("expected $reason") } catch (e: CertificateFailure) { assertEquals(reason, e.reason); return e }
        throw AssertionError()
    }
    private class Peer : CertificateHttp, AcmeDnsChallenge {
        val account = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
        val host = "https://acme.fixture.test"
        val domain = "dashplay.test"
        var clock = 0L
        var cancelled = false
        var badNonce = 1
        var wrongIdentifier = false
        var foreignEndpoint = false
        var rateLimited = false
        var wrongCertificate = false
        var invalidAuthorization = false
        var cleanupFails = false
        var stopOnPropagation = false
        var pendingForever = false
        var delayHeader = "2"
        var presentCount = 0
        var cleanupCount = 0
        var orders = 0
        var nonces = 0
        var authPolls = 0
        var orderPolls = 0
        var thumbprint = ""
        var txt: String? = null
        var chain = ""
        var acknowledged = false
        var finalized = false
        val requests = mutableListOf<String>()
        val usedNonces = mutableSetOf<String>()
        var availableNonce: String? = null
        private val challengeToken = "fixture_AcmeChallengeToken_123456789"
        private fun json(value: String) = Json.parseToJsonElement(value).jsonObject
        private fun reply(code: Int = 200, text: String, location: String? = null, extra: Map<String, String> = emptyMap()): CertificateReply {
            availableNonce = "fixture_nonce_${++nonces}"
            return CertificateReply(code, mapOf("Replay-Nonce" to availableNonce!!) +
                (location?.let { mapOf("Location" to it) } ?: emptyMap()) + extra, text.toByteArray())
        }
        override fun request(method: String, url: String, headers: Map<String, String>, body: ByteArray?): CertificateReply {
            assertEquals(host, URI(url).let { "${it.scheme}://${it.host}" })
            val path = URI(url).path; requests += "$method $path"
            if (path == "/directory") {
                assertEquals("GET", method)
                return reply(text="""{"newNonce":"$host/nonce","newAccount":"$host/account","newOrder":"${if (foreignEndpoint) "https://untrusted.invalid" else host}/order"}""")
            }
            if (path == "/nonce") { assertEquals("HEAD", method); return reply(204, "") }
            assertEquals("POST", method)
            assertEquals("application/jose+json", headers["Content-Type"])
            val jws = json(body!!.toString(Charsets.UTF_8))
            val protected = json(String(Base64.getUrlDecoder().decode(jws.string("protected"))))
            assertEquals(url, protected.string("url")); assertEquals("RS256", protected.string("alg"))
            val nonce = protected.string("nonce")
            assertEquals(availableNonce, nonce); assertTrue(usedNonces.add(nonce))
            if (path == "/account") {
                val jwk = protected["jwk"]!!.jsonObject
                assertFalse(protected.containsKey("kid"))
                val public = KeyFactory.getInstance("RSA").generatePublic(RSAPublicKeySpec(
                    BigInteger(1, Base64.getUrlDecoder().decode(jwk.string("n"))), BigInteger(1, Base64.getUrlDecoder().decode(jwk.string("e")))))
                assertArrayEquals(account.public.encoded, public.encoded)
                thumbprint = AcmeClient.b64(AcmeClient.sha256(jwk.toString().toByteArray()))
            } else {
                assertEquals("$host/account/1", protected.string("kid")); assertFalse(protected.containsKey("jwk"))
            }
            assertTrue(Signature.getInstance("SHA256withRSA").run {
                initVerify(account.public); update("${jws.string("protected")}.${jws.string("payload")}".toByteArray())
                verify(Base64.getUrlDecoder().decode(jws.string("signature")))
            })
            if (badNonce-- > 0) return reply(400, """{"type":"urn:ietf:params:acme:error:badNonce","detail":"secret must never escape"}""")
            val rawPayload = String(Base64.getUrlDecoder().decode(jws.string("payload")))
            val identifier = """{"type":"dns","value":"${if (wrongIdentifier) "other.test" else domain}"}"""
            fun order(status: String) = """{"status":"$status","identifiers":[$identifier],"authorizations":["$host/auth/1"],"finalize":"$host/finalize/1","certificate":"$host/cert/1"}"""
            return when (path) {
                "/account" -> { assertTrue(json(rawPayload)["termsOfServiceAgreed"]!!.jsonPrimitive.boolean); reply(201,"""{"status":"valid"}""","$host/account/1") }
                "/order" -> {
                    orders++
                    assertEquals(domain, json(rawPayload)["identifiers"]!!.jsonArray.single().jsonObject.string("value"))
                    if (rateLimited) reply(429,"""{"type":"urn:ietf:params:acme:error:rateLimited","detail":"secret token"}""",extra=mapOf("Retry-After" to "3600"))
                    else reply(201,order("pending"),"$host/order/1")
                }
                "/auth/1" -> {
                    assertEquals("", rawPayload) // POST-as-GET is an empty payload, not an empty JSON object.
                    val status = if (!acknowledged || authPolls++ == 0 || pendingForever) "pending" else if (invalidAuthorization) "invalid" else "valid"
                    reply(text="""{"status":"$status","identifier":$identifier,"challenges":[{"type":"dns-01","url":"$host/challenge/1","token":"$challengeToken"}]}""",extra=mapOf("Retry-After" to delayHeader))
                }
                "/challenge/1" -> {
                    assertEquals("{}", rawPayload)
                    assertEquals(AcmeClient.b64(AcmeClient.sha256("$challengeToken.$thumbprint".toByteArray())), txt)
                    acknowledged=true; reply(text="""{"status":"processing"}""")
                }
                "/order/1" -> {
                    assertEquals("", rawPayload)
                    reply(text=order(if (!finalized) "ready" else if (orderPolls++ == 0) "processing" else "valid"),extra=mapOf("Retry-After" to "1"))
                }
                "/finalize/1" -> {
                    assertNull(txt); assertEquals(1, cleanupCount)
                    val csr = PKCS10CertificationRequest(Base64.getUrlDecoder().decode(json(rawPayload).string("csr")))
                    assertTrue(csr.isSignatureValid(JcaContentVerifierProviderBuilder().setProvider(BouncyCastleProvider()).build(csr.subjectPublicKeyInfo)))
                    val extensions = Extensions.getInstance(csr.getAttributes(PKCSObjectIdentifiers.pkcs_9_at_extensionRequest).single().attrValues.getObjectAt(0))
                    val sans = GeneralNames.getInstance(extensions.getExtension(Extension.subjectAlternativeName).parsedValue).names
                    assertEquals(1, sans.size); assertEquals(domain, sans.single().name.toString())
                    chain = sign(csr, if (wrongCertificate) "wrong.test" else domain)
                    finalized=true; reply(text=order("processing"))
                }
                "/cert/1" -> { assertEquals("",rawPayload); assertEquals("application/pem-certificate-chain",headers["Accept"]); reply(text=chain) }
                else -> throw AssertionError("unexpected endpoint")
            }
        }
        override fun present(name: String, value: String) { assertEquals("_acme-challenge.$domain", name); txt=value; presentCount++ }
        override fun remove(name: String, value: String) { cleanupCount++; if (cleanupFails) throw CertificateFailure("dns_network"); assertEquals(txt,value); txt=null }
        fun client() = AcmeClient(this,"$host/directory",cancelled={cancelled},pause={
            clock += it * 1_000_000
            if (stopOnPropagation) cancelled=true
        },nanoTime={clock},propagationMillis=1000)
        private fun sign(csr: PKCS10CertificationRequest, name: String): String {
            val provider = BouncyCastleProvider()
            val issuerKey = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
            val issuer = X500Name("CN=ACME fixture root")
            val start = Date(System.currentTimeMillis()-60_000); val end = Date(System.currentTimeMillis()+10*OwnTlsMaterial.DAY_MS)
            val signer = JcaContentSignerBuilder("SHA256withRSA").setProvider(provider).build(issuerKey.private)
            val root = JcaX509v3CertificateBuilder(issuer,BigInteger.ONE,start,end,issuer,issuerKey.public)
                .addExtension(Extension.basicConstraints,true,BasicConstraints(true))
                .addExtension(Extension.keyUsage,true,KeyUsage(KeyUsage.keyCertSign)).build(signer)
            val leaf = X509v3CertificateBuilder(issuer,BigInteger.TEN,start,end,csr.subject,csr.subjectPublicKeyInfo)
                .addExtension(Extension.basicConstraints,true,BasicConstraints(false))
                .addExtension(Extension.keyUsage,true,KeyUsage(KeyUsage.digitalSignature))
                .addExtension(Extension.extendedKeyUsage,false,ExtendedKeyUsage(KeyPurposeId.id_kp_serverAuth))
                .addExtension(Extension.subjectAlternativeName,false,GeneralNames(GeneralName(GeneralName.dNSName,name))).build(signer)
            return StringWriter().also { out -> JcaPEMWriter(out).use { writer ->
                val converter = JcaX509CertificateConverter().setProvider(provider)
                writer.writeObject(converter.getCertificate(leaf)); writer.writeObject(converter.getCertificate(root))
            } }.toString()
        }
    }
    @Test fun dns01CompletesWithRealJwsCsrCertificateAndBadNonceRetry() {
        val peer=Peer(); val result=peer.client().issue(peer.domain,peer.account,peer,true)
        val material=OwnTlsMaterial.parse(peer.domain,result.chain,result.key)
        assertEquals(peer.domain,material.domain); assertFalse(material.systemTrusted)
        assertEquals(1,peer.orders);assertEquals(1,peer.presentCount);assertEquals(1,peer.cleanupCount);assertNull(peer.txt)
        assertTrue(peer.requests.count { it=="POST /account" }==2)
        assertTrue(peer.clock>=5_000_000_000L)
    }
    @Test fun foreignEndpointAndIdentifierAreRejectedBeforeDnsMutation() {
        for (foreign in listOf(true,false)) {
            val peer=Peer().apply { foreignEndpoint=foreign;wrongIdentifier=!foreign }
            failure(if(foreign)"endpoint" else "identifier") { peer.client().issue(peer.domain,peer.account,peer,true) }
            assertEquals(0,peer.presentCount);assertEquals(0,peer.cleanupCount)
        }
    }
    @Test fun rateLimitIsPreservedAndServerSecretsAreNotReturned() {
        val peer=Peer().apply { rateLimited=true }
        val error=failure("rate_limited") { peer.client().issue(peer.domain,peer.account,peer,true) }
        assertEquals(3600,error.retryAfterSeconds);assertFalse(error.toString().contains("secret"));assertEquals(0,peer.presentCount)
    }
    @Test fun cancellationAndInvalidAuthorizationAlwaysAttemptOwnedTxtCleanup() {
        for (cancel in listOf(true,false)) {
            val peer=Peer().apply { stopOnPropagation=cancel;invalidAuthorization=!cancel }
            failure(if(cancel)"cancelled" else "authorization") { peer.client().issue(peer.domain,peer.account,peer,true) }
            assertEquals(1,peer.cleanupCount);assertNull(peer.txt);assertFalse(peer.finalized)
        }
    }
    @Test fun cleanupFailureStopsBeforeIssuanceAndCertificateForAnotherNameIsRejected() {
        val dirty=Peer().apply { cleanupFails=true }
        failure("dns_cleanup") { dirty.client().issue(dirty.domain,dirty.account,dirty,true) }
        assertFalse(dirty.finalized);assertNotNull(dirty.txt)
        val wrong=Peer().apply { wrongCertificate=true }
        failure("certificate") { wrong.client().issue(wrong.domain,wrong.account,wrong,true) }
        assertEquals(1,wrong.cleanupCount)
    }
    @Test fun timeoutAndRepeatedBadNonceHaveFiniteLimits() {
        val endless=Peer().apply { pendingForever=true }
        failure("timeout") { endless.client().issue(endless.domain,endless.account,endless,true) }
        assertEquals(1,endless.cleanupCount);assertNull(endless.txt)
        val nonce=Peer().apply { badNonce=10 }
        failure("nonce") { nonce.client().issue(nonce.domain,nonce.account,nonce,true) }
        assertEquals(3,nonce.requests.count { it=="POST /account" });assertEquals(0,nonce.presentCount)
    }
    @Test fun accountPersistsAndDamagedRecordIsRetained() {
        val directory=Files.createTempDirectory("dashplay-acme-account-").toFile()
        try {
            val first=AcmeAccountStore(directory).loadOrCreate()
            assertArrayEquals(first.private.encoded,AcmeAccountStore(directory).loadOrCreate().private.encoded)
            val file=File(directory,"account.pk8");file.writeText("damaged fixture")
            failure("account_storage") { AcmeAccountStore(directory).loadOrCreate() }
            assertEquals("damaged fixture",file.readText())
        } finally { directory.deleteRecursively() }
    }
    @Test fun retryAfterSupportsDateAndTermsAreRequiredBeforeAnyRequest() {
        assertEquals(60,AcmeClient.retryAfter("Thu, 01 Jan 1970 00:01:00 GMT",0))
        assertEquals(0,AcmeClient.retryAfter("not a date",0));assertEquals(604800,AcmeClient.retryAfter("99999999",0))
        val peer=Peer()
        try { peer.client().issue(peer.domain,peer.account,peer,false);fail() } catch (_: IllegalArgumentException) { }
        assertTrue(peer.requests.isEmpty())
    }
}
