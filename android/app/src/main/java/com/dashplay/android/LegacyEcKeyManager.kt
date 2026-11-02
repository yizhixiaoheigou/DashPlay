package com.dashplay.android

import java.net.Socket
import java.security.Principal
import javax.net.ssl.X509KeyManager

/** Android 5 requests EC_EC for ECDHE_ECDSA and incorrectly filters by the leaf issuer's algorithm.
 * Used only with static ECDH suites disabled: ephemeral ECDHE needs an EC leaf, regardless of its issuer.
 * This changes server identity selection, never certificate validation or client trust.
 */
internal class LegacyEcKeyManager(private val delegate: X509KeyManager) : X509KeyManager by delegate {
    override fun chooseServerAlias(keyType: String?, issuers: Array<out Principal>?, socket: Socket?): String? =
        delegate.chooseServerAlias(keyType, issuers, socket)
            ?: if (keyType == "EC_EC") delegate.chooseServerAlias("EC", issuers, socket) else null
}
