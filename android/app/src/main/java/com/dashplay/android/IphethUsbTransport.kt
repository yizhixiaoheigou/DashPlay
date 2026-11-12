package com.dashplay.android

import android.hardware.usb.*
import android.os.Build
import java.io.IOException
import java.nio.ByteBuffer
import java.util.concurrent.TimeoutException

/** Separate ff/fd/01 Personal Hotspot interface; ownership never extends to USBMUX or CarPlay NCM. */
internal class IphethUsbTransport private constructor(
    private val connection: UsbDeviceConnection, private val usb: UsbInterface,
    private val input: UsbEndpoint, private val output: UsbEndpoint, private val mac: ByteArray,
) : UsbTetherSession.Wire {
    override val hostMac get() = mac.copyOf()
    private val state = Any(); private val reads = Any(); private val writes = Any(); private val controls = Any()
    @Volatile private var closed = false
    private val buffer = ByteBuffer.allocateDirect(1516)
    private var request: UsbRequest? = null
    private var queued = false

    /** No NCM-enable command: legacy ipheth RX keeps each USB completion within old Android limits. */
    override fun receive(timeoutMillis: Long): ByteArray? = synchronized(reads) {
        require(timeoutMillis in 1..5000); checkOpen()
        val bytes: ByteArray
        if (Build.VERSION.SDK_INT < 26) {
            val data = ByteArray(1516)
            val count = connection.bulkTransfer(input, data, data.size, timeoutMillis.coerceAtMost(50).toInt())
            checkOpen()
            if (count <= 0) return@synchronized null
            bytes = data.copyOf(count)
        } else {
            val pending = synchronized(state) {
                checkOpen()
                val p = request ?: UsbRequest().also {
                    if (!it.initialize(connection, input)) { it.close(); throw IOException("USB 热点读取初始化失败") }
                    request = it
                }
                if (!queued) {
                    buffer.clear()
                    if (!p.queue(buffer)) throw IOException("USB 热点读取排队失败")
                    queued = true
                }
                p
            }
            val done = try { connection.requestWait(timeoutMillis) } catch (_: TimeoutException) { return@synchronized null }
            checkOpen()
            if (done !== pending) throw IOException("USB 热点读取中断")
            queued = false
            bytes = ByteArray(buffer.position()); buffer.flip(); buffer.get(bytes)
        }
        UsbTetherProtocol.legacyFrame(bytes)
    }

    override fun send(frame: ByteArray) = synchronized(writes) {
        require(frame.size in 14..1514); checkOpen()
        // ipheth TX is raw Ethernet even on devices that support aggregated RX.
        val count = connection.bulkTransfer(output, frame, frame.size, 1000)
        checkOpen()
        if (count != frame.size) throw IOException("USB 热点写入中断")
    }

    override fun carrier(): Boolean = synchronized(controls) {
        checkOpen()
        val b = ByteArray(64)
        // Apple's vendor commands use fixed wIndex 2 (independent of descriptor interface numbering).
        val n = connection.controlTransfer(0xc0, 0x45, 0, 2, b, b.size, 1000)
        checkOpen()
        if (n <= 0) throw IOException("无法读取 iPhone 热点状态")
        (n == 1 && b[0] == 4.toByte()) || (n >= 2 && b[1] == 4.toByte())
    }
    private fun checkOpen() { if (closed) throw IOException("USB 热点接口已关闭") }
    override fun close() {
        val pending = synchronized(state) { if (closed) return; closed = true; request }
        runCatching { pending?.cancel() }
        runCatching { connection.releaseInterface(usb) }
        connection.close(); runCatching { pending?.close() }
    }

    companion object {
        /** Caller must already have USB consent, the active CarPlay configuration, and iPhone trust. */
        fun open(manager: UsbManager, device: UsbDevice, config: UsbConfiguration): IphethUsbTransport {
            check(manager.hasPermission(device)) { "请先允许 USB 访问" }
            require(device.vendorId == 0x05ac)
            val candidates = (0 until config.interfaceCount).map(config::getInterface).filter {
                it.interfaceClass == 255 && it.interfaceSubclass == 253 && it.interfaceProtocol == 1 && it.alternateSetting == 1
            }
            if (candidates.size != 1) throw IOException("iPhone 未提供唯一的 USB 热点接口")
            val usb = candidates.single()
            val endpoints = (0 until usb.endpointCount).map(usb::getEndpoint).filter { it.type == UsbConstants.USB_ENDPOINT_XFER_BULK }
            val input = endpoints.singleOrNull { it.direction == UsbConstants.USB_DIR_IN } ?: throw IOException("缺少 USB 热点输入端点")
            val output = endpoints.singleOrNull { it.direction == UsbConstants.USB_DIR_OUT } ?: throw IOException("缺少 USB 热点输出端点")
            val connection = manager.openDevice(device) ?: throw IOException("无法打开 USB 热点接口")
            var claimed = false
            try {
                // A userspace owner deserves the polite claim; a *kernel* driver does not. Many vendor kernels set
                // CONFIG_USB_IPHETH=y, which binds Apple's Ethernet interface before we ever see it, and Android never
                // configures that netdev — so borrowing the iPhone's network silently never comes up (observed on a TV box, 2026-11-02).
                // Try the polite claim first, then take the interface over from the kernel driver.
                claimed = connection.claimInterface(usb, false)
                if (!claimed) claimed = connection.claimInterface(usb, true)
                if (!claimed || !connection.setInterface(usb)) throw IOException("USB 热点接口已被系统占用或无法启用")
                val bytes = ByteArray(64)
                val count = connection.controlTransfer(0xc0, 0, 0, 2, bytes, bytes.size, 1000)
                val mac = bytes.copyOf(6)
                if (count < 6 || !UsbTetherProtocol.validMac(mac)) throw IOException("iPhone 未返回有效 USB 热点地址")
                return IphethUsbTransport(connection, usb, input, output, mac)
            } catch (error: Throwable) {
                if (claimed) runCatching { connection.releaseInterface(usb) }
                connection.close(); throw error
            }
        }
    }
}
