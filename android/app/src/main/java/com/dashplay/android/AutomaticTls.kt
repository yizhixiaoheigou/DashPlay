package com.dashplay.android

import android.content.Context
import android.net.ConnectivityManager
import java.io.Closeable
import java.io.File
import java.net.InetAddress
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

/** Shares the receiver's foreground lifecycle. No network work or private key parsing runs on the UI thread. */
internal object AutomaticTls {
    private var instance: AutomaticTlsStore? = null
    private val generation = AtomicLong()
    private val executor = Executors.newSingleThreadScheduledExecutor { task -> Thread(task, "dashplay-certificates") }
    private var future: ScheduledFuture<*>? = null
    private val pending = AtomicReference<String?>()

    @Synchronized fun store(context: Context): AutomaticTlsStore = instance ?: AutomaticTlsStore(
        File(context.applicationContext.filesDir, "automatic-tls")).also { instance = it }

    @Synchronized fun attach(context: Context): Closeable {
        val app = context.applicationContext
        val id = generation.incrementAndGet()
        future?.cancel(false)
        val configuration = store(app)
        val worker = AutomaticTlsWorker(configuration, OwnTls.store(app), {
            @Suppress("DEPRECATION")
            val connected = (app.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager).activeNetworkInfo?.isConnected == true
            connected && runCatching {
                val provider = configuration.read().profile?.dns?.provider
                InetAddress.getAllByName("acme-v02.api.letsencrypt.org").isNotEmpty() &&
                    InetAddress.getAllByName(if (provider == "desec") "desec.io" else "www.duckdns.org").isNotEmpty()
            }.getOrDefault(false)
        }, {
            val snapshot = DashPlayService.snapshot()
            when {
                snapshot.network == null -> null
                TeslaRootAccess.active -> TeslaRootPlan.ADDRESS
                Settings(app).teslaRootAccess -> null // Never publish the ordinary address while the requested alias is unavailable.
                else -> snapshot.network.address.hostAddress
            }
        }, { pending.set(it) }, stopped = { generation.get() != id })
        future = executor.scheduleWithFixedDelay({
            if (generation.get() == id) runCatching { worker.tick() }
            // A corrupt private record is shown by state(), and must not kill the receiver or replace secrets.
        }, 1, 5, TimeUnit.SECONDS)
        return Closeable { synchronized(this) {
            if (generation.compareAndSet(id, id + 1)) { future?.cancel(false); future = null }
        } }
    }

    fun shouldReload(context: Context, loadedFingerprint: String?, sessionActive: Boolean): Boolean {
        val expected = pending.get() ?: return false
        val current = OwnTls.store(context).state()
        if (expected == loadedFingerprint || expected != current.fingerprint || !current.enabled) {
            pending.compareAndSet(expected, null); return false
        }
        return !sessionActive
    }
    fun import(context: Context, request: TlsImport) {
        // Invalid uploads must leave both the current certificate and automation enabled state unchanged.
        OwnTlsMaterial.parse(request.domain, request.chain, request.key, System.currentTimeMillis())
        store(context).manual { OwnTls.store(context).install(request.domain, request.chain, request.key) }
    }
    fun enable(context: Context, enabled: Boolean) {
        val certs = OwnTls.store(context)
        require(!enabled || certs.state().let { it.domain.isNotEmpty() && it.expires > System.currentTimeMillis() }) { "请先导入有效证书。" }
        store(context).manual { certs.enable(enabled) }
    }
}
