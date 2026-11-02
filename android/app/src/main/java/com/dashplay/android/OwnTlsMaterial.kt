package com.dashplay.android

import com.playport.server.TlsCertificate
import java.io.StringReader
import java.security.KeyStore
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.KeyFactory
import java.security.spec.PKCS8EncodedKeySpec
import java.security.Signature
import java.security.cert.X509Certificate
import java.security.cert.CertificateFactory
import java.security.cert.CertPathValidator
import java.security.cert.PKIXParameters
import java.security.cert.TrustAnchor
import java.security.interfaces.ECPublicKey
import java.security.interfaces.RSAPublicKey
import java.util.Date
import java.util.Locale
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager
import org.bouncycastle.asn1.pkcs.PrivateKeyInfo
import org.bouncycastle.cert.X509CertificateHolder
import org.bouncycastle.jce.provider.BouncyCastleProvider
import org.bouncycastle.openssl.PEMKeyPair
import org.bouncycastle.openssl.PEMParser
import org.bouncycastle.openssl.jcajce.JcaPEMKeyConverter

/** Certificate validation shared by import and automatic issuance. Never expose parser errors or PEM in errors. */
object OwnTlsMaterial {
    const val MAX_CHAIN = 65_536
    const val MAX_KEY = 16_384
    const val DAY_MS = 86_400_000L
    private val provider = BouncyCastleProvider()

    fun domain(value: String): String {
        val name = value.trim().lowercase(Locale.ROOT).removeSuffix(".")
        require(name.length in 3..253 && '.' in name && !name.all { it.isDigit() || it == '.' } &&
            name.split('.').all { it.matches(Regex("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")) }) {
            "请填写完整域名，不包含 https://、端口或路径。"
        }
        return name
    }

    data class Loaded(val domain: String, val material: TlsCertificate.Material,
        val chain: List<X509Certificate>, val fingerprint: String, val systemTrusted: Boolean) {
        val expires: Long get() = chain.minOf { it.notAfter.time }
        fun validAt(now: Long): Boolean = chain.all { now >= it.notBefore.time && now < it.notAfter.time }
    }

    fun parse(name: String, chainPem: String, keyPem: String, now: Long = System.currentTimeMillis(),
        minimumRemaining: Long = DAY_MS): Loaded {
        val normalized = domain(name)
        require(chainPem.toByteArray(Charsets.UTF_8).size in 1..MAX_CHAIN &&
            keyPem.toByteArray(Charsets.UTF_8).size in 1..MAX_KEY) { "证书链或私钥为空，或者文件过大。" }
        try {
            val certificates = CertificateFactory.getInstance("X.509")
            val chain = PEMParser(StringReader(chainPem)).use { parser ->
                buildList {
                    while (true) {
                        val item = parser.readObject() ?: break
                        require(item is X509CertificateHolder)
                        // Keep BC for PEM decoding, but hand platform certificates/keys to Android's TLS provider.
                        add(certificates.generateCertificate(item.encoded.inputStream()) as X509Certificate)
                        require(size <= 10)
                    }
                }
            }
            require(chain.isNotEmpty())
            val keyConverter = JcaPEMKeyConverter().setProvider(provider)
            val key: PrivateKey = PEMParser(StringReader(keyPem)).use { parser ->
                val value = when (val item = parser.readObject()) {
                    is PrivateKeyInfo -> keyConverter.getPrivateKey(item)
                    is PEMKeyPair -> keyConverter.getPrivateKey(item.privateKeyInfo)
                    else -> throw IllegalArgumentException()
                }
                require(parser.readObject() == null)
                // BC calls EC private keys "ECDSA"; platform PKCS12/TLS expects the standard "EC" key type.
                KeyFactory.getInstance(if (value.algorithm == "ECDSA") "EC" else value.algorithm)
                    .generatePrivate(PKCS8EncodedKeySpec(value.encoded))
            }
            val leaf = chain.first()
            chain.forEach { it.checkValidity(Date(now)); require(it.notAfter.time - now >= minimumRemaining) }
            require(leaf.basicConstraints < 0)
            val usage = leaf.extendedKeyUsage
            require(usage == null || "1.3.6.1.5.5.7.3.1" in usage || "2.5.29.37.0" in usage)
            require(leaf.keyUsage == null || leaf.keyUsage[0])
            require(leaf.subjectAlternativeNames.orEmpty().any { san ->
                san[0] == 2 && san[1] is String && matches(normalized, san[1] as String)
            })
            val algorithm = when (val publicKey = leaf.publicKey) {
                is RSAPublicKey -> { require(publicKey.modulus.bitLength() >= 2048); "SHA256withRSA" }
                is ECPublicKey -> { require(publicKey.params.curve.field.fieldSize >= 256); "SHA256withECDSA" }
                else -> throw IllegalArgumentException()
            }
            val challenge = "DashPlay TLS private key verification".toByteArray(Charsets.US_ASCII)
            val signed = Signature.getInstance(algorithm).run { initSign(key); update(challenge); sign() }
            require(Signature.getInstance(algorithm).run { initVerify(leaf.publicKey); update(challenge); verify(signed) })
            chain.zipWithNext().forEach { (child, issuer) ->
                require(child.issuerX500Principal == issuer.subjectX500Principal && issuer.basicConstraints >= 0)
                require(issuer.keyUsage == null || issuer.keyUsage[5])
                child.verify(issuer.publicKey)
            }
            if (chain.size > 1) {
                // Structural chain validation is separate from system trust. Never make network OCSP requests here.
                val path = CertificateFactory.getInstance("X.509").generateCertPath(chain.dropLast(1))
                val parameters = PKIXParameters(setOf(TrustAnchor(chain.last(), null))).apply {
                    isRevocationEnabled = false; date = Date(now)
                }
                CertPathValidator.getInstance("PKIX").validate(path, parameters)
            }
            val password = "dashplay-own-tls".toCharArray()
            val keys = KeyStore.getInstance("PKCS12").apply {
                load(null, null)
                setKeyEntry("dashplay", key, password, chain.toTypedArray())
            }
            val trusted = runCatching {
                val factory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
                factory.init(null as KeyStore?)
                factory.trustManagers.filterIsInstance<X509TrustManager>().first()
                    .checkServerTrusted(chain.toTypedArray(), if (leaf.publicKey is RSAPublicKey) "RSA" else "ECDHE_ECDSA")
            }.isSuccess
            return Loaded(normalized, TlsCertificate.Material(keys, "dashplay", password), chain,
                MessageDigest.getInstance("SHA-256").digest(leaf.encoded).joinToString("") { "%02x".format(it) }, trusted)
        } catch (_: Exception) {
            throw IllegalArgumentException("证书与域名、私钥不匹配，证书链无效，或剩余有效期不足一天。请使用 PEM 证书链和未加密的 RSA/EC 私钥。")
        }
    }

    /** RFC 9525: SAN only; a wildcard covers exactly one complete leftmost label. */
    internal fun matches(name: String, presented: String): Boolean {
        val pattern = presented.lowercase(Locale.ROOT)
        if ('*' !in pattern) return name == pattern
        return pattern.startsWith("*.") && pattern.count { it == '*' } == 1 &&
            name.substringAfter('.', "") == pattern.substring(2)
    }
}
