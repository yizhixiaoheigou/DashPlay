package com.dashplay.android

import android.content.Context
import android.content.ContextWrapper
import android.os.Bundle

/** Preference-policy fixtures only: no real VPN consent, USB mode change or receiver startup. */
internal object UsbDefaultsProbe {
    fun run(context: Context, result: Bundle) {
        val isolated = object : ContextWrapper(context) {
            override fun getApplicationContext(): Context = this
            override fun getSharedPreferences(name: String, mode: Int) = super.getSharedPreferences("usb-defaults-probe", mode)
        }
        val prefs = isolated.getSharedPreferences("usb-defaults-probe", Context.MODE_PRIVATE)
        try {
            prefs.edit().clear().commit()
            val settings = Settings(isolated)
            check(settings.usbInternetRequested && !settings.usbInternet && !settings.usesUsb && !settings.usbChoiceMade)
            check(settings.initialIphoneLink(true) == IphoneLink.WIRED)
            check(settings.initialIphoneLink(false) == IphoneLink.ANY)
            check(runCatching { settings.usbInternet = true }.isFailure)
            check(runCatching { settings.apply(ManagementSettings(usbInternet = true)) }.isFailure)
            check(!settings.usbInternet && !settings.usbInternetAllowed)
            result.putBoolean("fresh_preselected_without_consent", true)
            settings.requestUsbInternet(false)
            settings.usbEnabled = true
            // The isolated consent value is a policy fixture, not a system authorization grant.
            prefs.edit().putBoolean("usb_internet_consent", true).commit()
            check(!Settings(isolated).usbInternetRequested && !Settings(isolated).usbInternet)
            settings.apply(ManagementSettings(usbInternet = true))
            check(settings.usbInternet)
            settings.usbEnabled = false
            check(settings.usbInternetRequested && !settings.usbInternet && settings.usbChoiceMade)
            settings.usbEnabled = true
            check(settings.usbInternet)
            prefs.edit().putBoolean("usb_internet_consent", false).commit()
            check(settings.usbInternetRequested && !settings.usbInternet)
            result.putBoolean("choice_permission_runtime_separated", true)
            // Existing explicit choices and old implicit ANY mode survive an upgrade.
            for (choice in IphoneLink.values()) {
                settings.iphoneLink = choice
                check(settings.initialIphoneLink(true) == choice)
            }
            prefs.edit().remove("iphone_link").putBoolean("setup_done", true).putBoolean("usb_internet", false).commit()
            check(settings.initialIphoneLink(true) == IphoneLink.ANY)
            check(!settings.usbInternetRequested && !settings.usbInternet)
            result.putBoolean("existing_choices_preserved", true)
            result.putBoolean("success", true)
        } finally { prefs.edit().clear().commit() }
    }
}
