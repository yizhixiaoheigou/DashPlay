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
    private var ssidFromSystem = false
    private var renderedAutoHotspot = false

    private var importMessage: String? = null
    private var importError = false
    private var importing = false
    private var wifiError: String? = null
    private var saveError: String? = null
    private var ssidField: EditText? = null
    private var passField: EditText? = null

    private lateinit var kind: DeviceKind
    private val single: Boolean get() = steps.size == 1
    private val wireless: Boolean get() = Plan.of(car, network, link, audioOutsideCarPlay).wireless
    private val automatic: Boolean get() = network == Network.THIS_HOTSPOT && settings.autoHotspot
    private fun included(step: String) = single || step != STEP_WIFI || wireless

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = Ui(this)
        settings = Settings(this)
        kind = settings.deviceKind
        car = settings.car
        network = settings.network
        audioOutsideCarPlay = settings.audioOutsideCarPlay
        link = settings.iphoneLink
        settings.hotspot()?.let { ssid = it.ssid; passphrase = it.passphrase; ssidFromSystem = it.fromSystem }
        settings.uplink()?.let { uplinkSsid = it.ssid; uplinkPassphrase = it.passphrase }
        savedInstanceState?.let {
            kind = DeviceKind.valueOf(it.getString("draft_device", kind.name))
            car = Car.valueOf(it.getString("draft_car", car.name))
            network = Network.valueOf(it.getString("draft_network", network.name))
            link = IphoneLink.valueOf(it.getString("draft_link", link.name))
            audioOutsideCarPlay = it.getBoolean("draft_audio", audioOutsideCarPlay)
            ssid = it.getString("draft_ssid", ssid)
            passphrase = it.getString("draft_password", passphrase)
            uplinkSsid = it.getString("draft_uplink_ssid", uplinkSsid)
            uplinkPassphrase = it.getString("draft_uplink_password", uplinkPassphrase)
        }
        val only = intent.getStringExtra(EXTRA_ONLY)
        steps = if (only != null) listOf(only) else QUESTIONS + STEP_SUMMARY
        index = savedInstanceState?.getInt(KEY_INDEX) ?: 0
        @Suppress("DEPRECATION")
        intent.getParcelableExtra<Uri>(EXTRA_APK)?.let(::importFrom)
        render()
    }

    override fun onResume() {
        super.onResume()
        if (::steps.isInitialized && steps[index] == STEP_WIFI && renderedAutoHotspot != settings.autoHotspot) {
            keepFields(); render()
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        keepFields()
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
        renderedAutoHotspot = settings.autoHotspot
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
            else -> R.string.q_summary to R.string.q_summary_why
        }
        root.addView(ui.text(R.style.Text_Title, getString(title)))
        root.addView(ui.text(R.style.Text_Small, getString(why)))
        saveError?.let { root.addView(ui.text(R.style.Text_Small, it).apply { setTextColor(ui.color(R.color.red)) }) }

        when (step) {
            STEP_DEVICE -> {
                root.addView(ui.choice(getString(R.string.device_phone), getString(R.string.device_phone_sub), kind == DeviceKind.PHONE) { kind = DeviceKind.PHONE; render() }, ui.matchWidth())
                root.addView(ui.choice(getString(R.string.device_box), getString(R.string.device_box_sub), kind == DeviceKind.BOX) { kind = DeviceKind.BOX; render() }, ui.matchWidth())
            }
            STEP_IDENTITY -> identityBody(root)
            STEP_CAR -> {
                root.addView(ui.choice(getString(R.string.car_tesla), getString(R.string.car_tesla_sub), car == Car.TESLA) { car = Car.TESLA; render() }, ui.matchWidth())
                root.addView(ui.choice(getString(R.string.car_other), getString(R.string.car_other_sub), car == Car.OTHER) { car = Car.OTHER; render() }, ui.matchWidth())
            }
            STEP_NETWORK -> {
                root.addView(ui.choice(getString(R.string.network_this), getString(R.string.network_this_sub), network == Network.THIS_HOTSPOT) { network = Network.THIS_HOTSPOT; render() }, ui.matchWidth())
                root.addView(ui.choice(getString(R.string.network_car), getString(R.string.network_car_sub), network == Network.CAR_HOTSPOT) { network = Network.CAR_HOTSPOT; render() }, ui.matchWidth())
                root.addView(ui.choice(getString(R.string.network_mifi), getString(R.string.network_mifi_sub), network == Network.MIFI) { network = Network.MIFI; render() }, ui.matchWidth())
                // Say it here, not four screens later: this ROM has no system hotspot entry at all.
                if (network == Network.THIS_HOTSPOT && !DeviceCapabilities.describe(this).optBoolean("managedHotspot")) {
                    root.addView(ui.text(R.style.Text_Small, getString(R.string.hotspot_unsupported)).apply { setTextColor(ui.color(R.color.red)) }, ui.matchWidth())
                }
            }
            STEP_WIFI -> wifiBody(root)
            STEP_VOICE -> {
                root.addView(ui.choice(getString(R.string.voice_car), getString(R.string.voice_car_sub), audioOutsideCarPlay) { audioOutsideCarPlay = true; render() }, ui.matchWidth())
                root.addView(ui.choice(getString(R.string.voice_page), getString(R.string.voice_page_sub), !audioOutsideCarPlay) { audioOutsideCarPlay = false; render() }, ui.matchWidth())
            }
            STEP_IPHONE -> {
                val plan = Plan.of(car, network, IphoneLink.WIRELESS, audioOutsideCarPlay)
                root.addView(ui.choice(getString(R.string.iphone_any), getString(R.string.iphone_any_sub), link == IphoneLink.ANY) { link = IphoneLink.ANY; render() }, ui.matchWidth())
                val wirelessSub = getString(if (plan.wirelessRuledOut) R.string.iphone_wireless_ruled_out else R.string.iphone_wireless_sub)
                root.addView(ui.choice(getString(R.string.iphone_wireless), wirelessSub, link == IphoneLink.WIRELESS, enabled = !plan.wirelessRuledOut) { link = IphoneLink.WIRELESS; render() }, ui.matchWidth())
                root.addView(ui.choice(getString(R.string.iphone_wired), getString(R.string.iphone_wired_sub), link == IphoneLink.WIRED) { link = IphoneLink.WIRED; render() }, ui.matchWidth())
            }
            STEP_SUMMARY -> summaryBody(root)
        }

        // Answers numbered top to bottom, for tests that cannot read the words.
        val choiceIds = listOf(R.id.choice_1, R.id.choice_2, R.id.choice_3)
        (0 until root.childCount).map(root::getChildAt).filter { it.isFocusable && it is LinearLayout }
            .forEachIndexed { i, view -> if (i < choiceIds.size) view.id = choiceIds[i] }
        val primary = ui.button(R.style.Button_Primary, primaryLabel(step)) { next() }.apply { id = R.id.primary }
        primary.isEnabled = canGoOn(step)
        root.addView(primary, ui.matchWidth())
        if (step == STEP_SUMMARY) root.addView(ui.button(R.style.Button_Text, getString(R.string.summary_edit)) { index = 0; render() }.apply { id = R.id.secondary }, ui.matchWidth())

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
        step == STEP_SUMMARY -> getString(if (wantsPairing()) R.string.summary_confirm_pair else R.string.summary_confirm)
        single && step == STEP_IDENTITY -> getString(R.string.done)
        single -> getString(R.string.save)
        else -> getString(R.string.next)
    }

    private fun canGoOn(step: String): Boolean = when (step) {
        STEP_IDENTITY -> IdentityImport.present(this) && !importing
        else -> true
    }

    private fun next() {
        val step = steps[index]
        saveError = null
        if (step == STEP_WIFI && wireless && !automatic) {
            keepFields()
            wifiError = runCatching { SettingValidation.wifi(ssid, passphrase, draftSecurity()) }.exceptionOrNull()?.message
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
            try {
                settings.apply(ManagementSettings(car = car, network = network, iphoneLink = link,
                    audioOutsideCarPlay = audioOutsideCarPlay, setupDone = true,
                    ssid = if (wireless && !automatic) ssid else null,
                    passphrase = if (wireless && !automatic) passphrase else null,
                    wifiSecurity = if (wireless && !automatic) draftSecurity() else null))
                settings.deviceKind = kind
            } catch (failure: IllegalArgumentException) {
                saveError = failure.message; render(); return
            } catch (_: IllegalStateException) {
                saveError = "设置未保存，请重试。"; render(); return
            }
            DashPlayService.restart(this)
            if (wantsPairing()) startActivity(Intent(this, PairActivity::class.java))
            finish()
            return
        }
        do { index++ } while (index < steps.lastIndex && !included(steps[index]))
        render()
    }

    private fun save(step: String) {
        when (step) {
            STEP_DEVICE -> settings.deviceKind = kind
            STEP_CAR -> settings.car = car
            STEP_NETWORK -> settings.network = network
            STEP_WIFI -> if (wireless && !automatic) {
                if (network == Network.THIS_HOTSPOT) {
                    settings.apply(ManagementSettings(ssid = ssid, passphrase = passphrase, wifiSecurity = draftSecurity()))
                } else {
                    settings.apply(ManagementSettings(uplinkSsid = uplinkSsid, uplinkPassphrase = uplinkPassphrase,
                        wifiSecurity = draftSecurity()))
                }
            }
            STEP_VOICE -> settings.audioOutsideCarPlay = audioOutsideCarPlay
            STEP_IPHONE -> settings.iphoneLink = link
        }
    }

    private fun wantsPairing(): Boolean =
        Plan.of(car, network, link, audioOutsideCarPlay).wireless && settings.iphoneAddress == null &&
            AndroidBluetoothLink(this, null).pairedIphone() == null

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
            if (current != null) {
                if (uplinkSsid.isBlank()) uplinkSsid = current
                val address = LocalNetwork.choose(this)?.address?.hostAddress
                root.addView(
                    ui.text(R.style.Text_Small, getString(R.string.network_current) + "：" + current + (address?.let { "（$it）" } ?: ""))
                        .apply { setTextColor(ui.color(R.color.green)) },
                    ui.matchWidth(),
                )
                val typed = uplinkSsid.trim()
                if (typed.isNotBlank() && !current.equals(typed, ignoreCase = true)) {
                    root.addView(
                        ui.text(R.style.Text_Small, getString(R.string.network_mismatch, current, typed))
                            .apply { setTextColor(ui.color(R.color.red)) },
                        ui.matchWidth(),
                    )
                }
                if (!current.equals(typed, ignoreCase = true)) {
                    root.addView(ui.button(R.style.Button_Secondary, getString(R.string.network_use_current, current)) {
                        uplinkSsid = current
                        render()
                    }, ui.matchWidth())
                }
            }
        }
        val name = if (useHotspot) ssid else uplinkSsid
        val secret = if (useHotspot) passphrase else uplinkPassphrase
        val (ssidBox, ssidInput) = ui.field(getString(R.string.wifi_ssid), name, password = false, fieldId = R.id.field_ssid)
        val (passBox, passInput) = ui.field(getString(R.string.wifi_password), secret, password = true, fieldId = R.id.field_password)
        ssidField = ssidInput
        passField = passInput
        root.addView(ssidBox, ui.matchWidth())
        root.addView(passBox, ui.matchWidth())
        wifiError?.let { root.addView(ui.text(R.style.Text_Small, it).apply { setTextColor(ui.color(R.color.red)) }) }
        if (useHotspot) {
            root.addView(ui.button(R.style.Button_Text, getString(R.string.wifi_open_settings)) { Intents.openHotspotSettings(this) }, ui.matchWidth())
        }
    }

    private fun keepFields() {
        val fieldSsid = ssidField?.text?.toString()?.trim()
        val fieldPass = passField?.text?.toString()
        if (network == Network.THIS_HOTSPOT) {
            fieldSsid?.let { ssid = it }
            fieldPass?.let { passphrase = it }
        } else {
            fieldSsid?.let { uplinkSsid = it }
            fieldPass?.let { uplinkPassphrase = it }
        }
    }

    private fun draftSecurity(): WifiSecurity {
        val previous = settings.hotspot()
        return if (previous?.ssid == ssid && previous.passphrase == passphrase) settings.wifiSecurity
            else if (passphrase.isEmpty()) WifiSecurity.OPEN else WifiSecurity.WPA2
    }

    // ---- summary ----

    private fun summaryBody(root: LinearLayout) {
        val plan = Plan.of(car, network, link, audioOutsideCarPlay)
        val pad = ui.dimen(R.dimen.pad)
        fun pair(label: Int, value: String) = ui.column().apply {
            setPadding(0, pad / 3, 0, pad / 3)
            addView(ui.text(R.style.Text_Tiny, getString(label)))
            addView(ui.text(R.style.Text_Name, value))
        }
        val automatic = network == Network.THIS_HOTSPOT && settings.autoHotspot
        val networkName = if (automatic) settings.localHotspotCredentials().ssid else if (plan.wireless) ssid else ""
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
        const val STEP_WIFI = "wifi"
        const val STEP_VOICE = "voice"
        const val STEP_IPHONE = "iphone"
        const val STEP_SUMMARY = "summary"
        private val QUESTIONS = listOf(STEP_DEVICE, STEP_IDENTITY, STEP_CAR, STEP_NETWORK, STEP_VOICE, STEP_IPHONE, STEP_WIFI)
        private const val KEY_INDEX = "index"
        private const val REQUEST_APK = 1
        private const val REQUEST_STORAGE = 3
    }
}

/** System screens the app sends the user to. */
object Intents {
    fun canPickFile(activity: Activity): Boolean =
        Intent(Intent.ACTION_GET_CONTENT).setType("*/*").addCategory(Intent.CATEGORY_OPENABLE).resolveActivity(activity.packageManager) != null

    /** The hotspot page where the system has one we can open, else Wi-Fi and networks. */
    fun openHotspotSettings(activity: Activity) {
        val candidates = listOf(
            Intent().setClassName("com.android.settings", "com.android.settings.TetherSettings"),
            Intent("android.settings.TETHER_SETTINGS"),
            Intent(android.provider.Settings.ACTION_WIRELESS_SETTINGS),
        )
        for (intent in candidates) {
            if (runCatching { activity.startActivity(intent) }.isSuccess) return
        }
    }
}
