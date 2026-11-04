package com.dashplay.android

import com.sun.net.httpserver.HttpServer
import org.junit.Assert.*
import org.junit.Test
import java.lang.reflect.InvocationTargetException
import java.net.InetSocketAddress

/** Real loopback HTTP requests; no public release or user device is involved. */
class OnlineUpdateFetchTest {
    private fun fetch(url: String, mirrors: String, limit: Int = 1024): ByteArray {
        val method = OnlineUpdate::class.java.getDeclaredMethod("fetchBytes", String::class.java,
            String::class.java, Int::class.javaPrimitiveType).apply { isAccessible = true }
        try { return method.invoke(OnlineUpdate, url, mirrors, limit) as ByteArray }
        catch (e: InvocationTargetException) { throw e.targetException }
    }
    private fun server(block: (String, MutableList<String>) -> Unit) {
        val requests = java.util.Collections.synchronizedList(mutableListOf<String>())
        val http = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        http.createContext("/") { exchange ->
            val path = exchange.requestURI.path
            requests += path
            val body = (if (path.startsWith("/large")) "x".repeat(2048) else "release fixture").toByteArray()
            val code = if (path.startsWith("/fail") || path.startsWith("/first")) 503 else 200
            exchange.sendResponseHeaders(code, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
            exchange.close()
        }
        http.start()
        try { block("http://127.0.0.1:${http.address.port}", requests) }
        finally { http.stop(0) }
    }
    @Test fun successfulDirectRequestDoesNotContactMirrors() = server { base, requests ->
        assertEquals("release fixture", String(fetch("$base/ok", "$base/mirror/")))
        assertEquals(listOf("/ok"), requests)
    }
    @Test fun httpFailureTriesMirrorsInOrder() = server { base, requests ->
        assertEquals("release fixture", String(fetch("$base/fail", "$base/first/ $base/second/")))
        assertEquals(listOf("/fail", "/first/$base/fail", "/second/$base/fail"), requests)
    }
    @Test fun noneDisablesFallbackAndReportsFailure() = server { base, requests ->
        val error = assertThrows(IllegalStateException::class.java) { fetch("$base/fail", "none") }
        assertTrue(error.message.orEmpty().contains("HTTP 503"))
        assertEquals(listOf("/fail"), requests)
    }
    @Test fun oversizedResponseIsRejectedBeforeParsing() = server { base, requests ->
        val error = assertThrows(IllegalStateException::class.java) { fetch("$base/large", "none", 32) }
        assertTrue(error.message.orEmpty().contains("大小限制"))
        assertEquals(listOf("/large"), requests)
    }
    @Test fun malformedSavedMirrorDoesNotPreventTryingTheNextOne() = server { base, requests ->
        assertEquals("release fixture", String(fetch("$base/fail", "not-a-url $base/second/")))
        assertEquals(2, requests.size)
        assertTrue(requests.last().startsWith("/second/"))
    }
    private fun state(url: String, size: Long = 15, hash: String = hash("release fixture")) =
        OnlineUpdate.State(available = true, latest = "0.3.1-beta.2", apkUrl = url, apkSize = size, apkSha256 = hash)
    private fun hash(text: String) = java.security.MessageDigest.getInstance("SHA-256")
        .digest(text.toByteArray()).joinToString("") { "%02x".format(it.toInt() and 255) }
    private fun target(block: (java.io.File) -> Unit) {
        val file = java.io.File.createTempFile("update-", ".apk")
        try { block(file) } finally { file.delete() }
    }
    private fun accepted(file: java.io.File): ApkUpdates.Candidate {
        assertEquals("release fixture", file.readText())
        return ApkUpdates.Candidate(file, "0.3.1-beta.2", 301302)
    }
    @Test fun downloadFallsBackAndVerifiesBeforeHandingToInstaller() = server { base, requests ->
        target { file ->
            var inspections = 0
            val candidate = OnlineUpdate.downloadFile(state("$base/fail"), "$base/second/", file) {
                inspections++; accepted(it)
            }
            assertEquals(1, inspections)
            assertEquals(file, candidate.file)
            assertEquals(listOf("/fail", "/second/$base/fail"), requests)
        }
    }
    @Test fun digestMismatchDeletesFileWithoutMirrorOrInspection() = server { base, requests ->
        target { file ->
            val error = assertThrows(IllegalArgumentException::class.java) {
                OnlineUpdate.downloadFile(state("$base/ok", hash = "0".repeat(64)), "$base/second/", file) {
                    fail("unverified download reached APK inspection"); accepted(it)
                }
            }
            assertTrue(error.message.orEmpty().contains("SHA-256"))
            assertFalse(file.exists())
            assertEquals(listOf("/ok"), requests)
        }
    }
    @Test fun shortDownloadIsDeletedAndNotInspected() = server { base, _ ->
        target { file ->
            val error = assertThrows(IllegalArgumentException::class.java) {
                OnlineUpdate.downloadFile(state("$base/ok", size = 100), "none", file) {
                    fail("incomplete download reached APK inspection"); accepted(it)
                }
            }
            assertTrue(error.message.orEmpty().contains("大小"))
            assertFalse(file.exists())
        }
    }
    @Test fun oversizedDownloadIsDeletedAndNotInspected() = server { base, _ ->
        target { file ->
            assertThrows(IllegalArgumentException::class.java) {
                OnlineUpdate.downloadFile(state("$base/large", size = 15), "none", file) {
                    fail("oversized download reached APK inspection"); accepted(it)
                }
            }
            assertFalse(file.exists())
        }
    }
    @Test fun signerFailureIsNotBypassedByTryingAnotherMirror() = server { base, requests ->
        target { file ->
            assertThrows(IllegalArgumentException::class.java) {
                OnlineUpdate.downloadFile(state("$base/ok"), "$base/second/", file) {
                    throw IllegalArgumentException("签名与当前版本不同")
                }
            }
            assertFalse(file.exists())
            assertEquals(listOf("/ok"), requests)
        }
    }
    @Test fun downloadAlsoSkipsMalformedSavedMirror() = server { base, requests ->
        target { file ->
            OnlineUpdate.downloadFile(state("$base/fail"), "not-a-url $base/second/", file, ::accepted)
            assertEquals(2, requests.size)
        }
    }

}
