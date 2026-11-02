package com.dashplay.android

import android.content.Context
import android.net.Uri
import com.playport.server.MfiIdentity
import com.playport.server.ServerConfig
import java.io.File
import java.io.IOException
import java.util.zip.ZipFile
import java.io.InputStream

/**
 * Takes the MFi identity out of a DiPlay release APK the user picked (assets/offline-mfi/). DashPlay never ships
 * or downloads one itself. The files stay in the app's private storage and are checked before they replace anything.
 */
object IdentityImport {
    private val ENTRIES = mapOf(
        "assets/offline-mfi/identity.pk8" to "identity.pk8",
        "assets/offline-mfi/certificate.p7b" to "certificate.p7b",
    )
    const val MAX_ARCHIVE_BYTES = 32L * 1024 * 1024
    private const val MAX_ENTRY_BYTES = 64 * 1024
    private const val MAX_CANDIDATES = 8

    @Synchronized
    fun identityDir(context: Context): File = File(context.filesDir, "identity").also { root ->
        val target = File(root, "offline-mfi")
        val previous = File(root, "previous")
        if (!target.exists() && previous.isDirectory) previous.renameTo(target)
    }

    /** DiPlay APKs in Download and on USB sticks: a TV box often has no file picker. Needs storage access before
     *  Android 11's scoped storage; later versions show nothing here and use the picker. */
    fun candidates(): List<File> {
        val roots = buildList {
            add(android.os.Environment.getExternalStoragePublicDirectory(android.os.Environment.DIRECTORY_DOWNLOADS))
            add(android.os.Environment.getExternalStorageDirectory())
            File("/storage").listFiles().orEmpty().filter { it.isDirectory && it.name != "emulated" && it.name != "self" }.forEach {
                add(it)
                add(File(it, "Download"))
            }
            File("/mnt/usb").listFiles().orEmpty().forEach { add(it) }
        }
        return roots.distinct().flatMap { dir ->
            runCatching { dir.listFiles().orEmpty().toList() }.getOrDefault(emptyList())
        }.filter { it.isFile && it.name.endsWith(".apk", ignoreCase = true) && it.name.contains("diplay", ignoreCase = true) }
            .distinctBy { it.canonicalPath }
            .take(MAX_CANDIDATES)
    }

    fun present(context: Context): Boolean =
        ENTRIES.values.all { File(identityDir(context), "offline-mfi/$it").isFile }

    /** Native picker and web upload share the same bounded, atomic identity import. */
    fun fromApk(context: Context, uri: Uri): String? = try {
        val file = File.createTempFile("identity-upload-", ".zip", context.cacheDir)
        try {
            context.contentResolver.openInputStream(uri)?.use { input ->
                file.outputStream().use { output ->
                    val buffer = ByteArray(8192)
                    var total = 0L
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        total += count
                        require(total <= MAX_ARCHIVE_BYTES) { "身份文件超过 32 MB。" }
                        output.write(buffer, 0, count)
                    }
                }
            } ?: return "无法读取身份文件。"
            fromArchive(context, file)
        } finally { file.delete() }
    } catch (_: Exception) { "无法读取身份文件，请重新选择。" }

    @Synchronized
    fun fromArchive(context: Context, archive: File): String? {
        if (archive.length() !in 1..MAX_ARCHIVE_BYTES) return "身份文件应在 32 MB 以内。"
        val found = mutableMapOf<String, ByteArray>()
        try {
            // Random access avoids inflating unrelated ZIP entries (including ZIP bombs).
            ZipFile(archive).use { zip ->
                val groups = listOf("", "offline-mfi/", "assets/offline-mfi/").filter { prefix ->
                    ENTRIES.values.all { zip.getEntry(prefix + it) != null }
                }
                if (groups.size != 1) return "所选文件中没有唯一、完整的 CarPlay 身份。"
                ENTRIES.values.forEach { name ->
                    val entry = zip.getEntry(groups.single() + name)
                    found[name] = zip.getInputStream(entry).use(::readLimited) ?: return "身份文件大小异常。"
                }
            }
        } catch (_: Exception) { return "无法读取身份文件，请重新选择。" }

        val root = identityDir(context).apply { mkdirs() }
        val staging = File(root, "import-staging").apply { deleteRecursively(); mkdirs() }
        val stagingMfi = File(staging, "offline-mfi").apply { mkdirs() }
        try {
            found.forEach { (name, bytes) -> File(stagingMfi, name).writeBytes(bytes) }
            MfiIdentity.load(ServerConfig(identityDir = staging.toPath(), stateDir = staging.toPath()))
        } catch (_: Exception) {
            staging.deleteRecursively()
            return "身份文件校验失败，原身份已保留。"
        }
        val target = File(root, "offline-mfi")
        val previous = File(root, "previous")
        // Recovery is also performed by identityDir after an interrupted import.
        if (target.exists()) {
            previous.deleteRecursively()
            if (!target.renameTo(previous)) {
                staging.deleteRecursively()
                return "无法替换身份，原身份已保留。"
            }
        }
        if (!stagingMfi.renameTo(target)) {
            previous.renameTo(target)
            staging.deleteRecursively()
            return "导入未完成，原身份已保留。"
        }
        previous.deleteRecursively()
        staging.deleteRecursively()
        return null
    }

    private fun readLimited(zip: InputStream): ByteArray? {
        val buffer = ByteArray(MAX_ENTRY_BYTES + 1)
        var total = 0
        while (total < buffer.size) {
            val read = zip.read(buffer, total, buffer.size - total)
            if (read < 0) break
            total += read
        }
        return if (total == 0 || total > MAX_ENTRY_BYTES) null else buffer.copyOf(total)
    }
}
