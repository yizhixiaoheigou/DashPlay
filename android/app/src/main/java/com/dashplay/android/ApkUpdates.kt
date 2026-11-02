package com.dashplay.android

import android.content.Context
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest

internal object ApkUpdates {
    const val MIME = "application/vnd.android.package-archive"
    const val MAX_BYTES = 128L * 1024 * 1024
    data class Candidate(val file: File, val version: String, val code: Long)
    // Old libcore can use a negative random number in File.createTempFile names.
    private val name = Regex("update--?[0-9]+\\.apk")
    internal fun validFileName(filename: String) = name.matches(filename)
    private fun directory(context: Context) = File(context.cacheDir, "apk-updates").apply { mkdirs() }
    fun file(context: Context, filename: String): File {
        require(validFileName(filename)) { "更新文件不可用，请重新选择。" }
        return File(directory(context), filename).also {
            require(it.isFile && it.canonicalFile.parentFile == directory(context).canonicalFile) { "更新文件不可用，请重新选择。" }
        }
    }
    @Suppress("DEPRECATION")
    private val flags get() = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
    @Suppress("DEPRECATION")
    private fun policy(info: PackageInfo): ApkUpdatePolicy.Package {
        val signatures = if (Build.VERSION.SDK_INT >= 28) info.signingInfo?.apkContentsSigners else info.signatures
        val signers = signatures.orEmpty().map { signature ->
            MessageDigest.getInstance("SHA-256").digest(signature.toByteArray()).joinToString("") { "%02x".format(it.toInt() and 255) }
        }.toSet()
        return ApkUpdatePolicy.Package(info.packageName, if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong(),
            signers, !info.splitNames.isNullOrEmpty())
    }
    @Suppress("DEPRECATION")
    fun inspect(context: Context, file: File): Candidate {
        require(file.length() in 1..MAX_BYTES) { "安装包为空或超过 128 MB。" }
        val pm = context.packageManager
        val candidate = pm.getPackageArchiveInfo(file.path, flags) ?: throw IllegalArgumentException("无法识别安装包，请选择完整 APK 文件。")
        val installed = pm.getPackageInfo(context.packageName, flags)
        ApkUpdatePolicy.check(policy(installed), policy(candidate))
        if (Build.VERSION.SDK_INT >= 24) require((candidate.applicationInfo?.minSdkVersion ?: 1) <= Build.VERSION.SDK_INT) {
            "这个版本不支持当前安卓系统。"
        }
        return Candidate(file, candidate.versionName.orEmpty(), policy(candidate).version)
    }
    fun prepare(context: Context, uri: Uri): Candidate {
        require(uri.scheme == "content") { "请使用系统文件选择器选择安装包。" }
        val target = File.createTempFile("update-", ".apk", directory(context))
        try {
            context.contentResolver.openInputStream(uri)?.use { input ->
                FileOutputStream(target).use { output ->
                    val buffer = ByteArray(64 * 1024); var size = 0L
                    while (true) {
                        val count = input.read(buffer); if (count < 0) break
                        size += count; require(size <= MAX_BYTES) { "安装包超过 128 MB。" }
                        output.write(buffer, 0, count)
                    }
                    output.fd.sync()
                }
            } ?: throw IllegalArgumentException("无法读取安装包，请重新选择。")
            return inspect(context, target)
        } catch (error: Exception) { target.delete(); throw error }
    }
    fun uri(context: Context, file: File): Uri = Uri.Builder().scheme("content")
        .authority(context.packageName + ".updates").appendPath(file.name).build()
}
