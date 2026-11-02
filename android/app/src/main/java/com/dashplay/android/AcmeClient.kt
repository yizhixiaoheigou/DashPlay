package com.dashplay.android

import java.net.URI
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.Signature
import java.security.interfaces.RSAPublicKey
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone
import kotlinx.serialization.json.*
import org.bouncycastle.asn1.pkcs.PKCSObjectIdentifiers
import org.bouncycastle.asn1.x500.X500Name
import org.bouncycastle.asn1.x509.Extension
import org.bouncycastle.asn1.x509.ExtensionsGenerator
import org.bouncycastle.asn1.x509.GeneralName
import org.bouncycastle.asn1.x509.GeneralNames
import org.bouncycastle.jce.provider.BouncyCastleProvider
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder
import org.bouncycastle.pkcs.jcajce.JcaPKCS10CertificationRequestBuilder
import org.bouncycastle.util.encoders.Base64

internal interface AcmeDnsChallenge {
    /** Implementations journal ownership before writing, so process death can be recovered. */
    fun present(name: String, value: String)
    /** Remove only the owned challenge; retain the journal when remote cleanup fails. */
    fun remove(name: String, value: String)
}

/** Single-name RFC 8555 DNS-01 client, independent of Android lifecycle and UI. */
internal class AcmeClient(
    private val http: CertificateHttp,
    private val directoryUrl: String = PRODUCTION,
    private val cancelled: () -> Boolean = { false },
    private val pause: (Long) -> Unit = { Thread.sleep(it) },
    private val nanoTime: () -> Long = System::nanoTime,
    private val now: () -> Long = System::currentTimeMillis,
    private val propagationMillis: Long = 60_000,
) {
    class Issued(val domain: String, val chain: String, val key: String) {
        override fun toString() = "Issued(domain=$domain)"
    }
    private lateinit var account: KeyPair
    private lateinit var directory: JsonObject
    private var kid: String? = null
    private var nonce: String? = null
    private var deadline = 0L

    fun issue(domain: String, accountKey: KeyPair, dns: AcmeDnsChallenge, termsAccepted: Boolean): Issued {
        require(termsAccepted) { "请先同意证书服务条款。" }
        val name = OwnTlsMaterial.domain(domain)
        account = accountKey; kid = null; nonce = null
        deadline = nanoTime() + 240_000_000_000L
        try {
            directory = read(request("GET", endpoint(directoryUrl))).jsonObject
            val registration = signed(endpoint(directory.string("newAccount")), buildJsonObject { put("termsOfServiceAgreed", true) })
            val accountState = read(registration).jsonObject
            if (accountState.string("status") != "valid") throw CertificateFailure("account")
            kid = endpoint(registration.header("Location") ?: throw CertificateFailure("protocol"))
            val orderReply = signed(endpoint(directory.string("newOrder")), buildJsonObject {
                put("identifiers", buildJsonArray { add(buildJsonObject { put("type", "dns"); put("value", name) }) })
            })
            val orderUrl = endpoint(orderReply.header("Location") ?: throw CertificateFailure("protocol"))
            var order = read(orderReply).jsonObject
            checkIdentifiers(order, name)
            val authorizations = order["authorizations"]?.jsonArray ?: throw CertificateFailure("protocol")
            if (authorizations.size != 1) throw CertificateFailure("identifier")
            val authUrl = endpoint(authorizations.single().jsonPrimitive.content)
            var authorization = read(signed(authUrl, null)).jsonObject
            checkIdentifier(authorization["identifier"]?.jsonObject, name)
            if (authorization["wildcard"]?.jsonPrimitive?.booleanOrNull == true) throw CertificateFailure("identifier")
            if (authorization.string("status") != "valid") {
                if (authorization.string("status") != "pending") throw CertificateFailure("authorization")
                val challenge = authorization["challenges"]?.jsonArray?.map { it.jsonObject }
                    ?.firstOrNull { it.string("type") == "dns-01" } ?: throw CertificateFailure("challenge")
                val token = challenge.string("token")
                if (!token.matches(Regex("[A-Za-z0-9_-]{22,256}"))) throw CertificateFailure("challenge")
                val challengeUrl = endpoint(challenge.string("url"))
                val value = b64(sha256("$token.${b64(sha256(jwk().toString().toByteArray(Charsets.UTF_8)))}".toByteArray(Charsets.US_ASCII)))
                val record = "_acme-challenge.$name"
                var failure: Exception? = null
                try {
                    guard(); dns.present(record, value)
                    waitMillis(propagationMillis)
                    signed(challengeUrl, buildJsonObject { })
                    authorization = poll(authUrl, setOf("pending", "processing"), "valid", "authorization")
                    checkIdentifier(authorization["identifier"]?.jsonObject, name)
                } catch (error: Exception) { failure = error; throw error }
                finally {
                    // Cleanup is deliberately attempted after cancellation; providers must still bound requests.
                    try { dns.remove(record, value) }
                    catch (_: Exception) { if (failure == null) throw CertificateFailure("dns_cleanup") }
                }
            }
            order = poll(orderUrl, setOf("pending"), "ready", "order")
            checkIdentifiers(order, name)
            val certificateKey = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
            val extensions = ExtensionsGenerator().apply {
                addExtension(Extension.subjectAlternativeName, false, GeneralNames(GeneralName(GeneralName.dNSName, name)))
            }
            val csr = JcaPKCS10CertificationRequestBuilder(X500Name("CN=$name"), certificateKey.public)
                .addAttribute(PKCSObjectIdentifiers.pkcs_9_at_extensionRequest, extensions.generate())
                .build(JcaContentSignerBuilder("SHA256withRSA").setProvider(BouncyCastleProvider()).build(certificateKey.private))
            signed(endpoint(order.string("finalize")), buildJsonObject { put("csr", b64(csr.encoded)) })
            order = poll(orderUrl, setOf("ready", "processing"), "valid", "order")
            checkIdentifiers(order, name)
            val chain = signed(endpoint(order.string("certificate")), null, "application/pem-certificate-chain").text()
            val key = "-----BEGIN PRIVATE KEY-----\n" + Base64.toBase64String(certificateKey.private.encoded)
                .chunked(64).joinToString("\n") + "\n-----END PRIVATE KEY-----\n"
            try { OwnTlsMaterial.parse(name, chain, key, now()) }
            catch (_: Exception) { throw CertificateFailure("certificate") }
            guard()
            return Issued(name, chain, key)
        } catch (error: CertificateFailure) { throw error }
        catch (_: Exception) { throw CertificateFailure(if (cancelled()) "cancelled" else "protocol") }
    }

    private fun poll(url: String, pending: Set<String>, success: String, failure: String): JsonObject {
        while (true) {
            val reply = signed(url, null)
            val value = read(reply).jsonObject
            val status = value.string("status")
            if (status == success) return value
            if (status !in pending) throw CertificateFailure(failure)
            val seconds = retryAfter(reply.header("Retry-After"), now()).coerceAtLeast(2)
            if (seconds > 240) throw CertificateFailure("pending", seconds)
            waitMillis(seconds * 1000)
        }
    }
    private fun signed(url: String, payload: JsonObject?, accept: String = "application/json"): CertificateReply {
        repeat(3) {
            if (nonce == null) nonce = request("HEAD", endpoint(directory.string("newNonce"))).header("Replay-Nonce")?.takeIf(::validNonce)
            val currentNonce = nonce ?: throw CertificateFailure("nonce")
            nonce = null
            val protected = buildJsonObject {
                put("alg", "RS256"); put("nonce", currentNonce); put("url", url)
                if (kid == null) put("jwk", jwk()) else put("kid", kid!!)
            }
            val protected64 = b64(protected.toString().toByteArray(Charsets.UTF_8))
            val payload64 = if (payload == null) "" else b64(payload.toString().toByteArray(Charsets.UTF_8))
            val signature = Signature.getInstance("SHA256withRSA").run {
                initSign(account.private); update("$protected64.$payload64".toByteArray(Charsets.US_ASCII)); sign()
            }
            val body = buildJsonObject { put("protected", protected64); put("payload", payload64); put("signature", b64(signature)) }
            val reply = request("POST", url, mapOf("Content-Type" to "application/jose+json", "Accept" to accept), body.toString().toByteArray(Charsets.UTF_8), checkStatus = false)
            nonce = reply.header("Replay-Nonce")?.takeIf(::validNonce)
            if (reply.code in 200..299) return reply
            val type = runCatching { read(reply).jsonObject.string("type") }.getOrDefault("")
            if (reply.code == 400 && type == "urn:ietf:params:acme:error:badNonce" && nonce != null) return@repeat
            throw CertificateFailure(when {
                reply.code == 429 || type == "urn:ietf:params:acme:error:rateLimited" -> "rate_limited"
                type == "urn:ietf:params:acme:error:userActionRequired" -> "terms"
                type == "urn:ietf:params:acme:error:unauthorized" -> "authorization"
                else -> "acme"
            }, retryAfter(reply.header("Retry-After"), now()))
        }
        throw CertificateFailure("nonce")
    }
    private fun request(method: String, url: String, headers: Map<String, String> = emptyMap(), body: ByteArray? = null,
        checkStatus: Boolean = true): CertificateReply {
        guard()
        val reply = http.request(method, endpoint(url), headers, body)
        guard()
        if (checkStatus && reply.code !in 200..299) throw CertificateFailure("acme", retryAfter(reply.header("Retry-After"), now()))
        return reply
    }
    private fun jwk(): JsonObject {
        val public = account.public as RSAPublicKey
        fun unsigned(bytes: ByteArray) = if (bytes.size > 1 && bytes[0] == 0.toByte()) bytes.copyOfRange(1, bytes.size) else bytes
        // RFC 7638 canonical member order for RSA thumbprints.
        return buildJsonObject { put("e", b64(unsigned(public.publicExponent.toByteArray()))); put("kty", "RSA"); put("n", b64(unsigned(public.modulus.toByteArray()))) }
    }
    private fun endpoint(value: String): String {
        val base = URI(directoryUrl); val uri = URI(value)
        if (uri.scheme != "https" || uri.host != base.host || uri.port != base.port || uri.rawUserInfo != null || uri.rawFragment != null)
            throw CertificateFailure("endpoint")
        return uri.toASCIIString()
    }
    private fun checkIdentifier(value: JsonObject?, name: String) {
        if (value?.string("type") != "dns" || value.string("value") != name) throw CertificateFailure("identifier")
    }
    private fun checkIdentifiers(value: JsonObject, name: String) {
        val identifiers = value["identifiers"]?.jsonArray ?: throw CertificateFailure("identifier")
        if (identifiers.size != 1) throw CertificateFailure("identifier")
        checkIdentifier(identifiers.single().jsonObject, name)
    }
    private fun guard() {
        if (cancelled() || Thread.currentThread().isInterrupted) throw CertificateFailure("cancelled")
        if (nanoTime() >= deadline) throw CertificateFailure("timeout")
    }
    private fun waitMillis(millis: Long) {
        val until = nanoTime() + millis * 1_000_000L
        while (nanoTime() < until) { guard(); pause(minOf(250L, ((until - nanoTime()) / 1_000_000L).coerceAtLeast(1))) }
        guard()
    }
    companion object {
        const val PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
        const val STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
        internal fun b64(bytes: ByteArray): String = Base64.toBase64String(bytes).replace('+', '-').replace('/', '_').trimEnd('=')
        internal fun sha256(bytes: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(bytes)
        internal fun retryAfter(value: String?, now: Long): Long {
            value?.toLongOrNull()?.let { return it.coerceIn(0, 604_800) }
            return runCatching {
                val date = SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss zzz", Locale.US).apply { timeZone = TimeZone.getTimeZone("GMT"); isLenient = false }
                    .parse(value ?: "") ?: return@runCatching 0L
                ((date.time - now + 999) / 1000).coerceIn(0, 604_800)
            }.getOrDefault(0)
        }
        private fun validNonce(value: String) = value.matches(Regex("[A-Za-z0-9_-]{1,1024}"))
        private fun read(reply: CertificateReply): JsonElement = Json.parseToJsonElement(reply.text())
    }
}

internal fun JsonObject.string(key: String): String = this[key]?.jsonPrimitive?.contentOrNull.orEmpty()
