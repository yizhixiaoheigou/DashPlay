package com.dashplay.android

/** Product policy and hardware/topology eligibility; permissions and readiness follow separately. */
internal object ConnectionSupport {
    const val PHONE_WIRELESS_ONLY = "安卓手机仅支持无线 CarPlay，不提供数据线或自动切换连接。"
    const val PHONE_NO_BLUETOOTH = "未检测到蓝牙，无法使用无线 CarPlay。安卓手机不提供有线连接，请使用具备蓝牙的安卓设备，或支持 USB Host 的安卓盒子。"
    const val PHONE_NETWORK = "安卓手机仅支持无线 CarPlay；当前车机热点方案需要有线连接，请选择本机热点或其他设备提供的外部 Wi-Fi。"

    fun effectiveLink(kind: DeviceKind, stored: IphoneLink): IphoneLink =
        if (kind == DeviceKind.PHONE) IphoneLink.WIRELESS else stored

    fun problem(network: Network, link: IphoneLink, usbHost: Boolean, bluetooth: Boolean,
        kind: DeviceKind = DeviceKind.BOX): String? {
        if (kind == DeviceKind.PHONE) return when {
            link != IphoneLink.WIRELESS -> PHONE_WIRELESS_ONLY
            network == Network.CAR_HOTSPOT -> PHONE_NETWORK
            !bluetooth -> PHONE_NO_BLUETOOTH
            else -> null
        }
        val wireless = network != Network.CAR_HOTSPOT && bluetooth
        return when {
            link == IphoneLink.WIRED && !usbHost ->
                "未检测到 USB Host，不能使用数据线连接；有蓝牙时可选择无线，并使用本机热点或外部 Wi-Fi。"
            link == IphoneLink.WIRELESS && network == Network.CAR_HOTSPOT ->
                "车机热点方案需要数据线连接；请改选数据线，或返回选择本机热点、外部 Wi-Fi。"
            link == IphoneLink.WIRELESS && !bluetooth ->
                "未检测到蓝牙，不能使用无线 CarPlay；有 USB Host 时可选择数据线连接。"
            link == IphoneLink.ANY && !usbHost && !wireless ->
                "当前组合没有可用连接路径：数据线需要 USB Host，无线需要蓝牙及本机热点或外部 Wi-Fi。请调整连接方式或设备。"
            else -> null
        }
    }
}
