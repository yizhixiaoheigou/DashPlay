package com.dashplay.android

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView

/** Home: where things stand, the car's address, and the one thing to do next. */
class MainActivity : Activity() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var ui: Ui
    private lateinit var head: TextView
    private lateinit var sub: TextView
    private lateinit var rows: LinearLayout
    private lateinit var qr: ImageView
    private lateinit var address: TextView
    private lateinit var addressHint: TextView
    private lateinit var action: Button
    private var addIphone: Button? = null
    private var shownAddress: String? = null
    /** What the screen shows now; it is only touched when this changes (a screen that redraws every second
     *  never settles for accessibility services and UI tests). */
    private var shown: List<Any?>? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        isAlive = true
        ui = Ui(this)
        setContentView(ui.page(build()))
        requestRuntimePermissions()
        handleShare(intent)
        if (!Settings(this).setupDone && !askedSetup) {
            askedSetup = true
            startActivity(Intent(this, SetupActivity::class.java))
        }
    }

    override fun onDestroy() {
        isAlive = false
        super.onDestroy()
    }

    override fun onResume() {
        super.onResume()
        if (!DashPlayService.userStopped && DashPlayService.status == Status.STOPPED) DashPlayService.start(this)
        refresh()
    }

    override fun onPause() {
        handler.removeCallbacksAndMessages(null)
        super.onPause()
    }

    /**
     * The wide screen a car browser or a remote is used from: a television, or a box-shaped screen held
     * landscape. A portrait phone keeps the phone screen whatever kind was chosen, so the address never wraps.
     */
    private val boxLayout: Boolean
        get() {
            if (ui.tv) return true
            if (Settings(this).deviceKind != DeviceKind.BOX) return false
            // A box screen is wide and roomy; a phone held sideways is neither, and the two columns would clip.
            val metrics = resources.displayMetrics
            val shortestDp = minOf(metrics.widthPixels, metrics.heightPixels) / metrics.density
            return resources.configuration.orientation == android.content.res.Configuration.ORIENTATION_LANDSCAPE &&
                shortestDp >= 600
        }

    private fun build(): View {
        val box = boxLayout
        val root = ui.stack()
        val title = ui.row().apply {
            addView(ui.text(R.style.Text_Title, getString(R.string.app_name)), ui.weight())
            if (!box) {
                addView(
                    ImageButton(this@MainActivity).apply {
                        id = R.id.settings_button
                        setImageResource(R.drawable.ic_settings)
                        setColorFilter(ui.color(R.color.text))
                        setBackgroundResource(R.drawable.btn_secondary)
                        contentDescription = getString(R.string.settings)
                        setOnClickListener { startActivity(Intent(this@MainActivity, SettingsActivity::class.java)) }
                    },
                    LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)),
                )
            }
        }
        root.addView(title)

        val pad = ui.dimen(R.dimen.pad)
        head = ui.text(R.style.Text_Head)
        sub = ui.text(R.style.Text_Small)
        val headCard = ui.card().apply {
            setPadding(pad, pad, pad, pad)
            addView(head)
            addView(sub)
        }
        rows = ui.column()

        qr = ImageView(this).apply {
            setBackgroundResource(R.drawable.qr_placeholder)
            contentDescription = getString(R.string.qr_description)
        }
        address = ui.text(R.style.Text_Name)
        addressHint = ui.text(R.style.Text_Tiny)
        val qrSize = ui.dimen(R.dimen.qr)
        val addressTexts = ui.column().apply {
            addView(ui.text(R.style.Text_Small, getString(R.string.address_label)))
            addView(address)
            addView(addressHint)
        }
        action = ui.button(R.style.Button_Primary, "") {}.apply { id = R.id.primary }
        val note = ui.text(R.style.Text_Tiny, getString(if (box) R.string.home_note_tv else R.string.home_note)).apply {
            gravity = if (box) Gravity.END else Gravity.CENTER_HORIZONTAL
        }

        if (box) {
            // TV: status on the left, the address large on the right, the buttons in one row a remote walks along.
            val left = ui.stack().apply { addView(headCard, ui.matchWidth()); addView(rows, ui.matchWidth()) }
            val right = ui.card().apply {
                gravity = Gravity.CENTER_HORIZONTAL
                setPadding(pad, pad, pad, pad)
                addView(qr, LinearLayout.LayoutParams(qrSize, qrSize))
                addView(addressTexts.apply { gravity = Gravity.CENTER_HORIZONTAL; setPadding(0, pad / 2, 0, 0) })
            }
            val top = ui.row().apply {
                gravity = Gravity.TOP
                addView(left, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.3f))
                addView(View(this@MainActivity), LinearLayout.LayoutParams(ui.dimen(R.dimen.gap), 1))
                addView(right, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            }
            val buttons = ui.row().apply {
                addView(action, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT))
                addView(gap())
                addView(ui.button(R.style.Button_Secondary, getString(R.string.settings)) {
                    startActivity(Intent(this@MainActivity, SettingsActivity::class.java))
                }.apply { id = R.id.settings_button })
                addView(gap())
                addView(ui.button(R.style.Button_Secondary, getString(R.string.action_add_iphone)) {
                    startActivity(Intent(this@MainActivity, PairActivity::class.java))
                }.apply { id = R.id.add_iphone }.also { addIphone = it })
                addView(note, ui.weight())
            }
            root.addView(top, ui.matchWidth())
            root.addView(buttons, ui.matchWidth())
            action.requestFocus()
        } else {
            val addressCard = ui.card().apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(pad, pad, pad, pad)
                addView(qr, LinearLayout.LayoutParams(qrSize, qrSize))
                addView(addressTexts.apply { setPadding(pad, 0, 0, 0) }, ui.weight())
            }
            root.addView(headCard, ui.matchWidth())
            root.addView(rows, ui.matchWidth())
            root.addView(addressCard, ui.matchWidth())
            root.addView(action, ui.matchWidth())
            root.addView(note, ui.matchWidth())
        }
        return root
    }

    private fun gap() = View(this).apply { layoutParams = LinearLayout.LayoutParams(ui.dimen(R.dimen.gap), 1) }

    private data class Home(val head: String, val sub: String, val rows: List<Triple<String, String, Light>>, val action: String, val run: () -> Unit)

    private fun home(): Home {
        val settings = Settings(this)
        val snapshot = DashPlayService.snapshot()
        val identity = IdentityImport.present(this)
        val plan = settings.plan()

        val net = snapshot.network
        // Set up for this device's hotspot but on someone's Wi-Fi: fine at home, but the car will not find it.
        val hotspotOff = settings.network == Network.THIS_HOTSPOT && net?.kind == LocalNetwork.Kind.WIFI
        val networkRow = Triple(
            getString(R.string.row_network),
            when (net?.kind) {
                LocalNetwork.Kind.HOTSPOT -> getString(R.string.net_hotspot, DashPlayService.managedHotspot?.connection?.ssid ?: settings.hotspot()?.ssid ?: net.address.hostAddress)
                LocalNetwork.Kind.WIFI -> getString(if (hotspotOff) R.string.net_hotspot_off else R.string.net_wifi, net.address.hostAddress)
                LocalNetwork.Kind.ETHERNET -> getString(R.string.net_ethernet, net.address.hostAddress)
                null -> getString(R.string.net_none)
            },
            when {
                net == null -> Light.BAD
                hotspotOff -> Light.WAIT
                else -> Light.OK
            },
        )
        val carRow = Triple(
            getString(R.string.row_car),
            getString(if (snapshot.viewers > 0) R.string.car_open else R.string.car_waiting),
            if (snapshot.viewers > 0) Light.OK else Light.WAIT,
        )
        val bluetooth = android.bluetooth.BluetoothAdapter.getDefaultAdapter() != null
        val phones = runCatching { AndroidPhones.get(this).describe().getJSONArray("phones") }.getOrNull()
        val paired = phones != null && phones.length() > 0
        val paused = phones?.let { list -> list.length() > 0 && (0 until list.length()).all { list.getJSONObject(it).getBoolean("paused") } } ?: false
        val iphoneRow = Triple(
            getString(R.string.row_iphone),
            when {
                !identity -> getString(R.string.iphone_no_identity)
                snapshot.sessionActive -> getString(R.string.iphone_live, snapshot.phoneName ?: "iPhone")
                settings.wiredCarPlaySupported && (!plan.wireless || settings.usesUsb && !bluetooth) -> AndroidWiredController.detail
                !bluetooth -> getString(R.string.iphone_no_bluetooth)
                phones == null -> "暂时无法读取手机记录"
                paused -> "已暂停自动连接"
                paired -> getString(R.string.iphone_paired)
                else -> getString(R.string.iphone_none)
            },
            when {
                snapshot.sessionActive -> Light.OK
                !identity || !plan.wireless || !bluetooth -> Light.BAD
                else -> Light.WAIT
            },
        )
        val rows = listOf(networkRow, carRow, iphoneRow)
        val wirelessProblem = WirelessReadiness.problem(this)

        fun setup(only: String? = null) = Intent(this, SetupActivity::class.java).also { if (only != null) it.putExtra(SetupActivity.EXTRA_ONLY, only) }
        return when {
            DashPlayService.userStopped || snapshot.status == Status.STOPPED && DashPlayService.userStopped ->
                Home(getString(R.string.home_stopped), getString(R.string.home_stopped_sub), rows, getString(R.string.action_start)) {
                    DashPlayService.start(this)
                }
            !settings.setupDone -> Home(getString(R.string.home_needs_setup), getString(R.string.home_needs_setup_sub), rows, getString(R.string.action_setup)) {
                startActivity(setup())
            }
            settings.connectionProblem(settings.network, settings.iphoneLink) != null ->
                Home("请调整连接设置", settings.connectionProblem(settings.network, settings.iphoneLink)!!, rows, "连接设置") {
                    startActivity(setup(SetupActivity.STEP_NETWORK))
                }
            !snapshot.sessionActive && wirelessProblem != null ->
                Home("请设置无线网络", wirelessProblem, rows, "设置无线网络") {
                    startActivity(setup(SetupActivity.STEP_WIFI))
                }
            !identity -> Home(getString(R.string.home_needs_identity), getString(R.string.home_needs_identity_sub), rows, getString(R.string.action_import)) {
                startActivity(setup(SetupActivity.STEP_IDENTITY))
            }
            !snapshot.sessionActive && (settings.iphoneLink == IphoneLink.WIRED || settings.iphoneLink == IphoneLink.ANY && !settings.usbChoiceMade &&
                packageManager.hasSystemFeature(PackageManager.FEATURE_USB_HOST)) &&
                (!settings.usbEnabled || android.net.VpnService.prepare(this) != null) ->
                Home("完成数据线授权", "首次需要允许本机 VPN 与 USB。是否使用 iPhone 网络，以数据线设置页保存的选择为准。", rows, "数据线连接设置") {
                    startActivity(Intent(this, UsbActivity::class.java))
                }
            snapshot.status == Status.FAILED -> Home(getString(R.string.home_failed), snapshot.detail.orEmpty(), rows, getString(R.string.action_retry)) {
                DashPlayService.restart(this)
            }
            settings.usesLocalHotspot && DashPlayService.managedHotspot?.connection == null ->
                Home("正在准备本地热点", DashPlayService.managedHotspot?.message.orEmpty(), rows, "本地热点设置") {
                    startActivity(Intent(this, HotspotActivity::class.java))
                }
            net == null && settings.network != Network.THIS_HOTSPOT -> Home("等待外部网络", snapshot.detail ?: "请让这台设备加入外部 Wi-Fi，或通过网线连接路由器。车机需能访问同一网络中的设备；本地接收不要求互联网。", rows, "打开系统网络设置") {
                runCatching { startActivity(Intent(android.provider.Settings.ACTION_WIRELESS_SETTINGS)) }
                    .onFailure { startActivity(Intent(android.provider.Settings.ACTION_SETTINGS)) }
            }
            net == null -> Home(getString(R.string.home_no_network), getString(R.string.home_no_network_sub), rows, getString(R.string.action_open_hotspot)) {
                Intents.openHotspotSettings(this)
            }
            snapshot.sessionActive -> Home(
                getString(R.string.home_live),
                getString(if (snapshot.wiredActive) R.string.home_live_sub_wired else R.string.home_live_sub, snapshot.phoneName ?: "iPhone"),
                rows,
                getString(R.string.action_stop),
            ) {
                DashPlayService.stop(this)
            }
            settings.wiredCarPlaySupported && (!plan.wireless || settings.usesUsb && (!bluetooth || AndroidWiredController.status !in setOf("OFF", "CABLE", "TETHER"))) -> Home(getString(R.string.home_wired_only), AndroidWiredController.detail, rows, "数据线连接设置") {
                startActivity(Intent(this, UsbActivity::class.java))
            }
            !bluetooth -> Home(getString(R.string.home_no_bluetooth), (if (settings.wiredCarPlaySupported) getString(R.string.home_no_bluetooth_sub) else ConnectionSupport.PHONE_NO_BLUETOOTH), rows, getString(R.string.settings)) {
                startActivity(Intent(this, SettingsActivity::class.java))
            }
            !settings.usesLocalHotspot && settings.connectionWifi() == null -> Home(getString(R.string.home_no_wifi_info), getString(R.string.home_no_wifi_info_sub), rows, getString(R.string.action_fill_wifi)) {
                startActivity(setup(SetupActivity.STEP_WIFI))
            }
            phones == null -> Home("暂时无法读取手机记录", "请进入手机管理重试。原有记录已保留。", rows, "管理 iPhone") {
                startActivity(Intent(this, PhonesActivity::class.java))
            }
            !paired -> Home(getString(R.string.home_no_iphone), getString(R.string.home_no_iphone_sub), rows, getString(R.string.action_add_iphone)) {
                startActivity(Intent(this, PairActivity::class.java))
            }
            paused -> Home("自动连接已暂停", "在已添加的 iPhone 中选择连接即可恢复。", rows, "管理 iPhone") {
                startActivity(Intent(this, PhonesActivity::class.java))
            }
            snapshot.status != Status.RUNNING -> Home(getString(R.string.home_starting), getString(R.string.home_starting_sub), rows, getString(R.string.action_retry)) {
                DashPlayService.restart(this)
            }
            boxLayout -> Home(getString(R.string.home_ready_tv), getString(R.string.home_ready_tv_sub), rows, getString(R.string.action_connect)) {
                startActivity(Intent(this, PhonesActivity::class.java))
            }
            else -> Home(getString(R.string.home_waiting), getString(R.string.home_waiting_sub), rows, getString(R.string.action_connect)) {
                startActivity(Intent(this, PhonesActivity::class.java))
            }
        }
    }

    private fun refresh() {
        val home = home()
        val snapshot = DashPlayService.snapshot()
        val link = DashPlayService.url
        val hint = getString(R.string.browser_scheme_hint)
        val key = listOf(home.head, home.sub, home.rows, home.action, snapshot.address, link, hint)
        action.setOnClickListener { home.run(); refresh() }
        // The TV's own Add iPhone button would repeat the main one.
        addIphone?.visibility = if (home.action == getString(R.string.action_add_iphone)) View.GONE else View.VISIBLE
        if (key != shown) {
            shown = key
            head.text = home.head
            sub.text = home.sub
            rows.removeAllViews()
            rows.addView(ui.list(home.rows.map { (name, value, light) -> ui.statusRow(name, value, light) }), ui.matchWidth())
            action.text = home.action
            address.text = link?.substringBefore("?") ?: "—"
            addressHint.text = hint
        }
        if (link != shownAddress) {
            shownAddress = link
            if (link != null) qr.setImageBitmap(ui.qr(link, QR_PIXELS)) else qr.setImageDrawable(null)
        }
        handler.removeCallbacksAndMessages(null)
        handler.postDelayed(::refresh, REFRESH_MS)
    }

    private fun requestRuntimePermissions() {
        if (Build.VERSION.SDK_INT < 23) return
        val wanted = buildList {
            if (Build.VERSION.SDK_INT >= 31) add("android.permission.BLUETOOTH_CONNECT")
            if (Build.VERSION.SDK_INT >= 33) add("android.permission.POST_NOTIFICATIONS")
            // iPhone USB Configuration 2 declares USB Audio Capture (mHasAudioCapture=true). Android 9+
            // refuses to show the "Always allow" USB checkbox or remember USB permission unless RECORD_AUDIO is granted.
            if (Settings(this@MainActivity).let { it.wiredCarPlaySupported || it.usesUsb } && packageManager.hasSystemFeature(PackageManager.FEATURE_USB_HOST)) add("android.permission.RECORD_AUDIO")
        }.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (wanted.isNotEmpty()) requestPermissions(wanted.toTypedArray(), REQUEST_PERMISSIONS)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_PERMISSIONS && grantResults.any { it == PackageManager.PERMISSION_GRANTED }) DashPlayService.restart(this)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    /** A DiPlay APK shared from a file manager: the identity step takes it from here. */
    private fun handleShare(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) return
        @Suppress("DEPRECATION")
        val uri = intent.getParcelableExtra<android.net.Uri>(Intent.EXTRA_STREAM) ?: return
        startActivity(Intent(this, SetupActivity::class.java).putExtra(SetupActivity.EXTRA_ONLY, SetupActivity.STEP_IDENTITY).putExtra(SetupActivity.EXTRA_APK, uri))
    }

    internal companion object {
        const val REQUEST_PERMISSIONS = 2
        const val REFRESH_MS = 1_000L
        const val QR_PIXELS = 400
        /** Setup opens by itself once per run of the app, not again after the user backs out of it. */
        var askedSetup = false
        @Volatile var isAlive = false
    }
}
