package com.shilapi.xcertplay.transport

import java.io.IOException
import java.nio.ByteBuffer
import org.junit.Assert.*
import org.junit.Test

class LockdownServiceCleanupTest {
    private class Stream(xml: String = "") : BlockingDuplexByteStream {
        private val bytes = xml.toByteArray().let { ByteBuffer.allocate(4 + it.size).putInt(it.size).put(it).array() }
        private var offset = 0
        var closes = 0
        override fun send(data: ByteArray) {}
        override fun recv(maxBytes: Int, timeoutMillis: Long): ByteArray? {
            if (offset == bytes.size) return null
            val end = minOf(bytes.size, offset + maxBytes)
            return bytes.copyOfRange(offset, end).also { offset = end }
        }
        override fun close() { closes++ }
    }
    private fun response(entries: String) = Stream("<?xml version=\"1.0\"?><plist version=\"1.0\"><dict>$entries</dict></plist>")
    private val port = "<key>Port</key><integer>1234</integer>"
    @Test fun rejectedServiceClosesTheExistingLockdownSession() {
        val stream = response("<key>Error</key><string>ServiceProhibited</string>")
        assertThrows(IphoneUsbException.Protocol::class.java) {
            LockdownCarKitClient.openServiceOnSession(LockdownPlistChannel(stream), "test", { error("unexpected connect") }, { it })
        }
        assertEquals(1, stream.closes)
    }
    @Test fun missingOrInvalidPortClosesTheExistingSession() {
        for (entry in listOf("", "<key>Port</key><integer>65536</integer>")) {
            val stream = response(entry)
            assertThrows(IphoneUsbException.Protocol::class.java) {
                LockdownCarKitClient.openServiceOnSession(LockdownPlistChannel(stream), "test", { error("unexpected connect") }, { it })
            }
            assertEquals(1, stream.closes)
        }
    }
    @Test fun serviceConnectionFailureClosesTheExistingSession() {
        val stream = response(port)
        assertThrows(IOException::class.java) {
            LockdownCarKitClient.openServiceOnSession(LockdownPlistChannel(stream), "test", { throw IOException("connect failure") }, { it })
        }
        assertEquals(1, stream.closes)
    }
    @Test fun serviceTlsFailureClosesBothOwnedChannels() {
        val stream = response(port + "<key>EnableServiceSSL</key><true/>")
        val service = Stream()
        assertThrows(IOException::class.java) {
            LockdownCarKitClient.openServiceOnSession(LockdownPlistChannel(stream), "test", { service }, { throw IOException("TLS failure") })
        }
        assertEquals(1, stream.closes); assertEquals(1, service.closes)
    }
    @Test fun successTransfersOwnershipUntilTheReturnedStreamIsClosed() {
        for (tls in listOf(false, true)) {
            val stream = response(port + "<key>EnableServiceSSL</key><${if (tls) "true" else "false"}/>")
            val service = Stream(); var secured = false
            val result = LockdownCarKitClient.openServiceOnSession(LockdownPlistChannel(stream), "test",
                { assertEquals(1234, it); service }, { secured = true; it })
            assertEquals(tls, secured); assertEquals(0, stream.closes); assertEquals(0, service.closes)
            result.close(); result.close()
            assertEquals(1, stream.closes); assertEquals(1, service.closes)
        }
    }
}
