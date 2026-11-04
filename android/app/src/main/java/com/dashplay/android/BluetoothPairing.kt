package com.dashplay.android

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.content.Context
import android.util.Log

/** Pairs an iPhone from the management page, which only works on a device where this app may use Root.
 *
 *  Android asks the person at this device to confirm the six digits in a system dialog; a browser on another
 *  machine cannot answer it. With Root the dialog can be answered from here (`input` needs shell/root), which is
 *  what makes pairing possible from a car's browser. Without Root the caller must say so instead of pretending.
 */
internal object BluetoothPairing {
    private const val TAG = "DashPlayPairing"

    /** Starts pairing [address] and answers the dialog in the background. */
    fun begin(context: Context, address: String): Boolean {
        val autoConfirm = RootShell.available()
        val adapter = BluetoothAdapter.getDefaultAdapter() ?: return false
        if (!adapter.isEnabled) return false
        val device: BluetoothDevice = runCatching { adapter.getRemoteDevice(address) }.getOrNull() ?: return false
        if (runCatching { device.bondState }.getOrDefault(BluetoothDevice.BOND_NONE) == BluetoothDevice.BOND_BONDED) return true
        if (runCatching { device.createBond() }.getOrDefault(false) != true) return false
        // Without Root the system dialog has to be answered on this device: Android only shows it when the
        // settings app is in the foreground (LocalBluetoothPreferences, 2026-11-03).
        if (autoConfirm) Thread({ confirmDialog(device) }, "dashplay-pair-confirm").apply { isDaemon = true }.start()
        return true
    }

    /** Watches for the system pairing dialog for a short while and taps its confirm button through Root. */
    private fun confirmDialog(device: BluetoothDevice) {
        val end = System.currentTimeMillis() + 30_000
        while (System.currentTimeMillis() < end) {
            if (runCatching { device.bondState }.getOrDefault(0) == BluetoothDevice.BOND_BONDED) return
            val xml = runCatching { su("uiautomator dump /data/local/tmp/pair-dialog.xml >/dev/null 2>&1; cat /data/local/tmp/pair-dialog.xml") }
                .getOrDefault("")
            if (xml.contains("配对") || xml.contains("Pair")) {
                val button = Regex("<node[^>]*text=\"(配对|确定|Pair)[^\"]*\"[^>]*bounds=\"\\[(\\d+),(\\d+)\\]\\[(\\d+),(\\d+)\\]\"")
                    .findAll(xml).lastOrNull()
                if (button != null) {
                    val x = (button.groupValues[2].toInt() + button.groupValues[4].toInt()) / 2
                    val y = (button.groupValues[3].toInt() + button.groupValues[5].toInt()) / 2
                    Log.i(TAG, "confirming pairing at $x,$y")
                    su("input tap $x $y")
                    Thread.sleep(2_000)
                }
            }
            Thread.sleep(700)
        }
    }

    private fun su(script: String): String = runCatching {
        val process = RootShell.open(script)
        val text = process.inputStream.bufferedReader().readText()
        process.waitFor()
        text
    }.getOrDefault("")
}
