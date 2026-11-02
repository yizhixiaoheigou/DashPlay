package com.dashplay.android

import android.app.Instrumentation
import android.content.*
import android.net.VpnService
import android.os.Bundle
import android.os.IBinder
import java.net.*
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.util.concurrent.*
import java.util.concurrent.atomic.AtomicInteger

/** Real Android VPN/kernel sockets against a synthetic Ethernet peer. NEVER run on a physical device. */
internal object UsbInternetProbe {
    private fun waitFor(label: String, timeout: Long = 15_000, check: () -> Boolean) {
        val end = System.nanoTime() + timeout * 1_000_000
        while (!check()) { check(System.nanoTime() < end) { "timeout: $label" }; Thread.sleep(50) }
    }
    fun run(instrumentation: Instrumentation, result: Bundle, lanPort: Int, share: Boolean = false, manager: Boolean = false, crash: Boolean = false,
        vpnCrashControl: Boolean = false) {
        check(android.os.Build.HARDWARE in listOf("ranchu", "goldfish")) { "emulator only" }
        require(lanPort in 1024..65535)
        val context = instrumentation.targetContext
        check(VpnService.prepare(context) == null) { "emulator VPN consent not configured" }
        val settings = Settings(context)
        check(!settings.usbInternet && !settings.usbInternetAllowed && !settings.usbHotspotShare && !settings.usbHotspotShareAllowed)
        val previous = settings.deviceName
        check(runCatching { settings.apply(ManagementSettings(deviceName = "must-not-save", usbInternet = true)) }.isFailure)
        check(settings.deviceName == previous && !settings.usbInternet)
        check(runCatching { settings.apply(ManagementSettings(deviceName = "must-not-save", usbHotspotShare = true)) }.isFailure)
        check(settings.deviceName == previous && !settings.usbHotspotShare)
        result.putString("local_consent_gate", "ok: browser update rejected atomically")
        fun lanAddresses() = NetworkInterface.getNetworkInterfaces().toList().filter { it.isUp && !it.isLoopback }
            .flatMap { it.inetAddresses.toList() }.filterIsInstance<Inet4Address>()
        // Boot completion can precede the emulator's virtual Wi-Fi/DHCP connection.
        waitFor("emulator LAN address", 45_000) { lanAddresses().isNotEmpty() }
        val lan = lanAddresses().first()
        val connected = CountDownLatch(1)
        var service: UsbVpnService? = null
        val connection = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                service = (binder as UsbVpnService.LocalBinder).service; connected.countDown()
            }
            override fun onServiceDisconnected(name: ComponentName?) { service = null }
        }
        check(context.bindService(Intent(context, UsbVpnService::class.java).setAction(UsbVpnService.LOCAL_BIND), connection, Context.BIND_AUTO_CREATE))
        var tunnel: UsbVpnService.Lease? = null
        val ncmClosed = AtomicInteger(); val fatal = AtomicInteger(); val peer = Peer()
        val ncm = object : UsbCarPlayWire {
            override fun receive(timeoutMillis: Long): ByteArray? { Thread.sleep(25); return null }
            override fun send(frame: ByteArray) { /* Kernel IPv6 neighbour/router discovery; no simulated CarPlay. */ }
            override fun close() { ncmClosed.incrementAndGet() }
        }
        try {
            check(connected.await(10, TimeUnit.SECONDS))
            tunnel = service!!.attach(ncm, peer.hostMac, { peer }) { fatal.incrementAndGet() }
            val scope = tunnel.address.scopeId
            val plan = tunnel.internetPlan!!
            result.putString("tun_address", UsbTetherProtocol.address(plan.address))
            result.putString("stage", "DHCP and ARP")
            waitFor("DHCP and ARP ready: ${UsbVpnService.internetMessage}") { UsbVpnService.internetReady }
            if (vpnCrashControl) {
                check(!share && !manager && !crash)
                // Control experiment: no Root runner or sharing manager is instantiated.
                java.io.File(context.cacheDir, "vpn-control").writeText("${android.os.Process.myPid()} ${tunnel.interfaceName}\n")
                Thread.sleep(120_000)
                error("host did not stop VPN control process")
            }
            result.putString("stage", "UDP")
            udpEcho()
            result.putString("stage", "TCP")
            tcpEcho()
            result.putString("stage", "DNS")
            val dns = InetAddress.getByName("dashplay-probe.invalid")
            check(dns.hostAddress == "198.51.100.77") { "DNS did not use VPN: $dns" }
            result.putString("kernel_sockets", "UDP echo, TCP echo and system DNS through VPN/IPv4 translation passed (synthetic peer)")
            val query = TeslaChecksProbe.query("dashplay-probe.invalid")
            val forwarded = TeslaCheckDns(context, "dp-ap0").exchange(query)
            check(forwarded != null && TeslaCheckProtocol.validResponse(query, forwarded) && forwarded.takeLast(4) == listOf(198.toByte(), 51.toByte(), 100.toByte(), 77.toByte()))
            result.putString("usb_dns_forward", "production DNS forwarder used virtual DNS over actual VPN TCP, without physical-network binding")
            if (share) sharedClient(context, tunnel, peer, result)
            ServerSocket(0, 1, lan).use { server ->
                server.soTimeout = 5000
                Socket().use { client ->
                    client.connect(InetSocketAddress(lan, server.localPort), 5000)
                    server.accept().use { accepted ->
                        accepted.soTimeout = 5000; client.getOutputStream().write(42)
                        check(accepted.getInputStream().read() == 42)
                    }
                }
            }
            result.putString("lan_preserved", "real local TCP socket reachable while VPN routes public IPv4")
            Socket().use { socket ->
                socket.soTimeout = 5000; socket.connect(InetSocketAddress("10.0.2.2", lanPort), 5000)
                val bytes = "dashplay-lan-outside-vpn".toByteArray(); socket.getOutputStream().write(bytes)
                val reply = ByteArray(bytes.size); DataInputStream(socket.getInputStream()).readFully(reply)
                check(reply.contentEquals(bytes))
            }
            result.putString("remote_lan_preserved", "TCP to actual Mac loopback via emulator LAN bypassed VPN; host independently counted request")
            peer.online = false
            waitFor("carrier loss") { !UsbVpnService.internetReady }
            check(ncmClosed.get() == 0 && fatal.get() == 0)
            check(NetworkInterface.getByIndex(scope) != null)
            peer.address++
            peer.online = true
            waitFor("fresh lease") { UsbVpnService.internetReady }
            udpEcho(); tcpEcho()
            check(tunnel.address.scopeId == scope && ncmClosed.get() == 0 && fatal.get() == 0)
            result.putString("lease_recovery", "changed DHCP address, same CarPlay TUN, UDP/TCP recovered")
            check(peer.dnsCount.get() > 0 && peer.udpCount.get() >= 2 && peer.tcpCount.get() >= 2)
            if (manager) UsbShareManagerProbe.run(context, tunnel, result, crash)
            tunnel.close(); tunnel = null
            check(!UsbVpnService.internetReady)
            waitFor("VPN removal") { NetworkInterface.getByIndex(scope)?.inetAddresses?.toList()?.none { it.hostAddress?.startsWith("fe80:0:0:0:0:0:0:2") == true } != false }
            result.putString("cleanup", "owned VPN removed; no default route settings or physical USB modified")
            val originalLink = settings.iphoneLink
            try {
                settings.usbEnabled = true; settings.iphoneLink = IphoneLink.WIRED
                settings.allowUsbInternet()
                check(Settings(context).usbInternet && Settings(context).usbInternetAllowed)
                settings.allowUsbHotspotShare()
                check(Settings(context).usbHotspotShare && Settings(context).usbHotspotShareAllowed)
                settings.apply(ManagementSettings(usbHotspotShare = false)); check(!Settings(context).usbHotspotShare)
                settings.apply(ManagementSettings(usbHotspotShare = true)); check(Settings(context).usbHotspotShare)
                settings.apply(ManagementSettings(usbInternet = false)); check(!Settings(context).usbInternet)
                settings.apply(ManagementSettings(usbInternet = true)); check(Settings(context).usbInternet)
                settings.usbEnabled = false
                check(runCatching { settings.apply(ManagementSettings(usbInternet = true)) }.isFailure)
                settings.apply(ManagementSettings(usbInternet = false)); check(!Settings(context).usbInternet)
                result.putString("settings_persistence", "local opt-in persisted; browser toggle allowed afterward; disabling remains possible without USB")
            } finally {
                settings.usbEnabled = false; settings.iphoneLink = originalLink
                context.getSharedPreferences("dashplay", Context.MODE_PRIVATE).edit()
                    .remove("usb_internet").remove("usb_internet_consent").remove("usb_hotspot_share").remove("usb_hotspot_share_consent").commit()
            }
            result.putBoolean("success", true)
        } finally {
            result.putString("internet_state", UsbVpnService.internetMessage)
            result.putInt("fatal_carplay_pipe_errors", fatal.get())
            tunnel?.close(); context.unbindService(connection); peer.close()
        }
    }
    private fun sharedClient(context: Context, tunnel: UsbVpnService.Lease, peer: Peer, result: Bundle) {
        val base = java.io.File(context.cacheDir, "usb-share-probe").apply { mkdirs() }
        for (name in listOf("ready", "round1", "round2", "round3", "done", "phase", "tunnel", "interfaces")) java.io.File(base, name).delete()
        val host = InetAddress.getByName("192.0.2.1")
        val dns = TeslaCheckDns(context, "dp-ap0")
        TeslaCheckServer(host, { "box.example.org" }, dns::exchange,
            { q -> TeslaCheckProtocol.local(q, "box.example.org", true, "192.0.2.1") }).use { server ->
            val network = tunnel.internetPlan!!
            val iface = tunnel.interfaceName
            java.io.File(base, "tunnel").writeText(iface)
            waitFor("root interface observation", 30_000) { java.io.File(base, "interfaces").exists() }
            val plan = UsbSharePlan("DP_USB_012345abcdef", "dp-ap0", iface, "192.0.2.0/24",
                UsbTetherProtocol.address(network.address), UsbTetherProtocol.address(network.dns),
                network.routes.map { "${it.text}/${it.prefix}" }, 45123, 9234, "192.0.2.1", server.dnsPort, guarded = true)
            val session = UsbShareSessionPlan.fromProbe(plan, listOf(java.io.File(base, "interfaces").readText().trim()), true)
            check(session.tunnelIndex == tunnel.address.scopeId)
            java.io.File(base, "install.sh").writeText(session.installScript("/data/local/tmp/dashplay-shared-vpn/lease"))
            java.io.File(base, "remove.sh").writeText(session.removeScript())
            java.io.File(base, "verify.sh").writeText(session.verifyScript())
            val publicRoute = network.routes.single { it.contains(0xc633644d.toInt()) }
            val route = "${publicRoute.text}/${publicRoute.prefix} dev $iface table 45123 proto 186 metric 45123"
            java.io.File(base, "lose-route.sh").writeText("ip -4 route del $route\n")
            java.io.File(base, "restore-route.sh").writeText("ip -4 route add $route\n")
            java.io.File(base, "tunnel").writeText(iface)
            for ((label, name) in listOf("check" to "connman.vn.cloud.tesla.cn", "own" to "box.example.org", "forward" to "dashplay-probe.invalid")) {
                val q = TeslaChecksProbe.query(name)
                val r = TeslaCheckProtocol.local(q, "box.example.org", true, "192.0.2.1") ?: TeslaCheckProtocol.reply(q, "198.51.100.77")!!
                java.io.File(base, "$label.query").writeBytes(q); java.io.File(base, "$label.response").writeBytes(r)
                java.io.File(base, "$label.tcp-query").writeBytes(byteArrayOf((q.size ushr 8).toByte(), q.size.toByte()) + q)
                java.io.File(base, "$label.tcp-response").writeBytes(byteArrayOf((r.size ushr 8).toByte(), r.size.toByte()) + r)
                if (label == "forward") java.io.File(base, "forward.offline-response").writeBytes(TeslaCheckProtocol.reply(q, error = 2)!!)
            }
            val beforeTcp = peer.tcpCount.get(); val beforeUdp = peer.udpCount.get(); val beforeDns = peer.dnsCount.get()
            java.io.File(base, "ready").writeText("ready")
            waitFor("shared client round 1", 120_000) { java.io.File(base, "round1").exists() }
            check(peer.tcpCount.get() > beforeTcp && peer.udpCount.get() > beforeUdp && peer.dnsCount.get() > beforeDns)
            check(server.queries.get() >= 6)
            peer.online = false
            waitFor("shared lease loss") { !UsbVpnService.internetReady }
            java.io.File(base, "phase").writeText("offline")
            waitFor("shared client offline check", 30_000) { java.io.File(base, "round2").exists() }
            peer.address++; peer.online = true
            waitFor("shared new lease") { UsbVpnService.internetReady }
            java.io.File(base, "phase").writeText("recovered")
            waitFor("shared client recovered", 60_000) { java.io.File(base, "round3").exists() }
            waitFor("shared rule cleanup", 30_000) { java.io.File(base, "done").exists() }
            check(server.healthy && tunnel.interfaceName == iface)
            result.putString("shared_vpn", "independent client TCP/UDP, real UDP/TCP DNS proxy and local answers, lease loss/recovery on same TUN; synthetic USB Ethernet peer")
        }
    }
    private fun udpEcho() {
        DatagramSocket().use { socket ->
            socket.soTimeout = 5000
            val data = "usb-internet-kernel-udp".toByteArray()
            socket.send(DatagramPacket(data, data.size, InetAddress.getByName("198.51.100.77"), 49001))
            val reply = DatagramPacket(ByteArray(256), 256); socket.receive(reply)
            check(reply.data.copyOf(reply.length).contentEquals(data))
        }
    }
    private fun tcpEcho() {
        Socket().use { socket ->
            socket.soTimeout = 5000; socket.connect(InetSocketAddress("198.51.100.77", 49002), 5000)
            val bytes = "usb-internet-kernel-tcp".toByteArray()
            socket.getOutputStream().write(bytes)
            val reply = ByteArray(bytes.size); DataInputStream(socket.getInputStream()).readFully(reply)
            check(reply.contentEquals(bytes))
        }
    }

    /** Minimal synthetic peer, scoped to 198.51.100.77 and dashplay-probe.invalid, not an Internet proxy. */
    private class Peer : UsbTetherSession.Wire {
        override val hostMac = byteArrayOf(2, 0, 0, 0, 0, 2)
        private val peerMac = byteArrayOf(2, 0, 0, 0, 0, 1)
        @Volatile var online = true
        @Volatile var address = 0xac140a02.toInt()
        private val gateway = 0xac140a01.toInt()
        private val remote = 0xc633644d.toInt()
        private val input = LinkedBlockingQueue<ByteArray>(128)
        private val tcpSequence = mutableMapOf<Int, Int>()
        private val tcpDns = mutableMapOf<Int, ByteArray>()
        val dnsCount = AtomicInteger(); val udpCount = AtomicInteger(); val tcpCount = AtomicInteger()
        override fun carrier() = online
        override fun receive(timeoutMillis: Long): ByteArray? = input.poll(timeoutMillis, TimeUnit.MILLISECONDS)
        override fun close() { online = false; input.clear() }
        private fun deliver(packet: ByteArray) { check(input.offer(UsbTetherProtocol.ethernet(peerMac, hostMac, 0x800, packet))) }
        override fun send(frame: ByteArray) {
            try { sendChecked(frame) } catch (error: Exception) {
                android.util.Log.e("UsbInternetProbe", "synthetic peer rejected packet", error); throw error
            }
        }
        private fun sendChecked(frame: ByteArray) {
            if (!online) return
            val arp = UsbTetherProtocol.parseArp(frame)
            if (arp != null) {
                if (arp.target == gateway) check(input.offer(UsbTetherProtocol.arp(peerMac, gateway, hostMac, arp.address, true)))
                return
            }
            if (word(frame, 12) != 0x800) return
            val b = frame.copyOfRange(14, 14 + word(frame, 16))
            check(UsbTetherProtocol.checksum(b, 0, 20) == 0)
            val source = number(b, 12); val target = number(b, 16)
            val ihl = (b[0].toInt() and 15) * 4
            val proto = b[9].toInt() and 255
            if (proto == 17 && word(b, ihl) == 68 && word(b, ihl + 2) == 67) {
                dhcp(b.copyOfRange(ihl + 8, b.size)); return
            }
            if (source != address) return
            if (proto in listOf(6, 17)) {
                val pseudo = word(b, 12).toLong() + word(b, 14) + word(b, 16) + word(b, 18) + proto + b.size - ihl
                check(UsbTetherProtocol.checksum(b, ihl, b.size - ihl, pseudo) == 0) { "bad translated transport checksum" }
            }
            if (proto == 17 && target == gateway && word(b, ihl + 2) == 53) {
                val body = answerDns(b.copyOfRange(ihl + 8, b.size)) ?: return
                deliver(UsbTetherProtocol.udp(gateway, address, 53, word(b, ihl), body))
            } else if (proto == 17 && target == remote && word(b, ihl + 2) == 49001) {
                udpCount.incrementAndGet()
                deliver(UsbTetherProtocol.udp(remote, address, 49001, word(b, ihl), b.copyOfRange(ihl + 8, b.size)))
            } else if (proto == 6 && target == remote && word(b, ihl + 2) == 49002) tcp(b, ihl)
            else if (proto == 6 && target == gateway && word(b, ihl + 2) == 53) tcp(b, ihl)
        }
        private fun answerDns(query: ByteArray): ByteArray? {
            val question = TeslaCheckProtocol.question(query) ?: return null
            if (question.name != "dashplay-probe.invalid") return null
            dnsCount.incrementAndGet()
            return TeslaCheckProtocol.reply(query, "198.51.100.77")
        }
        private fun dhcp(request: ByteArray) {
            var p = 240; var type = 0
            while (p < request.size) {
                val id = request[p++].toInt() and 255
                if (id == 255) break
                if (id == 0) continue
                val n = request[p++].toInt() and 255
                if (id == 53) type = request[p].toInt()
                p += n
            }
            if (type !in listOf(1, 3)) return
            val body = ByteArray(300); body[0] = 2; body[1] = 1; body[2] = 6
            request.copyInto(body, 4, 4, 8); hostMac.copyInto(body, 28)
            put32(body, 16, address); put32(body, 236, 0x63825363)
            val options = ByteArrayOutputStream()
            fun option(id: Int, bytes: ByteArray) { options.write(id); options.write(bytes.size); options.write(bytes) }
            fun ip(id: Int, n: Int) = option(id, ByteArray(4).also { put32(it, 0, n) })
            option(53, byteArrayOf(if (type == 1) 2 else 5)); ip(54, gateway); ip(1, 0xfffffff0.toInt())
            ip(3, gateway); ip(6, gateway); ip(51, 3600); options.write(255)
            options.toByteArray().copyInto(body, 240)
            deliver(UsbTetherProtocol.udp(gateway, -1, 67, 68, body))
        }
        private fun tcp(b: ByteArray, ihl: Int) {
            val port = word(b, ihl); val flags = b[ihl + 13].toInt() and 255
            val seq = number(b, ihl + 4); val dataAt = ihl + ((b[ihl + 12].toInt() and 255) ushr 4) * 4
            val payload = b.copyOfRange(dataAt, b.size)
            val reply: ByteArray; val replySeq: Int; val ack: Int; val replyFlags: Int
            if (flags and 2 != 0) {
                tcpDns.remove(port)
                tcpSequence[port] = 1001; reply = byteArrayOf(); replySeq = 1000; ack = seq + 1; replyFlags = 0x12
            } else if (payload.isNotEmpty()) {
                replySeq = tcpSequence[port] ?: return
                if (word(b, ihl + 2) == 53) {
                    val data = tcpDns.getOrDefault(port, byteArrayOf()) + payload
                    check(data.size <= 8194)
                    if (data.size >= 2 && data.size >= word(data, 0) + 2) {
                        val answer = answerDns(data.copyOfRange(2, 2 + word(data, 0))) ?: return
                        reply = byteArrayOf((answer.size ushr 8).toByte(), answer.size.toByte()) + answer
                        tcpDns.remove(port)
                    } else { tcpDns[port] = data; reply = byteArrayOf() }
                } else { tcpCount.incrementAndGet(); reply = payload }
                ack = seq + payload.size; replyFlags = if (reply.isEmpty()) 0x10 else 0x18
                tcpSequence[port] = replySeq + reply.size
            } else if (flags and 1 != 0) {
                tcpDns.remove(port)
                reply = byteArrayOf(); replySeq = tcpSequence.remove(port) ?: return; ack = seq + 1; replyFlags = 0x11
            } else return
            val packet = ByteArray(40 + reply.size); packet[0] = 0x45; packet[8] = 64; packet[9] = 6
            put16(packet, 2, packet.size); put32(packet, 12, number(b, 16)); put32(packet, 16, address)
            put16(packet, 20, word(b, ihl + 2)); put16(packet, 22, port); put32(packet, 24, replySeq); put32(packet, 28, ack)
            packet[32] = 0x50; packet[33] = replyFlags.toByte(); put16(packet, 34, 65535); reply.copyInto(packet, 40)
            val pseudo = word(packet, 12).toLong() + word(packet, 14) + word(packet, 16) + word(packet, 18) + 6 + packet.size - 20
            put16(packet, 36, UsbTetherProtocol.checksum(packet, 20, packet.size - 20, pseudo))
            put16(packet, 10, UsbTetherProtocol.checksum(packet, 0, 20)); deliver(packet)
        }
        private fun word(b: ByteArray, p: Int) = UsbTetherProtocol.u16(b, p)
        private fun number(b: ByteArray, p: Int) = UsbTetherProtocol.i32(b, p)
        private fun put16(b: ByteArray, p: Int, n: Int) = UsbTetherProtocol.put16(b, p, n)
        private fun put32(b: ByteArray, p: Int, n: Int) = UsbTetherProtocol.put32(b, p, n)
    }
}
