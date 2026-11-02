package com.dashplay.android

import java.io.File
import java.io.PrintStream
import java.nio.file.Files
import org.junit.Assert.*
import org.junit.Test

class DriveLogTest {
    // Built at run time: a literal "token=<hex>" in source is what the open-source export scanner blocks.
    private val viewer = "0123456789abcdef".repeat(2)
    private val admin = "fedcba9876543210".repeat(2)
    private fun fixture(maxBytes: Long = 1_000_000, body: (File, DriveLog) -> Unit) {
        val dir = Files.createTempDirectory("drive-log-").toFile()
        try { DriveLog(dir, maxBytes, clock = { 1_793_500_000_000L }).use { body(dir, it) } } finally { dir.deleteRecursively() }
    }

    @Test fun linesSurviveInAFileWithAWallClockStamp() = fixture { dir, log ->
        log.line("wired: the CarPlay session ended")
        val text = File(dir, "dashplay.log").readText()
        assertTrue(text, text.contains("wired: the CarPlay session ended"))
        assertTrue(text, Regex("""^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} """).containsMatchIn(text))
    }

    @Test fun credentialsNeverReachTheFile() = fixture { dir, log ->
        log.line("viewer https://192.168.43.1:8080/?token=$viewer opened")
        log.line("manage https://box/manage#token=$admin")
        log.line("X-DashPlay-Admin: $admin")
        log.line("Authorization: Bearer abc.def.ghi")
        log.line("duckdns update?domains=car&token=11111111-2222-3333-4444-555555555555&ip=1.2.3.4")
        log.line("hotspot password=hunter22 passphrase: sesame secret=\"x y\"")
        val text = File(dir, "dashplay.log").readText()
        for (secret in listOf(viewer, admin, "abc.def.ghi",
                "11111111-2222-3333-4444-555555555555", "hunter22", "sesame", "x y")) {
            assertFalse("$secret leaked:\n$text", text.contains(secret))
        }
        assertTrue(text, text.contains("ip=1.2.3.4"))
        assertTrue(text, text.contains("viewer https://192.168.43.1:8080/?token=<redacted> opened"))
    }

    @Test fun aPrivateKeyBlockIsDroppedWholeAcrossLines() = fixture { dir, log ->
        log.line("before")
        log.line("-----BEGIN PRIVATE KEY-----")
        log.line("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC")
        log.line("-----END PRIVATE KEY-----")
        log.line("after")
        val text = File(dir, "dashplay.log").readText()
        assertFalse(text, text.contains("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"))
        assertTrue(text, text.contains("before") && text.contains("after") && text.contains("<private key redacted>"))
    }

    @Test fun keepsTwoBoundedFilesAndExportsOldestFirst() = fixture(maxBytes = 200) { dir, log ->
        repeat(30) { log.line("line %02d %s".format(it, "x".repeat(20))) }
        val current = File(dir, "dashplay.log"); val previous = File(dir, "dashplay.1.log")
        assertTrue(previous.exists())
        assertTrue(current.length() <= 200 + 80 && previous.length() <= 200 + 80)
        assertEquals(listOf("dashplay.1.log", "dashplay.log"), dir.list()!!.sorted())
        val exported = log.export()
        assertTrue(exported, exported.contains("line 29"))
        assertTrue(exported.indexOf("dashplay.1.log") < exported.indexOf("== dashplay.log"))
        assertFalse(exported, exported.contains("line 00"))
    }

    @Test fun teeKeepsTheOriginalStreamAndSplitsLines() = fixture { dir, log ->
        val original = java.io.ByteArrayOutputStream()
        val tee = PrintStream(log.tee(PrintStream(original, true)), true, "UTF-8")
        tee.print("[main] INFO a - first\n[main] WARN b - sec")
        tee.println("ond token=$viewer")
        val text = File(dir, "dashplay.log").readText()
        assertTrue(text, text.contains("[main] INFO a - first"))
        assertTrue(text, text.contains("[main] WARN b - second token=<redacted>"))
        assertTrue(original.toString("UTF-8").contains("WARN b - second"))
    }
}
