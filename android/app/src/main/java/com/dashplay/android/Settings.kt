package com.dashplay.android

import android.content.Context
import android.net.wifi.WifiManager
import com.playport.server.ServerConfig

/** Car-specific access policy; the HTTP compatibility route is configured separately from HTTPS. */
enum class Car { TESLA, OTHER }

/** Which Wi-Fi the car and the iPhone meet on (the box's Q3). */
enum class Network {
    /** This device's own hotspot: the car and the iPhone join it. */
    THIS_HOTSPOT,
    /** The car's hotspot: this device and the iPhone join it. Its bandwidth rarely carries full-screen video, so cable is the default. */
    CAR_HOTSPOT,
    /** A pocket Wi-Fi everyone joins. */
    MIFI,
}

/** How the iPhone connects (the box's Q4): asked last, after the facts. */
enum class IphoneLink { ANY, WIRELESS, WIRED }

/** Selected product category: phones support wireless CarPlay only; boxes may also use USB. */
enum class DeviceKind { PHONE, BOX }

/**
 * How the page receives the picture when the HTTPS/WebCodecs path is not available (2026-11-03).
 *
 * AUTO is the default for the HTTP compatibility entry: try a real-time channel first, fall back to stills, and say
 * which one is running. LEGACY keeps the original HTTPS/WebCodecs behaviour for existing installations.
 */
enum class VideoTransport {
    /** WebRTC first, JPEG stills if it does not produce a frame; only meaningful on the HTTP entry. */
    AUTO,
    /** Still the original path: H.264/HEVC over WebSocket, decoded with WebCodecs (needs https or localhost). */
    WEBCODECS,
    /** Still images only. */
    JPEG,
}

/** What the user set in the app, kept across restarts. */
class Settings(context: Context) {
    private val app = context.applicationContext
    private val prefs = context.applicationContext.getSharedPreferences("dashplay", Context.MODE_PRIVATE)
    private val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager

    init {
        // Persist the product-policy upgrade, without changing credentials, identity or pairing records.
        // The effectiveLink read guard also covers another Settings instance changing the category.
        synchronized(WRITE_LOCK) {
            if (deviceKind == DeviceKind.PHONE) {
                val oldLink = prefs.getString(KEY_IPHONE, null)
                if (oldLink != IphoneLink.WIRELESS.name || !prefs.contains("phone_usb_internet")) {
                    val edit = prefs.edit().putString(KEY_IPHONE, IphoneLink.WIRELESS.name)
                    if (!prefs.contains("phone_usb_internet")) edit.putBoolean("phone_usb_internet", false)
                    if (network == Network.CAR_HOTSPOT) edit.putBoolean(KEY_SETUP_DONE, false)
                    check(edit.commit()) { "无法保存安卓手机无线连接设置" }
                }
            }
        }
    }

    /** The page token stays the same, so the car's bookmark keeps working. */
    val accessToken: String
        get() = token(KEY_TOKEN)

    /** Only shown on the device's management screen; never handed to the public viewer. */
    val adminToken: String get() = token("admin_token")

    private fun token(key: String): String = synchronized(WRITE_LOCK) {
        prefs.getString(key, null) ?: ServerConfig.generateToken().also {
            check(prefs.edit().putString(key, it).commit()) { "无法保存访问凭据" }
        }
    }

    val deviceName: String get() = prefs.getString("device_name", "DashPlay") ?: "DashPlay"

    /**
     * Chosen on the first setup screen. Until then a television or a screenless box counts as a box and a phone as a
     * phone, so an upgraded install keeps sensible words without asking again.
     */
    var deviceKind: DeviceKind
        get() = prefs.getString(KEY_DEVICE_KIND, null)?.let { name -> DeviceKind.values().firstOrNull { it.name == name } }
            ?: defaultDeviceKind()
        set(value) {
            val edit = prefs.edit().putString(KEY_DEVICE_KIND, value.name)
            if (value == DeviceKind.PHONE) {
                edit.putString(KEY_IPHONE, IphoneLink.WIRELESS.name)
                if (network == Network.CAR_HOTSPOT) edit.putBoolean(KEY_SETUP_DONE, false)
            }
            check(edit.commit())
        }

    val wiredCarPlaySupported: Boolean get() = deviceKind == DeviceKind.BOX

    val deviceKindChosen: Boolean get() = prefs.getString(KEY_DEVICE_KIND, null) != null

    private fun defaultDeviceKind(): DeviceKind {
        val manager = app.getSystemService(Context.UI_MODE_SERVICE) as? android.app.UiModeManager
        val television = manager?.currentModeType == android.content.res.Configuration.UI_MODE_TYPE_TELEVISION
        return if (television || !app.packageManager.hasSystemFeature(android.content.pm.PackageManager.FEATURE_TOUCHSCREEN))
            DeviceKind.BOX else DeviceKind.PHONE
    }
    val idleReleaseSeconds: Int get() = prefs.getInt("idle_release", 300)
    val wifiChannel: Int get() = prefs.getInt("wifi_channel", 0)
    val wifiSecurity: WifiSecurity get() = securityForNetwork(network)

    /** Encryption belongs to the selected credentials, not to the last edited network. */
    fun securityForNetwork(target: Network): WifiSecurity {
        val local = target == Network.THIS_HOTSPOT
        val password = if (local) hotspot()?.passphrase else uplink()?.passphrase
        val inferred = if (password.isNullOrEmpty()) WifiSecurity.OPEN else WifiSecurity.WPA2
        // Before the split, wifi_security described whichever network was selected.
        val legacy = if (local == (network == Network.THIS_HOTSPOT)) enumOr("wifi_security", inferred) else inferred
        return enumOr(if (local) "local_wifi_security" else "uplink_wifi_security", legacy)
    }

    var usbEnabled: Boolean
        get() = prefs.getBoolean("usb_enabled", false)
        set(value) { check(prefs.edit().putBoolean("usb_enabled", value).commit()) }

    val usesUsb: Boolean get() = usbEnabled && (iphoneLink != IphoneLink.WIRELESS || usbInternet)

    /** The saved choice is separate from permission and from an actually working upstream. */
    // Phone USB internet is a separate opt-in; old wired CarPlay consent must not start USB after upgrade.
    private fun usbInternetKey(kind: DeviceKind) = if (kind == DeviceKind.PHONE) "phone_usb_internet" else "usb_internet"
    val usbInternetRequested: Boolean get() = usbInternetRequested(deviceKind)
    fun usbInternetRequested(kind: DeviceKind): Boolean = prefs.getBoolean(usbInternetKey(kind), kind == DeviceKind.BOX)
    val usbChoiceMade: Boolean get() = prefs.contains("usb_enabled")
    val iphoneLinkChosen: Boolean get() = prefs.contains(KEY_IPHONE)
    fun initialIphoneLink(supportsUsb: Boolean): IphoneLink =
        if (wiredCarPlaySupported && !setupDone && !iphoneLinkChosen && supportsUsb) IphoneLink.WIRED else iphoneLink

    fun requestUsbInternet(value: Boolean, kind: DeviceKind = deviceKind) {
        check(prefs.edit().putBoolean(usbInternetKey(kind), value)
            .apply { if (!value) putBoolean("usb_hotspot_share", false) }.commit())
    }

    /** Runtime callers never install Internet routes merely because the checkbox is preselected. */
    var usbInternet: Boolean
        get() = usbInternetRequested && usbInternetAllowed && usbEnabled
        set(value) {
            require(!value || usbInternetAllowed) { "请先在安卓设备的 USB 设置页允许使用 iPhone 网络。" }
            requestUsbInternet(value)
        }
    val usbInternetAllowed: Boolean get() = prefs.getBoolean("usb_internet_consent", false)
    fun allowUsbInternet() {
        require(usbEnabled && android.net.VpnService.prepare(app) == null) { "请先在本机 USB 设置页启用 USB 并允许 VPN。" }
        check(prefs.edit().putBoolean("usb_internet_consent", true).putBoolean(usbInternetKey(deviceKind), true).commit())
    }

    var usbHotspotShare: Boolean
        get() = prefs.getBoolean("usb_hotspot_share", false)
        set(value) {
            require(!value || usbHotspotShareAllowed) { "请先在安卓设备上允许 Root 热点共享。" }
            check(prefs.edit().putBoolean("usb_hotspot_share", value).commit())
        }
    val usbHotspotShareAllowed: Boolean get() = prefs.getBoolean("usb_hotspot_share_consent", false)
    fun allowUsbHotspotShare() {
        require(usbEnabled && usbInternet && network == Network.THIS_HOTSPOT && android.net.VpnService.prepare(app) == null) {
            "请先启用通过数据线使用 iPhone 网络，并选择这台设备的热点。"
        }
        check(prefs.edit().putBoolean("usb_hotspot_share_consent", true).putBoolean("usb_hotspot_share", true).commit())
    }

    /** Root authorization can only be initiated on the Android device, never by a viewer. */
    var teslaRootAccess: Boolean
        get() = prefs.getBoolean("tesla_root_access", false)
        set(value) {
            require(!value || (!compatHttp && !UsbVpnService.compatAddressAttached)) { "请先关闭免 Root HTTP 并断开数据线，再启用 Root 地址。" }
            check(prefs.edit().putBoolean("tesla_root_access", value).commit())
        }

    /**
     * The plain-HTTP compatibility entry (2026-11-03): a Tesla's browser can open the page but has no WebCodecs over
     * HTTP, so the picture arrives as JPEG stills. Off by default — an upgrade must never open a new port by itself.
     * It never carries management: see BrowserEndpoint.
     */
    var compatHttp: Boolean
        get() = prefs.getBoolean("compat_http", false)
        set(value) {
            require(!value || !teslaRootAccess) { "请先关闭 Root 地址，再启用免 Root HTTP。" }
            check(prefs.edit().putBoolean("compat_http", value).commit())
        }

    /** A single non-privileged port for HTTPS and the optional HTTP viewer. */
    var browserPort: Int
        get() = BrowserPort.selected(
            if (prefs.contains("browser_port")) prefs.getInt("browser_port", BrowserPort.DEFAULT) else null,
            if (prefs.contains("compat_http_port")) prefs.getInt("compat_http_port", 8081) else null)
        set(value) {
            BrowserPort.validate(value)
            check(prefs.edit().putInt("browser_port", value).commit())
        }

    /** Compatibility alias for previous management clients. Both protocols now use this port. */
    var compatHttpPort: Int
        get() = browserPort
        set(value) { browserPort = value }

    /** Which picture channel a new page should prefer; a page that cannot do it falls back by itself. */
    var videoTransport: VideoTransport
        get() = enumOr("video_transport", VideoTransport.AUTO)
        set(value) { prefs.edit().putString("video_transport", value.name).apply() }

    var teslaOfflineChecks: Boolean
        get() = prefs.getBoolean("tesla_offline_checks", false)
        set(value) {
            require(!value || teslaRootAccess) { "请先在安卓设备上启用 Root 访问地址。" }
            check(prefs.edit().putBoolean("tesla_offline_checks", value).commit())
        }

    fun enableTeslaRootAccess() {
        require(!compatHttp && !UsbVpnService.compatAddressAttached) { "请先关闭免 Root HTTP 并断开数据线，再启用 Root 地址。" }
        check(prefs.edit().putBoolean("tesla_root_access", true).putString(KEY_CAR, Car.TESLA.name).commit())
    }

    /** N1/OpenWrt own_name_on_hotspot: on by default, requires Root 53 redirect on Android. */
    var ownNameOnHotspot: Boolean
        get() = prefs.getBoolean("own_name_on_hotspot", true)
        set(value) { check(prefs.edit().putBoolean("own_name_on_hotspot", value).commit()) }

    var hotspotOwnNameRoot: Boolean
        get() = prefs.getBoolean("hotspot_own_name_root", false)
        set(value) { check(prefs.edit().putBoolean("hotspot_own_name_root", value).commit()) }

    fun enableHotspotOwnNameRoot() {
        check(prefs.edit().putBoolean("hotspot_own_name_root", true).putBoolean("own_name_on_hotspot", true).commit())
    }

    var autoHotspot: Boolean
        get() = prefs.getBoolean("auto_hotspot", false)
        set(value) { check(prefs.edit().putBoolean("auto_hotspot", value).commit()) }

    val usesLocalHotspot: Boolean get() = network == Network.THIS_HOTSPOT && autoHotspot

    fun savedLocalHotspotCredentials(): Hotspot? {
        val ssid = prefs.getString("local_ssid", null) ?: return null
        val password = prefs.getString("local_password", null) ?: return null
        return Hotspot(ssid, password, false)
    }

    fun localHotspotCredentials(): Hotspot = synchronized(WRITE_LOCK) {
        val stored = prefs.getString("local_ssid", null)
        val pass = prefs.getString("local_password", null)
        if (stored != null && pass != null) return@synchronized Hotspot(stored, pass, false)
        val ssid = "DIRECT-DP-DashPlay-" + ServerConfig.generateToken().take(6)
        val password = ServerConfig.generateToken().take(20)
        check(prefs.edit().putString("local_ssid", ssid).putString("local_password", password).commit())
        Hotspot(ssid, password, false)
    }

    var setupDone: Boolean
        get() = prefs.getBoolean(KEY_SETUP_DONE, false)
        set(value) { prefs.edit().putBoolean(KEY_SETUP_DONE, value).apply() }

    var car: Car
        get() = enumOr(KEY_CAR, Car.OTHER)
        set(value) { prefs.edit().putString(KEY_CAR, value.name).apply() }

    var network: Network
        get() = enumOr(KEY_NETWORK, Network.THIS_HOTSPOT)
        set(value) = synchronized(WRITE_LOCK) {
            check(prefs.edit().putString("local_wifi_security", securityForNetwork(Network.THIS_HOTSPOT).name)
                .putString("uplink_wifi_security", securityForNetwork(Network.MIFI).name)
                .putString(KEY_NETWORK, value.name).commit())
        }

    /** Shared N1/OpenWrt sound output: all categories either remain in CarPlay or leave it. */
    var audioOutsideCarPlay: Boolean
        get() = prefs.getBoolean(KEY_AUDIO_OUTSIDE, false)
        set(value) { prefs.edit().putBoolean(KEY_AUDIO_OUTSIDE, value).apply() }

    /** N1/OpenWrt audio_routes.resolve: one output switch, and the page's music behaviour follows Tesla mode. */
    fun audioRoutes(): Map<String, String> = AudioPolicy.routes(audioOutsideCarPlay, car == Car.TESLA)

    /** run-receiver.py writes car_music for Tesla mode too, and car_voice only when calls/Siri left CarPlay. */
    val webCarMusic: Boolean get() = AudioPolicy.webCarMusic(audioOutsideCarPlay, car == Car.TESLA)
    val webCarVoice: Boolean get() = AudioPolicy.webCarVoice(audioOutsideCarPlay, car == Car.TESLA)

    var iphoneLink: IphoneLink
        get() = ConnectionSupport.effectiveLink(deviceKind, enumOr(KEY_IPHONE, IphoneLink.ANY))
        set(value) {
            require(wiredCarPlaySupported || value == IphoneLink.WIRELESS) { ConnectionSupport.PHONE_WIRELESS_ONLY }
            prefs.edit().putString(KEY_IPHONE, value.name).apply()
        }

    /** The iPhone added in the app (its Bluetooth address); null takes the paired iPhone. */
    var iphoneAddress: String?
        get() = prefs.getString(KEY_IPHONE_ADDRESS, null)
        set(value) { prefs.edit().putString(KEY_IPHONE_ADDRESS, value).apply() }

    var iphoneName: String?
        get() = prefs.getString(KEY_IPHONE_NAME, null)
        set(value) { prefs.edit().putString(KEY_IPHONE_NAME, value).apply() }

    var startAtBoot: Boolean
        get() = prefs.getBoolean(KEY_BOOT, true)
        set(value) { prefs.edit().putBoolean(KEY_BOOT, value).apply() }

    /** Daily online update check (N1/OpenWrt update_check, default true). Never installs automatically. */
    var updateCheck: Boolean
        get() = prefs.getBoolean("update_check", true)
        set(value) { check(prefs.edit().putBoolean("update_check", value).commit()) }

    /**
     * Download mirror prefixes tried after GitHub (N1/OpenWrt update_mirror).
     * Empty: built-in [OnlineUpdate.DEFAULT_MIRRORS]; "none": GitHub only.
     */
    var updateMirror: String
        get() = prefs.getString("update_mirror", "") ?: ""
        set(value) {
            SettingValidation.updateMirror(value.trim())
            check(prefs.edit().putString("update_mirror", value.trim()).commit())
        }

    data class Hotspot(val ssid: String, val passphrase: String, val fromSystem: Boolean)

    /** The Wi-Fi the iPhone is told to join: what the user typed, else the system hotspot's own settings where Android
     *  lets an app read them (7.1 and earlier), and only when that hotspot is the network in use. */
    fun hotspot(): Hotspot? {
        val ssid = prefs.getString(KEY_SSID, null)
        if (!ssid.isNullOrEmpty()) return Hotspot(ssid, prefs.getString(KEY_PASSPHRASE, null).orEmpty(), false)
        return if (network == Network.THIS_HOTSPOT) systemHotspot() else null
    }

    fun saveHotspot(ssid: String, passphrase: String) {
        prefs.edit().putString(KEY_SSID, ssid.trim()).putString(KEY_PASSPHRASE, passphrase)
            .putString("local_wifi_security", (if (passphrase.isEmpty()) WifiSecurity.OPEN else WifiSecurity.WPA2).name).apply()
    }

    /**
     * The Wi-Fi the box itself joins when the car and the iPhone meet on an external network (router, pocket Wi-Fi or
     * the car's own hotspot). Kept apart from [hotspot]: one value used to serve both meanings, so switching a box from
     * its own hotspot to a router kept telling the iPhone to join a hotspot that no longer existed.
     */
    data class Uplink(val ssid: String, val passphrase: String)

    fun uplink(): Uplink? {
        migrateSplit()
        val ssid = prefs.getString(KEY_UPLINK_SSID, null)
        if (ssid.isNullOrEmpty()) return null
        return Uplink(ssid, prefs.getString(KEY_UPLINK_PASSPHRASE, null).orEmpty())
    }

    val externalWifiChoice: ExternalWifiChoice?
        get() = if (prefs.getString("uplink_confirmed_ssid", null) == uplink()?.ssid)
            prefs.getString("uplink_choice", null)?.let { value -> ExternalWifiChoice.values().firstOrNull { it.name == value } }
        else null

    /** Manual system hotspot and external Wi-Fi must advertise their own credentials to the phone. */
    fun connectionWifi(): Hotspot? = if (network == Network.THIS_HOTSPOT) hotspot()
        else uplink()?.let { Hotspot(it.ssid, it.passphrase, false) }

    fun saveUplink(ssid: String, passphrase: String) {
        prefs.edit().putString(KEY_UPLINK_SSID, ssid.trim()).putString(KEY_UPLINK_PASSPHRASE, passphrase)
            .putString("uplink_wifi_security", (if (passphrase.isEmpty()) WifiSecurity.OPEN else WifiSecurity.WPA2).name).apply()
    }

    /**
     * One-time move from the single old field. Where the box is on an external network and its real SSID differs from
     * the stored one, the stored name was the bug: take the real name and leave the password empty rather than carry
     * a password that belongs to a different network.
     */
    private fun migrateSplit() {
        if (prefs.getBoolean(KEY_SPLIT_DONE, false)) return
        val oldSsid = prefs.getString(KEY_SSID, null).orEmpty()
        val oldPass = prefs.getString(KEY_PASSPHRASE, null).orEmpty()
        val edit = prefs.edit().putBoolean(KEY_SPLIT_DONE, true)
        if (oldSsid.isNotEmpty()) {
            val actual = runCatching { CurrentWifi.ssid(app) }.getOrNull()
            if (network != Network.THIS_HOTSPOT && actual != null && !actual.equals(oldSsid, ignoreCase = true)) {
                edit.putString(KEY_UPLINK_SSID, actual).putString(KEY_UPLINK_PASSPHRASE, "")
            } else {
                edit.putString(KEY_UPLINK_SSID, oldSsid).putString(KEY_UPLINK_PASSPHRASE, oldPass)
            }
        }
        edit.apply()
    }

    internal val bluetoothSupported: Boolean
        get() = app.packageManager.hasSystemFeature(android.content.pm.PackageManager.FEATURE_BLUETOOTH)

    internal fun connectionProblem(targetNetwork: Network, targetLink: IphoneLink, kind: DeviceKind = deviceKind): String? =
        ConnectionSupport.problem(targetNetwork, targetLink,
            app.packageManager.hasSystemFeature(android.content.pm.PackageManager.FEATURE_USB_HOST),
            bluetoothSupported, kind)

    /** One durable transaction for browser edits; invalid combinations leave all previous values intact. */
    fun apply(update: ManagementSettings) = synchronized(WRITE_LOCK) {
        val targetKind = update.deviceKind ?: deviceKind
        val targetLink = update.iphoneLink ?: ConnectionSupport.effectiveLink(targetKind, iphoneLink)
        if (update.deviceKind != null || update.network != null || update.iphoneLink != null || update.setupDone == true) {
            connectionProblem(update.network ?: network, targetLink, targetKind)?.let {
                throw IllegalArgumentException(it)
            }
        }
        val targetCar = update.car ?: car
        val target = update.network ?: network
        require(!(targetCar == Car.TESLA && target == Network.CAR_HOTSPOT)) {
            "当前特斯拉方案不提供车机热点路径。请选择本机热点或外部网络。"
        }
        update.deviceName?.let(SettingValidation::name)
        update.idleReleaseSeconds?.let { require(it in 0..86400) { "空闲释放时间需在 0–86400 秒之间。" } }
        update.wifiChannel?.let(SettingValidation::channel)
        update.updateMirror?.let { SettingValidation.updateMirror(it.trim()) }
        if (update.usbInternet == true) {
            require(usbInternetAllowed && usbEnabled) {
                "请先在安卓设备的 USB 设置页启用 USB，并允许使用 iPhone 网络。"
            }
        }
        if (update.usbHotspotShare == true) {
            require(usbHotspotShareAllowed && usbEnabled && (update.usbInternet ?: usbInternet) &&
                (update.network ?: network) == Network.THIS_HOTSPOT) {
                "请先在安卓设备上允许 Root 热点共享，并启用通过数据线使用 iPhone 网络。"
            }
        }
        val automatic = (update.autoHotspot ?: autoHotspot) && (update.network ?: network) == Network.THIS_HOTSPOT
        if (automatic) {
            if (android.os.Build.VERSION.SDK_INT < 29 || !app.packageManager.hasSystemFeature(android.content.pm.PackageManager.FEATURE_WIFI_DIRECT))
                throw IllegalArgumentException("此设备不支持应用本地热点，请使用系统热点或外部 Wi-Fi。")
            val permission = if (android.os.Build.VERSION.SDK_INT >= 33) android.Manifest.permission.NEARBY_WIFI_DEVICES else android.Manifest.permission.ACCESS_FINE_LOCATION
            require(app.checkSelfPermission(permission) == android.content.pm.PackageManager.PERMISSION_GRANTED) {
                "请先在安卓设备的本地热点设置中授予附近设备或位置信息权限。"
            }
        }
        val local = target == Network.THIS_HOTSPOT
        val targetCompat = update.compatHttp ?: compatHttp
        require(!targetCompat || !teslaRootAccess) { "请先关闭 Root 地址，再启用免 Root HTTP。" }
        require(update.browserPort == null || update.compatHttpPort == null || update.browserPort == update.compatHttpPort) {
            "HTTP 和 HTTPS 使用同一个网页端口，请填写相同端口。"
        }
        val targetPort = update.browserPort ?: update.compatHttpPort ?: browserPort
        BrowserPort.validate(targetPort)
        if (update.videoTransport == VideoTransport.WEBCODECS && targetCompat) {
            throw IllegalArgumentException("HTTP 兼容入口不提供 WebCodecs（明文页面没有安全上下文）。请改用自动或仅图片。")
        }
        val previousUplink = uplink()
        val uplinkSsid = (update.uplinkSsid ?: previousUplink?.ssid.orEmpty()).trim()
        if (update.externalWifiChoice == ExternalWifiChoice.CURRENT) {
            val actual = CurrentWifi.ssid(app)
            require(actual != null) { "暂时无法读取当前 Wi-Fi 名称。请在 App 中允许读取，或选择其他 Wi-Fi 后手动填写。" }
            require(actual == uplinkSsid) { "当前 Wi-Fi 已变化，请重新选择当前网络。" }
        }
        val uplinkPassphrase = ExternalWifiPolicy.password(previousUplink?.ssid, previousUplink?.passphrase,
            uplinkSsid, update.uplinkPassphrase)
        val targetWifiChoice = update.externalWifiChoice ?: externalWifiChoice?.takeIf { previousUplink?.ssid == uplinkSsid }
        val previous = hotspot()
        val ssid = update.ssid ?: previous?.ssid.orEmpty()
        val password = update.passphrase ?: previous?.passphrase.orEmpty()
        val localSecurity = if (local) update.wifiSecurity ?: securityForNetwork(Network.THIS_HOTSPOT)
            else securityForNetwork(Network.THIS_HOTSPOT)
        val uplinkSecurity = if (!local) update.wifiSecurity ?: securityForNetwork(Network.MIFI)
            else securityForNetwork(Network.MIFI)
        val changesWifi = update.ssid != null || update.passphrase != null || (local && update.wifiSecurity != null)
        val changesUplink = update.uplinkSsid != null || update.uplinkPassphrase != null || (!local && update.wifiSecurity != null)
        val wireless = Plan.of(update.car ?: car, target,
            targetLink, update.audioOutsideCarPlay ?: audioOutsideCarPlay).wireless && bluetoothSupported
        val requiresWifi = (update.setupDone ?: setupDone) && wireless && !automatic
        if (changesWifi || requiresWifi && local) SettingValidation.wifi(ssid, password, localSecurity)
        if (changesUplink || requiresWifi && !local) SettingValidation.wifi(uplinkSsid, uplinkPassphrase, uplinkSecurity)
        if (requiresWifi && !local) require(targetWifiChoice != null) {
            "请先选择使用当前 Wi-Fi 或其他 Wi-Fi，再保存无线连接设置。"
        }
        val edit = prefs.edit()
        update.autoHotspot?.let { edit.putBoolean("auto_hotspot", it) }
        update.usbInternet?.let { edit.putBoolean(if (targetKind == DeviceKind.PHONE) "phone_usb_internet" else "usb_internet", it) }
        update.usbHotspotShare?.let { edit.putBoolean("usb_hotspot_share", it) }
        if (update.usbInternet == false) edit.putBoolean("usb_hotspot_share", false)
        update.deviceName?.let { edit.putString("device_name", it) }
        update.car?.let { edit.putString(KEY_CAR, it.name) }
        update.network?.let { edit.putString(KEY_NETWORK, it.name) }
        update.deviceKind?.let { edit.putString(KEY_DEVICE_KIND, it.name) }
        if (update.iphoneLink != null || update.deviceKind != null) edit.putString(KEY_IPHONE, targetLink.name)
        if (changesWifi) edit.putString(KEY_SSID, ssid).putString(KEY_PASSPHRASE, password)
        if (changesUplink) edit.putString(KEY_UPLINK_SSID, uplinkSsid).putString(KEY_UPLINK_PASSPHRASE, uplinkPassphrase)
        if (update.externalWifiChoice != null) edit.putString("uplink_choice", update.externalWifiChoice.name)
            .putString("uplink_confirmed_ssid", uplinkSsid)
        else if (previousUplink?.ssid != uplinkSsid) edit.remove("uplink_choice").remove("uplink_confirmed_ssid")
        // Save both resolved modes before changing topology so legacy WPA3 also survives a switch.
        edit.putString("local_wifi_security", localSecurity.name).putString("uplink_wifi_security", uplinkSecurity.name)
        update.wifiChannel?.let { edit.putInt("wifi_channel", it) }
        update.audioOutsideCarPlay?.let { edit.putBoolean(KEY_AUDIO_OUTSIDE, it) }
        update.startAtBoot?.let { edit.putBoolean(KEY_BOOT, it) }
        update.idleReleaseSeconds?.let { edit.putInt("idle_release", it) }
        update.setupDone?.let { edit.putBoolean(KEY_SETUP_DONE, it) }
        update.ownNameOnHotspot?.let { edit.putBoolean("own_name_on_hotspot", it) }
        update.updateCheck?.let { edit.putBoolean("update_check", it) }
        update.updateMirror?.let { edit.putString("update_mirror", it.trim()) }
        update.compatHttp?.let { edit.putBoolean("compat_http", it) }
        if (update.browserPort != null || update.compatHttpPort != null) edit.putInt("browser_port", targetPort)
        update.videoTransport?.let { edit.putString("video_transport", it.name) }
        check(edit.commit()) { "设置未保存，请重试。" }
    }

    @Suppress("DEPRECATION")
    fun systemHotspot(): Hotspot? = try {
        val config = WifiManager::class.java.getMethod("getWifiApConfiguration").invoke(wifi) as? android.net.wifi.WifiConfiguration
        val ssid = config?.SSID?.removeSurrounding("\"")
        if (ssid.isNullOrEmpty()) null else Hotspot(ssid, config.preSharedKey.orEmpty(), true)
    } catch (_: Throwable) {
        null
    }

    /** What the answers come to (the box's 配置总结). */
    fun plan(): Plan = Plan.of(car, network, iphoneLink, audioOutsideCarPlay)

    private inline fun <reified T : Enum<T>> enumOr(key: String, fallback: T): T =
        prefs.getString(key, null)?.let { name -> enumValues<T>().firstOrNull { it.name == name } } ?: fallback

    private companion object {
        val WRITE_LOCK = Any()
        const val KEY_BOOT = "start_at_boot"
        const val KEY_DEVICE_KIND = "device_kind"
        const val KEY_TOKEN = "access_token"
        const val KEY_SSID = "hotspot_ssid"
        const val KEY_UPLINK_SSID = "uplink_ssid"
        const val KEY_UPLINK_PASSPHRASE = "uplink_passphrase"
        const val KEY_SPLIT_DONE = "ssid_split_done"
        const val KEY_PASSPHRASE = "hotspot_passphrase"
        const val KEY_SETUP_DONE = "setup_done"
        const val KEY_CAR = "car"
        const val KEY_NETWORK = "network"
        const val KEY_AUDIO_OUTSIDE = "audio_outside_carplay"
        const val KEY_IPHONE = "iphone_link"
        const val KEY_IPHONE_ADDRESS = "iphone_address"
        const val KEY_IPHONE_NAME = "iphone_name"
    }
}

/**
 * The receiver settings and the costs that follow from the answers. The rules are the box's
 * (docs/设置与装机交互设计-20261015.md, section 2); wired CarPlay needs device-local USB/VPN consent.
 */
data class Plan(
    val wireless: Boolean,
    /** Wireless asked for but ruled out by an earlier answer. */
    val wirelessRuledOut: Boolean,
    val audioOutsideCarPlay: Boolean,
    val notes: List<Int>,
    val unverified: List<Int>,
) {
    companion object {
        fun of(car: Car, network: Network, link: IphoneLink, audioOutsideCarPlay: Boolean): Plan {
            val notes = mutableListOf<Int>()
            val unverified = mutableListOf<Int>()
            // A car hotspot rarely has the bandwidth for wireless picture: recommend the cable instead of refusing it.
            val wirelessPossible = network != Network.CAR_HOTSPOT
            val wireless = wirelessPossible && link != IphoneLink.WIRED
            if (network == Network.THIS_HOTSPOT) notes += R.string.note_hotspot_on
            if (network == Network.CAR_HOTSPOT) notes += R.string.note_car_hotspot_cable
            if (car == Car.TESLA) unverified += R.string.unverified_tesla
            if (link != IphoneLink.WIRELESS) notes += R.string.note_wired_missing
            if (audioOutsideCarPlay) notes += R.string.note_car_voice
            return Plan(
                wireless = wireless,
                wirelessRuledOut = !wirelessPossible && link == IphoneLink.WIRELESS,
                audioOutsideCarPlay = audioOutsideCarPlay,
                notes = notes,
                unverified = unverified,
            )
        }
    }
}

/**
 * The same five sound destinations as the N1/OpenWrt receiver (scripts/onecloud/audio_routes.py):
 * one owner switch, alert always stays in CarPlay, and the page's car-music behaviour follows Tesla mode.
 */
object AudioPolicy {
    fun routes(outsideCarPlay: Boolean, tesla: Boolean): Map<String, String> {
        val music = if (outsideCarPlay || tesla) "bluetooth" else "carplay"
        val navigation = if (outsideCarPlay) "bluetooth" else "carplay"
        return linkedMapOf("media" to music, "navigation" to navigation, "alert" to "carplay", "siri" to music, "calls" to music)
    }

    /** run-receiver.py: the page's car-music behaviour also follows Tesla mode. */
    fun webCarMusic(outsideCarPlay: Boolean, tesla: Boolean): Boolean = outsideCarPlay || tesla

    /** run-receiver.py: calls and Siri only leave CarPlay when the owner's single sound switch says so. */
    fun webCarVoice(outsideCarPlay: Boolean, tesla: Boolean): Boolean = outsideCarPlay
}

/**
 * Words that follow what the app is installed on: a phone is not a box, and the same feature reads differently on
 * each. Anything that is true of both keeps the neutral word 设备.
 */
class Terms(private val kind: DeviceKind) {
    /** "这台手机" / "这个盒子" — the subject of a sentence. */
    val device: String get() = if (kind == DeviceKind.PHONE) "这台手机" else "这个盒子"
    /** Short form inside a longer phrase. */
    val nickname: String get() = if (kind == DeviceKind.PHONE) "手机" else "盒子"
    /** Boot behaviour: a box runs when plugged in, a phone starts with the system. */
    val bootSub: String get() = if (kind == DeviceKind.PHONE) "开机后自动运行" else "开机后运行并打开主界面，需系统允许"
    /** Where settings are edited from when there is no touch UI. */
    val manageSub: String
        get() = if (kind == DeviceKind.PHONE) "身份导入、网络、启动与诊断，也可以在本机浏览器打开"
        else "身份导入、网络、启动与诊断；盒子无屏时也能设置"
    /** The browser page's own name for the hardware. */
    val browserSetting: String get() = if (kind == DeviceKind.PHONE) "本机" else "盒子"
    /** A device that may not have a file picker. */
    val pickerFallback: String get() = if (kind == DeviceKind.PHONE) "没有文件选择器的设备" else "没有文件选择器的盒子"
    companion object {
        fun of(settings: Settings): Terms = Terms(settings.deviceKind)
    }
}
