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
    private var server: CarPlayServer? = null
    private var wired: AndroidWiredController? = null
    private var http: EmbeddedServer<*, *>? = null
    private var front: TlsFront? = null
    private var localFront: TlsFront? = null
    private var ownTlsExpires: Long? = null
    private var ownTlsFingerprint: String? = null
    private var certificateWorker: java.io.Closeable? = null
    private var updateWorker: java.io.Closeable? = null
    private lateinit var teslaAccess: TeslaRootAccess
    private lateinit var usbShare: UsbShareAccess
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
            "android=${Build.VERSION.SDK_INT} model=${Build.MANUFACTURER} ${Build.MODEL}")
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
        hotspot = ManagedHotspot(this) { if (!stopped) { usbShare.revoke(); revision.incrementAndGet() } }.also { managedHotspot = it }
        // All generations share one worker. A stopped service must finish closing its sockets before a new
        // instance can bind them (identity import, several settings changes and network handoffs can overlap).
        worker.execute(::runReceiver)
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
                val network = waitForNetwork() ?: break
                val applied = revision.get()
                startReceiver(network)
                // A phone can switch from Wi-Fi to its hotspot after startup; the old address must not remain
                // advertised forever. A failed start retries with backoff, not a new foreground service.
                val retryAt = System.nanoTime() + 15_000_000_000L
                while (!stopped && revision.get() == applied && selectedNetwork() == network) {
                    if (status == Status.FAILED && System.nanoTime() >= retryAt) break
                    // Expiry falls back only while idle. Explicit imports/switches restart after the HTTP response;
                    // watching the store's new fingerprint here would close that request before it can finish.
                    if (running?.hub?.sessionIsActive != true && ownTlsExpires?.let { System.currentTimeMillis() >= it } == true) break
                    if (AutomaticTls.shouldReload(this, ownTlsFingerprint, running?.hub?.sessionIsActive == true)) break
                    teslaAccess.maintain(network, port)
                    usbShare.maintain(network, Settings(this).usesLocalHotspot && hotspot?.connection?.network == network)
                    // If Android rebuilt its firewall, the current access route may have changed.
                    url = "https://${snapshot().address}/?token=${Settings(this).accessToken}"
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

    private fun startReceiver(network: LocalNetwork.Choice) {
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
            val automatic = if (settings.usesLocalHotspot) hotspot?.connection else null
            check(!settings.usesLocalHotspot || automatic != null) { "本地热点已断开" }
            // Two different names now: the box's own hotspot, or the external network the phone is told to join.
            val wifi = when {
                automatic != null -> Settings.Hotspot(automatic.ssid, automatic.password, false)
                settings.usesLocalHotspot -> settings.hotspot()
                else -> settings.uplink()?.let { Settings.Hotspot(it.ssid, it.passphrase, false) }
            }
            val bluetooth = android.bluetooth.BluetoothAdapter.getDefaultAdapter() != null
            val config = saved.copy(
                deviceName = settings.deviceName,
                bindAddress = network.address.hostAddress,
                accessToken = settings.accessToken,
                carVoice = settings.webCarVoice,
                audioRoutes = settings.audioRoutes(),
                mdns = "jmdns",
                // Wireless CarPlay: the iPhone is invited over Bluetooth and told this Wi-Fi; channel 0 is "auto".
                wireless = plan.wireless && bluetooth && wifi != null,
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
            val receiver = CarPlayServer(config, store, AndroidBluetoothLink(this, settings.iphoneAddress),
                idleReleaseMs = settings.idleReleaseSeconds * 1000L,
                platformPresence = AndroidPhones.get(this)::command,
                platformController = AndroidPhones.get(this)::authorize,
                platformCableRole = com.playport.server.CableRole(
                    File(filesDir, "state/cable-carplay-off").toPath(),
                    static = settings.iphoneLink == IphoneLink.WIRELESS,
                ),
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
            server = receiver
            running = receiver
            multicastLock = (applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
                .createMulticastLock("dashplay-mdns").apply { setReferenceCounted(false); acquire() }

            // The page comes up first, with or without an identity: a box without a screen shows its state there.
            val tlsAddresses = listOf(receiver.bindAddress) +
                if (settings.teslaRootAccess && settings.car == Car.TESLA) listOf(InetAddress.getByName(TeslaRootPlan.ADDRESS)) else emptyList()
            // Keep the original certificate; the separate Tesla certificate includes the stable access address.
            val tlsState = if (settings.teslaRootAccess && settings.car == Car.TESLA) stateDir.resolve("tls-tesla") else stateDir
            val ownTls = OwnTls.store(this).selected()
            val tls = ownTls?.material ?: TlsCertificate.loadOrCreate(tlsState, tlsAddresses)
            ownTlsExpires = ownTls?.expires
            ownTlsFingerprint = ownTls?.fingerprint
            val loopback = InetAddress.getByName(TlsFront.LOOPBACK)
            val backendPort = java.net.ServerSocket(0, 1, loopback).use { it.localPort }
            val management = AndroidManagement(this) {
                handler.postDelayed({ if (!stopped) {
                    hotspot?.configure(Settings(this).usesLocalHotspot, retry = true)
                    revision.incrementAndGet()
                } }, 750)
            }
            http = embeddedServer(CIO, port = backendPort, host = TlsFront.LOOPBACK) {
                management.install(this)
                receiver.hub.install(this, webRoot)
            }
            http?.start(wait = false)
            front = TlsFront(tls.keyStore, tls.password, receiver.bindAddress, config.httpPort, backendPort).also { it.start() }
            // Also on this device itself: a box with a screen can show the page, and adb can reach it.
            localFront = runCatching { TlsFront(tls.keyStore, tls.password, loopback, config.httpPort, backendPort).also { it.start() } }
                .onFailure { Log.w(TAG, "page on this device unavailable: $it") }.getOrNull()
            activeDomain = ownTls?.domain
            url = "https://${snapshot().address}/?token=${config.accessToken}"
            Log.i(TAG, "page on ${network.name} https://${network.address.hostAddress}:${config.httpPort}/ backend=$backendPort")
            teslaAccess.start(network, config.httpPort)
            url = "https://${snapshot().address}/?token=${config.accessToken}"

            if (!IdentityImport.present(this)) {
                status = Status.NEEDS_IDENTITY
                return
            }
            receiver.mfi
            receiver.start()
            UsbAttachActivity.sync(this)
            if (settings.usesUsb) wired = AndroidWiredController(this, receiver).also { it.start() }
            status = Status.RUNNING
            Log.i(TAG, "AirPlay listener on ${receiver.bindAddress.hostAddress}:${config.airPlayPort} wireless=${config.wireless}")
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
            selectedNetwork()?.let { return it }
            teslaAccess.waitingForNetwork()
            usbShare.maintain(null, false)
            val local = Settings(this).usesLocalHotspot
            status = if (local && hotspot?.phase == ManagedHotspot.Phase.FAILED) Status.FAILED else Status.WAITING_NETWORK
            detail = if (local) hotspot?.message else null
            Thread.sleep(NETWORK_RETRY_MS)
        }
        return null
    }

    private fun writeBrowserAccess(root: Path, settings: Settings, httpPort: Int) {
        val flags = JSONObject().put("token", settings.accessToken).put("settingsPort", httpPort).put("settingsPath", "/manage")
            .put("tesla", settings.car == Car.TESLA)
            .put("carMusic", settings.webCarMusic).put("carVoice", settings.webCarVoice)
            .put("audioRoutes", JSONObject(settings.audioRoutes()))
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
        runCatching { front?.close() }
        runCatching { localFront?.close() }
        runCatching { http?.stop(500, 1_000) }
        runCatching { teslaAccess.close() }
        runCatching { multicastLock?.release() }
        server = null
        front = null
        localFront = null
        http = null
        multicastLock = null
    }

    companion object {
        internal fun activeReceiver(): CarPlayServer? = running
        private const val TAG = "DashPlay"
        private const val CHANNEL = "receiver"
        private const val NOTIFICATION_ID = 1
        private const val NETWORK_RETRY_MS = 2_000L
        private const val ACTION_RESTART = "com.dashplay.android.RESTART"
        private val worker = Executors.newSingleThreadExecutor { task -> Thread(task, "dashplay-receiver") }

        @Volatile var managedHotspot: ManagedHotspot? = null
            private set
        @Volatile var status: Status = Status.STOPPED
            private set
        @Volatile var detail: String? = null
            private set
        @Volatile var url: String? = null
            private set
        @Volatile private var running: CarPlayServer? = null
        @Volatile private var current: LocalNetwork.Choice? = null
        @Volatile private var activeDomain: String? = null
        @Volatile private var port: Int = 8080
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
