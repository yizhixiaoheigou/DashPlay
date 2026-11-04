package com.dashplay.android

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.Toast

/** Settings, grouped in the order of the setup questions; each line says what it is now. */
class SettingsActivity : Activity() {
    private lateinit var ui: Ui

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = Ui(this)
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    private fun edit(step: String) = startActivity(Intent(this, SetupActivity::class.java).putExtra(SetupActivity.EXTRA_ONLY, step))

    private fun changed() {
        Toast.makeText(this, R.string.restart_note, Toast.LENGTH_SHORT).show()
        DashPlayService.restart(this)
        render()
    }

    private fun render() {
        val settings = Settings(this)
        val terms = Terms.of(settings)
        val focusedTag = currentFocus?.tag
        val root = ui.column()
        root.addView(
            ui.row().apply {
                addView(
                    ImageButton(this@SettingsActivity).apply {
                        setImageResource(R.drawable.ic_back)
                        setColorFilter(ui.color(R.color.blue_text))
                        setBackgroundResource(R.drawable.btn_text)
                        contentDescription = getString(R.string.back)
                        setOnClickListener { finish() }
                    },
                    LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)),
                )
                addView(ui.text(R.style.Text_Title, getString(R.string.settings)), ui.weight())
            },
        )

        fun group(title: Int, vararg rows: View) {
            root.addView(ui.groupTitle(getString(title)))
            val box = ui.stack(ui.dp(8))
            rows.forEach { box.addView(it, ui.matchWidth()) }
            root.addView(box, ui.matchWidth())
        }
        fun tagged(view: View, tag: String) = view.apply { this.tag = tag }

        val hotspot = if (settings.usesLocalHotspot) settings.localHotspotCredentials() else settings.hotspot()
        group(
            R.string.group_car,
            tagged(ui.settingRow(getString(R.string.set_tesla), getString(R.string.car_tesla_sub), switchOn = settings.car == Car.TESLA) {
                settings.car = if (settings.car == Car.TESLA) Car.OTHER else Car.TESLA
                changed()
            }, "tesla"),
        )
        group(
            R.string.group_network,
            tagged(ui.settingRow(getString(R.string.set_network), getString(when (settings.network) {
                Network.THIS_HOTSPOT -> R.string.network_this_short
                Network.CAR_HOTSPOT -> R.string.network_car
                Network.MIFI -> R.string.network_mifi
            })) { edit(SetupActivity.STEP_NETWORK) }, "network"),
            tagged(ui.settingRow("本地热点", if (settings.usesLocalHotspot) "由应用自动创建" else "系统热点或外部 Wi-Fi") {
                startActivity(Intent(this, HotspotActivity::class.java))
            }.apply { id = R.id.row_hotspot }, "hotspot"),
            tagged(ui.settingRow("特斯拉访问入口", when {
                settings.teslaRootAccess -> TeslaRootAccess.message
                settings.compatHttp -> "已开启免 Root HTTP 兼容入口（实验性）"
                else -> "免 Root HTTP 兼容入口、Root 热点与外部网络"
            }) {
                startActivity(Intent(this, TeslaAccessActivity::class.java))
            }.apply { id = R.id.row_tesla_access }, "tesla_access"),
            tagged(ui.settingRow("自己的网址与证书", "导入证书、切换与本地访问恢复") {
                startActivity(Intent(this, TlsActivity::class.java))
            }.apply { id = R.id.row_tls }, "tls"),
            tagged(ui.settingRow(getString(R.string.set_wifi), if (!settings.plan().wireless) "数据线连接无需填写，原参数保留" else hotspot?.ssid ?: getString(R.string.set_wifi_none)) {
                if (settings.usesLocalHotspot) startActivity(Intent(this, HotspotActivity::class.java)) else edit(SetupActivity.STEP_WIFI)
            }, "wifi"),
        )
        group(
            R.string.group_iphone,
            tagged(ui.settingRow(getString(R.string.set_iphone_link), getString(when (settings.iphoneLink) {
                IphoneLink.ANY -> R.string.iphone_any_short
                IphoneLink.WIRELESS -> R.string.iphone_wireless
                IphoneLink.WIRED -> R.string.iphone_wired
            })) { edit(SetupActivity.STEP_IPHONE) }, "link"),
            tagged(ui.settingRow(if (settings.wiredCarPlaySupported) "数据线连接" else "USB 上网（可选）", if (settings.usesUsb) AndroidWiredController.detail else if (settings.wiredCarPlaySupported) "连接 iPhone，设置 USB 与 VPN 权限" else "仅使用 iPhone 网络，不传输 CarPlay；默认关闭") {
                startActivity(Intent(this, UsbActivity::class.java))
            }.apply { id = R.id.row_usb }, "usb"),
            tagged(ui.settingRow(getString(R.string.set_iphone_added), settings.iphoneName ?: getString(R.string.set_iphone_none)) {
                startActivity(Intent(this, PhonesActivity::class.java))
            }.apply { id = R.id.row_phones }, "pair"),
        )
        group(
            R.string.group_sound,
            tagged(ui.settingRow(getString(R.string.set_voice), getString(R.string.voice_car_sub), switchOn = settings.audioOutsideCarPlay) {
                settings.audioOutsideCarPlay = !settings.audioOutsideCarPlay
                changed()
            }, "voice"),
        )
        group(
            R.string.group_other,
            tagged(ui.settingRow("版本与更新", versionName()) {
                startActivity(Intent(this, UpdateActivity::class.java))
            }.apply { id = R.id.row_update }, "update"),
            tagged(ui.settingRow(getString(R.string.manage_title), terms.manageSub) {
                startActivity(Intent(this, ManagementActivity::class.java))
            }.apply { id = R.id.row_manage }, "manage"),
            tagged(ui.settingRow("能力与限制", "Root、热点、特斯拉与域名各能做到什么") {
                startActivity(Intent(this, CapabilitiesActivity::class.java))
            }.apply { id = R.id.row_capabilities }, "capabilities"),
            tagged(ui.settingRow("开源许可", "随包第三方组件与许可证全文") {
                startActivity(Intent(this, LicensesActivity::class.java))
            }.apply { id = R.id.row_licenses }, "licenses"),
            tagged(ui.settingRow(getString(R.string.set_identity), getString(if (IdentityImport.present(this)) R.string.set_identity_ok else R.string.set_identity_missing)) {
                edit(SetupActivity.STEP_IDENTITY)
            }, "identity"),
            tagged(ui.settingRow("设备类型", if (settings.deviceKind == DeviceKind.PHONE) "安卓手机" else "安卓电视盒子") {
                edit(SetupActivity.STEP_DEVICE)
            }.apply { id = R.id.row_device }, "device"),
            tagged(ui.settingRow(getString(R.string.set_boot), terms.bootSub, switchOn = settings.startAtBoot) {
                settings.startAtBoot = !settings.startAtBoot
                render()
            }, "boot"),
            tagged(ui.settingRow(getString(R.string.set_background), getString(R.string.set_background_sub)) {
                runCatching {
                    startActivity(Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        android.net.Uri.parse("package:$packageName")))
                }.onFailure { Toast.makeText(this, R.string.background_unavailable, Toast.LENGTH_LONG).show() }
            }.apply { id = R.id.row_background }, "background"),
            tagged(ui.settingRow(getString(R.string.set_redo), getString(R.string.set_redo_sub)) {
                startActivity(Intent(this, SetupActivity::class.java))
            }.apply { id = R.id.row_redo }, "redo"),
        )
        root.addView(ui.text(R.style.Text_Tiny, getString(R.string.set_about) + " " + versionName()).apply { setPadding(ui.dp(4), ui.dp(16), 0, 0) })

        setContentView(ui.page(root))
        val again = focusedTag?.let { root.findViewWithTag<View>(it) }
        (again ?: root.findViewWithTag<View>("tesla"))?.let { if (ui.tv || again != null) it.requestFocus() }
    }

    private fun versionName(): String =
        runCatching { packageManager.getPackageInfo(packageName, 0).versionName }.getOrNull().orEmpty()
}
