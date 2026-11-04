package com.dashplay.android

import android.content.Context
import android.content.ContextWrapper
import android.os.Bundle

/** Real SharedPreferences transactions in an isolated namespace; never starts or reconfigures a receiver. */
internal object WifiSetupProbe {
    fun run(context: Context, result: Bundle) {
        val isolated = object : ContextWrapper(context) {
            override fun getApplicationContext(): Context = this
            override fun getSharedPreferences(name: String, mode: Int) = super.getSharedPreferences("wifi-setup-probe", mode)
        }
        val prefs = isolated.getSharedPreferences("wifi-setup-probe", Context.MODE_PRIVATE)
        try {
            prefs.edit().clear().putBoolean("ssid_split_done", true).commit()
            val settings = Settings(isolated)
            // A first-run external-network setup must not need any local-hotspot credentials.
            settings.apply(ManagementSettings(car = Car.OTHER, network = Network.MIFI, iphoneLink = IphoneLink.WIRELESS,
                uplinkSsid = "Probe external", uplinkPassphrase = "external-test", wifiSecurity = WifiSecurity.WPA2, setupDone = true))
            check(settings.uplink()?.ssid == "Probe external" && settings.hotspot() == null)
            check(settings.wifiSecurity == WifiSecurity.WPA2 && settings.connectionWifi()?.ssid == "Probe external")
            result.putBoolean("fresh_external_setup", true)
            // Keep both networks and their different encryption modes while switching topology.
            settings.apply(ManagementSettings(network = Network.THIS_HOTSPOT, ssid = "Probe local", passphrase = "",
                wifiSecurity = WifiSecurity.OPEN))
            check(settings.hotspot()?.ssid == "Probe local" && settings.wifiSecurity == WifiSecurity.OPEN)
            check(settings.connectionWifi()?.ssid == "Probe local" && settings.connectionWifi()?.passphrase == "")
            check(settings.uplink()?.passphrase == "external-test")
            settings.apply(ManagementSettings(network = Network.MIFI))
            check(settings.wifiSecurity == WifiSecurity.WPA2 && settings.uplink()?.ssid == "Probe external")
            settings.apply(ManagementSettings(uplinkPassphrase = "", wifiSecurity = WifiSecurity.OPEN))
            check(settings.uplink()?.passphrase == "" && settings.wifiSecurity == WifiSecurity.OPEN)
            settings.apply(ManagementSettings(network = Network.THIS_HOTSPOT))
            check(settings.hotspot()?.ssid == "Probe local" && settings.hotspot()?.passphrase == "")
            result.putBoolean("separate_network_security", true)
            val before = prefs.all.toMap()
            val failure = runCatching { settings.apply(ManagementSettings(network = Network.MIFI,
                uplinkSsid = "Bad external", uplinkPassphrase = "short", wifiSecurity = WifiSecurity.WPA2)) }.exceptionOrNull()
            check(failure is IllegalArgumentException && prefs.all == before)
            result.putBoolean("invalid_update_atomic", true)
            for (original in listOf(Network.MIFI, Network.THIS_HOTSPOT)) {
                prefs.edit().clear().putBoolean("ssid_split_done", true).putString("network", original.name)
                    .putString("hotspot_ssid", "Legacy local").putString("hotspot_passphrase", "local-secret")
                    .putString("uplink_ssid", "Legacy external").putString("uplink_passphrase", "external-secret")
                    .putString("wifi_security", "WPA3").commit()
                check(settings.wifiSecurity == WifiSecurity.WPA3)
                settings.network = if (original == Network.MIFI) Network.THIS_HOTSPOT else Network.MIFI
                settings.apply(ManagementSettings(network = original))
                check(settings.wifiSecurity == WifiSecurity.WPA3)
            }
            result.putBoolean("legacy_wpa3_survives_switch", true)
            result.putBoolean("success", true)
        } finally { prefs.edit().clear().commit() }
    }
}
