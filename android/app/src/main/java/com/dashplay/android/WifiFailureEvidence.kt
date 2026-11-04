package com.dashplay.android

/** Read-only ROM observation. Unknown/blocked APIs never authorize a reconnect. */
internal object WifiFailureEvidence {
    fun temporaryAssociationRejected(config: Any): Boolean? = runCatching {
        val status = config.javaClass.getMethod("getNetworkSelectionStatus").invoke(config) ?: return null
        val temporary = status.javaClass.getMethod("isNetworkTemporaryDisabled").invoke(status) as? Boolean ?: return null
        if (!temporary) return false
        val reason = status.javaClass.getMethod("getNetworkSelectionDisableReason").invoke(status) as? Int ?: return null
        val associationReason = listOf("NETWORK_SELECTION_DISABLED_ASSOCIATION_REJECTION", "DISABLED_ASSOCIATION_REJECTION")
            .firstNotNullOfOrNull { name -> runCatching { status.javaClass.getField(name).getInt(null) }.getOrNull() }
            ?: return null
        reason == associationReason
    }.getOrNull()
}
