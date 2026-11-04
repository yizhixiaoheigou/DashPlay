package com.dashplay.android

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.CheckBox
import android.net.VpnService
import android.widget.EditText
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import java.io.File

/**
 * First setup, one question a page, in the box's order (docs/设置与装机交互设计-20261015.md): facts first, the
 * iPhone preference after the facts, then only the connection details it needs. Opened with [EXTRA_ONLY] it edits one answer.
 */
class SetupActivity : Activity() {
    private lateinit var ui: Ui
    private lateinit var settings: Settings
    private lateinit var steps: List<String>
    private var index = 0
    private val handler = Handler(Looper.getMainLooper())

    // The answers until the summary is confirmed (or, editing one, until Save).
    private lateinit var car: Car
    private lateinit var network: Network
    private var audioOutsideCarPlay = false
    private lateinit var link: IphoneLink
    private var ssid = ""
    private var passphrase = ""
    /** The external network the box joins; kept apart from the box's own hotspot from 2026-11-02 on. */
    private var uplinkSsid = ""
    private var uplinkPassphrase = ""
    private var externalWifiChoice: ExternalWifiChoice? = null
    private var externalOpenNetwork = false
    private var ssidFromSystem = false
    private val guideAnswers = mutableMapOf<String, String>()
    private var finishedSetup = false
    private val draftPrefs by lazy { getSharedPreferences("setup_draft", MODE_PRIVATE) }
    private fun guideProblem(step: String) = SetupGuide.problem(step, car, network, link, usbSupported,
        settings.bluetoothSupported, RootShell.available(), guideAnswers, kind)
    private fun connectionProblem(target: IphoneLink = link) = SetupGuide.connectionProblem(network, target,
        usbSupported, settings.bluetoothSupported, guideAnswers, kind)
    private fun httpState() = HttpSetupStatus.read(this)
    private fun usesHttp() = SetupGuide.usesHttp(car, guideAnswers)
    private fun httpConfigurationMatches(): Boolean {
        val wifiMatches = if (!included("wifi") || automatic) true else {
            if (network == Network.THIS_HOTSPOT) settings.hotspot()?.let {
                it.ssid == draftSsid() && it.passphrase == draftPassword()
            } == true else settings.uplink()?.let {
                it.ssid == draftSsid() && it.passphrase == draftPassword()
            } == true
        }
        return settings.network == network && settings.car == car && settings.iphoneLink == link &&
            settings.audioOutsideCarPlay == audioOutsideCarPlay && wifiMatches
    }
    private fun checkHttpConfirmation() = SetupGuide.reconcileHttpConfirmation(guideAnswers, httpState().proofKey,
        httpConfigurationMatches())
    private fun answer(step: String, value: String) {
        if (step == SetupGuide.INTERNET && usbSupported) {
            try { settings.requestUsbInternet(value == "usb", kind) }
            catch (_: IllegalStateException) { saveError = "使用 iPhone 网络选择未保存，请重试。"; render(); return }
        }
        if (guideAnswers[step] != value) SetupGuide.invalidate(step, guideAnswers)
        guideAnswers[step] = value
        if (step == SetupGuide.HTTP_CHECK && value == "ready")
            httpState().proofKey?.let { guideAnswers[SetupGuide.HTTP_PROOF] = it }
        saveError = null
        render()
    }

    private var importMessage: String? = null
    private var importError = false
    private var importing = false
    private var wifiError: String? = null
    private var saveError: String? = null
    private var ssidField: EditText? = null
    private var passField: EditText? = null

    private lateinit var kind: DeviceKind
    private val single: Boolean get() = steps.size == 1
    private val wireless: Boolean get() = Plan.of(car, network, link, audioOutsideCarPlay).wireless && settings.bluetoothSupported
    private val automatic: Boolean get() = network == Network.THIS_HOTSPOT && settings.autoHotspot
    private val usbSupported get() = packageManager.hasSystemFeature(PackageManager.FEATURE_USB_HOST)
    private fun included(step: String) = single || SetupGuide.included(step, car, network, link,
        usbSupported, settings.bluetoothSupported, guideAnswers)
    private fun usbReady() = settings.usbEnabled && VpnService.prepare(this) == null &&
        (!settings.usbInternetRequested(kind) || settings.usbInternetAllowed)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = Ui(this)
        settings = Settings(this)
        kind = settings.deviceKind
        car = settings.car
        network = settings.network
        audioOutsideCarPlay = settings.audioOutsideCarPlay
        link = settings.initialIphoneLink(usbSupported)
        settings.hotspot()?.let { ssid = it.ssid; passphrase = it.passphrase; ssidFromSystem = it.fromSystem }
        settings.uplink()?.let { uplinkSsid = it.ssid; uplinkPassphrase = it.passphrase }
        externalWifiChoice = settings.externalWifiChoice
        externalOpenNetwork = settings.securityForNetwork(Network.MIFI) == WifiSecurity.OPEN
        val only = intent.getStringExtra(EXTRA_ONLY)
        val restored = savedInstanceState ?: if (only == null && draftPrefs.contains("draft_device")) Bundle().apply {
            for ((key, value) in draftPrefs.all) when (value) {
                is String -> putString(key, value)
                is Boolean -> putBoolean(key, value)
                is Int -> putInt(key, value)
            }
        } else null
        restored?.let {
            kind = DeviceKind.valueOf(it.getString("draft_device", kind.name))
            car = Car.valueOf(it.getString("draft_car", car.name))
            network = Network.valueOf(it.getString("draft_network", network.name))
            link = IphoneLink.valueOf(it.getString("draft_link", link.name))
            audioOutsideCarPlay = it.getBoolean("draft_audio", audioOutsideCarPlay)
            ssid = it.getString("draft_ssid", ssid)
            passphrase = it.getString("draft_password", passphrase)
            uplinkSsid = it.getString("draft_uplink_ssid", uplinkSsid)
            uplinkPassphrase = it.getString("draft_uplink_password", uplinkPassphrase)
            externalOpenNetwork = it.getBoolean("draft_external_open", externalOpenNetwork)
            externalWifiChoice = it.getString("draft_wifi_choice")?.let { name -> ExternalWifiChoice.values().firstOrNull { v -> v.name == name } }
        }
        link = ConnectionSupport.effectiveLink(kind, link)
        steps = if (only != null) listOf(only) else QUESTIONS + STEP_SUMMARY
        for (step in SetupGuide.questions) restored?.getString("guide_$step")?.let { guideAnswers[step] = it }
        index = (restored?.getInt(KEY_INDEX) ?: 0).coerceIn(0, steps.lastIndex)
        while (index > 0 && !included(steps[index])) index--
        @Suppress("DEPRECATION")
        intent.getParcelableExtra<Uri>(EXTRA_APK)?.let(::importFrom)
        render()
    }

    override fun onResume() {
        super.onResume()
        if (::steps.isInitialized) {
            keepFields()
            checkHttpConfirmation()
            if (usbSupported) SetupGuide.reconcileUsbChoice(guideAnswers, settings.usbInternetRequested(kind))
            render()
        }
    }

    override fun onStop() {
        if (::steps.isInitialized && !single && !finishedSetup) persistDraft()
        super.onStop()
    }

    private fun persistDraft(): Boolean {
        val state = Bundle()
        writeDraft(state)
        val edit = draftPrefs.edit().clear()
        for (key in state.keySet()) {
            @Suppress("DEPRECATION") val value = state.get(key)
            when (value) {
                is String -> edit.putString(key, value)
                is Boolean -> edit.putBoolean(key, value)
                is Int -> edit.putInt(key, value)
            }
        }
        return edit.commit()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        writeDraft(outState)
    }

    private fun writeDraft(outState: Bundle) {
        keepFields()
        guideAnswers.forEach { (step, value) -> outState.putString("guide_$step", value) }
        outState.putInt(KEY_INDEX, index)
        outState.putString("draft_device", kind.name)
        outState.putString("draft_car", car.name)
        outState.putString("draft_network", network.name)
        outState.putString("draft_link", link.name)
        outState.putBoolean("draft_audio", audioOutsideCarPlay)
        outState.putString("draft_ssid", ssid)
        outState.putString("draft_password", passphrase)
        outState.putString("draft_uplink_ssid", uplinkSsid)
        outState.putString("draft_uplink_password", uplinkPassphrase)
        outState.putString("draft_wifi_choice", externalWifiChoice?.name)
        outState.putBoolean("draft_external_open", externalOpenNetwork)
    }

    @Deprecated("Activity API without AndroidX")
    override fun onBackPressed() {
        if (index > 0) {
            keepFields()
            do { index-- } while (index > 0 && !included(steps[index]))
            render()
        } else super.onBackPressed()
    }

    private fun render() {
        ssidField = null
        passField = null
        val step = steps[index]
        val visibleSteps = steps.filter(::included)
        val root = ui.stack()

        val header = ui.row().apply {
            addView(
                ImageButton(this@SetupActivity).apply {
                    setImageResource(R.drawable.ic_back)
                    setColorFilter(ui.color(R.color.blue_text))
                    setBackgroundResource(R.drawable.btn_text)
                    contentDescription = getString(R.string.back)
                    setOnClickListener { onBackPressed() }
                },
                LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)),
            )
            val questions = visibleSteps.count { it != STEP_SUMMARY }
            if (!single && step != STEP_SUMMARY) {
                addView(ui.text(R.style.Text_Small, getString(R.string.step_count, visibleSteps.indexOf(step) + 1, questions)), ui.weight())
            }
        }
        root.addView(header, ui.matchWidth())
        if (!single && step != STEP_SUMMARY) root.addView(progress(visibleSteps.count { it != STEP_SUMMARY }, visibleSteps.indexOf(step)), ui.matchWidth())

        val (title, why) = when (step) {
            STEP_DEVICE -> R.string.q_device to R.string.q_device_why
            STEP_IDENTITY -> R.string.q_identity to R.string.q_identity_why
            STEP_CAR -> R.string.q_car to R.string.q_car_why
            STEP_NETWORK -> R.string.q_network to R.string.q_network_why
            STEP_WIFI -> if (!wireless) R.string.q_wifi_wired to R.string.q_wifi_wired_why else
                R.string.q_wifi to if (network == Network.THIS_HOTSPOT) R.string.q_wifi_why_hotspot else R.string.q_wifi_why_other
            STEP_VOICE -> R.string.q_voice to R.string.q_voice_why
            STEP_IPHONE -> R.string.q_iphone to R.string.q_iphone_why
            STEP_USB -> R.string.q_usb to R.string.q_usb_why
            else -> R.string.q_summary to R.string.q_summary_why
        }
        val guideText = when {
            step == STEP_IPHONE && kind == DeviceKind.PHONE -> "连接 iPhone" to "安卓手机仅支持无线 CarPlay。请准备蓝牙和可互访的 Wi-Fi，再添加 iPhone。"
            step == STEP_USB && kind == DeviceKind.PHONE -> "授权 USB 上网" to "此项仅用于使用 iPhone 网络，不承载 CarPlay。请在本机确认 USB 和 VPN 授权。"
            else -> guideText(step)
        }
        root.addView(ui.text(R.style.Text_Title, guideText?.first ?: getString(title)))
        root.addView(ui.text(R.style.Text_Small, guideText?.second ?: getString(why)))
        if (step in listOf(SetupGuide.ACCESS, SetupGuide.TLS, SetupGuide.DNS, SetupGuide.HTTP_CHECK)) {
            root.addView(ui.text(R.style.Text_Small, getString(R.string.browser_scheme_hint)))
        }
        saveError?.let { root.addView(ui.text(R.style.Text_Small, it).apply { setTextColor(ui.color(R.color.red)) }) }

        when (step) {
            STEP_DEVICE -> {
                root.addView(ui.choice(getString(R.string.device_phone), getString(R.string.device_phone_sub), kind == DeviceKind.PHONE) { kind = DeviceKind.PHONE; link = IphoneLink.WIRELESS; SetupGuide.invalidate(STEP_IPHONE, guideAnswers); render() }, ui.matchWidth())
                root.addView(ui.choice(getString(R.string.device_box), getString(R.string.device_box_sub), kind == DeviceKind.BOX) { kind = DeviceKind.BOX; render() }, ui.matchWidth())
            }
            STEP_IDENTITY -> identityBody(root)
            STEP_CAR -> {
                root.addView(ui.choice(getString(R.string.car_tesla), getString(R.string.car_tesla_sub), car == Car.TESLA) { if (car != Car.TESLA) SetupGuide.invalidate(STEP_CAR, guideAnswers); car = Car.TESLA; render() }, ui.matchWidth())
                root.addView(ui.choice(getString(R.string.car_other), getString(R.string.car_other_sub), car == Car.OTHER) { if (car != Car.OTHER) SetupGuide.invalidate(STEP_CAR, guideAnswers); car = Car.OTHER; render() }, ui.matchWidth())
            }
            STEP_NETWORK -> {
                root.addView(ui.choice(getString(R.string.network_this), getString(R.string.network_this_sub), network == Network.THIS_HOTSPOT) { if (network != Network.THIS_HOTSPOT) SetupGuide.invalidate(STEP_NETWORK, guideAnswers); network = Network.THIS_HOTSPOT; render() }, ui.matchWidth())
                val carHotspotSub = if (kind == DeviceKind.PHONE) ConnectionSupport.PHONE_NETWORK else if (car == Car.TESLA) "特斯拉车机不提供热点功能，请选择本机热点或外部网络。" else getString(R.string.network_car_sub)
                root.addView(ui.choice(getString(R.string.network_car), carHotspotSub, network == Network.CAR_HOTSPOT, enabled = car != Car.TESLA && kind == DeviceKind.BOX) { if (network != Network.CAR_HOTSPOT) SetupGuide.invalidate(STEP_NETWORK, guideAnswers); network = Network.CAR_HOTSPOT; render() }, ui.matchWidth())
                root.addView(ui.choice(getString(R.string.network_mifi), getString(R.string.network_mifi_sub), network == Network.MIFI) { if (network != Network.MIFI) SetupGuide.invalidate(STEP_NETWORK, guideAnswers); network = Network.MIFI; render() }, ui.matchWidth())
                // P2P eligibility does not establish whether the ROM has a working system SoftAP.
                if (network == Network.THIS_HOTSPOT) {
                    val capabilities = DeviceCapabilities.describe(this)
                    root.addView(ui.text(R.style.Text_Small, "应用热点：" + capabilities.optString("managedHotspotMessage") +
                        "\n系统热点：" + capabilities.optString("systemHotspotMessage")), ui.matchWidth())
                }
            }
            STEP_WIFI -> wifiBody(root)
            STEP_VOICE -> {
                if (car == Car.TESLA) root.addView(ui.text(R.style.Text_Small,
                    "特斯拉模式下，音乐、通话与 Siri 默认通过车机蓝牙播放。导航播报的实际输出位置和行驶中的播放限制仍需验证，请按实车结果确认。"))
                root.addView(ui.choice(getString(R.string.voice_car), getString(R.string.voice_car_sub), audioOutsideCarPlay) { audioOutsideCarPlay = true; render() }, ui.matchWidth())
                root.addView(ui.choice(getString(R.string.voice_page), getString(R.string.voice_page_sub), !audioOutsideCarPlay) { audioOutsideCarPlay = false; render() }, ui.matchWidth())
            }
            STEP_IPHONE -> {
                if (kind == DeviceKind.PHONE) root.addView(ui.text(R.style.Text_Small, ConnectionSupport.PHONE_WIRELESS_ONLY))
                if (kind == DeviceKind.BOX && !settings.bluetoothSupported && usbSupported) {
                    root.addView(ui.text(R.style.Text_Small, "未检测到蓝牙，不影响数据线 CarPlay；自动选择将仅使用数据线。"), ui.matchWidth())
                }
                connectionProblem()?.let { reason ->
                    root.addView(ui.text(R.style.Text_Small, reason).apply { setTextColor(ui.color(R.color.red)) }, ui.matchWidth())
                }
                if (kind == DeviceKind.BOX) root.addView(ui.choice(getString(R.string.iphone_any), (connectionProblem(IphoneLink.ANY) ?: getString(R.string.iphone_any_sub)), link == IphoneLink.ANY, enabled = connectionProblem(IphoneLink.ANY) == null) { if (link != IphoneLink.ANY) SetupGuide.invalidate(STEP_IPHONE, guideAnswers); link = IphoneLink.ANY; render() }, ui.matchWidth())
                val wirelessSub = connectionProblem(IphoneLink.WIRELESS)
                    ?: getString(R.string.iphone_wireless_sub)
                root.addView(ui.choice(getString(R.string.iphone_wireless), wirelessSub, link == IphoneLink.WIRELESS, enabled = connectionProblem(IphoneLink.WIRELESS) == null) { if (link != IphoneLink.WIRELESS) SetupGuide.invalidate(STEP_IPHONE, guideAnswers); link = IphoneLink.WIRELESS; render() }, ui.matchWidth())
                if (kind == DeviceKind.BOX) root.addView(ui.choice(getString(R.string.iphone_wired), if (usbSupported) getString(R.string.iphone_wired_sub) else "未检测到 USB Host，当前不能使用数据线连接。", link == IphoneLink.WIRED, enabled = usbSupported) { if (link != IphoneLink.WIRED) SetupGuide.invalidate(STEP_IPHONE, guideAnswers); link = IphoneLink.WIRED; render() }, ui.matchWidth())
            }
            STEP_USB -> {
                root.addView(CheckBox(this).apply {
                    id = R.id.usb_internet
                    text = "通过 USB 使用 iPhone 网络"
                    isChecked = settings.usbInternetRequested(kind)
                    minHeight = ui.dp(48)
                    setOnCheckedChangeListener { _, checked ->
                        settings.requestUsbInternet(checked, kind)
                        if (checked || guideAnswers[SetupGuide.INTERNET] == "usb") {
                            SetupGuide.invalidate(SetupGuide.INTERNET, guideAnswers)
                            guideAnswers[SetupGuide.INTERNET] = if (checked) "usb" else "offline"
                        }
                    }
                })
                root.addView(ui.text(R.style.Text_Small, "开启后可能使用 iPhone 流量；iPhone 需开启个人热点。这个选择会保存，仍需在本机完成 VPN 和 USB 授权。关闭后不使用 iPhone 网络；安卓手机的 CarPlay 始终使用无线。"))
                root.addView(ui.text(R.style.Text_Name, if (usbReady()) "本机 VPN 已授权；插上 iPhone 后还需确认 USB 与信任提示。"
                    else "等待本机授权，当前尚未就绪。"))
                root.addView(ui.button(R.style.Button_Text, "稍后配置") {
                    do { index++ } while (index < steps.lastIndex && !included(steps[index]))
                    render()
                }, ui.matchWidth())
            }
            STEP_SUMMARY -> summaryBody(root)
            else -> guideBody(root, step)
        }
        guideProblem(step)?.let { reason ->
            root.addView(ui.text(R.style.Text_Small, reason).apply { setTextColor(ui.color(R.color.orange)) }, ui.matchWidth())
        }

        // Answers numbered top to bottom, for tests that cannot read the words.
        val choiceIds = listOf(R.id.choice_1, R.id.choice_2, R.id.choice_3)
        (0 until root.childCount).map(root::getChildAt).filter { it.isFocusable && it is LinearLayout }
            .forEachIndexed { i, view -> if (i < choiceIds.size) view.id = choiceIds[i] }
        val primary = ui.button(R.style.Button_Primary, primaryLabel(step)) { next() }.apply { id = R.id.primary }
        primary.isEnabled = canGoOn(step)
        root.addView(primary, ui.matchWidth())
        if (!single && !canGoOn(step) && guideAnswers[step] != null) {
            if (car == Car.TESLA && step in listOf(SetupGuide.ACCESS, SetupGuide.TLS, SetupGuide.DNS) && guideAnswers[SetupGuide.ACCESS] != "http") {
                root.addView(ui.button(R.style.Button_Secondary, "改用免 Root HTTP 兼容路线（实验性）") {
                    index = steps.indexOf(SetupGuide.ACCESS)
                    answer(SetupGuide.ACCESS, "http")
                }, ui.matchWidth())
            }
            if (index > 0) {
                root.addView(ui.button(R.style.Button_Text, "返回上一题修改") { onBackPressed() }, ui.matchWidth())
            }
        }
        if (step == STEP_SUMMARY) root.addView(ui.button(R.style.Button_Text, getString(R.string.summary_edit)) { index = 0; render() }.apply { id = R.id.secondary }, ui.matchWidth())

        if (!single) root.addView(ui.button(R.style.Button_Text, "保存草稿，稍后继续") {
            if (persistDraft()) {
                finishedSetup = true
                Toast.makeText(this, "草稿已保存，尚未完成首次设置。", Toast.LENGTH_LONG).show()
                finish()
            } else { saveError = "草稿未保存，请重试。"; render() }
        }, ui.matchWidth())
        setContentView(ui.page(root))
        // A remote starts on the current answer, or on the button when there is nothing to choose.
        (root.findFocus() ?: firstSelected(root) ?: primary).requestFocus()
    }

    private fun progress(count: Int, at: Int): LinearLayout = ui.row().apply {
        for (i in 0 until count) {
            addView(
                View(this@SetupActivity).apply { setBackgroundResource(if (i <= at) R.drawable.progress_on else R.drawable.progress_off) },
                LinearLayout.LayoutParams(0, ui.dp(4), 1f).apply { if (i > 0) marginStart = ui.dp(6) },
            )
        }
    }

    private fun firstSelected(root: LinearLayout): View? =
        (0 until root.childCount).map(root::getChildAt).firstOrNull { it.isSelected && it.isFocusable }

    private fun primaryLabel(step: String): String = when {
        step == STEP_USB && !usbReady() -> "授权并继续"
        step == STEP_SUMMARY && pendingAction() != null -> pendingAction()!!.first
        step == STEP_SUMMARY -> getString(if (wantsPairing()) R.string.summary_confirm_pair else R.string.summary_confirm)
        single && step == STEP_IDENTITY -> getString(R.string.done)
        single -> getString(R.string.save)
        else -> getString(R.string.next)
    }

    private fun canGoOn(step: String): Boolean = when (step) {
        STEP_IDENTITY -> IdentityImport.present(this) && !importing
        else -> guideProblem(step) == null
    }

    private fun next() {
        val step = steps[index]
        saveError = null
        guideProblem(step)?.let { saveError = it; render(); return }
        if (step == STEP_IPHONE) {
            connectionProblem()?.let {
                saveError = it; render(); return
            }
        }
        if (step == STEP_USB && !usbReady()) {
            settings.deviceKind = kind
            settings.requestUsbInternet(guideAnswers[SetupGuide.INTERNET] == "usb", kind)
            startActivityForResult(Intent(this, UsbActivity::class.java).putExtra(UsbActivity.EXTRA_SETUP, true), REQUEST_USB)
            return
        }
        if (step == STEP_WIFI && wireless && !automatic) {
            keepFields()
            if (network == Network.MIFI && (externalWifiChoice == null ||
                    externalWifiChoice == ExternalWifiChoice.CURRENT && CurrentWifi.ssid(this) != uplinkSsid)) {
                wifiError = "请重新选择当前 Wi-Fi，或选择其他 Wi-Fi 后填写网络名称和密码。"
                render(); return
            }
            wifiError = runCatching { SettingValidation.wifi(draftSsid(), draftPassword(), draftSecurity()) }.exceptionOrNull()?.message
            if (wifiError != null) { render(); return }
        }
        if (single) {
            try { save(step) } catch (failure: IllegalArgumentException) {
                saveError = failure.message; render(); return
            } catch (_: IllegalStateException) {
                saveError = "设置未保存，请重试。"; render(); return
            }
            DashPlayService.restart(this)
            Toast.makeText(this, getString(R.string.saved_toast), Toast.LENGTH_SHORT).show()
            startActivity(Intent(this, SettingsActivity::class.java))
            finish()
            return
        }
        if (step == STEP_SUMMARY) {
            checkHttpConfirmation()
            // Recheck all visible branches after back-navigation, activity recreation or a capability change.
            if (!single) steps.filter(::included).firstOrNull { guideProblem(it) != null }?.let {
                index = steps.indexOf(it); saveError = guideProblem(it); render(); return
            }
            if (!IdentityImport.present(this)) { index = steps.indexOf(STEP_IDENTITY); render(); return }
            val action = pendingAction()
            try {
                settings.apply(summaryUpdate().copy(setupDone = action == null))
            } catch (failure: IllegalArgumentException) {
                saveError = failure.message; render(); return
            } catch (_: IllegalStateException) {
                saveError = "设置未保存，请重试。"; render(); return
            }
            if (!usesHttp() || !httpState().ready) DashPlayService.restart(this)
            if (action != null) {
                persistDraft()
                startActivity(Intent(this, action.second).apply {
                    if (action.second == UsbActivity::class.java) putExtra(UsbActivity.EXTRA_SETUP, true)
                })
                return
            }
            finishedSetup = true
            draftPrefs.edit().clear().apply()
            if (wantsPairing()) startActivity(Intent(this, PairActivity::class.java))
            finish()
            return
        }
        do { index++ } while (index < steps.lastIndex && !included(steps[index]))
        render()
    }

    private fun summaryUpdate() = ManagementSettings(deviceKind = kind, car = car, network = network, iphoneLink = link,
        audioOutsideCarPlay = audioOutsideCarPlay, setupDone = true,
        compatHttp = usesHttp(), videoTransport = if (usesHttp()) VideoTransport.AUTO else null,
        startAtBoot = when (guideAnswers[SetupGuide.STARTUP]) { "automatic" -> true; "manual" -> false; else -> null },
        ssid = if (wireless && !automatic && network == Network.THIS_HOTSPOT) ssid else null,
        passphrase = if (wireless && !automatic && network == Network.THIS_HOTSPOT) passphrase else null,
        externalWifiChoice = if (wireless && network == Network.MIFI) externalWifiChoice else null,
        uplinkSsid = if (wireless && !automatic && network != Network.THIS_HOTSPOT) uplinkSsid else null,
        uplinkPassphrase = if (wireless && !automatic && network != Network.THIS_HOTSPOT) uplinkPassphrase else null,
        wifiSecurity = if (wireless && !automatic) draftSecurity() else null,
        ownNameOnHotspot = if (included(SetupGuide.DNS)) guideAnswers[SetupGuide.DNS] == "hotspot" else null)

    private fun save(step: String) {
        when (step) {
            STEP_DEVICE -> settings.deviceKind = kind
            STEP_CAR -> settings.car = car
            STEP_NETWORK -> settings.apply(ManagementSettings(network = network))
            STEP_WIFI -> if (wireless && !automatic) {
                if (network == Network.THIS_HOTSPOT) {
                    settings.apply(ManagementSettings(ssid = ssid, passphrase = passphrase, wifiSecurity = draftSecurity()))
                } else {
                    settings.apply(ManagementSettings(uplinkSsid = uplinkSsid, uplinkPassphrase = uplinkPassphrase,
                        externalWifiChoice = externalWifiChoice,
                        wifiSecurity = draftSecurity()))
                }
            }
            STEP_VOICE -> settings.audioOutsideCarPlay = audioOutsideCarPlay
            STEP_IPHONE -> settings.apply(ManagementSettings(iphoneLink = link))
        }
    }

    private fun wantsPairing(): Boolean =
        wireless && settings.iphoneAddress == null &&
            AndroidBluetoothLink(this, null).pairedIphone() == null

    private fun guideText(step: String): Pair<String, String>? = when (step) {
        SetupGuide.SOURCE -> "谁提供这个外部 Wi-Fi？" to if (kind == DeviceKind.PHONE) "安卓手机使用无线 CarPlay，请使用本机热点或其他设备提供的 Wi-Fi；当前不支持由运行 CarPlay 的同一部 iPhone 提供 Wi-Fi。" else "如果是同一部 iPhone 一边开热点一边连 CarPlay，安卓盒子目前只能用数据线连接。"
        SetupGuide.LAN -> "车机和这台设备的网络连通了吗？" to "即使用数据线连接 iPhone，车机浏览器仍需能通过局域网访问这台设备。可以使用本机热点，也可以通过 Wi-Fi 或网线连接可互访的外部网络。"
        SetupGuide.ACCESS -> "准备使用哪种特斯拉访问方式？" to "HTTP 兼容方式不需要 Root、域名或证书。HTTPS 方式需要车机可访问的地址、域名解析和可信证书。两种方式都需要在车机上确认能否正常使用。"
        SetupGuide.INTERNET -> "这台设备本身怎样上网？" to "这里选的是这台安卓设备自己的上网方式；不插手机卡也能用，即使完全不上网也能在局域网里投屏 CarPlay。"
        SetupGuide.KEEP -> "特斯拉怎样保持连接这个 Wi-Fi？" to "特斯拉连上完全没网的 Wi-Fi 可能会自动断开。有外网可正常保持；没外网时需要启用离线保持方案，并在车机 Wi-Fi 设置里勾选“行驶中保持连接”。"
        SetupGuide.TLS -> "车机浏览器用哪种 HTTPS 证书？" to "特斯拉必须使用受信任的域名证书；其他车机如果浏览器允许点“继续前往”，可以直接用设备自带的本地证书。"
        SetupGuide.HTTP_CHECK -> "车机已打开 HTTP 页面并显示画面吗？" to "先启用页面，再在车机上检查画面和触控。页面优先使用 WebRTC，连接失败时改用 JPEG 图片；实际效果需要在当前设备和网络中确认。"
        SetupGuide.STARTUP -> "每次通电后，希望怎样启动？" to "装在车上长供电或随车通电时可选自动恢复；偶尔拿出来用可选手动开启。"
        SetupGuide.DNS -> "车机通过哪种方式解析域名？" to "域名解析结果必须指向车机能访问的接收端地址。车机能查询公共 DNS 时可使用已配置的公共记录；无法查询时需由本机热点或外部路由器提供本地解析。"
        else -> null
    }

    private fun guideChoices(step: String): List<Triple<String, String, String>> = when (step) {
        SetupGuide.SOURCE -> listOf(
            Triple("other", "路由器、随身 Wi-Fi 或另一台手机", "这台设备、车机和 iPhone 使用可互访的网络；路由器不能开启客户端隔离。安卓手机仅提供无线 CarPlay。"),
            Triple("same_iphone", "就是运行 CarPlay 的这部 iPhone", "用这部 iPhone 开个人热点给车机和盒子连；当前仅安卓盒子支持此组合，需用数据线连接 CarPlay；安卓手机请选择其他网络来源。"))
        SetupGuide.LAN -> listOf(
            Triple("ready", "已接入可以互访的网络", "这台设备的热点已开，或已和车机连上同一个路由器/随身 Wi-Fi。"),
            Triple("missing", "还没准备好网络", "先去开启本机热点或连接外部 Wi-Fi，也可以返回上一题换一种连法。"))
        SetupGuide.ACCESS -> listOf(Triple("http", "HTTP 兼容方式（无需 Root，实验性功能）", "无需域名或证书。优先使用 WebRTC 视频，连接失败时改用 JPEG 图片；需在车机上确认画面和触控。")) + if (network == Network.THIS_HOTSPOT) listOf(
            Triple("root", "HTTPS：本机热点与 App Root", "需要这台设备已具备 Root 权限，并允许 DashPlay 使用；还需域名解析和可信证书。仅电脑能使用 ADB root 不满足此条件。"),
            Triple("none", "没有 Root，或还不确定", "无法使用本机热点 HTTPS 模式；可改选上方的「免 Root HTTP 兼容入口（实验性）」，或返回改用外部路由器。"))
        else listOf(
            Triple("upstream", "HTTPS：外部网络已配置可访问的地址", "外部路由器需提供车机可以访问的接收端地址或转发规则；还需域名解析和可信证书。"),
            Triple("none", "路由器改不了，或还没准备好", "无法使用外部网络 HTTPS 模式；可改选上方的「免 Root HTTP 兼容入口（实验性）」，或返回更换方案。"))
        SetupGuide.INTERNET -> listOf(
            Triple("upstream", "用这台设备现有的网络（Wi-Fi、网线或手机卡）", "直接使用系统当前的网络连接。注意：同时连外部 Wi-Fi 又开本机热点，要看设备硬件是否支持。"),
            Triple("usb", "通过 USB 数据线使用 iPhone 网络", "用数据线连接已开启个人热点的 iPhone，使用其网络上网（需本机支持 USB Host 并允许 VPN）。"),
            Triple("offline", "暂时不上网（只用局域网传画面）", "CarPlay 画面和手机自身流量不受影响；仅这台设备在线更新或自动申请证书时才需要联网。"))
        SetupGuide.KEEP -> listOf(
            Triple("online", "车机连上后能正常上外网", "已确认车机连接此 Wi-Fi 后能访问互联网；还需开启行驶中保持 Wi-Fi 连接。"),
            if (network == Network.THIS_HOTSPOT && !usesHttp())
                Triple("offline", "用本机 Root 替车机回答联网检查", "用于帮助车机保持无互联网的热点连接，仍需实车确认；此功能不提供互联网。")
            else Triple("verified", "已确认车机能连住这个无外网 Wi-Fi", "已在车机上实测不会自动断开，或由外部路由器回应了联网检查。"),
            Triple("none", "目前做不到（车机会自动断开）", "请先准备能上网的网络，或改用支持保持连接的方案。"))
        SetupGuide.HTTP_CHECK -> listOf(
            Triple("ready", "已在车机打开上述 HTTP 地址，画面与触控正常", "确认画面持续显示，点击和滑动正常；更换网络或访问地址后需要重新确认。"),
            Triple("missing", "还没测试，或车机打不开", "可先点上方按钮配置入口再去车机试，或保存草稿、返回改用其他网络。"))
        SetupGuide.STARTUP -> listOf(
            Triple("automatic", "通电后自动启动（推荐放车上用）", "打开开机自启开关；放车上断电重启后能否自动开热点并恢复，建议上车前断电试一次。"),
            Triple("manual", "每次手动打开 App", "不开机自启；每次要用时自己打开热点和 DashPlay。"))
        SetupGuide.TLS -> listOf(
            Triple("domain", "使用自己的域名与正式证书", "支持用 DuckDNS / deSEC 自动免费申请，或手动导入已有证书（特斯拉必选）。"),
            Triple("local", "直接用设备自带的本地证书", "仅限非特斯拉车机，且车机浏览器弹出证书提醒时允许点“高级 → 继续访问”。"),
            Triple("none", "还未准备域名和证书", if (car == Car.TESLA) "可保存草稿后准备证书，或返回选择 HTTP 兼容方式。" else "可保存草稿后准备证书；如果浏览器允许使用本地证书，也可选择上一项。"))
        SetupGuide.DNS -> listOf(
            Triple("public", "车机可以查询公共 DNS，域名记录已配置", "需确认域名指向车机可访问的接收端地址，且当前网络允许查询该记录。仅能上网不代表解析已正确配置。"),
            if (network == Network.THIS_HOTSPOT)
                Triple("hotspot", "车机不上网，由本机热点（Root）回答域名", "需要 App Root 权限配置热点 DNS，使车机在无互联网时仍能解析域名。")
            else Triple("upstream", "外部路由器已配好本地域名解析", "在随身 Wi-Fi 或路由器后台把域名指向这台设备的局域网地址，无需安卓 Root。"),
            Triple("none", "还没配好解析", "没外网又没本地 DNS 时域名打不开；请先配置解析或返回改选有网方案。"))
        else -> emptyList()
    }

    private fun answerLabel(step: String) = guideChoices(step).firstOrNull { it.first == guideAnswers[step] }?.second ?: "未选择"

    private fun guideBody(root: LinearLayout, step: String) {
        for ((value, label, detail) in guideChoices(step)) {
            val enabled = !(step == SetupGuide.SOURCE && value == "same_iphone" && kind == DeviceKind.PHONE) &&
                !(step == SetupGuide.TLS && value == "local" && car == Car.TESLA) &&
                !(step == SetupGuide.INTERNET && value == "usb" && !usbSupported) &&
                !(step == SetupGuide.HTTP_CHECK && value == "ready" && (!httpState().ready || !httpConfigurationMatches()))
            root.addView(ui.choice(label, detail, guideAnswers[step] == value, enabled = enabled) {
                answer(step, value)
            }, ui.matchWidth())
        }
        if (step == SetupGuide.HTTP_CHECK) {
            root.addView(ui.text(R.style.Text_Small, httpState().message))
            root.addView(ui.button(R.style.Button_Secondary, "配置 HTTP 兼容入口并授权 VPN") {
                try {
                    settings.apply(summaryUpdate().copy(setupDone = false, compatHttp = false, startAtBoot = null))
                    persistDraft()
                    startActivity(Intent(this, HttpSetupActivity::class.java))
                } catch (error: Exception) { saveError = error.message ?: "配置未保存，请重试。"; render() }
            })
            if (wireless) root.addView(ui.button(R.style.Button_Secondary, "添加或连接 iPhone，完成画面验证") {
                if (httpState().ready && httpConfigurationMatches()) startActivity(Intent(this, PairActivity::class.java))
                else { saveError = "请先应用本次选择并配置 HTTP 入口。"; render() }
            })
            root.addView(ui.button(R.style.Button_Text, "重新读取入口状态") { checkHttpConfirmation(); render() })
        }
        if (step == SetupGuide.LAN) {
            if (network == Network.THIS_HOTSPOT) {
                val capabilities = DeviceCapabilities.describe(this)
                root.addView(ui.text(R.style.Text_Small, "应用热点：${capabilities.optString("managedHotspotMessage")}\n系统热点：${capabilities.optString("systemHotspotMessage")}"))
                root.addView(ui.button(R.style.Button_Secondary, "配置本机热点") {
                    startActivity(Intent(this, HotspotActivity::class.java))
                }, ui.matchWidth())
            } else root.addView(ui.button(R.style.Button_Secondary, "打开系统 Wi-Fi 设置") {
                runCatching { startActivity(Intent(android.provider.Settings.ACTION_WIFI_SETTINGS)) }
                    .onFailure { saveError = "本机没有可打开的 Wi-Fi 设置，请使用设备已有的网络配置入口。"; render() }
            }, ui.matchWidth())
        }
        if (step == SetupGuide.TLS && guideAnswers[step] == "domain") {
            root.addView(ui.text(R.style.Text_Small, OwnTls.store(this).state().message))
            root.addView(ui.text(R.style.Text_Small, "到总结页后，先应用本次网络方案，再进入域名与证书配置，避免登记到旧网络地址。"))
        }
        if (step == SetupGuide.ACCESS || step == SetupGuide.DNS) {
            root.addView(ui.text(R.style.Text_Small, "100.96.0.1 是本项目本机热点访问方案使用的地址，不是公网地址；外部网络应使用实际接收端地址或已配置的转发地址。"))
        }
    }

    /** Continue through the existing feature pages; setup stays incomplete until local prerequisites are ready. */
    private fun pendingAction(): Pair<String, Class<out Activity>>? {
        val tls = OwnTls.store(this).state()
        val ready = SetupGuide.Readiness(usbReady(), settings.teslaRootAccess, TeslaRootAccess.active,
            settings.teslaOfflineChecks, settings.ownNameOnHotspot && TeslaRootAccess.ownDnsActive, tls.usable, tls.enabled, httpState().ready && httpConfigurationMatches())
        return when (SetupGuide.pending(car, network, included(STEP_USB), guideAnswers, ready)) {
            SetupGuide.Pending.HTTP -> "配置 HTTP 兼容入口" to HttpSetupActivity::class.java
            SetupGuide.Pending.USB -> (if (kind == DeviceKind.PHONE) "完成 USB 上网授权" else "完成数据线授权") to UsbActivity::class.java
            SetupGuide.Pending.TESLA_ACCESS -> "配置特斯拉访问地址" to TeslaAccessActivity::class.java
            SetupGuide.Pending.OFFLINE_CHECKS ->
                (if (guideAnswers[SetupGuide.KEEP] == "offline") "启用离线热点联网检查" else "关闭离线检查，使用真实上游") to TeslaAccessActivity::class.java
            SetupGuide.Pending.TLS -> "配置域名与证书" to TlsActivity::class.java
            SetupGuide.Pending.LOCAL_TLS -> "停用自有证书，使用本地证书" to TlsActivity::class.java
            SetupGuide.Pending.DNS -> "配置热点域名解析" to TlsActivity::class.java
            null -> null
        }
    }

    // ---- identity ----

    private fun identityBody(root: LinearLayout) {
        if (IdentityImport.present(this) && importMessage == null) {
            root.addView(ui.list(listOf(ui.statusRow(getString(R.string.set_identity), getString(R.string.q_identity_done), Light.OK))), ui.matchWidth())
            return
        }
        importMessage?.let { message ->
            root.addView(ui.text(R.style.Text_Name, message).apply { setTextColor(ui.color(if (importError) R.color.red else R.color.green)) })
        }
        if (IdentityImport.present(this)) return
        if (Intents.canPickFile(this)) {
            root.addView(ui.button(R.style.Button_Secondary, getString(R.string.q_identity_pick)) { pickApk() }, ui.matchWidth())
        }
        val found = candidates()
        if (found.isNotEmpty()) {
            root.addView(ui.text(R.style.Text_Small, getString(R.string.q_identity_found)))
            found.forEach { file ->
                root.addView(ui.choice(file.name, file.parent.orEmpty(), false) { importFrom(Uri.fromFile(file)) }, ui.matchWidth())
            }
        } else if (ui.tv || !Intents.canPickFile(this)) {
            root.addView(ui.text(R.style.Text_Small, getString(R.string.q_identity_none_tv)))
        }
    }

    private fun candidates(): List<File> {
        if (Build.VERSION.SDK_INT in 23..32 && checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            if (ui.tv || !Intents.canPickFile(this)) requestPermissions(arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE), REQUEST_STORAGE)
            return emptyList()
        }
        return IdentityImport.candidates()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_WIFI_NAME) {
            val current = CurrentWifi.ssid(this)
            if (current != null) chooseCurrentWifi(current)
            else { wifiError = "仍无法读取 Wi-Fi 名称。请确认系统定位和 Wi-Fi 已开启，或选择其他 Wi-Fi 手动填写。"; render() }
        }
        if (requestCode == REQUEST_STORAGE && grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) render()
    }

    private fun pickApk() {
        val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
            type = "*/*"
            addCategory(Intent.CATEGORY_OPENABLE)
        }
        startActivityForResult(Intent.createChooser(intent, getString(R.string.q_identity_pick)), REQUEST_APK)
    }

    @Deprecated("Activity result API needs AndroidX")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQUEST_USB) {
            if (resultCode == RESULT_OK && usbReady()) next() else render()
            return
        }
        val uri = data?.data
        if (requestCode == REQUEST_APK && resultCode == RESULT_OK && uri != null) importFrom(uri)
    }

    private fun importFrom(uri: Uri) {
        importing = true
        importError = false
        importMessage = getString(R.string.q_identity_importing)
        render()
        Thread {
            val error = IdentityImport.fromApk(applicationContext, uri)
            handler.post {
                importing = false
                importError = error != null
                importMessage = error ?: getString(R.string.q_identity_done)
                if (error == null) DashPlayService.restart(this)
                if (!isFinishing) render()
            }
        }.start()
    }

    // ---- Wi-Fi ----

    private fun wifiBody(root: LinearLayout) {
        if (!wireless) return
        // A box that runs its own hotspot edits the hotspot name; a box on a router edits the uplink. One field used to
        // serve both, which is how an iPhone ended up being told to join a hotspot that no longer existed.
        val useHotspot = network == Network.THIS_HOTSPOT
        if (useHotspot) {
            root.addView(ui.button(R.style.Button_Secondary, "自动创建本地热点") {
                startActivity(Intent(this, HotspotActivity::class.java))
            }, ui.matchWidth())
            if (settings.autoHotspot) {
                root.addView(ui.text(R.style.Text_Small, "使用应用本地热点，连接参数由系统自动取得。"))
                return
            }
        }

        if (ssidFromSystem && useHotspot) {
            root.addView(ui.text(R.style.Text_Small, getString(R.string.q_wifi_from_system)).apply { setTextColor(ui.color(R.color.green)) })
        }
        if (!useHotspot) {
            val current = CurrentWifi.ssid(this)
            root.addView(ui.text(R.style.Text_Small, ExternalWifiPolicy.BAND_HINT))
            root.addView(ui.choice("使用当前 Wi-Fi", current ?: "需要允许读取网络名称，也可以手动填写。",
                externalWifiChoice == ExternalWifiChoice.CURRENT) {
                keepFields()
                if (current != null) chooseCurrentWifi(current)
                else if (Build.VERSION.SDK_INT >= 23) requestPermissions(arrayOf(
                    Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.ACCESS_FINE_LOCATION), REQUEST_WIFI_NAME)
            }, ui.matchWidth())
            root.addView(ui.choice("其他 Wi-Fi", "填写网络名称和密码；连接前需让安卓设备接入该网络。",
                externalWifiChoice == ExternalWifiChoice.OTHER) {
                keepFields()
                externalWifiChoice = ExternalWifiChoice.OTHER
                wifiError = null
                render()
            }, ui.matchWidth())
            if (externalWifiChoice == null) {
                root.addView(ui.text(R.style.Text_Small, wifiError ?: "请先选择要使用的 Wi-Fi。"))
                return
            }
            if (externalWifiChoice == ExternalWifiChoice.CURRENT)
                root.addView(ui.text(R.style.Text_Small, if (uplinkPassphrase.isNotEmpty())
                    "已使用 DashPlay 保存的该网络密码，可直接继续。" else
                    "已填写网络名称。系统未提供密码，请输入该 Wi-Fi 的密码；开放网络可留空。"))
            root.addView(ui.button(R.style.Button_Text, "打开系统 Wi-Fi 设置") {
                startActivity(Intent(android.provider.Settings.ACTION_WIFI_SETTINGS))
            }, ui.matchWidth())
        }
        val name = if (useHotspot) ssid else uplinkSsid
        val secret = if (useHotspot) passphrase else uplinkPassphrase
        val (ssidBox, ssidInput) = ui.field(getString(R.string.wifi_ssid), name, password = false, fieldId = R.id.field_ssid)
        val (passBox, passInput) = ui.field(getString(R.string.wifi_password), secret, password = true, fieldId = R.id.field_password)
        ssidInput.isEnabled = useHotspot || externalWifiChoice != ExternalWifiChoice.CURRENT
        ssidField = ssidInput
        passField = passInput
        if (!useHotspot) ssidInput.addTextChangedListener(object : android.text.TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                if (s.toString() != uplinkSsid) { uplinkSsid = s.toString(); uplinkPassphrase = ""; passInput.setText("") }
            }
            override fun afterTextChanged(s: android.text.Editable?) {}
        })
        root.addView(ssidBox, ui.matchWidth())
        root.addView(passBox, ui.matchWidth())
        if (!useHotspot) root.addView(CheckBox(this).apply {
            text = "这是无密码的开放网络"
            isChecked = externalOpenNetwork
            minHeight = ui.dp(48)
            setOnCheckedChangeListener { _, checked ->
                keepFields()
                externalOpenNetwork = checked
                if (checked) uplinkPassphrase = ""
                render()
            }
        }, ui.matchWidth())
        wifiError?.let { root.addView(ui.text(R.style.Text_Small, it).apply { setTextColor(ui.color(R.color.red)) }) }
        if (useHotspot) {
            root.addView(ui.button(R.style.Button_Text, getString(R.string.wifi_open_settings)) { Intents.openHotspotSettings(this) }, ui.matchWidth())
        }
    }

    private fun chooseCurrentWifi(current: String) {
        val saved = settings.uplink()
        uplinkPassphrase = ExternalWifiPolicy.password(saved?.ssid, saved?.passphrase, current, null)
        externalOpenNetwork = saved?.ssid == current && settings.securityForNetwork(Network.MIFI) == WifiSecurity.OPEN
        uplinkSsid = current
        externalWifiChoice = ExternalWifiChoice.CURRENT
        wifiError = null
        SetupGuide.invalidate("wifi", guideAnswers)
        render()
        if (uplinkPassphrase.isEmpty()) passField?.requestFocus()
    }

    private fun keepFields() {
        val fieldSsid = ssidField?.text?.toString()?.trim()
        val fieldPass = passField?.text?.toString()
        if ((fieldSsid != null && fieldSsid != draftSsid()) ||
            (fieldPass != null && fieldPass != draftPassword())) SetupGuide.invalidate("wifi", guideAnswers)
        if (network == Network.THIS_HOTSPOT) {
            fieldSsid?.let { ssid = it }
            fieldPass?.let { passphrase = it }
        } else {
            fieldSsid?.let { uplinkSsid = it }
            fieldPass?.let { uplinkPassphrase = it }
        }
    }

    private fun draftSsid() = if (network == Network.THIS_HOTSPOT) ssid else uplinkSsid
    private fun draftPassword() = if (network == Network.THIS_HOTSPOT) passphrase else uplinkPassphrase

    private fun draftSecurity(): WifiSecurity {
        if (network != Network.THIS_HOTSPOT && externalOpenNetwork) return WifiSecurity.OPEN
        val unchanged = if (network == Network.THIS_HOTSPOT) settings.hotspot()?.let {
            it.ssid == ssid && it.passphrase == passphrase
        } else settings.uplink()?.let { it.ssid == uplinkSsid && it.passphrase == uplinkPassphrase }
        return if (unchanged == true) settings.securityForNetwork(network)
            else if (network == Network.THIS_HOTSPOT && draftPassword().isEmpty()) WifiSecurity.OPEN else WifiSecurity.WPA2
    }

    // ---- summary ----

    private fun summaryBody(root: LinearLayout) {
        root.addView(ui.text(R.style.Text_Name, "核对你的设置方案"))
        root.addView(ui.text(R.style.Text_Small, getString(R.string.browser_scheme_hint)))
        root.addView(ui.text(R.style.Text_Small, "点击底部按钮保存设置或完成剩余授权；保存后请上车打开页面确认画面和声音。"))
        val observed = LocalNetwork.choose(this)
        root.addView(ui.text(R.style.Text_Small, "当前局域网地址：" + (observed?.address?.hostAddress ?: "等待连网")))
        if (usesHttp()) {
            root.addView(ui.text(R.style.Text_Small, "已选择 HTTP 兼容方式。优先使用 WebRTC，连接失败时改用 JPEG 图片。请在车机上确认画面和触控；无法正常使用时，可返回检查网络或选择其他访问方式。"))
        }
        for (step in listOf(SetupGuide.INTERNET, SetupGuide.ACCESS, SetupGuide.KEEP, SetupGuide.TLS, SetupGuide.DNS, SetupGuide.HTTP_CHECK, SetupGuide.STARTUP)) {
            if (included(step)) root.addView(ui.text(R.style.Text_Small,
                (guideText(step)?.first ?: step) + "：" + answerLabel(step)))
        }
        pendingAction()?.let { root.addView(ui.text(R.style.Text_Name, "下一步：${it.first}。完成后返回这里继续。")) }
        val plan = Plan.of(car, network, link, audioOutsideCarPlay)
        val pad = ui.dimen(R.dimen.pad)
        fun pair(label: Int, value: String) = ui.column().apply {
            setPadding(0, pad / 3, 0, pad / 3)
            addView(ui.text(R.style.Text_Tiny, getString(label)))
            addView(ui.text(R.style.Text_Name, value))
        }
        val automatic = network == Network.THIS_HOTSPOT && settings.autoHotspot
        connectionProblem()?.let { reason ->
            root.addView(ui.text(R.style.Text_Small, reason).apply { setTextColor(ui.color(R.color.red)) }, ui.matchWidth())
        }
        if (link != IphoneLink.WIRELESS) root.addView(ui.text(R.style.Text_Small, when {
            !usbSupported && link == IphoneLink.ANY -> "此设备未声明 USB Host，自动连接将使用无线；仍需完成蓝牙配对和网络设置。"
            !usbSupported -> "此设备未声明 USB Host；数据线连接尚不可用，请返回确认连接方式。"
            !usbReady() -> "数据线授权尚未完成。保存后请进入数据线连接完成初设，当前不能直接使用。"
            settings.usbInternetRequested(kind) -> "已允许通过数据线使用 iPhone 网络；实际联网仍需 iPhone 的个人热点可用。"
            else -> "已关闭通过数据线使用 iPhone 网络，重新插线或开机后保留此选择。"
        }))
        val networkName = if (automatic) settings.localHotspotCredentials().ssid else if (wireless) draftSsid() else ""
        val answers = ui.card().apply {
            setPadding(pad, pad * 2 / 3, pad, pad * 2 / 3)
            addView(pair(R.string.summary_device, getString(if (kind == DeviceKind.PHONE) R.string.device_phone else R.string.device_box)))
            addView(pair(R.string.summary_car, getString(if (car == Car.TESLA) R.string.car_tesla else R.string.car_other)))
            addView(pair(R.string.summary_network, getString(when (network) { Network.THIS_HOTSPOT -> R.string.network_this_short; Network.CAR_HOTSPOT -> R.string.network_car; Network.MIFI -> R.string.network_mifi }) + (if (networkName.isNotBlank()) " · $networkName" else "")))
            addView(pair(R.string.summary_voice, getString(if (audioOutsideCarPlay) R.string.voice_car else R.string.voice_page)))
            addView(pair(R.string.summary_iphone, getString(when (link) { IphoneLink.ANY -> R.string.iphone_any_short; IphoneLink.WIRELESS -> R.string.iphone_wireless; IphoneLink.WIRED -> R.string.iphone_wired })))
        }
        root.addView(answers, ui.matchWidth())
        if (plan.notes.isNotEmpty() || plan.unverified.isNotEmpty()) {
            val costs = ui.card().apply {
                setPadding(pad, pad, pad, pad)
                addView(ui.text(R.style.Text_Name, getString(R.string.summary_notes)).apply { setTypeface(typeface, android.graphics.Typeface.BOLD) })
                plan.notes.filterNot { automatic && it == R.string.note_hotspot_on }.forEach { addView(ui.text(R.style.Text_Small, getString(it)).apply { setPadding(0, ui.dp(6), 0, 0) }) }
                plan.unverified.forEach {
                    addView(ui.text(R.style.Text_Small, getString(R.string.summary_unverified) + "：" + getString(it)).apply {
                        setPadding(0, ui.dp(6), 0, 0)
                        setTextColor(ui.color(R.color.orange))
                    })
                }
            }
            root.addView(costs, ui.matchWidth())
        }
    }

    companion object {
        const val EXTRA_ONLY = "only"
        const val EXTRA_APK = "apk"
        const val STEP_DEVICE = "device"
        const val STEP_IDENTITY = "identity"
        const val STEP_CAR = "car"
        const val STEP_NETWORK = "network"
        private const val REQUEST_WIFI_NAME = 42
        const val STEP_WIFI = "wifi"
        const val STEP_USB = "usb"
        private const val REQUEST_USB = 43
        const val STEP_VOICE = "voice"
        const val STEP_IPHONE = "iphone"
        const val STEP_SUMMARY = "summary"
        private val QUESTIONS = SetupGuide.questions.filter { it != STEP_SUMMARY }
        private const val KEY_INDEX = "index"
        private const val REQUEST_APK = 1
        private const val REQUEST_STORAGE = 3
    }
}

/** System screens the app sends the user to. */
object Intents {
    /** Pages that may host tethering on the settings apps these boxes ship. Android TV ROMs replace
     *  `com.android.settings` with their own app, so the TV candidates are checked too. */
    private fun hotspotSettingsCandidates() = listOf(
        Intent().setClassName("com.android.settings", "com.android.settings.TetherSettings"),
        Intent("android.settings.TETHER_SETTINGS"),
        Intent().setClassName("com.android.tv.settings", "com.android.tv.settings.connectivity.TetheringActivity"),
        Intent().setClassName("com.android.tv.settings", "com.android.tv.settings.system.TetherSettings"),
    )

    /** Whether this device really has a tethering screen we can open.
     *
     *  `resolveActivity(...) != null` is not enough: on several ROMs an explicit intent for a package that does not
     *  exist still comes back as a ResolveInfo whose `activityInfo` is null, which made the capability page and the
     *  wizard claim an entry that could not be opened (SY910, 2026-11-03). Only a resolved component counts.
     */
    fun hasHotspotSettings(context: android.content.Context): Boolean = hotspotSettingsCandidates().any { intent ->
        runCatching { context.packageManager.resolveActivity(intent, 0)?.activityInfo != null }.getOrDefault(false)
    }

    fun canPickFile(activity: Activity): Boolean = runCatching {
        activity.packageManager.resolveActivity(
            Intent(Intent.ACTION_GET_CONTENT).setType("*/*").addCategory(Intent.CATEGORY_OPENABLE), 0,
        )?.activityInfo != null
    }.getOrDefault(false)

    /** The hotspot page where the system has one we can open, else Wi-Fi and networks. Says so on screen when this
     *  ROM has neither, instead of leaving the tap doing nothing. */
    fun openHotspotSettings(activity: Activity) {
        val candidates = hotspotSettingsCandidates() + Intent(android.provider.Settings.ACTION_WIRELESS_SETTINGS)
        for (intent in candidates) {
            if (runCatching { activity.startActivity(intent) }.isSuccess) return
        }
        android.widget.Toast.makeText(activity, "本机没有系统热点设置页面，请用外部网络", android.widget.Toast.LENGTH_LONG).show()
    }
}
