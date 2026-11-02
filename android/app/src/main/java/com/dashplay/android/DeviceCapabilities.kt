package com.dashplay.android

import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.usb.UsbManager
import android.os.Build
import org.json.JSONObject

/** Observations only. A Wi-Fi chip or su binary is not proof that AP/root networking works. */
object DeviceCapabilities {
    fun describe(context: Context): JSONObject {
        val pm = context.packageManager
        val bluetoothPermission = Build.VERSION.SDK_INT < 31 ||
            context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED
        val adapter = BluetoothAdapter.getDefaultAdapter()
        val network = if (Settings(context).usesLocalHotspot) DashPlayService.managedHotspot?.connection?.network else LocalNetwork.choose(context)
        val usb = context.getSystemService(Context.USB_SERVICE) as? UsbManager
        return JSONObject()
            .put("android", Build.VERSION.RELEASE).put("api", Build.VERSION.SDK_INT)
            .put("model", Build.MODEL)
            .put("managedHotspot", Build.VERSION.SDK_INT >= 29 && pm.hasSystemFeature(PackageManager.FEATURE_WIFI_DIRECT))
            .put("wifi", pm.hasSystemFeature(PackageManager.FEATURE_WIFI))
            .put("bluetooth", adapter != null).put("bluetoothPermission", bluetoothPermission)
            .put("bluetoothEnabled", bluetoothPermission && runCatching { adapter?.isEnabled == true }.getOrDefault(false))
            .put("usbHost", pm.hasSystemFeature(PackageManager.FEATURE_USB_HOST))
            .put("usbDevices", usb?.deviceList?.size ?: 0)
            .put("networkKind", network?.kind?.name ?: "NONE")
            .put("address", network?.address?.hostAddress ?: "")
            .put("interface", network?.name ?: "")
            .put("root", if (Settings(context).teslaRootAccess || TeslaRootAccess.phase != "OFF") TeslaRootAccess.phase else "not_requested")
            .put("hotspot", if (network?.kind == LocalNetwork.Kind.HOTSPOT) "observed" else "not_observed")
    }
}
