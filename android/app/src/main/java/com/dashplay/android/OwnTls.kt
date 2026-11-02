package com.dashplay.android

import android.content.Context
import java.io.File
import kotlinx.serialization.Serializable

object OwnTls {
    private var instance: OwnTlsStore? = null
    @Synchronized fun store(context: Context): OwnTlsStore = instance ?: OwnTlsStore(
        File(context.applicationContext.filesDir, "own-tls")).also { instance = it }
}

@Serializable data class TlsImport(val domain: String, val chain: String, val key: String)
@Serializable data class TlsEnable(val enabled: Boolean)
