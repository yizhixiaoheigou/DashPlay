package com.dashplay.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicInteger
import android.util.Log
import com.playport.server.CarPlayServer
import com.playport.server.ConfigStore
import com.playport.server.ServerConfig
import com.playport.server.TlsCertificate
import io.ktor.server.cio.CIO
import io.ktor.server.engine.EmbeddedServer
import io.ktor.server.engine.embeddedServer
import io.ktor.server.routing.routing
import io.ktor.server.routing.get
import io.ktor.server.response.respondRedirect
import java.io.File
import java.net.InetAddress
import java.nio.file.Path

enum class Status { STARTING, WAITING_NETWORK, NEEDS_IDENTITY, RUNNING, FAILED, STOPPED }

/** What the home screen shows, read once a second. */
data class Snapshot(
    val status: Status,
    val detail: String?,
    val network: LocalNetwork.Choice?,
    val port: Int,
    val viewers: Int,
    val sessionActive: Boolean,
    val wiredActive: Boolean = false,
    val phoneName: String?,
    val ownDomain: String? = null,
) {
    val directAddress: String? get() = network?.let { "${it.address.hostAddress}:$port" }
    val address: String? get() = network?.let { "${ownDomain ?: if (TeslaRootAccess.active) TeslaRootPlan.ADDRESS else it.address.hostAddress}:$port" }
}

/** Runs the receiver (AirPlay listener + browser page) while the app is in use or the box is on. */
class DashPlayService : Service() {
    private var hotspot: ManagedHotspot? = null
    private val wifiRecovery by lazy { WifiRecovery(this) }
    private var server: CarPlayServer? = null
    private var wired: AndroidWiredController? = null
    private var http: EmbeddedServer<*, *>? = null
    private var front: TlsFront? = null
    private val browserFronts = ArrayList<BrowserFront>()
    private var refreshPage: ((LocalNetwork.Choice?) -> Unit)? = null
    /** First-run page for a device with no screen; serves /manage while setup is incomplete (2026-11-03). */
    private var setupBrowserFront: BrowserFront? = null
    private var setupHttp: EmbeddedServer<*, *>? = null
    private var setupAddress: InetAddress? = null
    private var setupPort: Int = 0
    private var ownTlsExpires: Long? = null
    private var ownTlsFingerprint: String? = null
    private var certificateWorker: java.io.Closeable? = null
    private var updateWorker: java.io.Closeable? = null
    private lateinit var teslaAccess: TeslaRootAccess
    private lateinit var usbShare: UsbShareAccess
    /** Always-on HTTP management, plus the optional RTC/JPEG viewer. */
    private var compatHttp: CompatHttpEntry? = null
    /** JPEG stills for pages without WebCodecs; costs nothing until a page asks. */
    private var imageEncoder: com.dashplay.android.tesla.JpegVideoEncoder? = null
    /** The RTC channel (H.264 only, one viewer); null when the build has no native library. */
    private var rtcChannel: AndroidRtcChannel? = null
    /** The Root-free address that backs the compatibility entry; survives page refreshes, released with the entry. */
    private var addressRuntime: TeslaAddressRuntime? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    @Volatile private var stopped = false
    private var hotspotInitialized = false
    private val revision = AtomicInteger()
    private var wakeLock: PowerManager.WakeLock? = null
    private val handler = Handler(Looper.getMainLooper())

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        DriveLog.install(java.io.File(filesDir, "logs"))
        DriveLog.event("service start ${runCatching { packageManager.getPackageInfo(packageName, 0).versionName }.getOrNull()} " +
            "android=${Build.VERSION.SDK_INT} model=${Build.MANUFACTURER} ${Build.MODEL} uptimeMs=${android.os.SystemClock.elapsedRealtime()}")
        teslaAccess = TeslaRootAccess(this)
        usbShare = UsbShareAccess.get(this)
        userStopped = false
        UsbAttachActivity.sync(this)
        startForeground(NOTIFICATION_ID, notification())
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "DashPlay:receiver").apply {
                setReferenceCounted(false)
                acquire()
            }
        hotspot = ManagedHotspot(this) { if (!stopped) {
            usbShare.revoke()
            if (!canStartWiredWithoutLan()) revision.incrementAndGet()
        } }.also { managedHotspot = it }
        // All generations share one worker. A stopped service must finish closing its sockets before a new
        // instance can bind them (identity import, several settings changes and network handoffs can overlap).
        worker.execute(::runReceiver)
        // A box with no screen still has to be set up: keep a setup page reachable while the app runs.
        Thread(::runSetupPage, "dashplay-setup-page").apply { isDaemon = true }.start()
        certificateWorker = AutomaticTls.attach(this)
        updateWorker = OnlineUpdate.attach(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_RESTART) {
            usbShare.revoke()
            if (hotspotInitialized) hotspot?.configure(Settings(this).usesLocalHotspot, retry = true)
            revision.incrementAndGet()
        }
        return START_STICKY
    }

    /** Serves the management page while setup is incomplete, on whatever interface has an address.
     *
     *  Until now the page only existed once the receiver had a configured network and an identity, so a device with no
     *  screen could never be finished from a browser (2026-11-03). This page needs neither: import the identity and
     *  answer the wizard from any browser on the same LAN, then it bows out and leaves the port to the receiver.
     */
    private fun runSetupPage() {
        while (!stopped) {
            try {
                if (Settings(this).setupDone && IdentityImport.present(this)) { closeSetupPage(); return }
                val address = anyLocalAddress()
                if (address != null && (setupBrowserFront == null || setupAddress != address || setupPort != Settings(this).browserPort)) startSetupPage(address)
            } catch (error: Throwable) {
                Log.w(TAG, "setup page unavailable: $error")
            }
            Thread.sleep(3_000)
        }
        closeSetupPage()
    }

    private fun anyLocalAddress(): InetAddress? = runCatching {
        java.util.Collections.list(java.net.NetworkInterface.getNetworkInterfaces()).asSequence()
            .filter { it.isUp && !it.isLoopback }
            .flatMap { java.util.Collections.list(it.inetAddresses).asSequence() }
            .filterIsInstance<java.net.Inet4Address>()
            .firstOrNull { !it.isLoopbackAddress && !it.isLinkLocalAddress }
    }.getOrNull()

    private fun startSetupPage(address: InetAddress) {
        closeSetupPage()
        port = Settings(this).browserPort
        val loopback = InetAddress.getByName(TlsFront.LOOPBACK)
        val backendPort = java.net.ServerSocket(0, 1, loopback).use { it.localPort }
        val management = AndroidManagement(this) {
            handler.postDelayed({ if (!stopped) revision.incrementAndGet() }, 750)
        }
        val server = embeddedServer(CIO, port = backendPort, host = TlsFront.LOOPBACK) {
            management.install(this)
            routing { get("/") { call.respondRedirect("/manage") } }
        }.start(wait = false)
        setupHttp = server
        val entry = BrowserFront(address, port, null, backendPort)
        setupBrowserFront = entry
        entry.start()
        setupAddress = address
        setupPort = port
        Log.i(TAG, "setup page on http://${address.hostAddress}:$port/manage")
        DriveLog.event("page setup http://${address.hostAddress}:$port/manage")
    }

    private fun closeSetupPage() {
        runCatching { setupBrowserFront?.close() }
        setupBrowserFront = null
        runCatching { setupHttp?.stop(500, 1_000) }
        setupHttp = null
        setupAddress = null
        setupPort = 0
    }

    private fun runReceiver() {
        try {
            // The previous worker waits for its asynchronous P2P cleanup before this generation can create a group.
            val initialized = CountDownLatch(1)
            handler.post {
                try {
                    if (!stopped) {
                        hotspotInitialized = true
                        hotspot?.configure(Settings(this).usesLocalHotspot)
                    }
                } finally { initialized.countDown() }
            }
            initialized.await()
            // Clean a previous process's owned rules even if there is no network available now.
            teslaAccess.recover()
            usbShare.recover()
            while (!stopped) {
                var network = waitForNetwork()
                if (stopped) break
                val independentWired = canStartWiredWithoutLan()
                DriveLog.event("receiver network ready lan=${network != null} independentWired=$independentWired uptimeMs=${android.os.SystemClock.elapsedRealtime()}")
                val applied = revision.get()
                startReceiver(network, independentWired)
                // A phone can switch from Wi-Fi to its hotspot after startup; the old address must not remain
                // advertised forever. A failed start retries with backoff, not a new foreground service.
                val retryAt = System.nanoTime() + 15_000_000_000L
                while (!stopped && revision.get() == applied) {
                    val observed = selectedNetwork()
                    wifiRecovery.maintain(Settings(this).network != Network.THIS_HOTSPOT, observed)
                    if (status == Status.RUNNING) detail = if (observed == null) wifiRecovery.message else null
                    if (observed != network) {
                        if (!independentWired) break
                        // The cable listener is on the USB VPN, not on this browser LAN.
                        // Rebind only the page so late Wi-Fi never tears down the USB handshake.
                        usbShare.revoke()
                        network = observed
                        current = observed
                        refreshPage?.invoke(observed)
                    }
                    if (status == Status.FAILED && System.nanoTime() >= retryAt) break
                    // Expiry falls back only while idle. Explicit imports/switches restart after the HTTP response;
                    // watching the store's new fingerprint here would close that request before it can finish.
                    if (running?.hub?.sessionIsActive != true && ownTlsExpires?.let { System.currentTimeMillis() >= it } == true) break
                    if (network != null && AutomaticTls.shouldReload(this, ownTlsFingerprint, running?.hub?.sessionIsActive == true)) break
                    network?.let { teslaAccess.maintain(it, port) }
                    usbShare.maintain(network, Settings(this).usesLocalHotspot && hotspot?.connection?.network == network)
                    // If Android rebuilt its firewall, the current access route may have changed.
                    refreshBrowserUrl()
                    Thread.sleep(NETWORK_RETRY_MS)
                }
                closeReceiver()
            }
        } finally {
            closeReceiver()
            val closed = CountDownLatch(1)
            handler.post {
                val previous = hotspot
                if (previous == null) closed.countDown() else previous.close {
                    if (managedHotspot === previous) managedHotspot = null
                    closed.countDown()
                }
            }
            closed.await()
            runCatching { wakeLock?.release() }
            status = Status.STOPPED
        }
    }

    private fun canStartWiredWithoutLan(): Boolean = Settings(this).let {
        it.iphoneLink == IphoneLink.WIRED && it.usesUsb
    }

    private fun startReceiver(network: LocalNetwork.Choice?, independentWired: Boolean) {
        try {
            status = Status.STARTING
            detail = null
            current = network
            val webRoot = extractWebAssets()
            val stateDir = File(filesDir, "state").apply { mkdirs() }.toPath()
            val identityDir = IdentityImport.identityDir(this).apply { mkdirs() }.toPath()
            val store = ConfigStore(stateDir)
            val saved = store.load()?.toServerDefaults(identityDir, stateDir)
                ?: ServerConfig(identityDir = identityDir, stateDir = stateDir, deviceName = "DashPlay")
            val settings = Settings(this)
            val plan = settings.plan()
            // The compatibility entry needs an address the car can reach. Establish it once per service lifetime and
            // keep it across page rebuilds; turning the entry off releases it (closePage), never the cable lease.
            if (settings.compatHttp && settings.network == Network.THIS_HOTSPOT) {
                val runtime = addressRuntime ?: TeslaAddressRuntime(this).also {
                    addressRuntime = it
                    addressRuntimeInstance = it
                }
                runtime.invalidate()
                if (!runtime.start()) DriveLog.event("compat http: address unavailable: ${runtime.describe()}")
            } else { addressRuntime?.stop(); addressRuntime = null; addressRuntimeInstance = null }
            val automatic = if (settings.usesLocalHotspot) hotspot?.connection else null
            check(independentWired || !settings.usesLocalHotspot || automatic != null) { "本地热点已断开" }
            // Two different names now: the box's own hotspot, or the external network the phone is told to join.
            val wifi = when {
                automatic != null -> Settings.Hotspot(automatic.ssid, automatic.password, false)
                else -> settings.connectionWifi()
            }
            val bluetooth = android.bluetooth.BluetoothAdapter.getDefaultAdapter() != null
            val config = saved.copy(
                httpPort = settings.browserPort,
                deviceName = settings.deviceName,
                // Wired IPv6 traffic has its own listener, created by AndroidWiredController.
                bindAddress = if (independentWired) TlsFront.LOOPBACK else checkNotNull(network).address.hostAddress,
                accessToken = settings.accessToken,
                carVoice = settings.webCarVoice,
                audioRoutes = settings.audioRoutes(),
                mdns = "jmdns",
                // Wireless CarPlay: the iPhone is invited over Bluetooth and told this Wi-Fi; channel 0 is "auto".
                wireless = !independentWired && plan.wireless && bluetooth && wifi != null,
                wifiSsid = wifi?.ssid,
                wifiPassphrase = wifi?.passphrase,
                wifiChannel = automatic?.channel ?: settings.wifiChannel,
                wifiSecurity = when (if (automatic != null) WifiSecurity.WPA2 else settings.wifiSecurity) {
                    WifiSecurity.OPEN -> com.shilapi.xcertplay.transport.Iap2WirelessSecurity.NONE
                    WifiSecurity.WPA2 -> com.shilapi.xcertplay.transport.Iap2WirelessSecurity.WPA_WPA2
                    WifiSecurity.WPA3 -> com.shilapi.xcertplay.transport.Iap2WirelessSecurity.WPA3_ONLY
                },
                bluetoothDeviceAddress = settings.iphoneAddress,
            )
            port = config.httpPort
            writeBrowserAccess(webRoot, settings, config.httpPort)
            // Stills are produced only while a page asks for them: the encoder costs nothing on a cable-only box.
            val stills = if (settings.compatHttp) com.dashplay.android.tesla.JpegVideoEncoder(
                com.dashplay.android.tesla.ImageStreamConfig(fps = 15, maxLongSide = 1280, quality = 85),
                { info, jpeg -> running?.hub?.broadcastImage(info.sessionEpoch, info.sequence, info.width, info.height, info.ageMs, jpeg) },
                { type -> running?.bridge?.requestKeyFrame(type) },
            ).also { it.start(); imageEncoder = it } else null
            // The RTC channel exists only where the native library does; elsewhere the page keeps stills.
            if (settings.compatHttp && com.dashplay.android.tesla.RtcBridge.available && rtcChannel == null) {
                rtcChannel = AndroidRtcChannel()
            }
            // One media consumer fans out to both transports: stills and RTC see the same encoded picture.
            val media = when {
                stills == null && !com.dashplay.android.tesla.RtcBridge.available -> null
                stills == null -> rtcChannel?.let { rtc -> RelayConsumer(rtc = rtc) }
                else -> RelayConsumer(stills = stills, rtc = rtcChannel)
            }
            val receiver = CarPlayServer(config, store, AndroidBluetoothLink(this, settings.iphoneAddress, wifi?.ssid),
                idleReleaseMs = settings.idleReleaseSeconds * 1000L,
                platformPresence = AndroidPhones.get(this)::command,
                platformController = AndroidPhones.get(this)::authorize,
                platformCableRole = com.playport.server.CableRole(
                    File(filesDir, "state/cable-carplay-off").toPath(),
                    static = !settings.wiredCarPlaySupported || settings.iphoneLink == IphoneLink.WIRELESS,
                ),
                platformVideoConsumer = media,
                platformRtc = rtcChannel,
                platformAudioRoutes = audio@ { category, destination ->
                    if (category != null) {
                        // Match N1/OpenWrt: old viewers offering per-category routing must reload.
                        if (category != "navigation" || destination !in com.shilapi.xcertplay.airplay.AudioRoutes.destinations)
                            return@audio null
                        if (settings.audioOutsideCarPlay != (destination == "bluetooth")) {
                            settings.audioOutsideCarPlay = destination == "bluetooth"
                            // Let the successful response reach the browser before closing its connection.
                            handler.postDelayed({ if (!stopped) revision.incrementAndGet() }, 750)
                        }
                    }
                    JSONObject().put("supported", true).put("routes", JSONObject(settings.audioRoutes())).toString()
                })
            // The stills producer runs only while a page asks for stills; the page's "stills" message flips this.
            receiver.hub.onStillsChange = {
                val subscribed = receiver.hub.stillsViewers > 0
                DriveLog.event("jpeg demand wanted=$subscribed viewers=${receiver.hub.stillsViewers}")
                imageEncoder?.wanted = subscribed
                if (!subscribed) imageEncoder?.releaseDecoderForIdle()
            }
            server = receiver
            running = receiver
            multicastLock = (applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
                .createMulticastLock("dashplay-mdns").apply { setReferenceCounted(false); acquire() }

            // Wired CarPlay first. The cable handshake (and the iPhone switching to NCM) is the long pole at
            // boot; the page/TLS/Ktor stack below costs ~9 s on this box and nothing on the cable path waits
            // for it. Keeping it ahead of the cable added that delay to every cold start (2026-11-02).
            val hasIdentity = IdentityImport.present(this)
            if (hasIdentity) {
                receiver.mfi
                receiver.start()
                UsbAttachActivity.sync(this)
                if (settings.usesUsb) wired = AndroidWiredController(this, receiver).also { it.start() }
                status = Status.RUNNING
                Log.i(TAG, "AirPlay listener on ${receiver.bindAddress.hostAddress}:${config.airPlayPort} wireless=${config.wireless}")
            } else {
                status = Status.NEEDS_IDENTITY
            }

            // A pure wired receiver survives browser LAN changes. Keep the hub and cable alive,
            // recreating only HTTPS/Ktor and the LAN access rules when an address arrives or changes.
            refreshPage = { pageNetwork ->
                addressRuntime?.invalidate()
                closePage()
                if (pageNetwork != null) {
                    try {
                        val loopback = InetAddress.getByName(TlsFront.LOOPBACK)
                        val management = AndroidManagement(this) {
                            handler.postDelayed({ if (!stopped) {
                                hotspot?.configure(Settings(this).usesLocalHotspot, retry = true)
                                revision.incrementAndGet()
                            } }, 750)
                        }
                        // HTTP management starts first and remains available even without usable TLS material.
                        val entry = CompatHttpEntry()
                        compatHttp = entry
                        entry.start(listOf(loopback), 0, webRoot, receiver.hub, settings.compatHttp) {
                            management.install(this)
                        }.getOrThrow()
                        val httpPort = entry.localPort
                        var tlsPort: Int? = null
                        httpsError = null
                        runCatching {
                            val tlsAddresses = listOf(pageNetwork.address) +
                                if (settings.teslaRootAccess && settings.car == Car.TESLA) listOf(InetAddress.getByName(TeslaRootPlan.ADDRESS)) else emptyList()
                            val tlsState = if (settings.teslaRootAccess && settings.car == Car.TESLA) stateDir.resolve("tls-tesla") else stateDir
                            val ownTls = OwnTls.store(this).selected()
                            val tls = ownTls?.material ?: TlsCertificate.loadOrCreate(tlsState, tlsAddresses)
                            ownTlsExpires = ownTls?.expires
                            ownTlsFingerprint = ownTls?.fingerprint
                            val backendPort = java.net.ServerSocket(0, 1, loopback).use { it.localPort }
                            http = embeddedServer(CIO, port = backendPort, host = TlsFront.LOOPBACK) {
                                management.install(this)
                                receiver.hub.install(this, webRoot)
                            }
                            http?.start(wait = false)
                            val tlsFront = TlsFront(tls.keyStore, tls.password, loopback, 0, backendPort)
                            front = tlsFront
                            tlsFront.start()
                            tlsPort = tlsFront.localPort
                            httpsReady = true
                            activeDomain = ownTls?.domain
                        }.onFailure {
                            httpsError = "HTTPS 入口不可用：${it.message ?: it.javaClass.simpleName}；请用 HTTP 管理页检查证书设置。"
                            Log.w(TAG, "HTTPS unavailable; HTTP management remains available", it)
                            runCatching { front?.close() }; front = null
                            runCatching { http?.stop(0, 500) }; http = null
                        }
                        compatEndpoint = null
                        compatError = null
                        val publicFront = BrowserFront(pageNetwork.address, config.httpPort, tlsPort, httpPort)
                        browserFronts += publicFront
                        publicFront.start()
                        pageReady = true
                        // Local management still uses the same port, including on devices with a screen.
                        if (pageNetwork.address != loopback) {
                            val local = BrowserFront(loopback, config.httpPort, tlsPort, httpPort)
                            browserFronts += local
                            runCatching { local.start() }.onFailure { Log.w(TAG, "local page unavailable: $it") }
                        }
                        if (settings.compatHttp) {
                            val addressHost = addressRuntime
                                ?.takeIf { it.state == TeslaVpnAddress.State.ADDRESS_PRESENT || it.state == TeslaVpnAddress.State.PEER_VERIFIED }
                                ?.let { InetAddress.getByName(TeslaVpnAddress().address) }
                            val addressReady = addressHost == null || addressHost == pageNetwork.address || runCatching {
                                val vpnFront = BrowserFront(addressHost, config.httpPort, tlsPort, httpPort)
                                browserFronts += vpnFront
                                vpnFront.start()
                            }.onFailure {
                                compatError = "免 Root 地址监听失败：${it.message}"
                                DriveLog.event("ERROR compat address: $compatError")
                            }.isSuccess
                            if (addressReady) {
                                compatEndpoint = BrowserEndpoint("http", pageNetwork.address.hostAddress.orEmpty(),
                                    config.httpPort, BrowserEndpoint.COMPAT_PURPOSE, true)
                                DriveLog.event("compat http on ${compatEndpoint?.url()}")
                            }
                        }
                        Log.i(TAG, "page on ${pageNetwork.name} port=${config.httpPort} HTTP viewer=${settings.compatHttp}")
                        DriveLog.event("page ready port=${config.httpPort} HTTPS=$httpsReady uptimeMs=${android.os.SystemClock.elapsedRealtime()}")
                        teslaAccess.start(pageNetwork, config.httpPort)
                        refreshBrowserUrl()
                    } catch (error: Throwable) {
                        closePage()
                        compatError = "网页入口启动失败：${error.message ?: error.javaClass.simpleName}"
                        Log.w(TAG, "page failed to start; receiver keeps running", error)
                        DriveLog.event("ERROR page failed to start: ${error.javaClass.simpleName}: ${error.message}")
                    }
                } else {
                    teslaAccess.waitingForNetwork()
                    Log.i(TAG, "wired receiver ready; waiting for browser LAN")
                }
            }
            refreshPage?.invoke(network)
            if (!hasIdentity) return
        } catch (error: Throwable) {
            Log.e(TAG, "receiver failed to start", error)
            DriveLog.event("ERROR receiver failed to start: ${error.javaClass.simpleName}: ${error.message}")
            status = Status.FAILED
            detail = "${error.javaClass.simpleName}: ${error.message}"
        }
    }

    private fun selectedNetwork(): LocalNetwork.Choice? =
        if (Settings(this).usesLocalHotspot) hotspot?.connection?.network else LocalNetwork.choose(this)

    private fun waitForNetwork(): LocalNetwork.Choice? {
        var waitingRevision = revision.get()
        while (!stopped) {
            if (revision.get() != waitingRevision) {
                teslaAccess.close()
                waitingRevision = revision.get()
            }
            val observed = selectedNetwork()
            wifiRecovery.maintain(Settings(this).network != Network.THIS_HOTSPOT, observed)
            observed?.let { return it }
            if (canStartWiredWithoutLan()) return null
            teslaAccess.waitingForNetwork()
            usbShare.maintain(null, false)
            val local = Settings(this).usesLocalHotspot
            status = if (local && hotspot?.phase == ManagedHotspot.Phase.FAILED) Status.FAILED else Status.WAITING_NETWORK
            detail = if (local) hotspot?.message else wifiRecovery.message
            Thread.sleep(NETWORK_WAIT_MS)
        }
        return null
    }

    private fun refreshBrowserUrl() {
        if (!pageReady) { url = null; return }
        val settings = Settings(this)
        url = if (settings.compatHttp) {
            compatEndpoint?.let {
                val host = if (settings.network == Network.THIS_HOTSPOT) TeslaVpnAddress().address else it.host
                it.copy(host = host).url(settings.accessToken)
            }
        } else if (httpsReady) snapshot().address?.let { "https://$it/?token=${settings.accessToken}" } else null
    }

    private fun writeBrowserAccess(root: Path, settings: Settings, httpPort: Int) {
        val flags = JSONObject().put("token", settings.accessToken).put("settingsPort", httpPort).put("settingsPath", "/manage")
            .put("tesla", settings.car == Car.TESLA)
            .put("carMusic", settings.webCarMusic).put("carVoice", settings.webCarVoice)
            .put("audioRoutes", JSONObject(settings.audioRoutes()))
            // Which picture channel the owner chose; the page reads it per feature and never adopts a WebCodecs
            // requirement it cannot meet on the plain-HTTP entry (2026-11-03).
            .put("videoTransport", settings.videoTransport.name)
            .put("rtcAvailable", settings.compatHttp && com.dashplay.android.tesla.RtcBridge.available)
        File(root.toFile(), "receiver-access.json").writeText(flags.toString())
    }

    private fun extractWebAssets(): Path {
        val root = File(filesDir, "web")
        root.deleteRecursively()
        fun copy(assetPath: String, target: File) {
            val children = assets.list(assetPath).orEmpty()
            if (children.isEmpty()) {
                target.parentFile?.mkdirs()
                assets.open(assetPath).use { input -> target.outputStream().use(input::copyTo) }
            } else {
                children.forEach { copy("$assetPath/$it", File(target, it)) }
            }
        }
        copy("web", root)
        return root.toPath()
    }

    private fun notification(): Notification {
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL, getString(R.string.app_name), NotificationManager.IMPORTANCE_LOW),
            )
            Notification.Builder(this, CHANNEL)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 23) PendingIntent.FLAG_IMMUTABLE else 0),
        )
        return builder
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle(getString(R.string.app_name))
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        DriveLog.event("service stop")
        stopped = true
        usbShare.revoke()
        certificateWorker?.close()
        updateWorker?.close()
        // Keep lifecycle handoff callbacks queued; each checks stopped before starting new work.
        super.onDestroy()
    }

    private fun closePage() {
        pageReady = false
        httpsReady = false
        url = null
        activeDomain = null
        ownTlsExpires = null
        ownTlsFingerprint = null
        browserFronts.forEach { runCatching { it.close() } }
        browserFronts.clear()
        runCatching { compatHttp?.close() }
        compatHttp = null
        compatEndpoint = null
        if (!Settings(this).compatHttp) { runCatching { addressRuntime?.stop() }; addressRuntime = null }
        runCatching { front?.close() }
        runCatching { http?.stop(500, 1_000) }
        runCatching { teslaAccess.close() }
        front = null
        http = null
    }

    private fun closeReceiver() {
        url = null
        running = null
        current = null
        activeDomain = null
        ownTlsExpires = null
        ownTlsFingerprint = null
        runCatching { wired?.close() }
        wired = null
        runCatching { usbShare.close() }
        runCatching { server?.close() }
        runCatching { imageEncoder?.close() }
        imageEncoder = null
        runCatching { rtcChannel?.close() }
        rtcChannel = null
        runCatching { addressRuntime?.stop() }
        addressRuntime = null
        addressRuntimeInstance = null
        refreshPage = null
        closePage()
        runCatching { multicastLock?.release() }
        server = null
        front = null
        http = null
        multicastLock = null
    }

    companion object {
        internal fun activeReceiver(): CarPlayServer? = running
        private const val TAG = "DashPlay"
        private const val CHANNEL = "receiver"
        private const val NOTIFICATION_ID = 1
        private const val NETWORK_RETRY_MS = 2_000L
        private const val NETWORK_WAIT_MS = 500L
        private const val ACTION_RESTART = "com.dashplay.android.RESTART"
        private val worker = Executors.newSingleThreadExecutor { task -> Thread(task, "dashplay-receiver") }

        @Volatile var managedHotspot: ManagedHotspot? = null
            private set
        /** The HTTP compatibility entry as it is now (null when off or failed); read by the management page. */
        @Volatile var compatEndpoint: BrowserEndpoint? = null
            private set
        /** Why the compatibility entry is not running, when it was asked for. */
        @Volatile var compatError: String? = null
            private set

        /** The Root-free address as the running service sees it; null when the entry never asked for one. */
        internal fun addressState(): Pair<TeslaVpnAddress.State, String>? =
            addressRuntimeInstance?.let { it.state to it.describe() }
        @Volatile var status: Status = Status.STOPPED
            private set
        @Volatile var detail: String? = null
            private set
        @Volatile var url: String? = null
            private set
        @Volatile private var running: CarPlayServer? = null
        @Volatile private var current: LocalNetwork.Choice? = null
        @Volatile private var activeDomain: String? = null
        @Volatile internal var httpsReady: Boolean = false
            private set
        @Volatile internal var httpsError: String? = null
            private set
        @Volatile internal var pageReady: Boolean = false
            private set
        @Volatile private var port: Int = BrowserPort.DEFAULT
        /** Set by the live service so the management page can report the address without binding to it again. */
        @Volatile internal var addressRuntimeInstance: TeslaAddressRuntime? = null
        /** The user pressed Stop: the home screen does not start it again by itself. */
        @Volatile var userStopped = false

        fun snapshot(): Snapshot {
            val receiver = running
            val hub = receiver?.hub
            return Snapshot(
                status = status,
                detail = detail,
                network = current,
                port = port,
                viewers = hub?.viewerCount ?: 0,
                sessionActive = hub?.sessionIsActive ?: false,
                wiredActive = receiver?.wiredSessionActive() == true,
                phoneName = hub?.sessionPhoneName,
                ownDomain = activeDomain,
            )
        }

        fun start(context: Context) {
            val service = Intent(context, DashPlayService::class.java)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(service) else context.startService(service)
        }

        /** Settings changed: start over with them (the page reconnects by itself). */
        fun restart(context: Context) {
            val intent = Intent(context, DashPlayService::class.java).setAction(ACTION_RESTART)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent) else context.startService(intent)
        }

        fun stop(context: Context) {
            userStopped = true
            UsbAttachActivity.sync(context, active = false)
            context.stopService(Intent(context, DashPlayService::class.java))
        }
    }
}
