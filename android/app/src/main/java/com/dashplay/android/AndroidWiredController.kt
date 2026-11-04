package com.dashplay.android

import android.app.PendingIntent
import android.content.*
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.net.VpnService
import android.os.Build
import android.os.IBinder
import com.playport.server.CarPlayServer
import com.playport.server.PairRecordStore
import com.shilapi.xcertplay.iap2.session.Iap2Session
import com.shilapi.xcertplay.mfi.Iap2MfiAuthenticationClient
import com.shilapi.xcertplay.transport.*
import java.io.Closeable
import java.io.IOException
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Uses the shared wired protocol; only USB ownership and local NCM networking are Android-specific. */
class AndroidWiredController(private val context: Context, private val server: CarPlayServer) : Closeable {
    @Volatile private var stopped = false
    @Volatile private var selectedDevice: String? = null
    @Volatile private var vpn: UsbVpnService? = null
    private val manager = context.getSystemService(Context.USB_SERVICE) as UsbManager
    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private val resources = Any()
    private val usbChanges = UsbChangeSignal()
    private var opened: AndroidUsbAccess.Opened? = null
    private var mux: Iap2UsbMuxHost? = null
    private var session: Iap2Session? = null
    private var phoneDiagnostics: WiredPhoneDiagnostics? = null
    private val displayRestart = java.util.concurrent.atomic.AtomicReference<Iap2Session?>(null)
    private var lease: UsbVpnService.Lease? = null
    private val permissionRequested = mutableMapOf<String, Long>()
    @Volatile private var bound = CountDownLatch(1)
    private var binding = false
    private var registered = false
    private var transitions = 0
    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            vpn = (binder as? UsbVpnService.LocalBinder)?.service; bound.countDown()
        }
        override fun onServiceDisconnected(name: ComponentName?) { vpn = null; Thread({ closeLink() }, "usb-vpn-disconnected").start() }
    }
    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action == UsbManager.ACTION_USB_DEVICE_DETACHED) {
                @Suppress("DEPRECATION") val device = intent.getParcelableExtra<UsbDevice>(UsbManager.EXTRA_DEVICE)
                val detached = synchronized(resources) { opened?.takeIf { device?.deviceName == selectedDevice } }
                if (detached != null) Thread({ closeLink(detached) }, "usb-detach").start()
            }
            usbChanges.changed()
        }
    }

    fun start() {
        server.platformWiredRestart = {
            // A display change needs another AirPlay session, not a new USB network.
            // Keep iAP2/NCM/TUN and ask the phone again; the watcher falls back if needed.
            synchronized(resources) { session?.let { displayRestart.set(it) } }
        }
        server.platformWiredReselect = { Thread({ closeLink() }, "usb-reselect").start() }
        val filter = IntentFilter(UsbManager.ACTION_USB_DEVICE_DETACHED).apply {
            addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED)
        }
        if (Build.VERSION.SDK_INT >= 33) {
            val permissionFilter = IntentFilter(filter).apply { addAction(PERMISSION) }
            context.registerReceiver(receiver, permissionFilter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            // Only protected system USB broadcasts on legacy Android. Permission grants are
            // still detected by the bounded 500 ms poll; do not expose an app-action receiver.
            context.registerReceiver(receiver, filter)
        }
        registered = true
        Thread(::run, "android-wired-carplay").apply { isDaemon = true; start() }
    }

    private fun state(phase: String, message: String) {
        if (stopped) return
        if (phase != status || message != detail) DriveLog.event("wired $phase: $message uptimeMs=${android.os.SystemClock.elapsedRealtime()}")
        status = phase; detail = message
    }

    private fun run() {
        try {
            while (!stopped) {
                val observedUsb = usbChanges.snapshot()
                if (VpnService.prepare(context) != null) {
                    server.cableUnplugged()
                    state("CONSENT", "请在数据线连接设置中允许本地 VPN 通道")
                    usbChanges.awaitChange(observedUsb, 1000); continue
                }
                val devices = AndroidUsbAccess.candidates(manager)
                val now = android.os.SystemClock.elapsedRealtime()
                permissionRequested.keys.retainAll(devices.map { it.deviceName }.toSet())
                if (devices.size != 1) {
                    server.cableUnplugged(); transitions = 0
                    state("CABLE", if (devices.isEmpty()) "请用 OTG 数据线连接 iPhone" else "请只连接一部 iPhone")
                    usbChanges.awaitChange(observedUsb, 1000); continue
                }
                val device = devices.single()
                selectedDevice = device.deviceName
                if (!manager.hasPermission(device)) {
                    state("PERMISSION", "请允许 DashPlay 访问 iPhone；可勾选「始终打开」，部分系统仍会要求再次确认")
                    if (device.deviceName !in permissionRequested) {
                        permissionRequested[device.deviceName] = now
                        main.post {
                            if (!stopped && manager.deviceList.containsKey(device.deviceName) && !manager.hasPermission(device)) {
                                val pending = PendingIntent.getBroadcast(context, device.deviceId,
                                    Intent(PERMISSION).setPackage(context.packageName), PendingIntent.FLAG_UPDATE_CURRENT or
                                        (if (Build.VERSION.SDK_INT >= 23) PendingIntent.FLAG_IMMUTABLE else 0))
                                runCatching { manager.requestPermission(device, pending) }
                            }
                        }
                    }
                    usbChanges.awaitChange(observedUsb, 500); continue
                }
                if (transitions >= 3) {
                    state("FAILED", "iPhone 未切换到 CarPlay 模式，请重新插线或重试")
                    usbChanges.awaitChange(observedUsb, 1000); continue
                }
                var reenumerating = false
                var openingUsb = false
                try {
                    // Decide before open() changes the phone's USB mode, as on the 11.02 receiver.
                    // A live wireless session keeps the cable for power/internet; an explicit wired
                    // selection has already set cablePhone and overrides that temporary choice.
                    val carPlay = Settings(context).wiredCarPlaySupported && server.cableRole.carPlayOnCable() && server.prepareCablePhone()
                    state("STARTING", if (carPlay) "正在建立数据线连接" else "正在准备 USB 网络通道")
                    openingUsb = true
                    val usb = AndroidUsbAccess.open(manager, device, carPlay = carPlay)
                    openingUsb = false
                    synchronized(resources) { if (stopped) { usb.close(); return }; opened = usb }
                    if (carPlay) {
                        runSession(usb)
                    } else {
                        server.cableWithoutCarPlay()
                        runTetherOnly(usb)
                    }
                } catch (_: AndroidUsbAccess.Reenumerating) {
                    transitions++
                    state("REENUMERATING", "等待 iPhone 重新连接 USB，出现授权提示时请允许")
                    reenumerating = true
                } catch (error: Exception) {
                    DriveLog.event("wired failure stage=${if (openingUsb) "USB_SETUP" else "CARPLAY"}: ${error.javaClass.simpleName}: ${error.message}")
                    state("FAILED", if (openingUsb) "iPhone USB 通道初始化失败，正在重试；持续失败时请重新插线"
                        else if (status == "TRUST") "iPhone 信任授权未完成，请解锁并在 iPhone 上确认"
                        else "CarPlay 数据线连接中断，正在重试；请确认 iPhone 已解锁")
                } finally { closeLink(); releaseVpn() }
                if (reenumerating && !stopped) {
                    val started = android.os.SystemClock.elapsedRealtime()
                    val ready = usbChanges.awaitReady(3500) {
                        val next = AndroidUsbAccess.candidates(manager)
                        next.size == 1 && next.single().deviceName != device.deviceName
                    }
                    DriveLog.event("wired USB reenumeration ${if (ready) "ready" else "timeout"} after ${android.os.SystemClock.elapsedRealtime() - started} ms")
                    if (ready) continue
                }
                // Genuine failures still back off; broadcasts must not turn errors into a busy loop.
                if (!stopped) Thread.sleep(1500)
            }
        } finally {
            closeLink(); server.cableUnplugged()
            releaseVpn()
        }
    }

    private fun runTetherOnly(usb: AndroidUsbAccess.Opened) {
        transitions = 0
        if (Settings(context).usbInternet) {
            if (!binding) {
                binding = context.bindService(Intent(context, UsbVpnService::class.java).setAction(UsbVpnService.LOCAL_BIND), connection, Context.BIND_AUTO_CREATE)
                if (!binding) throw IOException("USB VPN 服务无法启动")
            }
            if (!bound.await(10, TimeUnit.SECONDS)) throw IOException("USB VPN 服务启动超时")
            val service = vpn ?: throw IOException("USB VPN 服务已断开")
            val mac = server.deviceId.split(':').map { it.toInt(16).toByte() }.toByteArray()
            val tunnel = service.attach(null as NcmUsbBridge?, mac, usb::openInternet) { closeLink(usb) }
            synchronized(resources) { if (stopped || opened !== usb) { tunnel.close(); return }; lease = tunnel }
            state("TETHER", "数据线仅用于借用网络；CarPlay 使用无线连接")
        } else {
            state("TETHER", "数据线当前不做 CarPlay（可在画面设置中切回有线）")
        }
        while (!stopped && synchronized(resources) { opened === usb } &&
            manager.deviceList.containsKey(selectedDevice) && !server.cableRole.carPlayOnCable()) {
            Thread.sleep(250)
        }
    }

    private fun runSession(usb: AndroidUsbAccess.Opened) {
        val pipe = checkNotNull(usb.pipe)
        val ncm = checkNotNull(usb.ncm)
        val host = Iap2UsbMuxHost.open(pipe)
        synchronized(resources) { if (stopped || opened !== usb) { host.close(); return }; mux = host }
        val id = MessageDigest.getInstance("SHA-256").digest(usb.serial.toByteArray()).joinToString("") { "%02x".format(it) }
        val store = PairRecordStore(server.config.stateDir.resolve("lockdown-pair-record-$id.properties"))
        fun pair(): LockdownPairRecord {
            state("TRUST", "请在 iPhone 上选择信任，并按提示解锁")
            return LockdownPairingClient(host).pair(label = "DashPlay", hostId = UUID.randomUUID().toString().uppercase(),
                systemBuid = UUID.randomUUID().toString().uppercase(), totalTimeoutMillis = 120_000,
                isCancelled = { stopped || synchronized(resources) { opened !== usb } }).pairRecord.also(store::save)
        }
        var record = store.load() ?: pair()
        val carkit = try { LockdownCarKitClient(host).open(record, "DashPlay") } catch (error: Exception) {
            if (error.message?.contains(Regex("InvalidPairRecord|InvalidHostID", RegexOption.IGNORE_CASE)) != true) throw error
            record = pair(); LockdownCarKitClient(host).open(record, "DashPlay")
        }
        val link = Iap2Session.open(carkit, traceContext = "wired")
        synchronized(resources) { if (stopped || opened !== usb) { link.close(); return }; session = link }
        if (context.applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE != 0 &&
            context.resources.getBoolean(R.bool.usb_phone_diagnostics)) {
            synchronized(resources) {
                if (!stopped && opened === usb) phoneDiagnostics = WiredPhoneDiagnostics(host, record)
            }
        }
        if (!binding) {
            binding = context.bindService(Intent(context, UsbVpnService::class.java).setAction(UsbVpnService.LOCAL_BIND), connection, Context.BIND_AUTO_CREATE)
            if (!binding) throw IOException("USB VPN 服务无法启动")
        }
        if (!bound.await(10, TimeUnit.SECONDS)) throw IOException("USB VPN 服务启动超时")
        val service = vpn ?: throw IOException("USB VPN 服务已断开")
        val mac = usb.hostMac ?: server.deviceId.split(':').map { it.toInt(16).toByte() }.toByteArray()
        val internet = if (Settings(context).usbInternet) usb::openInternet else null
        val tunnel = service.attach(ncm, mac, internet) { closeLink(usb) }
        synchronized(resources) { if (stopped || opened !== usb) { tunnel.close(); return }; lease = tunnel }
        synchronized(resources) { if (stopped || opened !== usb) return; server.listenWired(tunnel.address) }
        state("AUTHENTICATING", "正在认证 CarPlay")
        transitions = 0
        val endpoint = Iap2WiredCarPlayEndpoint(ipv6Addresses = listOf(UsbVpnService.LINK_LOCAL), airPlayPort = server.config.airPlayPort,
            publicKey = server.identity.publicKeyHex, sourceVersion = server.config.sourceVersion,
            deviceIdentifier = mac.joinToString(":") { "%02X".format(it) })
        val startSent = java.util.concurrent.atomic.AtomicBoolean(false)
        Iap2WiredControlClient(link, Iap2MfiAuthenticationClient(server.mfi)).run(
            identification = Iap2IdentificationConfig(name = server.config.deviceName, modelIdentifier = server.config.model,
                manufacturer = server.config.manufacturer, serialNumber = "DASHPLAY-" + server.deviceId.replace(":", ""),
                firmwareVersion = "1.0.0", hardwareVersion = "1.0", carPlayUsbInterfaceNumber = usb.interfaceNumber),
            endpoint = endpoint,
            availableCurrentMilliAmps = 500, timeoutMillis = Iap2WiredControlClient.NO_TIMEOUT_MILLIS,
            onProgress = {
                if (it == "iap2 tx=0x4301 carplay-start-session") startSent.set(true)
                DriveLog.event("wired control: ${it.substringBefore(" usbTransport=")}")
            },
            onReady = {
                // Keep NCM data inactive until authentication and our TUN/listener are ready.
                // This callback runs before the control client handles CarPlay availability.
                usb.activateNcm()
                DriveLog.event("wired NCM activated after authentication uptimeMs=${android.os.SystemClock.elapsedRealtime()}")
                state("AVAILABLE", "CarPlay 已就绪，等待 iPhone 连接")
                Thread({
                    // Keeps watching after LIVE: see WiredSessionWatch for why a session that ends is started over.
                    var watch = WiredSessionWatch(System.nanoTime())
                    while (!stopped && synchronized(resources) { opened === usb }) {
                        if (displayRestart.compareAndSet(link, null)) {
                            try {
                                link.send(Iap2WiredControlClient.carPlayStartSession(endpoint), 2000)
                                startSent.set(true)
                                watch = WiredSessionWatch(System.nanoTime(), startWaitNanos = 10_000_000_000L)
                                state("AVAILABLE", "正在重新协商 CarPlay 画面")
                                DriveLog.event("wired display start requested on existing USB network")
                            } catch (error: Exception) {
                                DriveLog.event("wired display start failed: ${error.javaClass.simpleName}")
                                closeLink(usb); return@Thread
                            }
                        }
                        when (watch.step(server.wiredSessionActive(), System.nanoTime(), startSent.get())) {
                            WiredSessionWatch.Step.RETRY_START -> {
                                try {
                                    link.send(Iap2WiredControlClient.carPlayStartSession(endpoint), 2000)
                                    DriveLog.event("wired start request repeated after network wait")
                                } catch (error: Exception) {
                                    DriveLog.event("wired start retry failed: ${error.javaClass.simpleName}")
                                    closeLink(usb); return@Thread
                                }
                            }
                            WiredSessionWatch.Step.LIVE -> state("LIVE", "数据线 CarPlay 已连接")
                            WiredSessionWatch.Step.GONE -> state("AVAILABLE", "CarPlay 画面已中断，正在等待 iPhone 恢复")
                            WiredSessionWatch.Step.RESTART -> {
                                DriveLog.event("wired session wait expired; restarting USB session")
                                closeLink(usb); return@Thread
                            }
                            WiredSessionWatch.Step.WAITING -> Unit
                        }
                        Thread.sleep(250)
                    }
                }, "usb-await-carplay").apply { isDaemon = true; start() }
            },
            onIncoming = server.nowPlayingFeed(link, "wired") { !stopped && synchronized(resources) { session === link } },
        )
    }

    private fun releaseVpn() {
        if (binding) runCatching { context.unbindService(connection) }
        binding = false; vpn = null; bound = CountDownLatch(1)
    }

    private fun closeLink(expected: AndroidUsbAccess.Opened? = null) {
        val close = synchronized(resources) {
            if (expected != null && opened !== expected) return
            val close = listOfNotNull(phoneDiagnostics, session, mux, lease, opened)
            phoneDiagnostics = null
            displayRestart.set(null)
            session = null; mux = null; lease = null; opened = null
            if (close.isNotEmpty()) server.stopWired()
            close
        }
        close.forEach { runCatching { it.close() } }
    }

    override fun close() {
        stopped = true; server.platformWiredRestart = null; server.platformWiredReselect = null
        usbChanges.close()
        main.removeCallbacksAndMessages(null)
        if (registered) { runCatching { context.unregisterReceiver(receiver) }; registered = false }
        closeLink(); status = "OFF"; detail = "数据线连接未启用"
    }

    companion object {
        const val PERMISSION = "com.dashplay.android.USB_PERMISSION"
        @Volatile var status = "OFF"; private set
        @Volatile var detail = "数据线连接未启用"; private set
    }
}
