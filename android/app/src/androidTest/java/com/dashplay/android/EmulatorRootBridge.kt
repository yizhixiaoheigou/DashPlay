package com.dashplay.android

import java.io.File

/** Test APK only. A separate adb-root harness executes these requests on a disposable emulator. */
internal class EmulatorRootBridge(private val base: File) {
    private var sequence = 0
    var commands = 0; private set
    var denyCleanup = false
    init {
        check(android.os.Build.HARDWARE in listOf("ranchu", "goldfish"))
        base.mkdirs()
    }
    fun run(script: String, marker: String, timeout: () -> Unit = {}): UsbShareRootResult {
        commands++
        if (denyCleanup && marker == "DASHPLAY_SHARE_SESSION_REMOVED") return UsbShareRootResult(false, true, false)
        val id = ++sequence
        File(base, "$id.script").writeText(script)
        File(base, "$id.ready").writeText(marker)
        val response = File(base, "$id.response")
        val end = System.nanoTime() + 35_000_000_000L
        while (!response.exists()) {
            if (System.nanoTime() >= end) {
                timeout()
                return UsbShareRootResult(false, false, false)
            }
            Thread.sleep(25)
        }
        val lines = response.readLines()
        val code = lines.first().toInt()
        if (code == 124) { timeout(); return UsbShareRootResult(false, false, false) }
        val output = lines.drop(1).filter { it.startsWith("DASHPLAY_") }
        return UsbShareRootResult(code == 0 && marker in output, true,
            output.any { it == "DASHPLAY_SHARE_STARTED" || it == "DASHPLAY_ROOT_STARTED" }, output)
    }
    fun tesla(script: String, marker: String): TeslaRootAccess.Result {
        val result = run(script, marker)
        return TeslaRootAccess.Result(result.success, "DASHPLAY_ROOT_OWNED" in result.output, result.finished, result.granted)
    }
}
