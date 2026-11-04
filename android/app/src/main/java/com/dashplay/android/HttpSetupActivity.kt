package com.dashplay.android

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.VpnService
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.TextView

/** The compatibility service owns networking; this screen requests the normal local VPN consent only. */
class HttpSetupActivity : Activity() {
    private var notice: String? = null
    private lateinit var status: TextView
    private val handler = Handler(Looper.getMainLooper())
    private val refresh = object : Runnable {
        override fun run() { status.text = notice ?: HttpSetupStatus.read(this@HttpSetupActivity).message; handler.postDelayed(this, 1000) }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = Ui(this)
        val root = ui.stack()
        root.addView(ui.button(R.style.Button_Text, "返回向导或设置") { finish() })
        root.addView(ui.text(R.style.Text_Title, "免 Root HTTP 兼容入口（实验性）"))
        root.addView(ui.text(R.style.Text, "HTTP 页面优先使用 WebRTC 视频传输；连接失败时自动改用 JPEG 图片传输。JPEG 的清晰度和流畅度受设备性能限制，最大边不超过 1280 像素。WebRTC 的实际效果取决于设备、浏览器和网络，需要在车机上确认。"))
        root.addView(ui.text(R.style.Text_Small, "这条路线不要求 Root、域名或证书。盒子自建热点使用 VPN 兼容地址；外部热点使用盒子的实际局域网地址。HTTP 管理页始终可用，不受此画面开关影响。"))
        root.addView(ui.text(R.style.Text_Small, getString(R.string.browser_scheme_hint)))
        status = ui.text(R.style.Text_Name, HttpSetupStatus.read(this).message)
        root.addView(status)
        root.addView(ui.button(R.style.Button_Primary, if (Settings(this).network == Network.THIS_HOTSPOT) "允许 VPN 并启用 HTTP" else "启用局域网 HTTP 入口") {
            notice = null
            val consent = if (Settings(this).network == Network.THIS_HOTSPOT) VpnService.prepare(this) else null
            if (consent != null) startActivityForResult(consent, VPN_REQUEST) else enable()
        })
        root.addView(ui.text(R.style.Text_Small, "此功能仅配置 HTTP 访问地址，不会自动启用 iPhone 网络连接。切换到此方式会停用 DashPlay 的 Root 地址与离线检查，避免同时占用同一地址；已有 HTTPS 证书保留。其他 VPN 占用时，请先自行决定使用哪一个。"))
        root.addView(ui.button(R.style.Button_Secondary, "关闭 HTTP 兼容入口") {
            notice = null
            runCatching { Settings(this).compatHttp = false; DashPlayService.restart(this) }
                .onFailure { notice = "关闭未保存，请重试。" }
        })
        root.addView(ui.text(R.style.Text_Head, "地址建立后做什么"))
        root.addView(ui.text(R.style.Text, "用车机打开上方完整的 HTTP 地址。看到 CarPlay 画面并能触控后，返回向导确认。只有接收端自己能打开、端口正在监听或出现 VPN 图标，都不能当作车机验证通过。"))
        root.addView(ui.text(R.style.Text_Small, "如果打不开，先检查车机是否加入同一网络、是否开启客户端隔离，以及当前 ROM 是否支持这条访问路线；可返回改用 HTTPS 地址方案或另一网络。不需要为了此步骤获取 ADB 或刷机。"))
        setContentView(ui.page(root))
    }
    private fun enable() {
        try {
            val settings = Settings(this)
            settings.teslaOfflineChecks = false
            settings.teslaRootAccess = false
            settings.apply(ManagementSettings(compatHttp = true, videoTransport = VideoTransport.AUTO))
            DashPlayService.restart(this)
            status.text = "已保存，正在建立兼容入口；尚未验证车机访问。"
        } catch (error: Exception) { notice = error.message ?: "未能保存，请重试。" }
    }
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == VPN_REQUEST) {
            if (resultCode == RESULT_OK && VpnService.prepare(this) == null) enable()
            else notice = "尚未允许 VPN，这条访问路线不能启用。可以返回保存草稿或改选其他路线。"
        }
    }
    override fun onResume() { super.onResume(); handler.post(refresh) }
    override fun onPause() { handler.removeCallbacks(refresh); super.onPause() }
    companion object { private const val VPN_REQUEST = 1104 }
}

/** Runtime readiness is separate from the user's explicit car-side confirmation. */
internal data class HttpSetupStatus(val ready: Boolean, val message: String, val proofKey: String?) {
    companion object {
        fun read(context: Context): HttpSetupStatus {
            val settings = Settings(context)
            val runtime = DashPlayService.addressRuntimeInstance
            val address = runtime?.state in listOf(TeslaVpnAddress.State.ADDRESS_PRESENT, TeslaVpnAddress.State.PEER_VERIFIED)
            val endpoint = DashPlayService.compatEndpoint
            val lan = LocalNetwork.choose(context)
            val reason = when {
                !settings.compatHttp -> "HTTP 兼容入口未启用。"
                lan == null -> "等待所选局域网连接。"
                settings.teslaRootAccess -> "Root 地址仍开启，请使用上方启用按钮切换为免 Root 路线。"
                settings.network == Network.THIS_HOTSPOT && VpnService.prepare(context) != null -> "等待本机 VPN 授权。"
                settings.network == Network.THIS_HOTSPOT && !address -> DashPlayService.addressState()?.second ?: "等待兼容地址建立。"
                endpoint?.available != true || endpoint.port != settings.compatHttpPort || endpoint.host != lan.address.hostAddress ->
                    DashPlayService.compatError ?: "等待 HTTP 监听就绪。"
                else -> null
            }
            if (reason != null) return HttpSetupStatus(false, reason, null)
            val url = BrowserEndpoint("http", if (settings.network == Network.THIS_HOTSPOT) TeslaVpnAddress().address else endpoint!!.host, settings.compatHttpPort,
                "车机兼容入口", true).url()
            val key = "${android.os.Process.myPid()}:${System.identityHashCode(runtime)}:${runtime?.generation}:${settings.network}:${lan?.name}:${lan?.address?.hostAddress}:$url"
            val boundary = if (settings.network == Network.THIS_HOTSPOT) "需车机实测热点到 VPN 地址的可达性。" else
                "外部热点不自动转发虚拟地址；这里使用实际 LAN 地址。特斯拉是否接受这个 HTTP 地址仍需实测，打不开时不能靠域名或证书补齐路由。"
            return HttpSetupStatus(true, "本机入口已就绪：$url\n请完整输入 http:// 开头的地址，不要省略或改为 https://。\n$boundary", key)
        }
    }
}
