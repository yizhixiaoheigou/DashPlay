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
        val managed = Build.VERSION.SDK_INT >= 29 && pm.hasSystemFeature(PackageManager.FEATURE_WIFI_DIRECT)
        val hotspotPermission = managed && context.checkSelfPermission(
            if (Build.VERSION.SDK_INT >= 33) Manifest.permission.NEARBY_WIFI_DEVICES else Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        val appHotspotObserved = DashPlayService.managedHotspot?.connection != null
        val appHotspotMessage = when {
            !managed -> "不满足应用自动热点条件；系统热点需另外检查"
            !hotspotPermission -> "等待附近设备或位置权限；请在本地热点设置中授权"
            appHotspotObserved -> "应用热点已建立；客户端接入和显示画面仍需验证"
            else -> "基础条件与权限已满足；尚未观察到应用热点建组"
        }
        val systemEntry = Intents.hasHotspotSettings(context)
        val systemHotspotMessage = when {
            network?.kind == LocalNetwork.Kind.HOTSPOT && !appHotspotObserved ->
                "当前已选中热点网络；客户端接入及开机恢复仍需验证"
            systemEntry -> "检测到系统设置入口；开启、客户端接入及开机恢复仍需验证"
            else -> "未检测到专用设置入口，不代表系统热点服务不可用；可使用外部网络"
        }
        val http = HttpSetupStatus.read(context)
        return JSONObject()
            .put("compatHttpSupported", true)
            .put("compatHttpReady", http.ready)
            .put("compatHttpMessage", http.message)
            .put("compatHttpClientVerified", false) // No persisted client-side proof; local readiness is not proof.
            .put("android", Build.VERSION.RELEASE).put("api", Build.VERSION.SDK_INT)
            .put("model", Build.MODEL)
            .put("managedHotspot", managed)
            .put("managedHotspotPermission", hotspotPermission)
            .put("managedHotspotObserved", appHotspotObserved)
            .put("managedHotspotMessage", appHotspotMessage)
            .put("systemHotspotSettings", systemEntry)
            .put("systemHotspotMessage", systemHotspotMessage)
            .put("wifi", pm.hasSystemFeature(PackageManager.FEATURE_WIFI))
            .put("bluetooth", Settings(context).bluetoothSupported).put("bluetoothPermission", bluetoothPermission)
            .put("bluetoothEnabled", bluetoothPermission && runCatching { adapter?.isEnabled == true }.getOrDefault(false))
            .put("usbHost", pm.hasSystemFeature(PackageManager.FEATURE_USB_HOST))
            .put("usbDevices", usb?.deviceList?.size ?: 0)
            .put("networkKind", network?.kind?.name ?: "NONE")
            .put("address", network?.address?.hostAddress ?: "")
            .put("interface", network?.name ?: "")
            // available: the app may execute su at all; some ROMs ship it as -rwsr-x--- root shell (2026-11-03).
            .put("rootUsable", RootShell.available())
            .put("root", if (Settings(context).teslaRootAccess || TeslaRootAccess.phase != "OFF") TeslaRootAccess.phase else "not_requested")
            .put("hotspot", if (network?.kind == LocalNetwork.Kind.HOTSPOT) "observed" else "not_observed")
    }
}
