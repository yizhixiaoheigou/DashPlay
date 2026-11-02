package com.dashplay.android

import android.content.ContentProvider
import android.content.ContentValues
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import java.io.FileNotFoundException

/** Non-exported; the system installer receives a read grant for one private, verified copy. */
class UpdateFileProvider : ContentProvider() {
    override fun onCreate() = true
    private fun file(uri: Uri): java.io.File {
        if (uri.authority != context!!.packageName + ".updates" || uri.pathSegments.size != 1 || uri.query != null || uri.fragment != null)
            throw FileNotFoundException()
        return try { ApkUpdates.file(context!!, uri.pathSegments.single()) } catch (_: Exception) { throw FileNotFoundException() }
    }
    override fun getType(uri: Uri): String { file(uri); return ApkUpdates.MIME }
    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor {
        if (mode != "r") throw FileNotFoundException("Read only")
        return ParcelFileDescriptor.open(file(uri), ParcelFileDescriptor.MODE_READ_ONLY)
    }
    override fun query(uri: Uri, projection: Array<out String>?, selection: String?, selectionArgs: Array<out String>?, sortOrder: String?): MatrixCursor {
        val file = file(uri)
        val columns = projection ?: arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE)
        return MatrixCursor(columns).apply {
            addRow(columns.map { when (it) { OpenableColumns.DISPLAY_NAME -> "DashPlay-update.apk"; OpenableColumns.SIZE -> file.length(); else -> null } })
        }
    }
    override fun insert(uri: Uri, values: ContentValues?): Uri = throw UnsupportedOperationException()
    override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?): Int = throw UnsupportedOperationException()
    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int = throw UnsupportedOperationException()
}
