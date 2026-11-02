package com.dashplay.android

import android.content.ComponentName
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.os.Bundle

/** Intercept launches and isolate preferences: never start a real service or change user settings. */
internal object BootReceiverProbe {
    fun run(context: Context, result: Bundle) {
        val probe = object : ContextWrapper(context) {
            val services = mutableListOf<Intent>()
            val activities = mutableListOf<Intent>()
            var blockActivity = false
            override fun getApplicationContext(): Context = this
            override fun getSharedPreferences(name: String, mode: Int) =
                super.getSharedPreferences("boot-receiver-probe", mode)
            override fun startService(service: Intent): ComponentName? {
                services.add(service)
                return service.component
            }
            override fun startForegroundService(service: Intent): ComponentName? = startService(service)
            override fun startActivity(intent: Intent) {
                if (blockActivity) throw SecurityException("simulated OEM restriction")
                activities.add(intent)
            }
        }
        val prefs = probe.getSharedPreferences("boot-receiver-probe", Context.MODE_PRIVATE)
        try {
            prefs.edit().clear().putBoolean("setup_done", true).commit()
            val settings = Settings(probe)
            val receiver = BootReceiver()
            val boot = Intent(Intent.ACTION_BOOT_COMPLETED)
            settings.deviceKind = DeviceKind.BOX
            settings.startAtBoot = false
            receiver.onReceive(probe, boot)
            check(probe.services.isEmpty() && probe.activities.isEmpty()) { "Boot opt-out was ignored" }
            settings.startAtBoot = true
            settings.deviceKind = DeviceKind.PHONE
            receiver.onReceive(probe, boot)
            check(probe.services.size == 1 && probe.activities.isEmpty()) { "Phone boot must remain in background" }
            settings.deviceKind = DeviceKind.BOX
            receiver.onReceive(probe, boot)
            check(probe.services.size == 2 && probe.activities.size == 1) { "Box boot must start receiver and home screen" }
            check(probe.activities.single().component?.className == MainActivity::class.java.name)
            check(probe.activities.single().flags and Intent.FLAG_ACTIVITY_NEW_TASK != 0)
            probe.blockActivity = true
            receiver.onReceive(probe, boot)
            check(probe.services.size == 3) { "Blocked UI must not prevent receiver startup" }
            result.putBoolean("boot_launch_policy", true)
        } finally {
            prefs.edit().clear().commit()
        }
    }
}
