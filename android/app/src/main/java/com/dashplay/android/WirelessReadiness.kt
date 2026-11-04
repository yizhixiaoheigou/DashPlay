package com.dashplay.android

import android.content.Context

/** Read-only preflight shared by the invitation path and every settings/status surface. */
internal object WirelessReadiness {
    fun problem(context: Context, expectedSsid: String? = null): String? {
        val settings = Settings(context)
        if (!settings.plan().wireless || settings.network != Network.MIFI) return null
        val ssid = settings.uplink()?.ssid
        if (expectedSsid != null && expectedSsid != ssid) return "无线网络设置正在更新，接收服务将重新连接。"
        return ExternalWifiPolicy.problem(settings.externalWifiChoice, ssid, CurrentWifi.ssid(context),
            LocalNetwork.choose(context)?.kind == LocalNetwork.Kind.WIFI)
    }
}
