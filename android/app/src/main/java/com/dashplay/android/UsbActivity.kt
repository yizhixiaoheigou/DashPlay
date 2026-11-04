package com.dashplay.android

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.VpnService
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.TextView
import android.widget.CheckBox

/** Consent stays on the Android device; a browser cannot approve a system VPN dialog. */
class UsbActivity : Activity() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var status: TextView
    private lateinit var enable: View
    private lateinit var disable: View
    private lateinit var internet: CheckBox
    private lateinit var internetStatus: TextView
    private lateinit var share: CheckBox
    private lateinit var shareStatus: TextView
    private lateinit var shareRetry: View
    private var filling = false
    private var message: String? = null
    private val supported get() = packageManager.hasSystemFeature(PackageManager.FEATURE_USB_HOST)

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        val ui = Ui(this)
        val root = ui.stack()
        root.addView(ui.button(R.style.Button_Text, "返回") { finish() })
        root.addView(ui.text(R.style.Text_Title, if (Settings(this).wiredCarPlaySupported) "数据线连接" else "USB 上网（可选）"))
        root.addView(ui.text(R.style.Text, if (!Settings(this).wiredCarPlaySupported) "安卓手机仅支持无线 CarPlay。这里仅设置通过 USB 使用 iPhone 网络，默认关闭；开启后仍需单独完成无线配对。USB 角色与连接兼容性需要实测。" else "用支持数据传输的 OTG 线连接 iPhone。首次连接时，勾选「始终打开 DashPlay」并允许 USB 访问，在 iPhone 上选择信任；完成后需验证再次插线和断电启动不再弹窗。"))
        root.addView(ui.text(R.style.Text_Small, "USB 通道需要本机 VPN 授权，会替换正在使用的其他 VPN。车机仍需通过本地网络打开画面。"))
        status = ui.text(R.style.Text_Name, "").apply { accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE }
        root.addView(status)
        enable = ui.button(R.style.Button_Primary, (if (Settings(this).wiredCarPlaySupported) "启用 / 重试数据线连接" else "启用 / 重试 USB 上网")) { requestEnable() }.apply {
            id = R.id.usb_enable; isEnabled = supported
        }
        disable = ui.button(R.style.Button_Secondary, (if (Settings(this).wiredCarPlaySupported) "关闭数据线连接" else "关闭 USB 上网")) {
            Settings(this).usbEnabled = false
            UsbAttachActivity.sync(this, active = false)
            message = null
            DashPlayService.restart(this)
            refresh()
        }.apply { id = R.id.usb_disable }
        root.addView(enable); root.addView(disable)
        internet = CheckBox(this).apply {
            id = R.id.usb_internet; text = "通过数据线使用 iPhone 网络"; minHeight = ui.dp(48)
            setOnCheckedChangeListener { _, checked ->
                if (!filling) {
                    try {
                        val settings = Settings(this@UsbActivity)
                        val previous = settings.usbInternet
                        settings.requestUsbInternet(checked)
                        if (checked && settings.usbEnabled && VpnService.prepare(this@UsbActivity) == null) settings.allowUsbInternet()
                        message = null
                        if (previous != settings.usbInternet) DashPlayService.restart(this@UsbActivity)
                    } catch (error: Exception) { message = error.message ?: "使用 iPhone 网络设置未保存，请重试。" }
                    refresh()
                }
            }
        }
        root.addView(internet)
        root.addView(ui.text(R.style.Text_Small, "安卓手机默认关闭此项。选择使用 iPhone 网络并点击启用、完成授权后生效。在 iPhone 上开启个人热点后，这台安卓设备的应用经 iPhone 使用 IPv4 互联网，可能消耗 iPhone 流量。个人热点暂不可用时暂停外网，CarPlay 和局域网仍可使用；拔线或关闭此项后恢复系统原有网络。"))
        root.addView(ui.text(R.style.Text_Small, "更改 iPhone 网络使用或热点共享设置后，接收服务会重启。"))
        internetStatus = ui.text(R.style.Text_Small, "").apply { accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE }
        root.addView(internetStatus)
        share = CheckBox(this).apply {
            id = R.id.usb_hotspot_share; text = "共享给热点设备（需要 Root）"; minHeight = ui.dp(48)
            setOnCheckedChangeListener { _, checked ->
                if (!filling) {
                    try {
                        val settings = Settings(this@UsbActivity)
                        if (checked) settings.allowUsbHotspotShare() else settings.usbHotspotShare = false
                        message = null; DashPlayService.restart(this@UsbActivity)
                    } catch (error: Exception) { message = error.message ?: "共享设置未保存，请重试。" }
                    refresh()
                }
            }
        }
        root.addView(share)
        root.addView(ui.text(R.style.Text_Small, "车机需连接这台安卓设备的热点。首次请在本机打开此项并允许 Root，才能将通过 iPhone 获得的 IPv4 网络共享给热点设备；可能增加 iPhone 流量。共享期间不转发热点设备的 IPv6 互联网，热点网段与 DHCP 保持原样。系统热点需已支持转发；也可使用本应用创建的热点。"))
        shareStatus = ui.text(R.style.Text_Small, "").apply { accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE }
        root.addView(shareStatus)
        shareRetry = ui.button(R.style.Button_Secondary, "重试 / 清理热点共享") {
            message = null; DashPlayService.restart(this); refresh()
        }.apply { id = R.id.usb_share_retry }
        root.addView(shareRetry)
        if (intent.getBooleanExtra(EXTRA_SETUP, false)) root.addView(ui.button(R.style.Button_Primary, "完成，返回向导") {
            if (Settings(this).usbEnabled && VpnService.prepare(this) == null) {
                setResult(RESULT_OK); finish()
            } else { message = "请先启用并允许 VPN；也可以返回向导，选择稍后配置。"; refresh() }
        }.apply { id = R.id.usb_setup_done })
        setContentView(ui.page(root))
    }

    override fun onResume() { super.onResume(); refresh() }
    override fun onPause() { handler.removeCallbacksAndMessages(null); super.onPause() }

    @Suppress("DEPRECATION")
    private fun requestEnable() {
        if (!supported) return
        if (!Settings(this).wiredCarPlaySupported && !Settings(this).usbInternetRequested) {
            message = "请先勾选使用 iPhone 网络；安卓手机不提供有线 CarPlay。"; refresh(); return
        }
        message = null
        if (android.os.Build.VERSION.SDK_INT >= 23 &&
            checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(android.Manifest.permission.RECORD_AUDIO), 9)
            return
        }
        try {
            val consent = VpnService.prepare(this)
            if (consent == null) enableConnection() else startActivityForResult(consent, 8)
        } catch (_: Exception) { message = "系统未能打开 VPN 授权，请检查是否有始终开启的 VPN。"; refresh() }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 9) {
            try {
                val consent = VpnService.prepare(this)
                if (consent == null) enableConnection() else startActivityForResult(consent, 8)
            } catch (_: Exception) { message = "系统未能打开 VPN 授权，请检查是否有始终开启的 VPN。"; refresh() }
        }
    }

    private fun enableConnection() {
        if (VpnService.prepare(this) != null) { message = "尚未允许本地 VPN，请重试授权。"; refresh(); return }
        val settings = Settings(this)
        settings.usbEnabled = true
        if (settings.usbInternetRequested) settings.allowUsbInternet()
        UsbAttachActivity.sync(this)
        message = null
        DashPlayService.restart(this)
        refresh()
    }

    @Deprecated("Android 5 compatible activity result")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != 8) return
        if (resultCode == RESULT_OK) enableConnection()
        else { message = "未启用：你取消了 VPN 授权。可以稍后重试。"; refresh() }
    }

    private fun refresh() {
        val settings = Settings(this)
        val next = message ?: when {
            !supported -> "此设备不支持 USB Host，请使用无线连接。"
            !settings.usbEnabled -> if (settings.wiredCarPlaySupported) "数据线连接未启用" else "USB 上网未启用，CarPlay 使用无线连接"
            !settings.usesUsb -> "当前仅使用无线 CarPlay，USB 上网未启用。是否供电由设备和线缆决定。"
            VpnService.prepare(this) != null -> "VPN 授权已失效，请重新启用。"
            DashPlayService.status != Status.RUNNING -> "等待接收服务启动，请先完成身份与网络设置。"
            else -> AndroidWiredController.detail
        }
        if (status.text.toString() != next) status.text = next
        disable.visibility = if (settings.usbEnabled) View.VISIBLE else View.GONE
        filling = true
        internet.isChecked = settings.usbInternetRequested
        internet.isEnabled = supported
        share.isChecked = settings.usbHotspotShare
        share.isEnabled = settings.usbHotspotShare || settings.usbInternet && settings.usbEnabled &&
            settings.network == Network.THIS_HOTSPOT && VpnService.prepare(this) == null
        filling = false
        val network = when {
            !settings.usbInternetRequested -> "已关闭通过数据线使用 iPhone 网络，重新插线或开机后仍保持关闭。"
            !settings.usbEnabled || VpnService.prepare(this) != null || !settings.usbInternetAllowed -> "已选择通过数据线使用 iPhone 网络，等待启用数据线连接并完成授权；当前尚未连接。"
            !settings.usbInternet -> "尚未启用通过数据线使用 iPhone 网络"
            else -> UsbVpnService.internetMessage
        }
        if (internetStatus.text.toString() != network) internetStatus.text = network
        val shared = UsbShareAccess.describe()
        shareStatus.text = if (!share.isEnabled) "先启用通过数据线使用 iPhone 网络，并选择这台设备的热点。" else shared.getString("message")
        shareRetry.visibility = if (settings.usbHotspotShare || shared.getString("state") == "CLEANUP_REQUIRED") View.VISIBLE else View.GONE
        handler.removeCallbacksAndMessages(null)
        handler.postDelayed(::refresh, 1000)
    }
    companion object { const val EXTRA_SETUP = "from_setup" }
}
