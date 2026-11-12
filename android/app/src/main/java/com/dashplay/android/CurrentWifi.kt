package com.dashplay.android

import android.content.Context
import android.net.wifi.WifiManager

/**
 * The SSID this device is actually joined to, or null when it cannot be read.
 *
 * Reading the SSID needs a location permission on Android 8–12 and NEARBY_WIFI_DEVICES on 13+; both are declared for
 * the phone scan, so on a phone we normally get a value. A box that is not on Wi-Fi, or a ROM that hides it, returns
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
        return current.equals(stored.trim(), ignoreCase = true)
    }
}
