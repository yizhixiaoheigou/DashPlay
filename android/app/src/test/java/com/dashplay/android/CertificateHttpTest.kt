package com.dashplay.android

import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import org.junit.Assert.*
import org.junit.Test

class CertificateHttpTest {
    private fun server(block: (HttpServer, String) -> Unit) {
        val server=HttpServer.create(InetSocketAddress("127.0.0.1",0),0)
        server.start()
        try { block(server,"http://127.0.0.1:${server.address.port}") } finally { server.stop(0) }
    }
    private fun failure(reason: String, action: () -> Unit) {
        try { action();fail() } catch (error: CertificateFailure) {
            assertEquals(reason,error.reason);assertFalse(error.toString().contains("fixture-secret"))
        }
    }
    @Test fun realHttpReturnsCaseInsensitiveHeadersAndNeverFollowsRedirects() = server { server,base ->
        var redirected=false
        server.createContext("/ok") { exchange ->
            assertEquals("POST",exchange.requestMethod)
            assertEquals("fixture body",exchange.requestBody.bufferedReader().readText())
            exchange.responseHeaders.add("Replay-Nonce","fixture_nonce")
            exchange.sendResponseHeaders(200,2);exchange.responseBody.use { it.write("OK".toByteArray()) }
        }
        server.createContext("/redirect") { exchange ->
            exchange.responseHeaders.add("Location","$base/target");exchange.sendResponseHeaders(302,-1);exchange.close()
        }
        server.createContext("/target") { exchange -> redirected=true;exchange.sendResponseHeaders(200,-1);exchange.close() }
        val http=UrlCertificateHttp(allowLoopbackHttp=true)
        val reply=http.request("POST","$base/ok",emptyMap(),"fixture body".toByteArray())
        assertEquals("OK",reply.text());assertEquals("fixture_nonce",reply.header("replay-nonce"))
        failure("redirect") { http.request("GET","$base/redirect?token=fixture-secret",mapOf("Authorization" to "fixture-secret"),null) }
        assertFalse(redirected)
        failure("endpoint") { UrlCertificateHttp().request("GET","$base/ok",emptyMap(),null) }
    }
    @Test fun responseSizeCancellationAndTimeoutAreBounded() = server { server,base ->
        server.createContext("/large") { exchange ->
            exchange.sendResponseHeaders(200,131073);runCatching { exchange.responseBody.use { it.write(ByteArray(131073)) } }
        }
        server.createContext("/slow") { exchange ->
            Thread.sleep(700);runCatching { exchange.sendResponseHeaders(200,-1);exchange.close() }
        }
        val http=UrlCertificateHttp(allowLoopbackHttp=true)
        failure("response_size") { http.request("GET","$base/large",emptyMap(),null) }
        failure("cancelled") { UrlCertificateHttp(cancelled={true},allowLoopbackHttp=true).request("GET","$base/large",emptyMap(),null) }
        val start=System.nanoTime()
        failure("timeout") { UrlCertificateHttp(timeoutMillis=100,allowLoopbackHttp=true).request("GET","$base/slow?token=fixture-secret",emptyMap(),null) }
        assertTrue((System.nanoTime()-start)/1_000_000<1500)
    }
}
