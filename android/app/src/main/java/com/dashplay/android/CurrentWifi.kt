package com.dashplay.android

import android.content.Context
import android.net.wifi.WifiManager

/**
 * The SSID this device is actually joined to, or null when it cannot be read.
 *
 * SSID is location-sensitive on modern Android too. Nearby-device consent alone may still return UNKNOWN_SSID;
 * setup can request foreground location permission or let the user enter the network explicitly. A box that is not on Wi-Fi, or a ROM that hides it, returns
 * null and the setup screen falls back to asking the user (never invent a name).
 */
object CurrentWifi {
    fun ssid(context: Context): String? = runCatching {
        val manager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
            ?: return null
        @Suppress("DEPRECATION")
        val raw = manager.connectionInfo?.ssid ?: return null
        raw.removeSurrounding("\"")
            .takeIf { it.isNotBlank() && !it.contains("unknown", ignoreCase = true) && it != "0x" }
    }.getOrNull()

    /** True when the stored name matches what the device is on; null when we cannot tell. */
    fun matches(context: Context, stored: String): Boolean? {
        val current = ssid(context) ?: return null
        return current == stored.trim()
    }
}
