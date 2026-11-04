package com.dashplay.android

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.wifi.WifiManager
import android.net.wifi.SupplicantState
import android.net.wifi.p2p.WifiP2pConfig
import android.net.wifi.p2p.WifiP2pGroup
import android.net.wifi.p2p.WifiP2pManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import java.net.Inet4Address
import java.net.NetworkInterface

/** Autonomous Wi-Fi Direct group. Matching saved credentials identify our group across process restarts. */
class ManagedHotspot(private val context: Context, private val changed: () -> Unit) {
    data class Connection(val ssid: String, val password: String, val channel: Int, val network: LocalNetwork.Choice)
    enum class Phase { OFF, STARTING, READY, FAILED, STOPPING }
    private val handler = Handler(Looper.getMainLooper())
    @Volatile private var controller: HotspotController<Connection>? = null
    val connection: Connection? get() = controller?.connection
    val phase: Phase get() = controller?.phase?.let { Phase.valueOf(it.name) } ?: Phase.OFF
    val message: String get() = controller?.message.orEmpty()
    private var disposed = false

    fun configure(wanted: Boolean, retry: Boolean = false) {
        check(Looper.myLooper() == Looper.getMainLooper())
        if (disposed) return
        if (controller == null) {
            val settings = Settings(context)
            // Never initialize Wi-Fi Direct or create credentials on the ordinary system-hotspot path.
            val credentials = if (wanted) settings.localHotspotCredentials() else settings.savedLocalHotspotCredentials()
            if (credentials == null) return
            controller = HotspotController(AndroidDriver(credentials), android.os.SystemClock::elapsedRealtime,
                { delay, action ->
                    val runnable = Runnable(action)
                    handler.postDelayed(runnable, delay)
                    HotspotController.Cancel { handler.removeCallbacks(runnable) }
                }, changed)
        }
        controller?.configure(wanted, retry)
    }
    fun close(done: () -> Unit = {}) {
        disposed = true
        val current = controller
        if (current == null) done() else current.close(done)
    }

    private inner class AndroidDriver(private val credentials: Settings.Hotspot) : HotspotController.Driver<Connection> {
        private var manager: WifiP2pManager? = null
        private var channel: WifiP2pManager.Channel? = null
        private var channelGeneration = 0L
        override fun unavailable(): String? {
            if (Build.VERSION.SDK_INT < 29 || !context.packageManager.hasSystemFeature(PackageManager.FEATURE_WIFI_DIRECT))
                return "此设备不支持应用本地热点，请使用系统热点或外部 Wi-Fi。"
            val permission = if (Build.VERSION.SDK_INT >= 33) Manifest.permission.NEARBY_WIFI_DEVICES else Manifest.permission.ACCESS_FINE_LOCATION
            if (context.checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED)
                return "请在本地热点设置中允许附近设备或位置信息权限。"
            val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            return if (!wifi.isWifiEnabled) "等待系统 Wi-Fi 开启；开启后会自动恢复本地热点。" else null
        }
        private fun open(): WifiP2pManager.Channel {
            channel?.let { return it }
            val service = (context.getSystemService(Context.WIFI_P2P_SERVICE) as? WifiP2pManager)
                ?: throw IllegalStateException("Wi-Fi Direct 不可用")
            manager = service
            val generation = ++channelGeneration
            return service.initialize(context, Looper.getMainLooper()) {
                if (generation == channelGeneration) {
                    channel = null
                    controller?.disconnected()
                }
            }.also { channel = it }
        }
        private fun owns(group: WifiP2pGroup?): Boolean = group != null &&
            HotspotPolicy.owns(group.isGroupOwner, group.networkName, group.passphrase, credentials.ssid, credentials.passphrase)

        override fun inspect(done: (Result<HotspotController.Group<Connection>?>) -> Unit) {
            val current = open()
            manager!!.requestGroupInfo(current) { group ->
                if (channel !== current) { done(Result.failure(IllegalStateException("Wi-Fi 服务已改变"))); return@requestGroupInfo }
                if (group == null) { done(Result.success(null)); return@requestGroupInfo }
                val owned = owns(group)
                val number = if (Build.VERSION.SDK_INT >= 29) HotspotPolicy.channel(group.frequency) else null
                val iface = group.getInterface()
                val address = if (owned) runCatching { NetworkInterface.getByName(iface)?.inetAddresses?.toList()
                    ?.firstOrNull { it is Inet4Address && !it.isLinkLocalAddress && !it.isLoopbackAddress } }.getOrNull() else null
                val ready = if (owned && number != null && address != null)
                    Connection(credentials.ssid, credentials.passphrase, number, LocalNetwork.Choice(iface, address, LocalNetwork.Kind.HOTSPOT)) else null
                done(Result.success(HotspotController.Group(owned, ready)))
            }
        }
        override fun create(done: (Result<Unit>) -> Unit) {
            if (Build.VERSION.SDK_INT < 29) {
                done(Result.failure(IllegalStateException("此设备不支持应用本地热点，请使用系统热点或外部 Wi-Fi。")))
                return
            }
            val current = open()
            val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION") val station = wifi.connectionInfo
            val frequency = station?.frequency?.takeIf { station.supplicantState == SupplicantState.COMPLETED && HotspotPolicy.channel(it) != null }
            val builder = WifiP2pConfig.Builder().setNetworkName(credentials.ssid).setPassphrase(credentials.passphrase)
            if (frequency != null) builder.setGroupOperatingFrequency(frequency)
            else builder.setGroupOperatingBand(if (wifi.is5GHzBandSupported) WifiP2pConfig.GROUP_OWNER_BAND_5GHZ else WifiP2pConfig.GROUP_OWNER_BAND_2GHZ)
            manager!!.createGroup(current, builder.build(), listener(done))
        }
        override fun remove(done: (Result<Unit>) -> Unit) {
            val current = open()
            // Recheck immediately before removal: a group observed earlier may have been replaced by another app.
            manager!!.requestGroupInfo(current) { group ->
                if (channel !== current) { done(Result.failure(IllegalStateException("Wi-Fi 服务已改变"))); return@requestGroupInfo }
                if (!owns(group)) { done(Result.success(Unit)); return@requestGroupInfo }
                try { manager!!.removeGroup(current, listener(done)) }
                catch (error: RuntimeException) { done(Result.failure(error)) }
            }
        }
        private fun listener(done: (Result<Unit>) -> Unit) = object : WifiP2pManager.ActionListener {
            override fun onSuccess() { done(Result.success(Unit)) }
            override fun onFailure(reason: Int) { done(Result.failure(IllegalStateException(when (reason) {
                WifiP2pManager.P2P_UNSUPPORTED -> "硬件不支持本地热点，请使用系统热点或外部 Wi-Fi。"
                WifiP2pManager.BUSY -> "无线网络正忙；关闭系统热点或投屏后会自动重试。"
                else -> "系统热点操作失败（$reason），将自动重试。"
            }))) }
        }
        override fun close() {
            channelGeneration++
            val old = channel; channel = null
            if (Build.VERSION.SDK_INT >= 27) runCatching { old?.close() }
        }
    }
}
