package com.dashplay.android

import android.content.Context
import android.os.Bundle
import java.io.File
import java.net.InetAddress
import java.net.ServerSocket
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json

/** Exercises the real manager, journal, DNS sockets and lease callbacks with adb-root executing its commands. */
internal object UsbShareManagerProbe {
    fun run(context: Context, tunnel: UsbVpnService.Lease, result: Bundle, crash: Boolean = false) {
        check(android.os.Build.HARDWARE in listOf("ranchu", "goldfish"))
        val base = File(context.cacheDir, "usb-share-manager").apply { mkdirs() }
        fun await(name: String) {
            val end = System.nanoTime() + 60_000_000_000L
            while (!File(base, name).exists()) {
                check(System.nanoTime() < end) { "manager fixture timeout: $name" }
                Thread.sleep(25)
            }
        }
        fun phase(name: String) { File(base, "phase").writeText(name); await("done-$name") }
        val root = EmulatorRootBridge(File(base, "requests"))
        var now = 1L
        var manager = UsbShareAccess(context, root::run) { now }
        val tesla = TeslaRootAccess(context, root::tesla)
        val settings = Settings(context)
        val originalLink = settings.iphoneLink; val originalCar = settings.car; val originalNetwork = settings.network
        val prefs = context.getSharedPreferences("dashplay", Context.MODE_PRIVATE)
        val local = InetAddress.getByName("192.0.2.1")
        val network = LocalNetwork.Choice("dp-ap0", local, LocalNetwork.Kind.HOTSPOT)
        fun plan() = Json.decodeFromString<UsbShareSessionPlan>(prefs.getString("usb_share_journal", null)!!)
        fun ready() {
            check(manager.status().getString("state") == "READY") { manager.status().toString() }
            check(manager.status().getBoolean("active"))
            File(base, "table").writeText(plan().rules.table.toString())
            File(base, "owner").writeText(plan().rules.owner)
        }
        ServerSocket(0, 10, local).use { server ->
            val responder = Thread {
                while (!server.isClosed) {
                    val socket = runCatching { server.accept() }.getOrNull() ?: break
                    socket.use {
                        it.soTimeout = 5000
                        runCatching {
                            val line = it.getInputStream().bufferedReader().readLine()
                            if (line == "manager-alias") it.getOutputStream().write("manager-alias\n".toByteArray())
                        }
                    }
                }
            }.apply { isDaemon = true; start() }
            try {
                File(base, "tunnel").writeText(tunnel.interfaceName)
                await("baseline")
                for ((label, name) in listOf("forward" to "dashplay-probe.invalid", "check" to "connman.vn.cloud.tesla.cn")) {
                    val query = TeslaChecksProbe.query(name)
                    val reply = if (label == "check") TeslaCheckProtocol.local(query, null)!! else TeslaCheckProtocol.reply(query, "198.51.100.77")!!
                    File(base, "$label.query").writeBytes(query)
                    File(base, "$label.response").writeBytes(reply)
                    File(base, "$label.tcp-query").writeBytes(byteArrayOf((query.size ushr 8).toByte(), query.size.toByte()) + query)
                    File(base, "$label.tcp-response").writeBytes(byteArrayOf((reply.size ushr 8).toByte(), reply.size.toByte()) + reply)
                }
                settings.usbEnabled = true; settings.iphoneLink = IphoneLink.WIRED; settings.network = Network.THIS_HOTSPOT
                settings.allowUsbInternet(); settings.allowUsbHotspotShare()
                check(manager.recover()); manager.maintain(network, true); ready()
                phase("shared")
                settings.enableTeslaRootAccess(); settings.teslaOfflineChecks = true
                tesla.start(network, server.localPort)
                check(TeslaRootAccess.active && TeslaRootAccess.checksActive) { TeslaRootAccess.describe().toString() }
                phase("tesla-last")
                if (crash) {
                    // The host stops the whole package here. No finally/close callback may run.
                    check(prefs.contains("usb_share_journal") && prefs.contains("tesla_root_journal"))
                    File(base, "crash-pid").writeText(android.os.Process.myPid().toString())
                    File(base, "crash-share-journal").writeText(prefs.getString("usb_share_journal", null)!!)
                    File(base, "crash-tesla-journal").writeText(prefs.getString("tesla_root_journal", null)!!)
                    val teslaPlan = Json.decodeFromString<TeslaRootPlan>(prefs.getString("tesla_root_journal", null)!!)
                    File(base, "late-tesla-remove.sh").writeText(teslaPlan.removeScript())
                    phase("crash-ready")
                    error("host acknowledged crash instead of stopping the process")
                }
                val first = plan().rules.owner
                manager.revoke(); manager.maintain(network, true); ready()
                check(plan().rules.owner != first)
                phase("share-last")
                val damaged = plan()
                check(root.run(damaged.rules.removeScript(), "DASHPLAY_SHARE_REMOVED").success)
                now += 16_000; manager.maintain(network, true); ready()
                check(plan().rules.owner != damaged.rules.owner)
                phase("recovered")
                val previousManager = manager
                previousManager.revoke()
                manager = UsbShareAccess(context, root::run) { now }
                check(manager.recover())
                check(!prefs.contains("usb_share_journal"))
                previousManager.close()
                manager.maintain(network, true); ready()
                phase("new-manager")
                root.denyCleanup = true; settings.usbHotspotShare = false
                manager.maintain(network, true)
                check(manager.status().getString("state") == "CLEANUP_REQUIRED" && prefs.contains("usb_share_journal"))
                val failedCalls = root.commands
                repeat(3) { manager.maintain(network, true) }
                check(root.commands == failedCalls) { "cleanup denial repeated Root requests" }
                root.denyCleanup = false; manager.close()
                check(!prefs.contains("usb_share_journal"))
                phase("tesla-only")
                tesla.close(); check(!TeslaRootAccess.active && !TeslaRootAccess.checksActive)
                phase("clean")
                settings.teslaOfflineChecks = false; settings.teslaRootAccess = false
                settings.usbHotspotShare = true
                manager.maintain(network, true); ready()
                phase("before-vpn-close")
                tunnel.close()
                check(!File(context.filesDir, "usb-share-lease").exists()) { "VPN callback did not revoke share lease" }
                phase("vpn-closed")
                manager.maintain(network, true)
                check(manager.status().getString("state") == "WAITING_USB") { manager.status().toString() }
                check(!prefs.contains("usb_share_journal"))
                phase("vpn-clean")
                result.putString("shared_manager", "real manager: local opt-in, both Tesla/DNS rule orders, firewall recovery, journal recovery in new manager, denied cleanup bounded retry, actual VPN close callback and cleanup")
            } finally {
                root.denyCleanup = false
                manager.close(); tesla.close()
                settings.usbEnabled = false; settings.iphoneLink = originalLink; settings.car = originalCar; settings.network = originalNetwork
                prefs.edit().remove("usb_internet").remove("usb_internet_consent").remove("usb_hotspot_share")
                    .remove("usb_hotspot_share_consent").remove("tesla_root_access").remove("tesla_offline_checks").commit()
                server.close(); responder.join(1000)
            }
        }
    }

    fun recoverAfterCrash(context: Context, result: Bundle) {
        check(android.os.Build.HARDWARE in listOf("ranchu", "goldfish"))
        val base = File(context.cacheDir, "usb-share-manager")
        val oldPid = File(base, "crash-pid").readText().toInt()
        val pid = android.os.Process.myPid()
        check(oldPid != pid) { "recovery must run in a fresh process" }
        check(UsbVpnService.internetLease == null && !UsbVpnService.internetReady)
        val prefs = context.getSharedPreferences("dashplay", Context.MODE_PRIVATE)
        for (name in listOf("share", "tesla")) {
            val key = if (name == "share") "usb_share_journal" else "tesla_root_journal"
            check(prefs.getString(key, null) == File(base, "crash-$name-journal").readText())
        }
        for (name in listOf("usb-share-lease", "tesla-root-lease")) check(File(context.filesDir, name).exists())
        val root = EmulatorRootBridge(File(base, "recovery-requests"))
        val tesla = TeslaRootAccess(context, root::tesla)
        val manager = UsbShareAccess(context, root::run)
        // Match DashPlayService startup order; this uses on-disk journals, not the old objects.
        check(tesla.recover())
        check(manager.recover())
        check(!prefs.contains("tesla_root_journal") && !prefs.contains("usb_share_journal"))
        for (name in listOf("usb-share-lease", "tesla-root-lease")) check(!File(context.filesDir, name).exists())
        val settings = Settings(context)
        check(settings.usbHotspotShare && settings.usbHotspotShareAllowed && settings.usbInternet && settings.usbInternetAllowed)
        val network = LocalNetwork.Choice("dp-ap0", InetAddress.getByName("192.0.2.1"), LocalNetwork.Kind.HOTSPOT)
        val calls = root.commands
        repeat(3) { manager.maintain(network, true) }
        check(manager.status().getString("state") == "WAITING_USB" && !manager.status().getBoolean("active"))
        check(root.commands == calls) { "recovery tried to install without a live VPN" }
        File(base, "recovered-pid").writeText(pid.toString())
        File(base, "phase").writeText("crash-recovered")
        val end = System.nanoTime() + 60_000_000_000L
        while (!File(base, "done-crash-recovered").exists()) {
            check(System.nanoTime() < end) { "host did not verify crash recovery" }
            Thread.sleep(25)
        }
        result.putInt("old_pid", oldPid); result.putInt("new_pid", pid)
        result.putString("process_death_recovery", "whole package force-stopped; fresh process recovered both durable journals and waited for USB")
        result.putBoolean("success", true)
    }
}
