package com.dashplay.android

import kotlinx.serialization.Serializable

/** Partial update: omitted fields are retained. Validation finishes before any preference is written. */
@Serializable
data class ManagementSettings(
    val deviceName: String? = null,
    val deviceKind: DeviceKind? = null,
    val autoHotspot: Boolean? = null,
    val usbInternet: Boolean? = null,
    val usbHotspotShare: Boolean? = null,
    val car: Car? = null,
    val network: Network? = null,
    val iphoneLink: IphoneLink? = null,
    val ssid: String? = null,
    val passphrase: String? = null,
    /** External network the box joins (router / pocket Wi-Fi / car hotspot); never the box's own hotspot. */
    val uplinkSsid: String? = null,
    val externalWifiChoice: ExternalWifiChoice? = null,
    val uplinkPassphrase: String? = null,
    val wifiSecurity: WifiSecurity? = null,
    val wifiChannel: Int? = null,
    val audioOutsideCarPlay: Boolean? = null,
    val startAtBoot: Boolean? = null,
    val idleReleaseSeconds: Int? = null,
    val setupDone: Boolean? = null,
    val ownNameOnHotspot: Boolean? = null,
    val updateCheck: Boolean? = null,
    val updateMirror: String? = null,
    /** The plain-HTTP compatibility entry (no management, JPEG/RTC picture only). */
    val compatHttp: Boolean? = null,
    val browserPort: Int? = null,
    /** Legacy alias, accepted for older clients. */
    val compatHttpPort: Int? = null,
    val videoTransport: VideoTransport? = null,
)

enum class WifiSecurity { OPEN, WPA2, WPA3 }

object SettingValidation {
    fun name(value: String) {
        require(value.isNotBlank() && value.toByteArray(Charsets.UTF_8).size <= 32 && value.none { it.isISOControl() }) {
            "设备名称需为 1–32 字节，不能包含控制字符。"
        }
    }

    fun wifi(ssid: String, password: String, security: WifiSecurity) {
        require(ssid.isNotBlank() && ssid.toByteArray(Charsets.UTF_8).size <= 32 && ssid.none { it.isISOControl() }) {
            "Wi-Fi 名称需为 1–32 字节。"
        }
        require(if (security == WifiSecurity.OPEN) password.isEmpty() else
            (password.length in 8..63 && password.all { it.code in 32..126 }) ||
                (security == WifiSecurity.WPA2 && password.length == 64 && password.all { it in "0123456789abcdefABCDEF" })) {
            "加密 Wi-Fi 需 8–63 位英文或数字密码；开放网络的密码应为空。"
        }
    }

    fun channel(value: Int) {
        require(value == 0 || value in 1..14 || value in 32..177) { "Wi-Fi 信道无效，未知时填写 0。" }
    }

    fun updateMirror(value: String) {
        require(value.length <= 256 && value.none { it.isISOControl() }) { "镜像加速地址过长或包含非法字符。" }
        if (value.isEmpty() || value == "none") return
        for (part in value.split(Regex("\\s+")).filter { it.isNotEmpty() }) {
            require(part.startsWith("https://") || part.startsWith("http://")) {
                "镜像加速地址需以 https:// 或 http:// 开头（留空使用默认镜像，填 none 只用 GitHub）。"
            }
        }
    }
}
