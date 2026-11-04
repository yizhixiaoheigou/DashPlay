package com.dashplay.android

import android.app.Activity
import android.app.Instrumentation
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.widget.CheckBox
import android.widget.TextView

/** Exercises the authorization step with isolated preferences; intercepts the permission-screen launch. */
internal object UsbWizardProbe {
    fun run(instrumentation: Instrumentation, result: Bundle) {
        val isolated = object : ContextWrapper(instrumentation.targetContext) {
            override fun getApplicationContext(): Context = this
            override fun getSharedPreferences(name: String, mode: Int) = super.getSharedPreferences("usb-wizard-probe", mode)
        }
        val prefs = isolated.getSharedPreferences("usb-wizard-probe", Context.MODE_PRIVATE)
        prefs.edit().clear().putBoolean("ssid_split_done", true).commit()
        val settings = Settings(isolated)
        val activity = instrumentation.startActivitySync(Intent(instrumentation.targetContext, SetupActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as SetupActivity
        fun set(name: String, value: Any) = SetupActivity::class.java.getDeclaredField(name).apply { isAccessible = true }.set(activity, value)
        fun get(name: String): Any? = SetupActivity::class.java.getDeclaredField(name).apply { isAccessible = true }.get(activity)
        fun call(name: String) = SetupActivity::class.java.getDeclaredMethod(name).apply { isAccessible = true }.invoke(activity)
        fun views(view: View): List<View> = listOf(view) + if (view is ViewGroup)
            (0 until view.childCount).flatMap { views(view.getChildAt(it)) } else emptyList()
        val monitor = instrumentation.addMonitor(UsbActivity::class.java.name,
            Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null), true)
        try {
            instrumentation.runOnMainSync {
                set("settings", settings); set("network", Network.MIFI); set("link", IphoneLink.WIRED)
                set("car", Car.OTHER); set("index", 6); call("render")
                check(activity.findViewById<CheckBox>(R.id.usb_internet).isChecked)
                check(activity.findViewById<TextView>(R.id.primary).text.toString() == "授权并继续")
                activity.findViewById<CheckBox>(R.id.usb_internet).performClick()
                check(!settings.usbInternetRequested && !settings.usbInternet)
                activity.findViewById<View>(R.id.primary).performClick()
            }
            instrumentation.waitForIdleSync()
            check(monitor.hits == 1 && !settings.usbEnabled && !settings.usbInternetAllowed)
            instrumentation.runOnMainSync {
                check(get("index") == 6) // Cancelled authorization must not advance or fake consent.
                check(!activity.findViewById<CheckBox>(R.id.usb_internet).isChecked)
                views(activity.window.decorView).filterIsInstance<TextView>().single { it.text.toString() == "稍后配置" }.performClick()
                check(get("index") == 8) // Wired setup skips Wi-Fi credentials.
                check(views(activity.window.decorView).filterIsInstance<TextView>().any {
                    it.text.contains("尚未") || it.text.contains("尚不可用")
                })
                activity.onBackPressed()
                // Real hardware without USB Host skips this step when traversing; retain the same checkbox draft.
                set("index", 6); call("render")
                check(!activity.findViewById<CheckBox>(R.id.usb_internet).isChecked)
            }
            result.putBoolean("usb_step_preselect_cancel_and_skip", true)
            result.putBoolean("no_fake_usb_or_vpn_authorization", true)
            result.putBoolean("success", true)
        } finally {
            instrumentation.removeMonitor(monitor)
            instrumentation.runOnMainSync { activity.finish() }
            prefs.edit().clear().commit()
        }
    }
}
