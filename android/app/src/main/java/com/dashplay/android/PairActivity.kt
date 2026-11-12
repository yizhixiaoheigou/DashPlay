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

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            @Suppress("DEPRECATION")
            val device = intent.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE)
            when (intent.action) {
                BluetoothDevice.ACTION_FOUND -> if (device != null && safeName(device) != null) { found[device.address] = device; render() }
                BluetoothAdapter.ACTION_DISCOVERY_FINISHED -> { scanning = false; render() }
                BluetoothAdapter.ACTION_STATE_CHANGED -> render()
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
        if (!hasPermissions() || !adapter.isEnabled) return
        found.clear()
        message = null
        scanning = runCatching { adapter.cancelDiscovery(); adapter.startDiscovery() }.getOrDefault(false)
        render()
    }

    private fun pair(device: BluetoothDevice) {
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

    private fun chosen(device: BluetoothDevice) {
        val name = safeName(device) ?: "iPhone"
        try { AndroidPhones.get(this).add(device.address, name) }
        catch (_: IllegalStateException) {
            bonding = null; message = "手机记录保存失败，请重试。"; messageLight = Light.BAD; render(); return
        }
        Settings(this).apply { iphoneAddress = device.address; iphoneName = name }
        bonding = null
        message = getString(R.string.pair_done, name)
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
        val wifiName = Settings(this).hotspot()?.ssid?.takeIf { it.isNotBlank() } ?: CurrentWifi.ssid(this)
        if (wifiName != null) {
            root.addView(ui.card().apply {
                val pad = ui.dimen(R.dimen.pad)
                setPadding(pad, pad, pad, pad)
                addView(ui.text(R.style.Text_Small, getString(R.string.pair_need_same_wifi, wifiName)))
            }, ui.matchWidth())
        }

        val adapter = adapter
        when {
            adapter == null -> root.addView(ui.list(listOf(ui.statusRow(getString(R.string.row_iphone), getString(R.string.pair_no_bt), Light.BAD))), ui.matchWidth())
            !hasPermissions() -> {
                root.addView(ui.text(R.style.Text_Name, getString(R.string.pair_permission)))
                root.addView(ui.button(R.style.Button_Primary, "允许蓝牙权限") { requestPermissions() })
            }
            !adapter.isEnabled -> {
                root.addView(ui.list(listOf(ui.statusRow(getString(R.string.row_iphone), getString(R.string.pair_bt_off), Light.BAD))), ui.matchWidth())
                root.addView(ui.button(R.style.Button_Primary, getString(R.string.pair_turn_on)) {
                    runCatching { startActivity(Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE)) }
                }, ui.matchWidth())
            }
            else -> {
                message?.let { root.addView(ui.list(listOf(ui.statusRow(getString(R.string.row_iphone), it, messageLight))), ui.matchWidth()) }
                val bonded = runCatching { adapter.bondedDevices.orEmpty() }.getOrDefault(emptySet()).filter(::looksLikeIphone)
                val nearby = found.values.filter { d -> bonded.none { it.address == d.address } }
                    .sortedByDescending(::looksLikeIphone)
                (bonded + nearby).forEach { device ->
                    val already = bonded.any { it.address == device.address }
                    root.addView(ui.choice(safeName(device) ?: device.address, if (already) getString(R.string.pair_paired_not_connected) else device.address, Settings(this).iphoneAddress == device.address) { pair(device) }.apply { tag = device.address }, ui.matchWidth())
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
        root.addView(ui.button(R.style.Button_Text, getString(R.string.pair_skip)) { finish() }, ui.matchWidth())
        root.addView(ui.button(R.style.Button_Primary, getString(R.string.pair_done_button)) {
            startActivity(Intent(this, SettingsActivity::class.java))
            finish()
        }, ui.matchWidth())

        // Found devices re-render the list: keep the remote where it was.
        val focusedTag = currentFocus?.tag
        setContentView(ui.page(root))
        (focusedTag?.let { root.findViewWithTag<View>(it) } ?: firstFocusable(root))?.requestFocus()
    }

    private fun firstFocusable(root: LinearLayout): View? = (0 until root.childCount).map(root::getChildAt).firstOrNull { it.isFocusable && it.isEnabled }

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
