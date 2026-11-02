package com.dashplay.android

import android.bluetooth.BluetoothAdapter
import android.content.Context
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
class AndroidBluetoothLink(private val context: Context, private val preferredAddress: String?) : BluetoothLink {
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
        if (!AndroidPhones.get(context).canInvite(deviceAddress)) throw IOException("Phone invitations are paused")
        val adapter = adapter ?: throw IOException("no Bluetooth adapter")
        if (!adapter.isEnabled) throw IOException("Bluetooth is off")
        runCatching { adapter.cancelDiscovery() }
        val socket = adapter.getRemoteDevice(deviceAddress).createRfcommSocketToServiceRecord(IAP2_IPHONE)
        try {
            socket.connect()
        } catch (error: IOException) {
            runCatching { socket.close() }
            throw error
        }
        return BluetoothRfcommDuplexStream(socket)
    }

    private companion object {
        const val HIDDEN = "02:00:00:00:00:00"
        val MAC = Regex("^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
        /** The iPhone's iAP2 RFCOMM service. */
        val IAP2_IPHONE: UUID = UUID.fromString("00000000-deca-fade-deca-deafdecacafe")
    }
}
