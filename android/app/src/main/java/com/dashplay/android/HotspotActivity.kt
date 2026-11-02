package com.dashplay.android

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.ImageView

/** Device-local credentials screen; passwords are not exposed by diagnostics or browser polling. */
class HotspotActivity : Activity() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var ui: Ui
    private var shown = ""
    override fun onCreate(state: Bundle?) { super.onCreate(state); ui = Ui(this) }
    override fun onResume() { super.onResume(); shown = ""; refresh() }
    override fun onPause() { handler.removeCallbacksAndMessages(null); super.onPause() }

    private fun enable() {
        if (Build.VERSION.SDK_INT < 29) return
        val permissions = if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.NEARBY_WIFI_DEVICES)
            else arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
        if (permissions.any { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }) {
            requestPermissions(permissions, 7); return
        }
        Settings(this).apply { network = Network.THIS_HOTSPOT; autoHotspot = true }
        DashPlayService.restart(this); shown = ""; refresh()
    }

    override fun onRequestPermissionsResult(code: Int, permissions: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, permissions, results)
        if (code == 7 && results.isNotEmpty() && results.all { it == PackageManager.PERMISSION_GRANTED }) enable()
    }

    private fun refresh() {
        val settings = Settings(this)
        val local = DashPlayService.managedHotspot
        val active = local?.connection
        val key = "${settings.usesLocalHotspot}:${local?.phase}:${local?.message}:${active?.ssid}:${active?.channel}"
        if (shown != key) {
            shown = key
            val root = ui.stack()
            root.addView(ui.button(R.style.Button_Text, "返回") { finish() })
            root.addView(ui.text(R.style.Text_Title, "本地热点"))
            root.addView(ui.text(R.style.Text_Small, "让车机和 iPhone 连接这台设备。不需要 SIM 卡；应用热点只用于设备互联，不提供互联网。"))
            val supported = Build.VERSION.SDK_INT >= 29 && packageManager.hasSystemFeature(PackageManager.FEATURE_WIFI_DIRECT)
            root.addView(ui.choice("应用自动创建", if (supported) "自动取得连接参数，无需填写信道；需保持系统 Wi-Fi 开启。" else "此设备不支持，请使用系统热点或外部网络。", settings.usesLocalHotspot, enabled = supported) { enable() })
            root.addView(ui.choice("使用系统热点或外部网络", "保留已有 Wi-Fi 名称和密码；系统热点由你开启。", !settings.usesLocalHotspot) {
                settings.autoHotspot = false; DashPlayService.restart(this); shown = ""; refresh()
            })
            if (settings.usesLocalHotspot) {
                root.addView(ui.text(R.style.Text_Name, local?.message ?: "正在启动"))
                if (active != null) {
                    root.addView(ui.text(R.style.Text_Name, active.ssid))
                    root.addView(ui.text(R.style.Text_Small, "密码：${active.password}\n信道：${active.channel}"))
                    root.addView(ImageView(this).apply {
                        contentDescription = "连接本地热点的二维码"
                        setImageBitmap(ui.qr("WIFI:T:WPA;S:${active.ssid};P:${active.password};;", 400))
                    }, android.widget.LinearLayout.LayoutParams(ui.dp(180), ui.dp(180)))
                }
                root.addView(ui.button(R.style.Button_Secondary, "重试本地热点") { enable() })
            }
            root.addView(ui.button(R.style.Button_Secondary, "打开系统 Wi-Fi 设置") { startActivity(Intent(android.provider.Settings.ACTION_WIFI_SETTINGS)) })
            root.addView(ui.button(R.style.Button_Text, "打开系统热点设置") { Intents.openHotspotSettings(this) })
            setContentView(ui.page(root))
        }
        handler.removeCallbacksAndMessages(null); handler.postDelayed(::refresh, 1_000)
    }
}
