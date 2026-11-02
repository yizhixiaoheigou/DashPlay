package com.dashplay.android

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import java.io.File

/** API 21–23 installers cannot consume content URIs. Stage privately through the public session API. */
internal object LegacyApkInstaller {
    const val RESULT = "com.dashplay.android.UPDATE_RESULT"
    fun stage(context: Context, file: File): Int {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName); setAppLabel("DashPlay"); setSize(file.length())
        }
        val id = installer.createSession(params)
        try {
            installer.openSession(id).use { session ->
                session.openWrite("base.apk", 0, file.length()).use { output ->
                    file.inputStream().use { it.copyTo(output, 64 * 1024) }
                    session.fsync(output)
                }
            }
            return id
        } catch (error: Exception) { abandon(context, id); throw error }
    }
    fun commit(context: Context, id: Int) {
        val intent = Intent(context, UpdateActivity::class.java).setAction(RESULT)
            .setData(Uri.parse("dashplay-update://session/$id"))
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0
        val callback = PendingIntent.getActivity(context, id, intent, flags)
        context.packageManager.packageInstaller.openSession(id).use { it.commit(callback.intentSender) }
    }
    fun abandon(context: Context, id: Int) {
        runCatching { context.packageManager.packageInstaller.abandonSession(id) }
    }
}
