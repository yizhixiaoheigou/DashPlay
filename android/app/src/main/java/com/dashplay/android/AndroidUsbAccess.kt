package com.dashplay.android

import android.hardware.usb.*
import android.os.Build
import com.shilapi.xcertplay.transport.*
import java.io.Closeable
import java.io.IOException
import java.nio.ByteBuffer
import java.util.concurrent.TimeoutException

/** Android owns USB permissions; this class owns only explicitly opened iPhone interfaces. */
object AndroidUsbAccess {
    fun candidates(manager: UsbManager): List<UsbDevice> = manager.deviceList.values.filter { device ->
        device.vendorId == 0x05ac && (0 until device.configurationCount).any { c ->
            val config = device.getConfiguration(c)
            IphoneCarPlayConfiguration.usbMuxInterface(config) != null
        }
    }.sortedBy { it.deviceId }

    class Reenumerating : IOException("等待 iPhone 重新连接 USB")
    internal class Opened(val pipe: Iap2UsbSession?, val ncm: UsbCarPlayWire?, val interfaceNumber: Int, val serial: String,
        private val connection: UsbDeviceConnection, private val manager: UsbManager,
        private val device: UsbDevice, private val config: UsbConfiguration, val hostMac: ByteArray? = null,
        val activateNcm: () -> Unit = {}) : Closeable {
        internal fun openInternet(): UsbTetherSession.Wire = IphethUsbTransport.open(manager, device, config)
        override fun close() {
            runCatching { ncm?.close() }
            if (pipe != null) runCatching { pipe?.close() } else runCatching { connection.close() }
        }
    }

    internal fun open(manager: UsbManager, device: UsbDevice, carPlay: Boolean = true): Opened {
        check(manager.hasPermission(device)) { "请允许访问 USB 设备" }
        val connection = manager.openDevice(device) ?: throw IOException("无法打开 iPhone USB 连接")
        var pipe: UsbBulkPipe? = null
        try {
            val config = IphoneCarPlayConfiguration.find(device)
            if (config == null) {
                val reply = ByteArray(1)
                val count = connection.controlTransfer(0xc0, 0x52, 0, 4, reply, 1, 1000)
                if (count != 1) throw IOException("iPhone 未接受 CarPlay USB 模式，请解锁手机并重新插线")
                throw Reenumerating()
            }
            // Re-selecting the active configuration can reset its interface/endpoint state.
            val selection = UsbConfigurationSelection.ensure(config.id, current = {
                val reply = ByteArray(1)
                val count = connection.controlTransfer(0x80, 8, 0, 0, reply, 1, 1000)
                if (count == 1) reply[0].toInt() and 0xff else null
            }, select = { connection.setConfiguration(config) })
            DriveLog.event("wired USB configuration desired=${config.id} before=${selection.before} after=${selection.after} selected=${selection.selectionResult} uptimeMs=${android.os.SystemClock.elapsedRealtime()}")
            val serial = runCatching { device.serialNumber }.getOrNull()
            if (serial.isNullOrBlank()) throw IOException("无法读取 iPhone 标识，请重新授权 USB")
            if (!carPlay) {
                return Opened(null, null, 0, serial, connection, manager, device, config)
            }
            val usb = IphoneCarPlayConfiguration.usbMuxInterface(config) ?: throw IOException("缺少 USBMUX 接口")
            val endpoints = IphoneCarPlayConfiguration.usbMuxEndpoints(usb) ?: throw IOException("缺少 USBMUX 数据端点")
            if (!connection.claimInterface(usb, true)) throw IOException("USBMUX 接口正被其他应用占用")
            pipe = UsbBulkPipe(connection, usb, endpoints.first, endpoints.second)
            val functions = NcmFunctionDiscovery.findAll(config)
            if (functions.isEmpty()) throw IOException("缺少 CarPlay NCM 网络接口")
            val bridges = mutableListOf<NcmUsbBridge>()
            try {
                for (function in functions) {
                    val ncmConnection = manager.openDevice(device) ?: throw IOException("无法打开 NCM 数据通道")
                    bridges += NcmUsbBridge.open(ncmConnection, function, config.id, deferActivation = true)
                }
                val ncm = UsbCarPlayWireGroup(bridges.mapIndexed { index, bridge -> object : UsbCarPlayWire {
                    private var loggedTcp = false
                    private var rxTraces = 0
                    private var txTraces = 0
                    override fun receive(timeoutMillis: Long) = bridge.recv(timeoutMillis)?.also { frame ->
                        if (rxTraces < 8) UsbHandshakeTrace.describe(frame)?.let {
                            rxTraces++
                            DriveLog.event("wired ncm rx interface=${functions[index].control.id} uptimeMs=${android.os.SystemClock.elapsedRealtime()} $it")
                        }
                        if (!loggedTcp && frame.size >= 74 && frame[12] == 0x86.toByte() &&
                            frame[13] == 0xdd.toByte() && frame[20] == 6.toByte()) {
                            loggedTcp = true
                            DriveLog.event("wired NCM TCP received on interface=${functions[index].control.id}")
                        }
                    }
                    override fun send(frame: ByteArray) {
                        if (txTraces < 8) UsbHandshakeTrace.describe(frame)?.let {
                            txTraces++
                            DriveLog.event("wired ncm tx-attempt interface=${functions[index].control.id} uptimeMs=${android.os.SystemClock.elapsedRealtime()} $it")
                        }
                        bridge.send(frame, 500)
                    }
                    override fun close() = bridge.close()
                } })
                DriveLog.event("wired NCM functions=${functions.joinToString { "${it.control.id}/${it.data.id}" }}")
                return Opened(pipe, ncm, functions.first().control.id, serial, connection, manager, device, config,
                    bridges.first().hostMac, activateNcm = { bridges.forEach { it.activate() } })
            } catch (error: Throwable) {
                bridges.forEach { runCatching { it.close() } }
                throw error
            }
        } catch (error: Throwable) {
            pipe?.close() ?: connection.close()
            throw error
        }
    }
}

/** Separate read/write locks permit full duplex. Timed-out requests remain queued, preserving packets. */
private class UsbBulkPipe(
    private val connection: UsbDeviceConnection,
    private val usb: UsbInterface,
    private val output: UsbEndpoint,
    private val input: UsbEndpoint,
) : Iap2UsbSession {
    private val state = Any()
    private val readLock = Any()
    private val writeLock = Any()
    private val buffer = ByteBuffer.allocateDirect(16_384)
    private var request: UsbRequest? = null
    private var queued = false
    @Volatile private var closed = false

    override fun write(data: ByteArray, timeoutMillis: Int) = synchronized(writeLock) {
        check(!closed) { "USB 已断开" }
        require(timeoutMillis > 0)
        var offset = 0
        while (offset < data.size) {
            val length = minOf(16_384, data.size - offset)
            val count = connection.bulkTransfer(output, data, offset, length, timeoutMillis)
            if (count != length) throw IOException("USB 写入中断")
            offset += count
        }
    }

    override fun read(timeoutMillis: Long): ByteArray? = synchronized(readLock) {
        check(!closed) { "USB 已断开" }
        require(timeoutMillis > 0)
        if (Build.VERSION.SDK_INT < 26) {
            val bytes = ByteArray(16_384)
            val count = connection.bulkTransfer(input, bytes, bytes.size, timeoutMillis.coerceIn(1, 50).toInt())
            return@synchronized if (count > 0) bytes.copyOf(count) else null
        }
        val active = synchronized(state) {
            check(!closed)
            val active = request ?: UsbRequest().also {
                if (!it.initialize(connection, input)) { it.close(); throw IOException("USB 读取初始化失败") }
                request = it
            }
            if (!queued) {
                buffer.clear()
                if (!active.queue(buffer)) throw IOException("USB 读取排队失败")
                queued = true
            }
            active
        }
        val completed = try { connection.requestWait(timeoutMillis) } catch (_: TimeoutException) { return@synchronized null }
        if (completed !== active) throw IOException("USB 读取已断开")
        queued = false
        ByteArray(buffer.position()).also { buffer.flip(); buffer.get(it) }
    }

    override fun close() {
        val pending = synchronized(state) {
            if (closed) return
            closed = true; request
        }
        runCatching { pending?.cancel() }
        runCatching { connection.releaseInterface(usb) }
        connection.close()
        runCatching { pending?.close() }
    }
}
