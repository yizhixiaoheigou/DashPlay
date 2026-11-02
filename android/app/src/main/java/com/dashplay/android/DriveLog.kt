package com.dashplay.android

import java.io.Closeable
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStream
import java.io.PrintStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The receiver's log kept on the device, so a problem in the car can be read afterwards (the N1 keeps
 * `state/logs/` for the same reason). Logcat alone is gone by the time anyone asks, and needs adb.
 *
 * Two files of at most [maxBytes] each, in the app's private directory. Lines are redacted before they are
 * written: viewer/admin tokens, Authorization headers, DNS provider tokens, passwords and private key blocks.
 */
class DriveLog(private val directory: File, private val maxBytes: Long = MAX_BYTES,
               private val clock: () -> Long = System::currentTimeMillis) : Closeable {
    private val current = File(directory, "dashplay.log")
    private val previous = File(directory, "dashplay.1.log")
    private val stamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.ROOT)
    private var out: FileOutputStream? = null
    private var inKey = false
    private var lastSync = 0L

    @Synchronized fun line(raw: String) {
        val text = redactLine(raw) ?: return
        try {
            if (out == null) open()
            if (current.length() >= maxBytes) rotate()
            val stream = out ?: return
            val now = clock()
            stream.write("${stamp.format(Date(now))} $text\n".toByteArray(Charsets.UTF_8))
            // A box is simply switched off in the car: get warnings and errors onto storage at once.
            if (now - lastSync >= SYNC_MILLIS || IMPORTANT.containsMatchIn(text)) { stream.fd.sync(); lastSync = now }
        } catch (_: Exception) { /* Logging must never take the receiver down. */ }
    }

    /** Both files, oldest first, for the owner to send along. Already redacted when written. */
    @Synchronized fun export(): String {
        runCatching { out?.fd?.sync() }
        return read(directory)
    }

    /** An output stream that copies every complete line here and passes the bytes on to [original] unchanged. */
    fun tee(original: PrintStream): OutputStream = object : OutputStream() {
        private val pending = java.io.ByteArrayOutputStream()
        override fun write(b: Int) {
            original.write(b)
            synchronized(pending) {
                if (b == '\n'.code) flushLine() else pending.write(b)
                if (pending.size() > MAX_LINE) flushLine()
            }
        }
        override fun write(b: ByteArray, off: Int, len: Int) {
            original.write(b, off, len)
            synchronized(pending) {
                for (i in off until off + len) {
                    if (b[i] == '\n'.code.toByte()) flushLine() else pending.write(b[i].toInt())
                }
                if (pending.size() > MAX_LINE) flushLine()
            }
        }
        override fun flush() = original.flush()
        private fun flushLine() {
            val text = pending.toString("UTF-8").trimEnd('\r'); pending.reset()
            if (text.isNotEmpty()) line(text)
        }
    }

    private fun open(): FileOutputStream {
        if (!directory.exists()) directory.mkdirs()
        return FileOutputStream(current, true).also { out = it }
    }

    private fun rotate() {
        runCatching { out?.close() }; out = null
        if (previous.exists()) previous.delete()
        current.renameTo(previous)
        open()
    }

    @Synchronized private fun redactLine(raw: String): String? {
        if (inKey) {
            if (KEY_END.containsMatchIn(raw)) inKey = false
            return null
        }
        if (KEY_BEGIN.containsMatchIn(raw)) {
            inKey = !KEY_END.containsMatchIn(raw)
            return "<private key redacted>"
        }
        return redact(raw)
    }

    @Synchronized override fun close() { runCatching { out?.fd?.sync(); out?.close() }; out = null }

    companion object {
        const val MAX_BYTES = 1_000_000L
        private const val MAX_LINE = 8192
        private const val SYNC_MILLIS = 10_000L
        private val IMPORTANT = Regex("""\b(WARN|ERROR|FATAL)\b|Exception|crash""")
        private val KEY_BEGIN = Regex("""-----BEGIN [A-Z ]*PRIVATE KEY-----""")
        private val KEY_END = Regex("""-----END [A-Z ]*PRIVATE KEY-----""")
        private val RULES = listOf(
            // URL query/fragment credentials: viewer ?token=, /manage#token=, DuckDNS &token=.
            Regex("""(?i)([?#&](?:token|access_token|admin_token|key|secret)=)[^&#\s"']+""") to "$1<redacted>",
            Regex("""(?i)(X-DashPlay-Admin\s*:\s*)\S+""") to "$1<redacted>",
            Regex("""(?i)(Authorization\s*:\s*)(?:\w+\s+)?\S+""") to "$1<redacted>",
            Regex("""(?i)\b(password|passphrase|passwd|secret|psk|token)(\s*[=:]\s*)(?!<redacted>)("[^"]*"|[^\s&#]+)""") to "$1$2<redacted>",
        )

        fun redact(line: String): String = RULES.fold(line) { text, (rule, with) -> rule.replace(text, with) }

        @Volatile var installed: DriveLog? = null; private set

        /** Both files, oldest first; also works when the service (and so [installed]) is not running. */
        fun read(directory: File): String = buildString {
            for (name in listOf("dashplay.1.log", "dashplay.log")) {
                val file = File(directory, name)
                if (!file.exists()) continue
                append("== ").append(name).append('\n')
                append(file.readText(Charsets.UTF_8))
            }
        }

        fun export(directory: File): String = installed?.export() ?: read(directory)

        /** Once per process: keep System.err (where the receiver's slf4j-simple writes) in the drive log too. */
        @Synchronized fun install(directory: File): DriveLog {
            installed?.let { return it }
            val log = DriveLog(directory)
            val original = System.err
            System.setErr(PrintStream(log.tee(original), true, "UTF-8"))
            val previousHandler = Thread.getDefaultUncaughtExceptionHandler()
            Thread.setDefaultUncaughtExceptionHandler { thread, error ->
                log.line("FATAL uncaught in ${thread.name}: ${error.stackTraceToString()}")
                previousHandler?.uncaughtException(thread, error)
            }
            installed = log
            return log
        }

        /** An Android-side event (service lifecycle, cable states), next to the receiver's own lines. */
        fun event(text: String) { installed?.line("[android] $text") }
    }
}
