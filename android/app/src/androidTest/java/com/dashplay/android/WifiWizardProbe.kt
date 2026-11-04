package com.dashplay.android

import android.app.Instrumentation
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.TextView

/** Exercises the Wi-Fi/summary portion of the real wizard, with isolated preferences and no receiver restart. */
internal object WifiWizardProbe {
    fun run(instrumentation: Instrumentation, result: Bundle) {
        val isolated = object : ContextWrapper(instrumentation.targetContext) {
            override fun getApplicationContext(): Context = this
            override fun getSharedPreferences(name: String, mode: Int) = super.getSharedPreferences("wifi-wizard-probe", mode)
        }
        val prefs = isolated.getSharedPreferences("wifi-wizard-probe", Context.MODE_PRIVATE)
        prefs.edit().clear().putBoolean("ssid_split_done", true).commit()
        val settings = Settings(isolated)
        var activity = instrumentation.startActivitySync(Intent(instrumentation.targetContext, SetupActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as SetupActivity
        fun set(name: String, value: Any) = SetupActivity::class.java.getDeclaredField(name).apply { isAccessible = true }.set(activity, value)
        fun get(name: String): Any? = SetupActivity::class.java.getDeclaredField(name).apply { isAccessible = true }.get(activity)
        fun stepIndex(step: String) = (get("steps") as List<*>).indexOf(step)
        fun call(name: String): Any? = SetupActivity::class.java.getDeclaredMethod(name).apply { isAccessible = true }.invoke(activity)
        fun texts(view: View): List<String> = (if (view is TextView) listOf(view.text.toString()) else emptyList()) +
            (if (view is ViewGroup) (0 until view.childCount).flatMap { texts(view.getChildAt(it)) } else emptyList())
        try {
            instrumentation.runOnMainSync {
                set("settings", settings); set("network", Network.MIFI); set("link", IphoneLink.WIRELESS)
                set("car", Car.OTHER); set("kind", DeviceKind.BOX); set("index", stepIndex(SetupActivity.STEP_WIFI))
                set("ssid", "Unused local"); set("passphrase", "local-secret")
                call("render")
                activity.findViewById<EditText>(R.id.field_ssid).setText("Wizard external")
                activity.findViewById<EditText>(R.id.field_password).setText("external-secret")
                call("next")
                check(get("index") == stepIndex(SetupActivity.STEP_SUMMARY) && get("wifiError") == null)
                check(texts(activity.window.decorView).any { it.contains("Wizard external") })
                check(texts(activity.window.decorView).none { it.contains("Unused local") })
                activity.onBackPressed()
                check(activity.findViewById<EditText>(R.id.field_ssid).text.toString() == "Wizard external")
                check(activity.findViewById<EditText>(R.id.field_password).text.toString() == "external-secret")
                call("next")
            }
            val monitor = instrumentation.addMonitor(SetupActivity::class.java.name, null, false)
            instrumentation.runOnMainSync { activity.recreate() }
            activity = monitor.waitForActivityWithTimeout(5000) as? SetupActivity ?: error("Wizard recreation timed out")
            instrumentation.removeMonitor(monitor)
            instrumentation.waitForIdleSync()
            instrumentation.runOnMainSync {
                set("settings", settings)
                check(get("index") == stepIndex(SetupActivity.STEP_SUMMARY) && get("uplinkSsid") == "Wizard external")
                check(get("uplinkPassphrase") == "external-secret" && get("passphrase") == "local-secret")
                val update = call("summaryUpdate") as ManagementSettings
                check(update.ssid == null && update.passphrase == null && update.uplinkSsid == "Wizard external")
                settings.apply(update)
                check(settings.setupDone && settings.uplink()?.ssid == "Wizard external" && settings.hotspot() == null)
                activity.onBackPressed()
                set("network", Network.THIS_HOTSPOT); call("render")
                check(activity.findViewById<EditText>(R.id.field_ssid).text.toString() == "Unused local")
                call("next")
                val localUpdate = call("summaryUpdate") as ManagementSettings
                check(localUpdate.uplinkSsid == null && localUpdate.uplinkPassphrase == null)
                settings.apply(localUpdate)
                check(settings.hotspot()?.ssid == "Unused local" && settings.uplink()?.ssid == "Wizard external")
            }
            result.putBoolean("wifi_summary_back_recreate", true)
            result.putBoolean("wizard_payload_both_networks", true)
            result.putBoolean("success", true)
        } finally {
            instrumentation.runOnMainSync { activity.finish() }
            prefs.edit().clear().commit()
        }
    }
}
