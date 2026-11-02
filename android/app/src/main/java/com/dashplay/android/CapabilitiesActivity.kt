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
        root.addView(ui.text(R.style.Text_Head, "特斯拉怎么打开"))
        root.addView(ui.text(R.style.Text_Small,
            "特斯拉浏览器拒绝 192.168.x.x 这类局域网地址。三种做法，按有没有 Root 选：\n" +
                "① 有 Root：本机在热点接口上接管 100.96.0.1 并转接到网页端口，车机打开 https://100.96.0.1:8080/ 。" +
                "不改热点名称、密码，也不占用 VPN。\n" +
                "② 无 Root，用外部网络：让车机和本机接入一台能把网段设成 100.64–100.127 的路由器或随身 Wi-Fi，" +
                "车机打开这台设备的那个地址。\n" +
                "③ 车机自带热点：带宽通常很低，无线画面容易卡顿，这种组合建议用数据线。\n" +
                "VPN 不是替代方案：普通 VpnService 只改本机路由，热点客户端拿不到那个地址，特斯拉仍然只会看到 192.168.x.x。"))
        root.addView(ui.button(R.style.Button_Secondary, "打开特斯拉访问设置") {
            startActivity(Intent(this, TeslaAccessActivity::class.java))
        })

        root.addView(ui.text(R.style.Text_Head, "没有 Root 也能做"))
        root.addView(ui.text(R.style.Text_Small,
            "· 有线 CarPlay：USB/OTG 接 iPhone，用本机 VPN 转发（需要允许 VPN）。\n" +
                "· 无线 CarPlay：用系统热点，或应用自建热点（Android 10+ 且支持 Wi-Fi Direct）；也可以只接入外部 Wi-Fi/以太网。\n" +
                "· 蓝牙邀请 iPhone、添加与管理手机、切换两部手机。\n" +
                "· 声音留在 CarPlay 或全部不走 CarPlay（与 N1/OpenWrt 同一个开关）。\n" +
                "· 导入自己的证书；用 DuckDNS/deSEC 自动申请和续期证书（设备能上网即可）。\n" +
                "· 浏览器管理页 /manage、诊断、离线更新。"))
        root.addView(ui.text(R.style.Text_Head, "没有 Root 做不到"))
        root.addView(ui.text(R.style.Text_Small,
            "· 改热点网段 / 用 100.96.0.1 打开（特斯拉，见上）。\n" +
                "· 替车机回答自己的域名：车机的 DNS 由系统热点服务占用 53 端口，应用无法写入解析记录。" +
                "只能用外部路由器/AdGuard 这类设备做，或在车机上装 CA 证书后仍用 IP。\n" +
                "· 把本机网络分享给热点客户端：系统热点本身不带 NAT 转发，应用热点（Wi-Fi Direct）也不转发。" +
                "车机连上热点只能访问本机，上不了外网。\n" +
                "· 热点客户端访问 USB 借来的网络（需要 iptables 转发与 DNS 转换）。\n" +
                "· 应用热点在部分 ROM 上会被系统热点抢占，或强停后不会自动重建。"))
        root.addView(ui.text(R.style.Text_Head, "有 Root 可以多做"))
        root.addView(ui.text(R.style.Text_Small,
            "· 100.96.0.1 接管（只加本应用自己的 PREROUTING 规则，关闭时逐条撤回）。\n" +
                "· 车机上网：把热点客户端 NAT 到本机的 Wi-Fi/数据/USB 网络，并转换 DNS（IPv4；IPv6 默认拒绝防泄漏）。\n" +
                "· 把 53 端口重定向到应用，替车机回答自己的域名（需配合自己的域名与证书）。\n" +
                "· 特斯拉的离线联网检查应答，让车机在无外网时保持连接。\n" +
                "Root 授权只在设备上发起，浏览管理页不能开启。规则带进程约束和持久 journal，关闭或进程死亡后按记录精确清理。"))
        root.addView(ui.text(R.style.Text_Head, "开不了热点的设备"))
        root.addView(ui.text(R.style.Text_Small,
            "无论安卓手机还是电视盒子，只要硬件和系统支持热点或 Root，都能直接使用对应功能。若遇到不支持开热点的设备（如部分电视盒子没有 AP 或 Wi-Fi Direct 模式），也可以这样用：\n" +
                "· 用网线或外部 Wi-Fi 接入同一台路由器，车机和 iPhone 也接入它；本机只要有一个局域网地址就够。\n" +
                "· 只能用有线 CarPlay 时，照样能出画，浏览器打开本机地址即可。\n" +
                "· 无屏时用 HDMI 首次设置后，全部设置都在 /manage。\n" +
                "· 没有电池的设备：断电重启后按开机自启设置恢复，持久化设置不受影响。"))
        root.addView(ui.text(R.style.Text_Head, "自己的 DNS 与域名"))
        root.addView(ui.text(R.style.Text_Small,
            "本机不做 DNS 服务器时，域名解析交给上游（路由器/运营商）。要让车机用域名打开，需要：\n" +
                "· 在 DuckDNS 或 deSEC 建一个只给本机用的域名，填 token；本机联网时自动登记地址、申请 Let's Encrypt 证书并续期。\n" +
                "· 车机不认识这个证书时，可先导出/安装 CA，或点「继续访问」。\n" +
                "· 车机连本机热点时想直接用域名，需要 Root 替它回答（见上）；否则用 IP 打开。"))
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
        Settings(this).teslaRootAccess && TeslaRootAccess.phase != "OFF" -> "Root：已授权，规则状态 " + TeslaRootAccess.message
        TeslaRootAccess.phase == "OFF" -> "Root：未授权（多数功能不需要）。在「特斯拉访问地址」里授权。"
        else -> "Root：" + TeslaRootAccess.message
    }

    private fun describe(settings: Settings): String {
        val pm = packageManager
        val direct = Build.VERSION.SDK_INT >= 29 && pm.hasSystemFeature(android.content.pm.PackageManager.FEATURE_WIFI_DIRECT)
        val network = if (settings.usesLocalHotspot) DashPlayService.managedHotspot?.connection?.network else LocalNetwork.choose(this)
        return listOf(
            "Android " + Build.VERSION.RELEASE + "（API " + Build.VERSION.SDK_INT + "），" + Build.MODEL,
            "应用热点（Wi-Fi Direct）：" + if (direct) "支持" else "不支持",
            "系统热点：" + if (pm.hasSystemFeature(android.content.pm.PackageManager.FEATURE_WIFI)) "有 Wi-Fi，可在系统设置里开" else "无 Wi-Fi",
            "USB Host：" + if (pm.hasSystemFeature(android.content.pm.PackageManager.FEATURE_USB_HOST)) "支持，可接 iPhone 数据线" else "不支持，只能无线",
            "当前网络：" + (network?.let { it.name + " " + it.address.hostAddress + "（" + it.kind.name + "）" } ?: "没有可用局域网地址"),
            "su 二进制：" + if (listOf("/system/bin/su", "/system/xbin/su", "/sbin/su", "/su/bin/su")
                .any { File(it).exists() }) "存在（不代表已授权）" else "未发现（不代表一定没有）",
            "当前声音：" + settings.audioRoutes().entries.joinToString("，") { it.key + "=" + it.value },
        ).joinToString("\n")
    }
}