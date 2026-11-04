package com.dashplay.android

import android.app.Activity
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.widget.TextView
import java.io.File

/**
 * What this device can and cannot do, on this device's own facts: Root, hotspot, USB Host, Android version.
 * The same boundaries are the N1/OpenWrt ones (docs/安卓能力与设置对照-20261031.md); a chip or an su binary alone
 * proves nothing, so Root is only reported as "granted" after the app's own rule install succeeded.
 */
class CapabilitiesActivity : Activity() {
    private lateinit var facts: TextView
    private lateinit var rootStatus: TextView

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        val ui = Ui(this)
        val settings = Settings(this)
        val root = ui.stack()
        root.addView(ui.button(R.style.Button_Text, getString(R.string.back)) { finish() })
        root.addView(ui.text(R.style.Text_Title, "能力与限制"))
        root.addView(ui.text(R.style.Text_Small,
            "安卓手机和安卓电视盒子都能装 DashPlay。这里按本机的实际情况列出能做到和做不到的事；" +
                "没有 Root 也能完成大部分工作，需要 Root 的几项都写明了原因。"))

        facts = ui.text(R.style.Text_Small, "")
        root.addView(facts)
        root.addView(ui.text(R.style.Text_Head, "特斯拉访问方式"))
        root.addView(ui.text(R.style.Text_Small, getString(R.string.browser_scheme_hint)))
        root.addView(ui.text(R.style.Text_Small,
            "特斯拉的 HTTP 与 HTTPS 访问条件不同。当前方案使用本机热点或外部网络，可选以下三种方式：\n" +
                "① HTTP 兼容方式（实验性功能）：不要求 Root、域名或证书。本机热点需授权 VPN 并验证访问地址，外部网络使用接收端的实际局域网地址。优先使用 WebRTC 视频，连接失败时改用 JPEG 图片；需在车机上确认画面和触控。\n" +
                "② HTTPS 访问方式（本机热点 + App Root）：本机在热点接口上接管 100.96.0.1 并配置域名解析和可信证书。不改热点名称密码，也不占用 VPN。\n" +
                "③ HTTPS 访问方式（无 Root + 可配置外部路由器）：让车机和本机接入同一台已配置可达地址/转发的路由器或随身 Wi-Fi，并配合域名与可信证书。"))
        root.addView(ui.button(R.style.Button_Secondary, "打开特斯拉访问设置") {
            startActivity(Intent(this, TeslaAccessActivity::class.java))
        })

        root.addView(ui.text(R.style.Text_Head, "无需 Root 的功能"))
        root.addView(ui.text(R.style.Text_Small,
            "· 有线 CarPlay：USB/OTG 接 iPhone，用本机 VPN 转发（需要允许 VPN，不依赖蓝牙）。\n" +
                "· 无线 CarPlay：用系统热点，或应用自建热点（Android 10+ 且支持 Wi-Fi Direct）；也可以只接入外部 Wi-Fi/以太网。\n" +
                "· 免 Root HTTP 兼容入口（实验性）：在无 Root 设备上供车机通过 WebRTC / JPEG 查看画面与触控（实际效果需在车机上确认）。\n" +
                "· 蓝牙邀请 iPhone、添加与管理手机、切换两部手机。\n" +
                "· 按声音设置选择音乐、导航等输出路径；提示音保留在 CarPlay。\n" +
                "· 导入自己的证书；通过 DuckDNS/deSEC 自动申请和续期证书（需配置域名、服务凭据及可用互联网）。\n" +
                "· 浏览器管理页 /manage、诊断、在线与本地更新。"))
        root.addView(ui.text(R.style.Text_Head, "需要 App Root 的功能"))
        root.addView(ui.text(R.style.Text_Small,
            "· 在本机热点下使用 HTTPS 100.96.0.1 地址或回应特斯拉离线联网检查（无 Root 只能走上方的实验性 HTTP 兼容入口，或借助外部路由器）。\n" +
                "· 替车机回答自己的域名：车机的 DNS 由系统热点服务占用 53 端口，应用无 Root 无法拦截 53 端口。" +
                "可使用已配置且车机能查询到的公共 DNS 记录，或由外部路由器提供本地解析。\n" +
                "· 将通过 USB 获得的 iPhone 网络转发给热点上的车机（需要 iptables 转发与 DNS 转换）。"))
        root.addView(ui.text(R.style.Text_Head, "Root 功能说明"))
        root.addView(ui.text(R.style.Text_Small,
            "· 为本机热点配置特斯拉可访问的地址；关闭功能后移除本应用添加的配置。\n" +
                "· 向热点设备共享本机的 IPv4 网络，包括通过 USB 使用的 iPhone 网络；仍需验证设备是否支持转发。\n" +
                "· 向热点设备提供本地域名解析；HTTPS 访问还需有效证书。\n" +
                "· 回应特斯拉联网检查，帮助车机保持无互联网的热点连接；需实车验证。\n" +
                "首次使用这些功能需要在安卓设备上允许 Root。管理网页不能代替系统授权。"))
        root.addView(ui.text(R.style.Text_Head, "不支持本机热点时"))
        root.addView(ui.text(R.style.Text_Small,
            "能否创建热点取决于硬件、系统和授权；Root 不能替代热点硬件能力。不支持创建热点时，可使用以下方式：\n" +
                "· 通过网线或 Wi-Fi 连接外部网络，并确认车机可以访问接收端；无线 iPhone 也需接入可互访网络。\n" +
                "· 有线 CarPlay 不依赖蓝牙；车机仍需通过局域网打开接收端页面。\n" +
                "· 可通过屏幕和遥控器完成首次设置。已运行且有网络时可使用 /manage 管理页；系统授权仍需本机完成。\n" +
                "· 开机启动设置会保存；断电后能否恢复热点和 CarPlay，需按实际设备验证。"))
        root.addView(ui.text(R.style.Text_Head, "域名与 DNS"))
        root.addView(ui.text(R.style.Text_Small,
            "使用域名访问时，证书和解析需分别配置：\n" +
                "· 可使用 DuckDNS 或 deSEC 配置专用域名和访问凭据；接收端联网后申请证书，并在到期前联网续期。\n" +
                "· 特斯拉需使用其浏览器信任的域名证书。其他浏览器只有实际支持时，才可安装本地 CA 或继续访问本地证书页面。\n" +
                "· 车机能查询公共 DNS 时，可使用指向可访问地址的公共记录；无法查询时，需本机热点 Root DNS 或外部网络本地 DNS。"))
        rootStatus = ui.text(R.style.Text_Name, "")
        root.addView(rootStatus)
        root.addView(ui.button(R.style.Button_Secondary, "自动申请与续期（DNS）") {
            startActivity(Intent(this, AutomaticTlsActivity::class.java))
        })
        root.addView(ui.button(R.style.Button_Text, "查看本机能力诊断") {
            facts.text = describe(settings)
        })
        facts.text = describe(settings)
        rootStatus.text = rootLine()
        setContentView(ui.page(root))
    }

    private fun rootLine(): String = when {
        TeslaRootAccess.phase == "READY" -> "Root 访问规则已生效：" + TeslaRootAccess.message
        TeslaRootAccess.phase == "OFF" -> "Root 访问未启用（多数功能不需要）。可在「特斯拉访问地址」里申请授权。"
        else -> "Root：" + TeslaRootAccess.message
    }

    private fun describe(settings: Settings): String {
        val pm = packageManager
        val capabilities = DeviceCapabilities.describe(this)
        val network = if (settings.usesLocalHotspot) DashPlayService.managedHotspot?.connection?.network else LocalNetwork.choose(this)
        val kindLabels = mapOf("HOTSPOT" to "本机热点", "WIFI" to "Wi-Fi", "ETHERNET" to "网线")
        val audioCategoryLabels = mapOf("media" to "音乐", "navigation" to "导航", "alert" to "提示音", "siri" to "Siri", "calls" to "通话")
        val audioTargetLabels = mapOf("carplay" to "留在 CarPlay", "bluetooth" to "走车机蓝牙/手机")
        return listOf(
            "Android " + Build.VERSION.RELEASE + "（API " + Build.VERSION.SDK_INT + "），" + Build.MODEL,
            "应用热点（Wi-Fi Direct）：" + capabilities.optString("managedHotspotMessage"),
            "系统热点：" + capabilities.optString("systemHotspotMessage"),
            "USB Host：" + if (pm.hasSystemFeature(android.content.pm.PackageManager.FEATURE_USB_HOST)) "支持，可接 iPhone 数据线" else "系统未声明支持；无线还需蓝牙与局域网",
            "当前网络：" + (network?.let { it.name + " " + it.address.hostAddress + "（" + (kindLabels[it.kind.name] ?: it.kind.name) + "）" } ?: "没有可用局域网地址"),
            "su 二进制：" + if (listOf("/system/bin/su", "/system/xbin/su", "/sbin/su", "/su/bin/su")
                .any { File(it).exists() }) "存在（不代表应用可获授权）" else "未发现（不代表一定没有）",
            "当前声音：" + settings.audioRoutes().entries.joinToString("，") {
                (audioCategoryLabels[it.key] ?: it.key) + "：" + (audioTargetLabels[it.value] ?: it.value)
            },
        ).joinToString("\n")
    }
}
