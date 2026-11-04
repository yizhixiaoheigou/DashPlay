package com.dashplay.android

import com.shilapi.xcertplay.transport.*
import java.io.Closeable

/** Optional debug-only two-minute relay. Failure never controls the CarPlay session. */
internal class WiredPhoneDiagnostics(host: Iap2UsbMuxHost, record: LockdownPairRecord) : Closeable {
    private val lock = Any()
    @Volatile private var closed = false
    private var relay: BlockingDuplexByteStream? = null
    init {
        Thread({
            try {
                val stream = LockdownCarKitClient(host).openService(record, "DashPlay", "com.apple.syslog_relay")
                synchronized(lock) {
                    if (closed) { stream.close(); return@Thread }
                    relay = stream
                }
                DriveLog.event("wired phone diagnostics ready (event words only)")
                val deadline = System.nanoTime() + 120_000_000_000L
                val pending = StringBuilder()
                var emitted = 0
                var previous: String? = null
                while (!closed && System.nanoTime() < deadline && emitted < 200) {
                    val bytes = stream.recv(8192, 1000) ?: continue
                    if (bytes.isEmpty()) break
                    pending.append(bytes.toString(Charsets.UTF_8).replace('\u0000', '\n'))
                    while (true) {
                        val end = pending.indexOf("\n")
                        if (end < 0) break
                        val event = PhoneConnectionEvent.summarize(pending.substring(0, end))
                        pending.delete(0, end + 1)
                        if (!closed && event != null && event != previous && emitted < 200) {
                            previous = event; emitted++
                            DriveLog.event("wired phone uptimeMs=${android.os.SystemClock.elapsedRealtime()} $event")
                        }
                    }
                    if (pending.length > 65_536) pending.clear()
                }
                DriveLog.event("wired phone diagnostics ended events=$emitted")
            } catch (error: Exception) {
                if (!closed) DriveLog.event("wired phone diagnostics unavailable type=${error.javaClass.simpleName}")
            } finally { close() }
        }, "wired-phone-diagnostics").apply { isDaemon = true; start() }
    }
    override fun close() {
        val active = synchronized(lock) { closed = true; relay.also { relay = null } }
        runCatching { active?.close() }
    }
}
