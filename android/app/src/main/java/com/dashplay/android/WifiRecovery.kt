package com.dashplay.android

import android.content.Context
import android.net.wifi.SupplicantState
import android.net.wifi.WifiManager
import android.os.Build
import android.os.SystemClock

/** Recover a previously usable external Wi-Fi only when the OS explicitly reports temporary association rejection.
 * No Wi-Fi toggles, credential writes, network creation, forced root or manual-disable override.
 * Android 10+ restricts these APIs; unavailable observations fail closed and leave recovery to system settings.
 */
@Suppress("DEPRECATION")
internal class WifiRecovery(context: Context) {
    private val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
    private val prefs = context.getSharedPreferences("wifi_recovery", Context.MODE_PRIVATE)
    private var targetId = prefs.getInt("network_id", -1)
    private var targetSsid = prefs.getString("ssid", null)
    private var policy = WifiRecoveryPolicy()
    private var nextPoll = 0L
    var message: String? = null; private set

    fun maintain(externalNetwork: Boolean, network: LocalNetwork.Choice?) {
        val now = SystemClock.elapsedRealtime()
        if (now < nextPoll) return
        nextPoll = now + 5_000
        if (!externalNetwork || wifi == null) { message = null; policy.retry(now, false); return }
        try {
            if (!wifi.isWifiEnabled) { message = "Wi-Fi 已关闭；请在系统设置中开启，应用不会自行打开。"; policy.retry(now,false); return }
            val info = wifi.connectionInfo
            if (network?.kind == LocalNetwork.Kind.WIFI && info?.supplicantState == SupplicantState.COMPLETED) {
                val ssid = info.ssid?.removeSurrounding("\"")
                if (info.networkId >= 0 && !ssid.isNullOrBlank() && ssid != "<unknown ssid>" && ssid != "0x") {
                    if (targetId != info.networkId || targetSsid != ssid) {
                        targetId = info.networkId; targetSsid = ssid; policy = WifiRecoveryPolicy()
                        prefs.edit().putInt("network_id",targetId).putString("ssid",ssid).apply()
                    }
                }
                policy.connected(now); message = null; return
            }
            // Ethernet/AP or a user's new Wi-Fi selection wins. Do not steal a connection back.
            if (network != null || (info != null && info.networkId >= 0 && info.networkId != targetId)) {
                message = null; policy.retry(now,false); return
            }
            if (targetId < 0 || targetSsid == null) { message = null; policy.retry(now,false); return }
            if (Build.VERSION.SDK_INT >= 29) {
                message = "Wi-Fi 尚未恢复；此 Android 版本限制应用重连已保存网络，请在系统 Wi-Fi 设置中重新连接。"
                policy.retry(now,false); return
            }
            val saved = wifi.configuredNetworks?.firstOrNull {
                it.networkId == targetId && it.SSID?.removeSurrounding("\"") == targetSsid
            }
            if (saved == null) { message = "原 Wi-Fi 未连接或其配置无法读取，请在系统 Wi-Fi 设置中检查。"; policy.retry(now,false); return }
            val rejected = WifiFailureEvidence.temporaryAssociationRejected(saved)
            if (rejected != true) {
                message = "等待系统恢复 Wi-Fi；应用不会重启无线或覆盖手动断开。若一直未恢复，请在系统设置中重新连接。"
                policy.retry(now,false); return
            }
            message = if (policy.attempts >= 3) "Wi-Fi 自动恢复已尝试 3 次仍未连接，请检查热点并在系统设置中重新连接。"
                else "原 Wi-Fi 因关联失败被系统临时停用，正在等待有限次数的自动恢复。"
            if (policy.retry(now,true)) {
                // false keeps other networks enabled; reconnect is requested only after clearing a known temporary state.
                val enabled = wifi.enableNetwork(targetId, false)
                val requested = enabled && wifi.reconnect()
                DriveLog.event("wifi recovery attempt=${policy.attempts} enabled=$enabled requested=$requested")
                if (!requested) message = "系统未接受 Wi-Fi 恢复请求，请在系统 Wi-Fi 设置中重新连接。"
            }
        } catch (_: SecurityException) {
            policy.retry(now,false)
            message = "应用无法读取或恢复 Wi-Fi；请在系统 Wi-Fi 设置中检查连接。"
        } catch (error: Exception) {
            policy.retry(now,false)
            message = "此系统暂不支持应用自动恢复 Wi-Fi，请在系统设置中重新连接。"
        }
    }

}
