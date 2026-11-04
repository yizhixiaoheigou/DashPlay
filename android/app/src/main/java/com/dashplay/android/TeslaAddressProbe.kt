package com.dashplay.android

import android.content.Context
import android.net.ConnectivityManager
import kotlinx.serialization.Serializable
import java.io.File
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.atomic.AtomicBoolean

/** A persistent marker page for an exclusively owned address lease. The activity owns its lifetime.
 * Address existence alone is never external reachability; a peer request and marker echo are both required.
 */
internal object TeslaAddressProbe {
    /** Address under test. The project's own range; never a public address borrowed from somewhere else. */
    const val TEST_ADDRESS = TeslaRootPlan.ADDRESS

    @Serializable
    data class Result(
        val stage: String,
        val reachable: Boolean,
        val detail: String,
        val marker: String,
    )

    /** What the person or their helper sees on the page; no secrets, so it can be opened from any device. */
    fun page(marker: String): String = """
        <!doctype html><meta charset="utf-8"><title>DashPlay 地址测试</title>
        <h1>地址可达性测试</h1>
        <p>如果这一页是在另一台设备（车机或手机）上打开的，说明这个地址从热点客户端可达。</p>
        <p>标记：<code>$marker</code></p>
        <p>把这个标记填回盒子上的测试入口，盒子才会记录“已由真实客户端验证”。</p>
    """.trimIndent()

    /**
     * Serves the test page on the test address for the lifetime of this object. It is a plain {@link ServerSocket},
     * not the receiver: nothing here can expose the app's management or identity routes.
     */
    class Page(private val marker: String, private val port: Int, private val onPeer: (String) -> Unit = {}) : AutoCloseable {
        private val socket: ServerSocket? = runCatching {
            val listener = ServerSocket()
            try {
                listener.reuseAddress = true
                listener.bind(java.net.InetSocketAddress(InetAddress.getByName(TEST_ADDRESS), port))
                listener
            } catch (error: Exception) { listener.close(); throw error }
        }.getOrNull()
        private val open = AtomicBoolean(true)
        private var thread: Thread? = null

        val started: Boolean get() = socket != null

        fun start() {
            val listener = socket ?: return
            thread = Thread({
                val body = page(marker).toByteArray()
                while (open.get() && !listener.isClosed) {
                    runCatching {
                        listener.accept().use { client ->
                            client.soTimeout = 3000
                            val received = client.getInputStream().read(ByteArray(2048))
                            if (received <= 0) return@use
                            val peer = client.inetAddress
                            val local = java.net.NetworkInterface.getNetworkInterfaces().toList().flatMap { it.inetAddresses.toList() }
                            if (!peer.isLoopbackAddress && local.none { it == peer }) onPeer(peer.hostAddress.orEmpty())
                            val header = "HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n" +
                                "Content-Length: ${body.size}\r\nCache-Control: no-store\r\n\r\n"
                            client.getOutputStream().write(header.toByteArray())
                            client.getOutputStream().write(body)
                        }
                    }
                }
            }, "dashplay-address-probe").apply { isDaemon = true }
            thread?.start()
        }

        override fun close() {
            open.set(false)
            runCatching { socket?.close() }
            thread?.interrupt()
        }
    }

    /** The real hotspot interface to name in the result: a VPN address is never the carrier network (LocalNetwork). */
    fun carrierName(context: Context): String? = runCatching {
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        manager.allNetworks.mapNotNull { manager.getLinkProperties(it)?.interfaceName }.firstOrNull { name ->
            !name.startsWith("tun") && !name.startsWith("ppp") && name != "lo"
        }
    }.getOrNull()

    /** Journal for the box: which topology and configuration produced which answer. */
    fun journal(file: File, results: List<Result>) {
        runCatching { file.writeText(results.joinToString("\n") { "${System.currentTimeMillis()} ${it.stage} ${it.reachable} ${it.detail}" }) }
    }
}
