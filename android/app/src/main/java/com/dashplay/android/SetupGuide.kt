package com.dashplay.android

/** First-run decisions, not proof of runtime readiness. Never infer App Root from an ADB shell. */
internal object SetupGuide {
    const val HTTP_CHECK = "http-check"
    const val HTTP_PROOF = "http-proof"
    const val STARTUP = "startup"
    fun usesHttp(car: Car, answers: Map<String, String>) = car == Car.TESLA && answers[ACCESS] == "http"
    const val SOURCE = "source"
    const val LAN = "lan"
    const val ACCESS = "access"
    const val INTERNET = "internet"
    const val KEEP = "keep"
    const val TLS = "tls"
    const val DNS = "dns"
    val questions = listOf("device", "car", "network", SOURCE, LAN, ACCESS, "iphone", INTERNET,
        "usb", KEEP, TLS, DNS, "wifi", "identity", "voice", HTTP_CHECK, STARTUP, "summary")

    fun included(step: String, car: Car, network: Network, link: IphoneLink, usb: Boolean,
        bluetooth: Boolean, answers: Map<String, String>): Boolean = when (step) {
        SOURCE -> network == Network.MIFI
        ACCESS, KEEP -> car == Car.TESLA
        TLS -> !usesHttp(car, answers)
        DNS -> !usesHttp(car, answers) && (car == Car.TESLA || answers[TLS] == "domain")
        HTTP_CHECK -> usesHttp(car, answers)
        "wifi" -> network != Network.CAR_HOTSPOT && link != IphoneLink.WIRED && bluetooth &&
            (network != Network.MIFI || answers[SOURCE] != "same_iphone")
        "usb" -> usb && (link != IphoneLink.WIRELESS || answers[INTERNET] == "usb")
        else -> true
    }

    fun connectionProblem(network: Network, link: IphoneLink, usb: Boolean, bluetooth: Boolean,
        answers: Map<String, String>, kind: DeviceKind = DeviceKind.BOX): String? =
        if (kind == DeviceKind.PHONE && network == Network.MIFI && answers[SOURCE] == "same_iphone")
            "安卓手机仅支持无线 CarPlay，当前不支持由运行 CarPlay 的同一部 iPhone 提供 Wi-Fi。请改用安卓本机热点、路由器或另一台设备的热点。"
        else if (network == Network.MIFI && answers[SOURCE] == "same_iphone" && link != IphoneLink.WIRED)
            "同一部 iPhone 开热点并运行 CarPlay 的组合，当前安卓向导只提供数据线连接。请选择数据线，或返回改用另一台设备的网络。"
        else ConnectionSupport.problem(network, link, usb, bluetooth, kind)

    fun problem(step: String, car: Car, network: Network, link: IphoneLink, usb: Boolean,
        bluetooth: Boolean, rootExecutable: Boolean, answers: Map<String, String>, kind: DeviceKind = DeviceKind.BOX): String? {
        val answer = answers[step]
        return when (step) {
            "network" -> if (kind == DeviceKind.PHONE && network == Network.CAR_HOTSPOT) ConnectionSupport.PHONE_NETWORK
                else if (car == Car.TESLA && network == Network.CAR_HOTSPOT)
                "当前特斯拉方案不提供车机热点路径。请选择本机热点或外部网络。" else null
            SOURCE -> if (kind == DeviceKind.PHONE && answer == "same_iphone") connectionProblem(network, link, usb, bluetooth, answers, kind)
                else if (answer !in listOf("other", "same_iphone")) "请选择提供网络的设备。" else null
            LAN -> if (answer != "ready")
                "还缺少车机到接收端的网络。USB 只连接 iPhone 和接收端，不能代替这段网络。请准备可用热点或可互访的外部网络，也可以保存草稿。" else null
            ACCESS -> when {
                answer == "vpn_address" -> "旧的免 Root 地址测试不等于 HTTP 画面验证。请选择 HTTP 兼容路线，完成授权并在车机实际验证。"
                network == Network.THIS_HOTSPOT && answer == "root" && !rootExecutable ->
                    "当前未检测到本应用可执行的 Root。只有电脑 ADB root 或存在 su 文件还不够；需要设备允许 DashPlay 使用 Root。可点击下方改用免 Root HTTP 兼容路线（实验性功能），或返回上一题更换为外部网络。"
                answer == "http" -> null // Runtime, VPN and client checks follow in HTTP_CHECK; no Root inference.
                network == Network.THIS_HOTSPOT && answer == "root" -> null
                network != Network.THIS_HOTSPOT && answer == "upstream" -> null
                else -> "当前所选的 HTTPS 组合缺少可达地址。需要本机热点加 App Root，或由外部路由器提供车机能访问的地址（仅有域名或证书不够）。你可以改选上方的「免 Root HTTP 兼容入口（实验性）」，或返回上一题更换网络。"
            }
            "iphone" -> connectionProblem(network, link, usb, bluetooth, answers, kind)
            INTERNET -> when {
                answer == "usb" && !usb -> "本机没有 USB Host，不能通过数据线使用 iPhone 网络。请选择已有网络，或只使用局域网。"
                answer !in listOf("upstream", "usb", "offline") -> "请选择这台设备的上网来源；没有外网也可以继续在局域网内传画面。"
                else -> null
            }
            KEEP -> when {
                answer == "online" && network == Network.THIS_HOTSPOT && answers[INTERNET] == "offline" ->
                    "前面选择了「暂时只用局域网（不上网）」，车机连本机热点时不会有真实互联网。请返回修改上网来源，或改选离线保持连接方案。"
                answer == "online" || answer == "verified" -> null
                answer == "offline" && network == Network.THIS_HOTSPOT && answers[ACCESS] == "root" -> null
                answer == "external" && network != Network.THIS_HOTSPOT -> null
                else -> "特斯拉需要保持连接所选 Wi-Fi。盒子自己能上网不等于车机能上网；本机离线热点需要 App Root 联网检查功能，外部网络需自行提供可用外网或已实测的保持连接方案。可返回上一题修改。"
            }
            HTTP_CHECK -> if (answer == "ready") null else
                "请先点击上方按钮启用 HTTP 兼容入口（本机热点需允许 VPN，外部热点使用局域网地址），再用车机打开验证画面和触控。若无法打开，请检查网络或返回选择其他访问方式。"
            STARTUP -> if (answer in listOf("manual", "automatic")) null else
                "请选择每次手动开启，还是通电后自动启动。"
            TLS -> when {
                answer == "domain" -> null
                answer == "local" && car != Car.TESLA -> null
                else -> "这条 HTTPS 路线需要车机信任的证书。特斯拉方案需要可信的域名证书。请准备证书，或返回选择当前车型支持的其他访问方式；也可先保存草稿。"
            }
            DNS -> when {
                answer == "public" && ((car == Car.TESLA && answers[KEEP] != "online") ||
                    (network == Network.THIS_HOTSPOT && answers[INTERNET] == "offline")) ->
                    "离线局域网下车机无法访问公共 DNS 解析域名。请返回恢复外网，或改用本机热点 Root DNS / 外部路由器本地 DNS。"
                answer == "public" -> null
                answer == "hotspot" && network == Network.THIS_HOTSPOT && rootExecutable -> null
                answer == "upstream" && network != Network.THIS_HOTSPOT -> null
                else -> "域名必须能被车机解析到这台设备的地址。有外网可用公共 DNS；没外网需本机 Root DNS 或外部路由器提供本地解析（单靠导入证书或缓存不够）。可返回上一题修改。"
            }
            else -> null
        }
    }

    fun reconcileHttpConfirmation(answers: MutableMap<String, String>, proofKey: String?, configurationMatches: Boolean) {
        if (proofKey == null || answers[HTTP_PROOF] != proofKey || !configurationMatches) {
            answers.remove(HTTP_CHECK); answers.remove(HTTP_PROOF)
        }
    }

    /** The existing USB page saves immediately; returning to the wizard must reflect that explicit choice. */
    fun reconcileUsbChoice(answers: MutableMap<String, String>, requested: Boolean) {
        val source = answers[INTERNET] ?: return
        if ((source == "usb") == requested) return
        invalidate(INTERNET, answers)
        answers[INTERNET] = if (requested) "usb" else "offline"
    }

    enum class Pending { HTTP, USB, TESLA_ACCESS, OFFLINE_CHECKS, TLS, LOCAL_TLS, DNS }
    data class Readiness(val usb: Boolean, val rootEnabled: Boolean, val rootActive: Boolean,
        val offlineChecks: Boolean, val dnsActive: Boolean, val tlsUsable: Boolean, val tlsEnabled: Boolean, val httpReady: Boolean = false)

    fun pending(car: Car, network: Network, needsUsb: Boolean, answers: Map<String, String>,
        ready: Readiness): Pending? = when {
        needsUsb && !ready.usb -> Pending.USB
        usesHttp(car, answers) && !ready.httpReady -> Pending.HTTP
        car == Car.TESLA && !usesHttp(car, answers) && network == Network.THIS_HOTSPOT &&
            (!ready.rootEnabled || !ready.rootActive) -> Pending.TESLA_ACCESS
        car == Car.TESLA && !usesHttp(car, answers) && network == Network.THIS_HOTSPOT &&
            ((answers[KEEP] == "offline") != ready.offlineChecks) -> Pending.OFFLINE_CHECKS
        !usesHttp(car, answers) && (car == Car.TESLA || answers[TLS] == "domain") && !ready.tlsUsable -> Pending.TLS
        car != Car.TESLA && answers[TLS] == "local" && ready.tlsEnabled -> Pending.LOCAL_TLS
        !usesHttp(car, answers) && answers[DNS] == "hotspot" && !ready.dnsActive -> Pending.DNS
        else -> null
    }

    /** An earlier answer invalidates dependent confirmations, but not Wi-Fi credentials or connection preferences. */
    fun invalidate(changed: String, answers: MutableMap<String, String>) {
        val affected = when (changed) {
            "car" -> listOf(ACCESS, KEEP, TLS, DNS)
            "network" -> listOf(SOURCE, LAN, ACCESS, INTERNET, KEEP, TLS, DNS)
            SOURCE -> listOf(LAN, ACCESS, INTERNET, KEEP, TLS, DNS)
            LAN -> listOf(ACCESS, KEEP, DNS)
            ACCESS -> listOf(KEEP, TLS, DNS)
            "iphone" -> listOf(INTERNET, KEEP, DNS)
            INTERNET -> listOf(KEEP, DNS)
            KEEP -> listOf(DNS)
            TLS -> listOf(DNS)
            else -> emptyList()
        }
        affected.forEach(answers::remove)
        if (changed in listOf("car", "network", SOURCE, LAN, ACCESS, "iphone", INTERNET, TLS, KEEP, "wifi")) {
            answers.remove(HTTP_CHECK); answers.remove(HTTP_PROOF)
        }
    }
}
