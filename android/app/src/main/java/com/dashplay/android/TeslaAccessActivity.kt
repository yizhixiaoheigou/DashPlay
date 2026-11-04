package com.dashplay.android

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.widget.CheckBox
import android.widget.EditText
import android.widget.TextView

/** Access routes share a LAN prerequisite; HTTP VPN and HTTPS Root are distinct choices. */
class TeslaAccessActivity : Activity() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var status: TextView
    private lateinit var compatStatus: TextView
    private lateinit var checks: CheckBox
    private lateinit var port: EditText
    private val update = object : Runnable {
        override fun run() {
            val settings = Settings(this@TeslaAccessActivity)
            status.text = TeslaRootAccess.message
            checks.isEnabled = settings.teslaRootAccess || checks.isChecked
            compatStatus.text = HttpSetupStatus.read(this@TeslaAccessActivity).message
            handler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = Ui(this)
        val settings = Settings(this)
        val root = ui.stack()
        root.addView(ui.button(R.style.Button_Text, "返回") { finish() })
        root.addView(ui.text(R.style.Text_Title, "特斯拉访问"))

        root.addView(ui.text(R.style.Text_Head, "HTTPS：本机热点的 Root 地址"))
        root.addView(ui.text(R.style.Text, "这条 HTTPS 路线需要设备已有 Root 并允许 DashPlay 使用，同时准备域名、可信证书和解析。没有 Root 可选择下面的 HTTP 兼容路线，或由外部网络提供可达地址。"))
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
            if (runCatching { settings.compatHttp = false; settings.enableTeslaRootAccess() }.isSuccess) DashPlayService.restart(this)
            else status.text = "设置未保存，请重试。"
        }.apply { id = R.id.tesla_root_enable })
        root.addView(ui.button(R.style.Button_Secondary, "关闭并清理访问规则") {
            if (runCatching { settings.teslaRootAccess = false }.isSuccess) DashPlayService.restart(this)
            else status.text = "设置未保存，请重试。"
        }.apply { id = R.id.tesla_root_disable })

        root.addView(ui.text(R.style.Text_Head, "HTTP：免 Root 兼容入口（实验性功能）"))
        root.addView(ui.text(R.style.Text_Small, "无需 Root、域名或证书。HTTP 页面优先使用 WebRTC 视频传输；连接失败时自动改用 JPEG 图片传输。JPEG 的清晰度和流畅度受设备性能限制，最大边不超过 1280 像素。WebRTC 的实际效果取决于设备、浏览器和网络，需要在车机上确认。设置和更新通过同端口的 HTTP 管理页提供。"))
        root.addView(ui.text(R.style.Text_Small, getString(R.string.browser_scheme_hint)))
        compatStatus = ui.text(R.style.Text_Name, "正在读取状态……")
        root.addView(compatStatus)
        root.addView(ui.button(R.style.Button_Primary, "配置免 Root HTTP 入口") {
            startActivity(Intent(this, HttpSetupActivity::class.java))
        }.apply { id = R.id.compat_http })
        root.addView(ui.text(R.style.Text_Small, "HTTP 和 HTTPS 共用网页端口，默认 18743，可设置为 1024–65535。管理页始终使用 HTTP，关闭 HTTP 画面不影响管理功能。"))
        port = EditText(this).apply {
            id = R.id.compat_http_port
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(settings.browserPort.toString())
            hint = "网页端口（默认 ${BrowserEndpoint.DEFAULT_COMPAT_PORT}）"
            minHeight = ui.dp(48)
        }
        root.addView(port)
        root.addView(ui.button(R.style.Button_Secondary, "保存端口并重启服务") {
            val typed = port.text.toString().toIntOrNull() ?: 0
            runCatching { settings.browserPort = typed; DashPlayService.restart(this) }
                .onFailure { compatStatus.text = it.message ?: "端口无效。" }
        }.apply { id = R.id.compat_http_save })

        root.addView(ui.text(R.style.Text_Head, "外部网络与故障定位"))
        root.addView(ui.text(R.style.Text_Small, "普通热点可作为 HTTP 路线的网络候选，但客户端隔离、路由和系统差异仍可能阻止访问；启用成功不等于车机已能访问。HTTPS 外部网络路线另需上游提供车机可达地址。两条路线都必须保证车机能保持 Wi-Fi 连接。"))
        root.addView(ui.button(R.style.Button_Text, "本地热点设置") { startActivity(Intent(this, HotspotActivity::class.java)) })
        root.addView(ui.text(R.style.Text_Head, "高级：独立地址诊断"))
        root.addView(ui.text(R.style.Text_Small, "独立诊断用于排查地址可达性，不能代替 HTTP 实际画面验收；已有会话时请先结束测试再使用，避免争用 VPN。正常首次设置请使用上面的配置入口。"))
        root.addView(ui.button(R.style.Button_Text, "打开地址测试") { startActivity(Intent(this, TeslaAddressProbeActivity::class.java)) })
        setContentView(ui.page(root))
    }

    override fun onResume() { super.onResume(); handler.post(update) }
    override fun onPause() { handler.removeCallbacks(update); super.onPause() }
}
