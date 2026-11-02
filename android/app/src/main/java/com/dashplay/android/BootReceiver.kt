package com.dashplay.android

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

/** Start the receiver at boot; a dedicated box also opens its home screen. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val settings = Settings(context)
        if (!settings.startAtBoot) return
        val service = Intent(context, DashPlayService::class.java)
        if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(service) else context.startService(service)
        if (settings.deviceKind == DeviceKind.BOX && settings.setupDone) {
            // Android 10+ / OEM policies may require an explicit launcher autostart setting.
            // Starting the screen must never be a prerequisite for the receiver to work.
            try {
                context.startActivity(Intent(context, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                android.util.Log.i("DashPlay", "Boot: requested box home screen")
            } catch (error: RuntimeException) {
                android.util.Log.w("DashPlay", "Boot: box home screen was blocked; receiver remains running", error)
            }
        }
    }
}
