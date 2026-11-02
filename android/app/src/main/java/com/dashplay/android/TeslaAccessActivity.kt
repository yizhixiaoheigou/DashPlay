package com.dashplay.android

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.TextView
import android.widget.CheckBox

class TeslaAccessActivity : Activity() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var status: TextView
    private lateinit var checks: CheckBox
    private val update = object : Runnable {
        override fun run() {
            status.text = TeslaRootAccess.message
            checks.isEnabled = Settings(this@TeslaAccessActivity).teslaRootAccess || checks.isChecked
            handler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = Ui(this)
        val settings = Settings(this)
        val root = ui.stack()
        root.addView(ui.button(R.style.Button_Text, "返回") { finish() })
        root.addView(ui.text(R.style.Text_Title, "特斯拉访问地址"))
        root.addView(ui.text(R.style.Text, "车机连接这台安卓设备的热点时，可通过 100.96.0.1 打开 DashPlay。此方式需要设备已有 Root，并允许本应用使用。"))
        root.addView(ui.text(R.style.Text_Small, "只转接热点客户端访问 DashPlay 的网页端口，关闭时移除本应用规则。不会更改热点名称、密码或网段，也不会提供互联网；车机仍需保持连接这个热点。无需占用 VPN。"))
        status = ui.text(R.style.Text_Name, TeslaRootAccess.message)
        root.addView(status)
        checks = CheckBox(this).apply {
            id = R.id.tesla_offline_checks; text = "帮助特斯拉保持无互联网的热点连接"; minHeight = ui.dp(48)
            isChecked = settings.teslaOfflineChecks; isEnabled = settings.teslaRootAccess || isChecked
            setOnCheckedChangeListener { _, value ->
                try { settings.teslaOfflineChecks = value; DashPlayService.restart(this@TeslaAccessActivity) }
                catch (_: Exception) { status.text = "设置未保存，请先启用 Root 访问地址。"; isChecked = settings.teslaOfflineChecks }
            }
        }
        root.addView(checks)
        root.addView(ui.text(R.style.Text_Small, "先启用 Root 访问方式。开启后由本机回应车机联网检查；没有外网时，CarPlay 可在局域网运行，车机自带音乐和在线地图仍不能上网。关闭 Root 访问也会停止此功能。"))
        root.addView(ui.button(R.style.Button_Primary, "授权 Root 并启用") {
            if (runCatching { settings.enableTeslaRootAccess() }.isSuccess) DashPlayService.restart(this)
            else status.text = "设置未保存，请重试。"
        }.apply { id = R.id.tesla_root_enable })
        root.addView(ui.button(R.style.Button_Secondary, "关闭并清理访问规则") {
            if (runCatching { settings.teslaRootAccess = false }.isSuccess) DashPlayService.restart(this)
            else status.text = "设置未保存，请重试。"
        }.apply { id = R.id.tesla_root_disable })
        root.addView(ui.text(R.style.Text_Head, "没有 Root 或不能开热点"))
        root.addView(ui.text(R.style.Text_Small, "使用可配置局域网网段的外部路由器，让安卓、iPhone 和车机接入同一网络；由路由器提供车机可访问的地址。普通 VPN 的本机地址不能直接当作热点访问地址。"))
        root.addView(ui.button(R.style.Button_Text, "本地热点设置") { startActivity(Intent(this, HotspotActivity::class.java)) })
        setContentView(ui.page(root))
    }

    override fun onResume() { super.onResume(); handler.post(update) }
    override fun onPause() { handler.removeCallbacks(update); super.onPause() }
}
