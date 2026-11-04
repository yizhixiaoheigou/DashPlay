package com.dashplay.android

import android.app.Activity
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.LinearLayout

/**
 * Add the iPhone. The iPhone can be found while its Bluetooth settings page is open; this device searches, the user
 * picks the iPhone, and both show the same six digits (DiPlay pairs the same way, from the head unit's side).
 */
class PairActivity : Activity() {
    private lateinit var ui: Ui
    private val handler = Handler(Looper.getMainLooper())
    private val adapter: BluetoothAdapter? get() = BluetoothAdapter.getDefaultAdapter()
    private val found = LinkedHashMap<String, BluetoothDevice>()
    private var scanning = false
    private var message: String? = null
    private var messageLight = Light.WAIT
    private var bonding: BluetoothDevice? = null
    /** Addresses whose advertised name was remembered; an unpaired iPhone often advertises just "iPhone". */
    private val names = LinkedHashMap<String, String>()
    private var renderPosted = false
    private var foreground = false
    private var shownProblem: String? = null
    private val statusPoll = object : Runnable {
        override fun run() {
            if (!foreground) return
            render()
            handler.postDelayed(this, 2_000)
        }
    }
    override fun onResume() {
        super.onResume()
        foreground = true
        handler.post(statusPoll)
    }
    override fun onPause() {
        foreground = false
        handler.removeCallbacks(statusPoll)
        super.onPause()
    }
    private fun wifiSettings() = startActivity(Intent(this, SetupActivity::class.java)
        .putExtra(SetupActivity.EXTRA_ONLY, SetupActivity.STEP_WIFI))

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            @Suppress("DEPRECATION")
            val device = intent.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE)
            when (intent.action) {
                BluetoothDevice.ACTION_FOUND -> if (device != null) {
                    val name = safeName(device)
                    // Keep the most specific name seen: "程浩轩的 iPhone" beats the generic "iPhone" an
                    // unpaired phone advertises in some advertisements (2026-11-03).
                    if (name != null) {
                        val old = names[device.address]
                        if (old == null || score(name) > score(old)) names[device.address] = name
                    }
                    if (name != null) { found[device.address] = device; later() }
                }
                BluetoothAdapter.ACTION_DISCOVERY_FINISHED -> {
                    scanning = false
                    render()
                    // Keep the list alive: an iPhone only shows while its Bluetooth page is open, so a single
                    // discovery pass goes stale in seconds and there was nothing to refresh it (2026-11-03).
                    handler.postDelayed({
                        if (!isFinishing && !scanning && Settings(this@PairActivity).iphoneAddress == null) scan()
                    }, 3_000)
                }
                BluetoothAdapter.ACTION_STATE_CHANGED -> render()
                BluetoothDevice.ACTION_PAIRING_REQUEST -> if (device != null && device.address == bonding?.address) {
                    showPairingDialog(intent, device)
                }
                BluetoothDevice.ACTION_BOND_STATE_CHANGED -> if (device != null && device.address == bonding?.address) {
                    when (intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.ERROR)) {
                        BluetoothDevice.BOND_BONDED -> chosen(device)
                        BluetoothDevice.BOND_NONE -> { bonding = null; message = getString(R.string.pair_failed); messageLight = Light.BAD; render() }
                    }
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = Ui(this)
        registerReceiver(
            receiver,
            IntentFilter().apply {
                addAction(BluetoothDevice.ACTION_FOUND)
                addAction(BluetoothAdapter.ACTION_DISCOVERY_FINISHED)
                addAction(BluetoothAdapter.ACTION_STATE_CHANGED)
                addAction(BluetoothDevice.ACTION_BOND_STATE_CHANGED)
                // The system asks this device to confirm the six digits here. This ROM's own receiver decides the
                // dialog is not worth showing (LocalBluetoothPreferences, 2026-11-03), so the request is forwarded to
                // that dialog explicitly instead of relying on the decision.
                addAction(BluetoothDevice.ACTION_PAIRING_REQUEST)
            },
        )
        if (hasPermissions()) scan() else requestPermissions()
        render()
    }

    override fun onDestroy() {
        runCatching { unregisterReceiver(receiver) }
        runCatching { adapter?.cancelDiscovery() }
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    private fun neededPermissions(): List<String> = when {
        Build.VERSION.SDK_INT >= 31 -> listOf("android.permission.BLUETOOTH_SCAN", "android.permission.BLUETOOTH_CONNECT")
        Build.VERSION.SDK_INT >= 23 -> listOf("android.permission.ACCESS_FINE_LOCATION")
        else -> emptyList()
    }

    private fun hasPermissions(): Boolean = Build.VERSION.SDK_INT < 23 ||
        neededPermissions().all { checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }

    private fun requestPermissions() {
        if (Build.VERSION.SDK_INT >= 23) requestPermissions(neededPermissions().toTypedArray(), REQUEST_PERMISSIONS)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (hasPermissions()) scan() else { message = getString(R.string.pair_permission); messageLight = Light.BAD }
        render()
    }

    private fun scan() {
        val adapter = adapter ?: return
        if (!hasPermissions() || !runCatching { adapter.isEnabled }.getOrDefault(false)) return
        found.clear()
        message = null
        scanning = runCatching { adapter.cancelDiscovery(); adapter.startDiscovery() }.getOrDefault(false)
        render()
    }

    private fun pair(device: BluetoothDevice) {
        if (!hasPermissions()) {
            message = getString(R.string.pair_permission); messageLight = Light.BAD
            render(); return
        }
        runCatching { adapter?.cancelDiscovery() }
        if (runCatching { device.bondState }.getOrNull() == BluetoothDevice.BOND_BONDED) { chosen(device); return }
        bonding = device
        message = getString(R.string.pair_bonding, safeName(device) ?: device.address)
        messageLight = Light.WAIT
        if (runCatching { device.createBond() }.getOrDefault(false) != true) {
            bonding = null
            message = getString(R.string.pair_failed)
            messageLight = Light.BAD
        }
        render()
    }

    /** Opens the system's pairing dialog by component.
     *
     *  On this ROM that dialog lives in the vendor settings app and is only started when that app considers itself
     *  foreground; otherwise pairing silently times out after 30 s (2026-11-03). Forwarding the request with the
     *  same extras it expects gets the dialog on screen, and that app is the one allowed to answer it.
     */
    private fun showPairingDialog(request: Intent, device: BluetoothDevice) {
        val dialog = Intent(request).apply {
            component = android.content.ComponentName(
                "com.android.smart.terminal.settings",
                "com.example.blueetoothmanager.bluetooth.lib.BluetoothPairingDialog",
            )
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        runCatching { startActivity(dialog) }
            .onFailure { message = "配对确认界面打不开：${it.javaClass.simpleName}"; messageLight = Light.BAD; render() }
    }

    private fun chosen(device: BluetoothDevice) {
        val name = safeName(device) ?: "iPhone"
        try { AndroidPhones.get(this).add(device.address, name) }
        catch (_: IllegalStateException) {
            bonding = null; message = "手机记录保存失败，请重试。"; messageLight = Light.BAD; render(); return
        }
        Settings(this).apply { iphoneAddress = device.address; iphoneName = name }
        bonding = null
        message = null
        messageLight = Light.OK
        render()
        // With an iPhone named, the receiver invites it right away.
        DashPlayService.restart(this)
        // Was an automatic finish 1.5 s later: on a TV that reads as "it jumped back to the launcher" and the
        // user never learns whether anything was saved. Keep the result on screen, exit through 「完成」.
    }

    private fun safeName(device: BluetoothDevice): String? = runCatching { device.name }.getOrNull()

    private fun looksLikeIphone(device: BluetoothDevice): Boolean =
        safeName(device)?.contains("iPhone", ignoreCase = true) == true ||
            runCatching { device.bluetoothClass?.majorDeviceClass == android.bluetooth.BluetoothClass.Device.Major.PHONE }.getOrDefault(false)

    private fun render() {
        val root = ui.stack()
        root.addView(ui.text(R.style.Text_Title, getString(R.string.pair_title)))
        val steps = listOf(R.string.pair_step1, R.string.pair_step2, R.string.pair_step3, R.string.pair_step4)
        val stepsView: View = if (ui.tv) {
            // TV: the four steps side by side, readable from the sofa.
            ui.row().apply {
                steps.forEachIndexed { i, id ->
                    addView(stepCard(i + 1, getString(id)), LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.MATCH_PARENT, 1f).apply { if (i > 0) marginStart = ui.dimen(R.dimen.gap) })
                }
            }
        } else {
            ui.card().apply {
                val pad = ui.dimen(R.dimen.pad)
                setPadding(pad, pad, pad, pad)
                steps.forEachIndexed { i, id -> addView(ui.text(R.style.Text_Name, "${i + 1}. ${getString(id)}").apply { setPadding(0, if (i > 0) ui.dp(8) else 0, 0, 0) }) }
            }
        }
        root.addView(stepsView, ui.matchWidth())

        // Pairing is only half of it: the iPhone must also be on the network the box is on, or CarPlay never starts.
        val wifiName = Settings(this).connectionWifi()?.ssid?.takeIf { it.isNotBlank() } ?: CurrentWifi.ssid(this)
        if (wifiName != null) {
            root.addView(ui.card().apply {
                val pad = ui.dimen(R.dimen.pad)
                setPadding(pad, pad, pad, pad)
                addView(ui.text(R.style.Text_Small, getString(R.string.pair_need_same_wifi, wifiName)))
            }, ui.matchWidth())
        }

        val problem = WirelessReadiness.problem(this)
        if (problem != null) {
            root.addView(ui.text(R.style.Text_Name, problem))
            root.addView(ui.button(R.style.Button_Primary, "设置无线网络") { wifiSettings() }, ui.matchWidth())
            if (foreground && problem != shownProblem) {
                shownProblem = problem
                android.app.AlertDialog.Builder(this).setTitle("需要确认无线网络").setMessage(problem)
                    .setPositiveButton("去设置") { _, _ -> wifiSettings() }.setNegativeButton("稍后", null).show()
            }
        } else shownProblem = null
        val adapter = adapter
        when {
            adapter == null -> root.addView(ui.list(listOf(ui.statusRow(getString(R.string.row_iphone), (if (Settings(this).wiredCarPlaySupported) getString(R.string.pair_no_bt) else ConnectionSupport.PHONE_NO_BLUETOOTH), Light.BAD))), ui.matchWidth())
            !hasPermissions() -> {
                root.addView(ui.text(R.style.Text_Name, getString(R.string.pair_permission)))
                root.addView(ui.button(R.style.Button_Primary, "允许蓝牙权限") { requestPermissions() })
            }
            !runCatching { adapter.isEnabled }.getOrDefault(false) -> {
                root.addView(ui.list(listOf(ui.statusRow(getString(R.string.row_iphone), getString(R.string.pair_bt_off), Light.BAD))), ui.matchWidth())
                root.addView(ui.button(R.style.Button_Primary, getString(R.string.pair_turn_on)) {
                    runCatching { startActivity(Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE)) }
                }, ui.matchWidth())
            }
            else -> {
                // One line that answers "connected or not", instead of "已添加 …" above "已配对，还没连上" (2026-11-03).
                val state = message?.let { it to messageLight } ?: connectionState()
                state?.let { root.addView(ui.list(listOf(ui.statusRow(getString(R.string.row_iphone), it.first, it.second))), ui.matchWidth()) }
                val bonded = runCatching { adapter.bondedDevices.orEmpty() }.getOrDefault(emptySet()).filter(::looksLikeIphone)
                // Only devices that look like a phone: a list that also offers printers and sensors invites a
                // wrong tap, which then sat in the phone policy for good (2026-11-03).
                val nearby = found.values.filter { d -> bonded.none { it.address == d.address } && looksLikeIphone(d) }
                    .sortedByDescending(::looksLikeIphone)
                (bonded + nearby).forEach { device ->
                    val already = bonded.any { it.address == device.address }
                    val record = AndroidPhones.get(this).record(device.address)
                    val selected = record?.removed != true && Settings(this).iphoneAddress == device.address
                    val detail = when {
                        record?.removed == true -> "已从 DashPlay 移除；系统配对仍保留，点击重新添加。"
                        record?.paused == true -> "已暂停自动连接，点击重新启用。"
                        selected -> connectionState()?.first ?: "已添加"
                        already -> "已完成系统配对，点击添加到 DashPlay。"
                        else -> device.address
                    }
                    root.addView(ui.choice(displayName(device), detail, selected) { pair(device) }.apply { tag = device.address }, ui.matchWidth())
                }
                when {
                    scanning -> root.addView(ui.text(R.style.Text_Small, getString(R.string.pair_scanning)))
                    found.isEmpty() && hasPermissions() && message == null -> root.addView(ui.text(R.style.Text_Small, getString(R.string.pair_none)))
                }
                root.addView(ui.button(R.style.Button_Secondary, getString(R.string.pair_scan_again)) { if (hasPermissions()) scan() else requestPermissions() }, ui.matchWidth())
            }
        }
        root.addView(ui.button(R.style.Button_Text, getString(R.string.pair_system)) {
            runCatching { startActivity(Intent(android.provider.Settings.ACTION_BLUETOOTH_SETTINGS)) }
        }, ui.matchWidth())
        root.addView(ui.button(R.style.Button_Text, (if (Settings(this).wiredCarPlaySupported) getString(R.string.pair_skip) else "稍后配对")) { finish() }, ui.matchWidth())
        root.addView(ui.button(R.style.Button_Primary, getString(R.string.pair_done_button)) {
            startActivity(Intent(this, SettingsActivity::class.java))
            finish()
        }, ui.matchWidth())

        // Found devices re-render the list: keep the remote where it was, and always leave it on a device row so the
        // D-pad cannot get stranded on a status line after a failed pairing (2026-11-03).
        val focusedTag = currentFocus?.tag
        setContentView(ui.page(root))
        val keep = focusedTag?.let { root.findViewWithTag<View>(it) }
        (keep ?: firstChoice(root) ?: firstFocusable(root))?.requestFocus()
    }

    /** Pairing and connecting are different things; say which one this device is actually in. */
    private fun connectionState(): Pair<String, Light>? {
        val settings = Settings(this)
        val address = settings.iphoneAddress ?: return null
        val record = AndroidPhones.get(this).record(address)
        if (record?.removed == true) return null
        if (record?.paused == true) return "已暂停自动连接，点击手机重新启用。" to Light.WAIT
        WirelessReadiness.problem(this)?.let { return it to Light.BAD }
        val progress = (DashPlayService.activeReceiver()?.bluetoothLink as? AndroidBluetoothLink)?.progress
            ?.takeIf { it.address.equals(address, ignoreCase = true) }
        val active = runCatching {
            val phones = AndroidPhones.get(this).describe().optJSONArray("phones") ?: return@runCatching false
            (0 until phones.length()).any { i ->
                val phone = phones.getJSONObject(i)
                phone.optString("address").equals(address, ignoreCase = true) && phone.optBoolean("active")
            }
        }.getOrDefault(false)
        val bonded = runCatching {
            adapter?.getRemoteDevice(address)?.bondState == BluetoothDevice.BOND_BONDED
        }.getOrDefault(false)
        val name = settings.iphoneName ?: address
        return when {
            active -> getString(R.string.pair_state_connected, name) to Light.OK
            bonded -> when (progress?.phase) {
                "BLUETOOTH" -> "正在通过蓝牙连接 $name，无需再次配对。"
                "AUTHENTICATING" -> "正在验证 $name 的 CarPlay 连接。若 iPhone 提示允许 CarPlay，请允许。"
                "WIFI_HANDOFF" -> if (android.os.SystemClock.elapsedRealtime() - progress.since < 45_000)
                    "已完成验证，正在通过 Wi-Fi 建立 CarPlay，无需再次配对。"
                    else "Wi-Fi 连接尚未完成。请检查无线网络名称、密码，以及路由器是否允许设备互访。"
                "FAILED" -> "本次连接未完成，正在自动重试。请确认 iPhone 蓝牙和 Wi-Fi 已开启，并允许 CarPlay。"
                else -> "已完成系统配对，正在自动连接 $name，无需再次配对。"
            } to Light.WAIT
            else -> getString(R.string.pair_state_unpaired, name) to Light.BAD
        }
    }

    private fun firstFocusable(root: LinearLayout): View? = (0 until root.childCount).map(root::getChildAt).firstOrNull { it.isFocusable && it.isEnabled }

    private fun firstChoice(root: LinearLayout): View? = (0 until root.childCount).map(root::getChildAt)
        .firstOrNull { it.isFocusable && it.isEnabled && it.tag is String }

    /** Advertised names are unreliable before pairing; prefer the most specific one seen for this address. */
    private fun displayName(device: BluetoothDevice): String {
        val remembered = names[device.address] ?: Settings(this).takeIf { it.iphoneAddress == device.address }?.iphoneName
        val advertised = safeName(device)
        return listOfNotNull(remembered, advertised).maxByOrNull(::score) ?: device.address
    }

    /** Longer, more specific names win over the generic "iPhone" an unpaired phone may advertise. */
    private fun score(name: String): Int = when {
        name.equals("iPhone", ignoreCase = true) -> 1
        name.contains("iPhone", ignoreCase = true) -> 2
        else -> 1
    }

    /** Coalesces the burst of ACTION_FOUND callbacks into one re-render so the list does not thrash under the D-pad. */
    private fun later() {
        if (renderPosted) return
        renderPosted = true
        handler.postDelayed({ renderPosted = false; render() }, 400)
    }

    private fun stepCard(number: Int, body: String): LinearLayout = ui.card().apply {
        val pad = ui.dimen(R.dimen.pad)
        setPadding(pad, pad, pad, pad)
        addView(ui.text(R.style.Text_Small, getString(R.string.pair_step_label, number)).apply { setTextColor(ui.color(R.color.blue_text)) })
        addView(ui.text(R.style.Text_Name, body).apply { setPadding(0, ui.dp(8), 0, 0) })
    }

    private companion object {
        const val REQUEST_PERMISSIONS = 4
    }
}
