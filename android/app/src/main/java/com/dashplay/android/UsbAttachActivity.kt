package com.dashplay.android

import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Build
import android.os.UserManager

/**
 * Receives USB_DEVICE_ATTACHED so Android remembers "Always open DashPlay" across plug-ins and 0x52
 * CarPlay re-enumerations. Enabled for initial USB setup, then while wired USB is enabled and DashPlay has not
 * been explicitly stopped, so stopping DashPlay or turning off USB leaves normal photo/MTP transfer untouched.
 */
class UsbAttachActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // USB enumeration can precede user unlock even on an unencrypted TV box (SY910:
        // attach at 12 s, unlock at 14.5 s). Android must resolve this activity during
        // Direct Boot to grant the saved USB default. CE preferences/identity are not
        // available yet; BootReceiver starts the receiver after unlock instead.
        if (Build.VERSION.SDK_INT >= 24 && !getSystemService(UserManager::class.java).isUserUnlocked) {
            android.util.Log.i("DashPlay", "USB attached before user unlock; waiting for normal boot")
            finish()
            return
        }
        val settings = Settings(this)
        if (!DashPlayService.userStopped) {
            if (settings.usesUsb) {
                if (DashPlayService.status == Status.STOPPED) DashPlayService.start(this)
            } else if (settings.wiredCarPlaySupported && !settings.usbChoiceMade) {
                // First attach leads to visible local setup, never silently grants VPN/Internet consent.
                startActivity(Intent(this, if (settings.setupDone) UsbActivity::class.java else MainActivity::class.java))
            }
        }
        finish()
    }

    companion object {
        fun sync(context: Context, active: Boolean = !DashPlayService.userStopped && Settings(context).let { it.usesUsb || it.wiredCarPlaySupported && !it.usbChoiceMade }) {
            runCatching {
                val component = ComponentName(context.applicationContext, UsbAttachActivity::class.java)
                val state = if (active) PackageManager.COMPONENT_ENABLED_STATE_ENABLED
                else PackageManager.COMPONENT_ENABLED_STATE_DISABLED
                val pm = context.applicationContext.packageManager
                if (pm.getComponentEnabledSetting(component) != state) {
                    pm.setComponentEnabledSetting(component, state, PackageManager.DONT_KILL_APP)
                }
            }
        }
    }
}
