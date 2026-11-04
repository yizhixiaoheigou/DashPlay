package com.dashplay.android

import com.playport.server.WebHub
import io.ktor.server.application.Application
import io.ktor.server.engine.EmbeddedServer
import io.ktor.server.engine.embeddedServer
import io.ktor.server.cio.CIO
import io.ktor.server.routing.routing
import io.ktor.server.routing.get
import io.ktor.server.response.respondRedirect
import java.io.Closeable
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.nio.file.Path

/**
 * Where a browser can reach the DashPlay page, as one value instead of hand-built strings.
 *
 * Until now every caller wrote `https://…` itself, which is exactly what stops a plain-HTTP compatibility entry
 * (a Tesla opens the page, but WebCodecs needs a secure context — 2026-11-03) from ever being shown. One type also
 * tracks the protocol explicitly; management is always reachable over HTTP on the shared port.
 */
data class BrowserEndpoint(
    /** "https" for the existing TLS entry, "http" for the compatibility entry. */
    val scheme: String,
    val host: String,
    val port: Int,
    /** Where this entry is meant to be used; shown to the person, never used for routing. */
    val purpose: String,
    val available: Boolean,
    /** Why it is not available, when [available] is false. */
    val reason: String = "",
) {
    init {
        require(scheme == "https" || scheme == "http") { "scheme must be http or https" }
        require(port in 1..65535) { "port must be 1..65535" }
        require(purpose.isNotBlank()) { "purpose must be set" }
    }

    fun url(token: String? = null): String =
        "${scheme}://$host${if (port == defaultPort) "" else ":$port"}/" + (token?.let { "?token=$it" } ?: "")

    private val defaultPort: Int get() = if (scheme == "https") 443 else 80

    companion object {
        /** The compatibility entry is a non-privileged port: no Root, no listener on 80/443 for plain HTTP. */
        const val DEFAULT_COMPAT_PORT = BrowserPort.DEFAULT
        const val COMPAT_PURPOSE = "HTTP 画面入口"
        const val TLS_PURPOSE = "HTTPS 主入口"

        /** Validates a port the person typed. Same rules as the receiver's own HTTPS port: never 0 or privileged. */
        fun compatPort(typed: Int): Result<Int> {
            if (typed !in 1024..65535) return Result.failure(IllegalArgumentException("网页端口需在 1024–65535 之间。"))
            return Result.success(typed)
        }
    }
}

/**
 * The plain-HTTP backend: bound to loopback by the service, serving management independently of the optional viewer.
 *
 * BrowserFront routes cleartext here and TLS to a separate private TLS backend. The page arrives over HTTP because
 * WebCodecs needs a secure context and a Tesla cannot be given a certificate — so on this entry WebCodecs is absent
 * by design and the picture comes over the fallback transports (JPEG, RTC). That is a property of the transport, and
 * the page checks it per feature instead of refusing to start.
 *
 * Management is installed even with the viewer disabled. Its existing same-origin checks stay in force;
 * viewer sockets still use WebHub authorization. TLS/certificate initialization cannot prevent this backend starting.
 */
internal class CompatHttpEntry : Closeable {
    private val servers = ArrayList<EmbeddedServer<*, *>>()
    val localPort: Int get() = boundPort
    private var boundPort: Int = 0

    @Volatile var endpoint: BrowserEndpoint? = null
        private set
    /** Backend addresses; production binds only to loopback behind BrowserFront. */
    @Volatile var addresses: List<String> = emptyList()
        private set

    /**
     * Starts (or replaces) the entry. A failure is returned, not thrown: the receiver keeps its TLS page and the
     * Tesla page simply says why the compatibility entry is missing.
     */
    fun start(addresses: List<InetAddress>, port: Int, webRoot: Path, hub: WebHub,
        viewerEnabled: Boolean = true, management: Application.() -> Unit = {}): Result<BrowserEndpoint> {
        stop()
        val hosts = addresses.distinctBy { it.hostAddress }
        if (hosts.isEmpty()) return Result.failure(IllegalStateException("没有可监听的地址。"))
        // Check every address before binding any: Ktor's failure for a busy port is less useful than this message.
        for (host in hosts) {
            val probe = runCatching {
                ServerSocket().use { socket ->
                    socket.reuseAddress = true
                    socket.bind(InetSocketAddress(host, port))
                }
            }
            probe.exceptionOrNull()?.let {
                return Result.failure(IllegalStateException("端口 $port 在 ${host.hostAddress} 上已被占用或不允许监听。", it))
            }
        }
        return runCatching {
            val app: Application.() -> Unit = {
                HttpBrowserRoutes.install(this, management, if (viewerEnabled) ({ hub.install(this, webRoot, viewerOnly = true) }) else null)
            }
            for (host in hosts) {
                val server = embeddedServer(CIO, port = port, host = host.hostAddress, module = app).start(wait = false)
                servers += server
                boundPort = kotlinx.coroutines.runBlocking { server.engine.resolvedConnectors().first().port }
            }
            this.addresses = hosts.mapNotNull { it.hostAddress }
            BrowserEndpoint("http", hosts.first().hostAddress.orEmpty(), boundPort,
                BrowserEndpoint.COMPAT_PURPOSE, available = true).also { endpoint = it }
        }.onFailure { stop() }
    }

    fun stop() {
        servers.forEach { runCatching { it.stop(200, 800) } }
        servers.clear()
        endpoint = null
        boundPort = 0
        addresses = emptyList()
    }

    override fun close() = stop()
}

/** Management is independent of the optional HTTP picture route and of certificate readiness. */
internal object HttpBrowserRoutes {
    fun install(app: Application, management: Application.() -> Unit, viewer: (Application.() -> Unit)?) {
        management(app)
        if (viewer != null) viewer(app) else app.routing {
            get("/") { call.respondRedirect("/manage") }
        }
    }
}
