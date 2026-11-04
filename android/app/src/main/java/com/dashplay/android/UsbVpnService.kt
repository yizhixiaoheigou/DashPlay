package com.dashplay.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
import android.net.VpnService
import android.os.Binder
import android.os.Build
import android.os.ParcelFileDescriptor
import android.system.OsConstants
import com.shilapi.xcertplay.transport.EthernetIpv6Codec
import com.shilapi.xcertplay.transport.NcmUsbBridge
import java.io.Closeable
import java.io.IOException
import java.net.Inet4Address
import java.net.Inet6Address
import java.net.InetAddress
import java.net.NetworkInterface
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.CopyOnWriteArrayList

/** CarPlay IPv6 and explicitly opted-in USB Internet share one stable VPN. Hotspot forwarding is separate. */
class UsbVpnService : VpnService() {
    inner class LocalBinder : Binder() { val service get() = this@UsbVpnService }
    private val lock = Any()
    private var current: Lease? = null
    private var nextId = 0L
    private var attaching = false
    private var destroyed = false
    private var addressTun: ParcelFileDescriptor? = null
    private var cableHasAddress = false
    /** Who owns which resource: closing one must never close the other (2026-11-03). */
    private val ownership = VpnResourceOwnership()
    /** The Root-free address tunnel, when the compatibility route asked for one. */
    internal var addressLease: AddressLease? = null
        private set

    inner class Lease internal constructor(val id: Long, val address: Inet6Address, private val bridge: PacketBridge,
        internal val internetPlan: UsbInternetPlan?, internal val failed: () -> Unit) : Closeable {
        private val closing = CopyOnWriteArrayList<() -> Unit>()
        internal fun onClosing(action: () -> Unit): Closeable = synchronized(lock) {
            check(current === this) { "USB VPN 已关闭" }
            closing += action
            Closeable { closing.remove(action) }
        }
        internal val interfaceName: String get() = synchronized(lock) {
            check(current === this) { "USB VPN 接口已关闭" }
            // NetworkInterface may expose only an address label (tun0:1), not the kernel link name.
            // if_indextoname is public from API 21 and resolves the actual owned IPv6 scope directly.
            android.system.Os.if_indextoname(address.scopeId) ?: throw IOException("USB VPN 接口已关闭")
        }
        override fun close() { detach(this) }
        internal fun release() { closing.forEach { runCatching(it) }; closing.clear(); bridge.close() }
    }

    override fun onCreate() {
        super.onCreate()
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel("usb-carplay", "数据线 CarPlay", NotificationManager.IMPORTANCE_LOW))
            Notification.Builder(this, "usb-carplay")
        } else { @Suppress("DEPRECATION") Notification.Builder(this) }
        startForeground(2, builder.setSmallIcon(R.drawable.ic_launcher).setContentTitle("DashPlay 数据线连接")
            .setContentText("本地 USB 通道").setOngoing(true).build())
    }

    override fun onBind(intent: Intent?) = if (intent?.action == LOCAL_BIND) LocalBinder() else super.onBind(intent)

    /** Called off main after USB permission and VPN consent. Ownership of NCM transfers on success. */
    internal fun attach(ncm: NcmUsbBridge?, hostMac: ByteArray, internet: (() -> UsbTetherSession.Wire)? = null,
        failed: () -> Unit): Lease = attach(if (ncm != null) object : UsbCarPlayWire {
            override fun receive(timeoutMillis: Long) = ncm.recv(timeoutMillis)
            override fun send(frame: ByteArray) { ncm.send(frame, 500) }
            override fun close() = ncm.close()
        } else object : UsbCarPlayWire {
            override fun receive(timeoutMillis: Long): ByteArray? { Thread.sleep(timeoutMillis.coerceIn(1, 250)); return null }
            override fun send(frame: ByteArray) { }
            override fun close() { }
        }, hostMac, internet, failed)

    internal fun attach(ncm: UsbCarPlayWire, hostMac: ByteArray, internet: (() -> UsbTetherSession.Wire)? = null,
        failed: () -> Unit): Lease {
        check(prepare(this) == null) { "请先允许数据线连接的 VPN 通道" }
        val id = synchronized(lock) {
            check(!destroyed && !attaching && current == null && addressLease?.exclusive != true) { "USB VPN 通道正在使用" }
            attaching = true
            ++nextId
        }
        var tun: ParcelFileDescriptor? = null
        var bridge: PacketBridge? = null
        try {
            val plan = if (internet == null) null else UsbInternetPlan.create(
                NetworkInterface.getNetworkInterfaces().toList().filter { it.isUp && !it.isLoopback }
                    .flatMap { it.interfaceAddresses }.mapNotNull {
                        val addr = it.address as? Inet4Address ?: return@mapNotNull null
                        val prefix = it.networkPrefixLength.toInt().takeIf { n -> n in 1..32 } ?: return@mapNotNull null
                        val number = UsbTetherProtocol.i32(addr.address, 0)
                        UsbInternetPlan.Cidr(number and UsbInternetPlan.Cidr.mask(prefix), prefix)
                    })
            val builder = Builder().setSession(if (plan == null) "DashPlay USB" else "DashPlay iPhone 上网")
                .addAddress(LINK_LOCAL, 64).addRoute("fe80::", 64).setMtu(1500).setBlocking(false)
            if (plan == null) builder.allowFamily(OsConstants.AF_INET).addAllowedApplication(packageName)
            else {
                builder.addAddress(UsbTetherProtocol.address(plan.address), 32)
                    .addDnsServer(UsbTetherProtocol.address(plan.dns)).addRoute("::", 0)
                for (route in plan.routes) builder.addRoute(route.text, route.prefix)
                if (Build.VERSION.SDK_INT >= 29) builder.setMetered(true)
            }
            synchronized(lock) {
                check(!destroyed && nextId == id) { "USB VPN 已取消" }
                // Reserve the compatibility /32 on the SAME active USB TUN. Enabling HTTP later needs no VPN swap.
                cableHasAddress = !Settings(this).teslaRootAccess
                if (cableHasAddress) builder.addAddress(TeslaRootPlan.ADDRESS, 32)
                tun = builder.establish() ?: throw IOException("系统未建立 USB VPN 通道")
                compatAddressAttached = cableHasAddress
                addressTun?.close(); addressTun = null
                addressLease?.changed()
            }
            val deadline = System.nanoTime() + 10_000_000_000L
            var local: Inet6Address? = null
            val target = InetAddress.getByName(LINK_LOCAL).address
            while (local == null && System.nanoTime() < deadline) {
                synchronized(lock) { check(!destroyed && nextId == id) { "USB VPN 已取消" } }
                local = NetworkInterface.getNetworkInterfaces().toList().flatMap { it.inetAddresses.toList() }
                    .filterIsInstance<Inet6Address>().firstOrNull { it.address.contentEquals(target) && it.scopeId > 0 }
                if (local == null) Thread.sleep(100)
            }
            val address = local ?: throw IOException("USB VPN 没有可监听的 IPv6 地址")
            bridge = PacketBridge(ncm, tun!!, hostMac, plan, internet, { ready, message ->
                synchronized(lock) {
                    if (current?.id == id) { internetReady = ready; internetMessage = message }
                }
            }) {
                val owned = synchronized(lock) { current?.takeIf { it.id == id } }
                if (owned != null) { owned.close(); failed() }
            }
            val lease = Lease(id, address, bridge, plan, failed)
            synchronized(lock) {
                check(!destroyed && nextId == id && current == null) { "USB VPN 已取消" }
                ownership.acquire(VpnResourceOwnership.Kind.CABLE, "lease-$id")
                current = lease
                internetLease = if (plan == null) null else lease
                internetDns = plan?.let { InetAddress.getByName(UsbTetherProtocol.address(it.dns)) }
                internetReady = false; internetMessage = if (plan == null) "尚未启用通过数据线使用 iPhone 网络" else "正在连接 iPhone 网络"
                bridge.start()
            }
            return lease
        } catch (error: Throwable) {
            bridge?.close() ?: tun?.close()
            synchronized(lock) {
                if (nextId == id) {
                    cableHasAddress = false; compatAddressAttached = false
                    if (addressLease?.isOpen == true && !destroyed) restoreAddressLocked()
                }
            }
            throw error
        }
        finally { synchronized(lock) { if (nextId == id) attaching = false } }
    }

    /** A logical user of the shared address, never an independently active VPN. */
    internal class AddressLease internal constructor(val address: String, val exclusive: Boolean, private val onClosed: () -> Unit) : Closeable {
        private val open = AtomicBoolean(true)
        private val listeners = CopyOnWriteArrayList<() -> Unit>()
        private val changes = CopyOnWriteArrayList<() -> Unit>()
        fun onChanged(action: () -> Unit) { changes += action }
        fun changed() { changes.forEach { runCatching(it) } }
        fun onClosed(action: () -> Unit) {
            listeners += action
            if (!isOpen && listeners.remove(action)) action()
        }
        fun release() {
            if (!open.compareAndSet(true, false)) return
            onClosed()
            listeners.forEach { runCatching(it) }; listeners.clear(); changes.clear()
        }
        override fun close() = release()
        val isOpen get() = open.get()
    }

    private fun establishAddressLocked(): ParcelFileDescriptor? = Builder()
        .setSession("DashPlay 免 Root 地址")
        .addAddress(TeslaRootPlan.ADDRESS, 32)
        // No default route and no IPv6 suppression. Local address traffic is delivered by the kernel.
        .allowFamily(OsConstants.AF_INET).allowFamily(OsConstants.AF_INET6)
        .addAllowedApplication(packageName).setMtu(1500).setBlocking(false).establish()

    private fun restoreAddressLocked() {
        addressTun = runCatching { establishAddressLocked() }.getOrNull()
        compatAddressAttached = addressTun != null
        if (addressTun == null) addressLease?.release() else addressLease?.changed()
    }

    internal fun attachAddress(address: TeslaVpnAddress, exclusive: Boolean = false): AddressLease? = synchronized(lock) {
        if (prepare(this) != null || destroyed || attaching || Settings(this).teslaRootAccess) return null
        if (address.address != TeslaRootPlan.ADDRESS || addressLease != null) return null
        if (exclusive && current != null) return null // diagnostic probe must not touch a live USB session
        if (current != null && !cableHasAddress) return null
        if (current == null) {
            addressTun = runCatching { establishAddressLocked() }.getOrNull() ?: return null
            compatAddressAttached = true
        }
        val lease = AddressLease(address.address, exclusive) {
            synchronized(lock) {
                addressLease = null
                addressTun?.close(); addressTun = null
                compatAddressAttached = cableHasAddress
            }
        }
        addressLease = lease
        lease
    }

    internal fun detachAddress() { synchronized(lock) { addressLease?.release() } }

    private fun detach(lease: Lease) {        val release = synchronized(lock) {
            if (current !== lease) false else { attaching = true; current = null; if (internetLease === lease) internetLease = null; internetDns = null; internetReady = false; internetMessage = "已断开通过 USB 建立的 iPhone 网络连接"; true }
        }
        if (release) {
            ownership.release(VpnResourceOwnership.Kind.CABLE, "lease-${lease.id}")
            lease.release()
            synchronized(lock) {
                if (nextId == lease.id) {
                    cableHasAddress = false; compatAddressAttached = false
                    if (!destroyed && current == null && addressLease?.isOpen == true) restoreAddressLocked()
                    attaching = false
                }
            }
        }
    }
    private fun cancel(destroy: Boolean) {
        val lease = synchronized(lock) {
            destroyed = destroyed || destroy
            nextId++; attaching = false
            if (internetLease === current) internetLease = null
            internetDns = null
            internetReady = false; internetMessage = "已断开通过 USB 建立的 iPhone 网络连接"
            current.also { current = null }
        }
        // Revocation is the system taking the whole VPN away: everything this app owns goes with it.
        synchronized(lock) {
            addressLease?.let { address -> runCatching { address.release() } }
            addressLease = null
            ownership.releaseAll()
            addressTun?.close(); addressTun = null
            cableHasAddress = false; compatAddressAttached = false
        }
        lease?.let { it.release(); Thread(it.failed, "usb-vpn-revoked").start() }
    }
    override fun onRevoke() { cancel(false); super.onRevoke() }
    override fun onDestroy() { cancel(true); super.onDestroy() }

    companion object {
        @Volatile internal var compatAddressAttached = false; private set
        const val LOCAL_BIND = "com.dashplay.android.USB_VPN_BIND"
        const val LINK_LOCAL = "fe80::2"
        @Volatile var internetReady = false; private set
        @Volatile internal var internetLease: Lease? = null; private set
        @Volatile internal var internetDns: InetAddress? = null; private set
        @Volatile var internetMessage = "尚未启用通过数据线使用 iPhone 网络"; private set
    }
}

/** Testable boundary: production is the existing NcmUsbBridge; tests never claim actual USB. */
internal interface UsbCarPlayWire : Closeable {
    fun receive(timeoutMillis: Long): ByteArray?
    fun send(frame: ByteArray)
}

/** One TUN, independent CarPlay and Personal Hotspot pipes; a lost Internet lease cannot close NCM. */
internal class PacketBridge(private val ncm: UsbCarPlayWire, private val tun: ParcelFileDescriptor,
    private val hostMac: ByteArray, private val plan: UsbInternetPlan?,
    private val openInternet: (() -> UsbTetherSession.Wire)?, private val internetState: (Boolean, String) -> Unit,
    private val failed: () -> Unit) : Closeable {
    private val open = AtomicBoolean(true)
    private val peers = Ipv6PeerTable()
    private var neighborRxTraces = 0
    private var neighborTxTraces = 0
    private val threads = CopyOnWriteArrayList<Thread>()
    private val outputLock = Any()
    private val internetLock = Any()
    private val packets = TunPacketIo(tun)
    private val nat = plan?.let { UsbInternetNat(it.address, it.dns) }
    @Volatile private var internet: UsbTetherSession? = null
    init { require(hostMac.size == 6 && (plan == null) == (openInternet == null)) }
    private fun deliver(bytes: ByteArray, offset: Int = 0, length: Int = bytes.size) = synchronized(outputLock) {
        if (open.get()) packets.write(bytes, offset, length)
    }
    fun start() {
        launch("dashplay-usb-in") {
            while (open.get()) {
                val frame = ncm.receive(250) ?: continue
                val packet = EthernetIpv6Codec.parseIpv6View(frame) ?: continue
                peers.learn(frame, packet.payloadOffset, packet.payloadLength, packet.sourceMac)
                if (packet.payloadLength >= 64 && frame[packet.payloadOffset + 6] == 58.toByte() &&
                    frame[packet.payloadOffset + 40] == 135.toByte() && neighborRxTraces++ < 4) {
                    DriveLog.event("wired neighbor request ip=${InetAddress.getByAddress(frame.copyOfRange(packet.payloadOffset + 8, packet.payloadOffset + 24)).hostAddress} mac=${packet.sourceMac.joinToString(":") { "%02x".format(it.toInt() and 255) }}")
                }
                deliver(frame, packet.payloadOffset, packet.payloadLength)
            }
        }
        launch("dashplay-usb-out") {
            val buffer = ByteArray(65_535)
            while (open.get()) {
                val size = packets.read(buffer) ?: return@launch
                val bytes = buffer.copyOf(size)
                when ((bytes[0].toInt() and 255) ushr 4) {
                    4 -> nat?.outgoing(bytes, System.nanoTime() / 1_000_000)?.let { internet?.sendIpv4(it) }
                    6 -> {
                        if (size < 40) continue
                        // Personal Hotspot currently carries IPv4. Global IPv6 cannot leak to another uplink.
                        val linkLocal = bytes[24] == 0xfe.toByte() && bytes[25].toInt() and 0xc0 == 0x80
                        val multicast = bytes[24] == 0xff.toByte() && bytes[25] == 2.toByte()
                        if (!linkLocal && !multicast) continue
                        val packet = EthernetIpv6Codec.addNeighborAdvertisementTargetMac(bytes, hostMac)
                        val destination = EthernetIpv6Codec.multicastDestinationMac(packet) ?: peers.destination(packet) ?: continue
                        if (packet.size >= 64 && packet[6] == 58.toByte() && packet[40] == 136.toByte() && neighborTxTraces++ < 4) {
                            DriveLog.event("wired neighbor response ip=${InetAddress.getByAddress(packet.copyOfRange(24, 40)).hostAddress} mac=${destination.joinToString(":") { "%02x".format(it.toInt() and 255) }} host=${hostMac.joinToString(":") { "%02x".format(it.toInt() and 255) }} flags=${packet[44].toInt() and 255} hop=${packet[7].toInt() and 255}")
                        }
                        ncm.send(EthernetIpv6Codec.build(hostMac, destination, packet))
                    }
                }
            }
        }
        if (openInternet != null) launch("dashplay-usb-internet") {
            var attempts = 0
            while (open.get()) {
                var session: UsbTetherSession? = null
                val acquiredLease = AtomicBoolean(false)
                try {
                    internetState(false, "等待 iPhone 开启个人热点并分配地址")
                    val wire = openInternet.invoke()
                    session = UsbTetherSession(wire, { lease, message ->
                        if (open.get()) {
                            nat!!.configure(lease)
                            if (lease != null) acquiredLease.set(true)
                            internetState(lease != null, if (lease != null) "USB 网络地址已就绪；能否访问互联网取决于 iPhone 的网络连接" else message)
                        }
                    }, { bytes -> nat!!.incoming(bytes, System.nanoTime() / 1_000_000)?.let { deliver(it) } })
                    synchronized(internetLock) {
                        if (!open.get()) { session.close(); return@launch }
                        internet = session; session.start()
                    }
                    while (open.get() && session.isRunning) Thread.sleep(250)
                } catch (_: Exception) {
                    if (open.get()) internetState(false, "USB 热点接口暂不可用，正在重试；请开启 iPhone 个人热点")
                } finally {
                    synchronized(internetLock) { if (internet === session) internet = null }
                    session?.close(); nat!!.configure(null)
                }
                if (acquiredLease.get()) attempts = 0
                val delay = longArrayOf(2000, 5000, 15000, 30000)[attempts.coerceAtMost(3)]
                attempts = (attempts + 1).coerceAtMost(3)
                val deadline = System.nanoTime() + delay * 1_000_000
                while (open.get() && System.nanoTime() < deadline) Thread.sleep(100)
            }
        }
    }
    private fun launch(name: String, work: () -> Unit) {
        val thread = Thread({ try { work() } catch (_: Exception) { if (open.get()) failed() } }, name).apply { isDaemon = true }
        threads.add(thread); thread.start()
    }
    override fun close() {
        if (!open.compareAndSet(true, false)) return
        nat?.configure(null)
        runCatching { packets.close() }; runCatching { ncm.close() }
        val session = synchronized(internetLock) { internet.also { internet = null } }
        session?.close()
        threads.filter { it !== Thread.currentThread() }.forEach { runCatching { it.join(1000) }; if (it.isAlive) it.interrupt() }
    }
}
