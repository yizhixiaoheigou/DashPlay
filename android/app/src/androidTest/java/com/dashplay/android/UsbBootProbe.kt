package com.dashplay.android

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.Bundle

/** Read-only package-manager regression: the USB handler must be resolvable before user unlock. */
internal object UsbBootProbe {
    fun run(context: Context, result: Bundle) {
        check(Build.VERSION.SDK_INT >= 24) { "Direct Boot requires API 24+" }
        val component = ComponentName("com.dashplay.android", "com.dashplay.android.UsbAttachActivity")
        val pm = context.packageManager
        @Suppress("DEPRECATION")
        val info = pm.getActivityInfo(component, PackageManager.GET_META_DATA)
        result.putBoolean("usb_direct_boot_aware", info.directBootAware)
        check(info.directBootAware) { "USB handler cannot receive an attached phone before user unlock" }
        @Suppress("DEPRECATION")
        val matches = pm.queryIntentActivities(Intent(UsbManager.ACTION_USB_DEVICE_ATTACHED),
            PackageManager.MATCH_DIRECT_BOOT_AWARE or PackageManager.GET_META_DATA)
        check(matches.any { it.activityInfo.packageName == component.packageName && it.activityInfo.name == component.className }) {
            "USB handler is not enabled/resolvable during Direct Boot"
        }
        result.putBoolean("usb_resolves_before_unlock", true)
        result.putBoolean("success", true)
    }
}

/** app_process keeps this check out of the running receiver, so observing it cannot restart CarPlay. */
object UsbBootProbeMain {
    @JvmStatic fun main(args: Array<String>) {
        try {
            android.os.Looper.prepareMainLooper()
            val activityThread = Class.forName("android.app.ActivityThread")
            val instance = activityThread.getMethod("systemMain").invoke(null)
            val context = activityThread.getMethod("getSystemContext").invoke(instance) as Context
            val result = Bundle()
            UsbBootProbe.run(context, result)
            result.keySet().sorted().forEach { println("$it=${result.get(it)}") }
        } catch (error: Throwable) {
            println("PROBE_FAILED " + generateSequence(error) { it.cause }.take(5)
                .joinToString(" / ") { "${it.javaClass.simpleName}: ${it.message}" })
            kotlin.system.exitProcess(1)
        }
        kotlin.system.exitProcess(0)
    }
}
