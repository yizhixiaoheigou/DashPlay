package com.dashplay.android

import android.bluetooth.BluetoothAdapter
import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import com.playport.server.BluetoothLink
import com.shilapi.xcertplay.transport.BlockingDuplexByteStream
import com.shilapi.xcertplay.transport.BluetoothRfcommDuplexStream
import java.io.IOException
import java.util.UUID

/**
 * The receiver's Bluetooth side on Android: the iPhone is paired in the system's Bluetooth settings (like a car),
 * and this connects to its iAP2 service over RFCOMM, as DiPlay does.
 */
class AndroidBluetoothLink(private val context: Context, private val preferredAddress: String?, private val expectedSsid: String? = null) : BluetoothLink {
    data class Progress(val address: String, val phase: String, val since: Long)
    @Volatile var progress: Progress? = null
        private set
    override fun connectionProgress(deviceAddress: String, phase: String) {
        progress = Progress(deviceAddress, phase, android.os.SystemClock.elapsedRealtime())
    }
    private val adapter: BluetoothAdapter? get() = BluetoothAdapter.getDefaultAdapter()

    @Suppress("HardwareIds")
    override fun localAddress(): String? {
        // Android 6.0 and later answer 02:00:00:00:00:00 to apps; some versions still keep the real one in settings.
        val candidates = listOfNotNull(
            runCatching { adapter?.address }.getOrNull(),
            runCatching { Settings.Secure.getString(context.contentResolver, "bluetooth_address") }.getOrNull(),
        )
        return candidates.firstOrNull { MAC.matches(it) && !it.equals(HIDDEN, ignoreCase = true) }
    }

    override fun pairedIphone(): String? = AndroidPhones.get(context).candidate()

    override fun open(deviceAddress: String): BlockingDuplexByteStream {
        WirelessReadiness.problem(context, expectedSsid)?.let { throw IOException(it) }
        if (Build.VERSION.SDK_INT >= 31 &&
            context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED)
            throw IOException("Bluetooth connection permission is required")
        if (!AndroidPhones.get(context).canInvite(deviceAddress)) throw IOException("Phone invitations are paused")
        try {
            return connect(deviceAddress)
        } catch (error: SecurityException) {
            // Permission may be revoked after the check, including while connect() is blocked.
            throw IOException("Bluetooth permission was revoked", error)
        }
    }

    private fun connect(deviceAddress: String): BlockingDuplexByteStream {
        val adapter = adapter ?: throw IOException("no Bluetooth adapter")
        var socket: android.bluetooth.BluetoothSocket? = null
        try {
            if (!adapter.isEnabled) throw IOException("Bluetooth is off")
            try { adapter.cancelDiscovery() } catch (_: SecurityException) { /* Scan permission is separate. */ }
            val connected = adapter.getRemoteDevice(deviceAddress).createRfcommSocketToServiceRecord(IAP2_IPHONE)
            socket = connected
            connected.connect()
            return BluetoothRfcommDuplexStream(connected)
        } catch (error: SecurityException) {
            runCatching { socket?.close() }
            throw IOException("Bluetooth permission was revoked", error)
        } catch (error: Exception) {
            runCatching { socket?.close() }
            throw error
        }
    }

    private companion object {
        const val HIDDEN = "02:00:00:00:00:00"
        val MAC = Regex("^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
        /** The iPhone's iAP2 RFCOMM service. */
        val IAP2_IPHONE: UUID = UUID.fromString("00000000-deca-fade-deca-deafdecacafe")
    }
}
