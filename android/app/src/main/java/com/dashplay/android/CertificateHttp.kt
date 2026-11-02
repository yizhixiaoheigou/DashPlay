package com.dashplay.android

import java.io.ByteArrayOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URI
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Bounded outbound requests. No redirects, raw server errors or credential-bearing URLs reach diagnostics. */
internal fun interface CertificateHttp {
    fun request(method: String, url: String, headers: Map<String, String>, body: ByteArray?): CertificateReply
}

internal data class CertificateReply(val code: Int, val headers: Map<String, String>, val body: ByteArray) {
    fun header(name: String): String? = headers.entries.firstOrNull { it.key.equals(name, true) }?.value
    fun text(): String = body.toString(Charsets.UTF_8)
    override fun toString() = "CertificateReply(code=$code, bytes=${body.size})"
}

internal class CertificateFailure(val reason: String, val retryAfterSeconds: Long = 0,
    val causeKind: String = "") : IOException(reason)

internal class UrlCertificateHttp(
    private val cancelled: () -> Boolean = { false },
    private val timeoutMillis: Int = 30_000,
    private val allowLoopbackHttp: Boolean = false,
) : CertificateHttp {
    override fun request(method: String, url: String, headers: Map<String, String>, body: ByteArray?): CertificateReply {
        checkCancelled()
        val uri = try { URI(url) } catch (_: Exception) { throw CertificateFailure("endpoint") }
        if (uri.rawUserInfo != null || uri.rawFragment != null || uri.host == null ||
            !(uri.scheme == "https" || allowLoopbackHttp && uri.scheme == "http" && uri.host in setOf("127.0.0.1", "localhost", "[::1]")))
            throw CertificateFailure("endpoint")
        if (body != null && body.size > 131_072) throw CertificateFailure("request_size")
        val connection = try { uri.toURL().openConnection() as HttpURLConnection }
            catch (_: Exception) { throw CertificateFailure("network") }
        // Covers a peer that trickles bytes indefinitely, in addition to connect/read inactivity timeouts.
        val deadline = System.nanoTime() + timeoutMillis * 1_000_000L
        val timer = watchdog.schedule({ connection.disconnect() }, timeoutMillis.toLong(), TimeUnit.MILLISECONDS)
        try {
            if (connection is javax.net.ssl.HttpsURLConnection) CertificateTrust.forHost(uri.host)?.let {
                connection.sslSocketFactory = it // Keep the platform hostname verifier; never accept all certificates.
            }
            connection.instanceFollowRedirects = false
            connection.connectTimeout = minOf(timeoutMillis, 10_000)
            connection.readTimeout = timeoutMillis
            connection.requestMethod = method
            connection.useCaches = false
            connection.setRequestProperty("Accept", "application/json")
            headers.forEach(connection::setRequestProperty)
            if (body != null) {
                connection.doOutput = true
                connection.setFixedLengthStreamingMode(body.size)
                connection.outputStream.use { it.write(body) }
            }
            checkCancelled()
            val code = connection.responseCode
            val result = ByteArrayOutputStream()
            if (method != "HEAD") (if (code >= 400) connection.errorStream else connection.inputStream)?.use { input ->
                val buffer = ByteArray(4096)
                while (true) {
                    checkCancelled()
                    if (System.nanoTime() >= deadline) throw CertificateFailure("timeout")
                    val count = input.read(buffer)
                    if (count < 0) break
                    if (result.size() + count > 131_072) throw CertificateFailure("response_size")
                    result.write(buffer, 0, count)
                }
            }
            if (code in 300..399) throw CertificateFailure("redirect")
            return CertificateReply(code, connection.headerFields.entries.filter { it.key != null }
                .associate { it.key to it.value.firstOrNull().orEmpty() }, result.toByteArray())
        } catch (failure: CertificateFailure) { throw failure }
        catch (error: Exception) {
            checkCancelled()
            throw CertificateFailure(when {
                error is java.net.UnknownHostException -> "dns"
                error is javax.net.ssl.SSLHandshakeException -> "tls_trust"
                error is java.net.SocketTimeoutException || System.nanoTime() >= deadline -> "timeout"
                else -> "network"
            }, causeKind = error.javaClass.simpleName)
        } finally { timer.cancel(false); connection.disconnect() }
    }
    private fun checkCancelled() {
        if (cancelled() || Thread.currentThread().isInterrupted) throw CertificateFailure("cancelled")
    }
    companion object {
        private val watchdog = Executors.newSingleThreadScheduledExecutor { task ->
            Thread(task, "certificate-request-timeout").apply { isDaemon = true }
        }
    }
}
