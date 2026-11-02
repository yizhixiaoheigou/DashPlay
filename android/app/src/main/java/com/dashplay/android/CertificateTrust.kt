package com.dashplay.android

import java.security.KeyStore
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

/** Old Android trust stores predate ISRG Root X1. Scope the extra anchor to our fixed certificate-service hosts. */
internal object CertificateTrust {
    private val hosts = setOf("acme-v02.api.letsencrypt.org", "acme-staging-v02.api.letsencrypt.org", "www.duckdns.org", "desec.io")
    fun forHost(host: String): SSLSocketFactory? = if (host in hosts) factory else null
    private val factory: SSLSocketFactory by lazy {
        val root = bundledRoot()
        val extra = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null); setCertificateEntry("isrg-x1", root) }
        SSLContext.getInstance("TLS").apply { init(null, arrayOf(combine(manager(null), manager(extra))), null) }.socketFactory
    }
    internal fun bundledRoot(): X509Certificate {
        val root = CertificateTrust::class.java.getResourceAsStream("/certificates/isrg-root-x1.pem")?.use {
            CertificateFactory.getInstance("X.509").generateCertificate(it) as X509Certificate
        } ?: throw CertificateException("Missing certificate service trust anchor")
        val digest = MessageDigest.getInstance("SHA-256").digest(root.encoded).joinToString("") { "%02x".format(it) }
        check(digest == "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6")
        return root
    }
    internal fun manager(keys: KeyStore?): X509TrustManager = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
        .apply { init(keys) }.trustManagers.filterIsInstance<X509TrustManager>().single()
    internal fun combine(system: X509TrustManager, extra: X509TrustManager): X509TrustManager = object : X509TrustManager {
        override fun getAcceptedIssuers(): Array<X509Certificate> = system.acceptedIssuers + extra.acceptedIssuers
        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) = system.checkClientTrusted(chain, authType)
        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
            try { system.checkServerTrusted(chain, authType) }
            catch (_: CertificateException) { extra.checkServerTrusted(chain, authType) }
        }
    }
}
