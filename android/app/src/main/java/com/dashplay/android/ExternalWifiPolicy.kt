package com.dashplay.android

/** The user's external Wi-Fi choice, confirmed before any wireless invitation. */
@kotlinx.serialization.Serializable
enum class ExternalWifiChoice { CURRENT, OTHER }

internal object ExternalWifiPolicy {
    const val BAND_HINT = "建议使用 5 GHz Wi-Fi，以获得更好的画面流畅度；这里指 Wi-Fi 频段，不是手机的 5G 移动网络。"
    fun password(previousSsid: String?, previousPassword: String?, nextSsid: String, entered: String?): String =
        entered ?: if (previousSsid == nextSsid) previousPassword.orEmpty() else ""

    fun problem(choice: ExternalWifiChoice?, saved: String?, current: String?, wifiClient: Boolean): String? = when {
        choice == null -> "请先选择无线 CarPlay 使用当前 Wi-Fi 还是其他 Wi-Fi，并确认网络名称和密码。"
        saved.isNullOrBlank() -> "尚未填写 iPhone 要连接的 Wi-Fi，请完成无线网络设置。"
        wifiClient && current != null && saved != current ->
            "安卓当前连接「$current」，CarPlay 配置为「$saved」。请修改无线网络设置，或先让安卓连接「$saved」；当前不会发送连接邀请。"
        else -> null
    }
}
