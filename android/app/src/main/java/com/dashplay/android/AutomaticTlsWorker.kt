package com.dashplay.android

import java.io.File

/** One serialized caller drives tick(); configuration changes cancel publication, but never discard cleanup ownership. */
internal class AutomaticTlsWorker(
    private val store: AutomaticTlsStore,
    private val certificates: OwnTlsStore,
    private val online: () -> Boolean,
    private val address: () -> String?,
    private val installed: (String) -> Unit,
    private val stopped: () -> Boolean = { false },
    private val now: () -> Long = System::currentTimeMillis,
    private val dnsFactory: (AutomaticTlsStore.Profile) -> CertificateDns = {
        CertificateDns(it.dns, UrlCertificateHttp(), store.journal(it))
    },
    private val issue: (AutomaticTlsStore.Profile, CertificateDns, () -> Boolean) -> AcmeClient.Issued = { profile, dns, cancelled ->
        AcmeClient(UrlCertificateHttp(cancelled), cancelled = cancelled).issue(profile.dns.domain,
            AcmeAccountStore(File(store.directory, "production-account")).loadOrCreate(), dns, true)
    },
) {
    fun tick() {
        val start = store.read()
        if (stopped() || now() < maxOf(start.nextAttempt, start.serverNotBefore)) return
        val profile = start.profile
        // No remote operation is needed for a configuration that never wrote a challenge.
        start.retired.filterNot { store.journal(it).exists() }.forEach { store.completeCleanup(it.id) }
        val hasCleanup = start.retired.any { store.journal(it).exists() } || profile?.let { store.journal(it).exists() } == true
        if (!start.enabled && !hasCleanup) return
        fun cancelled() = stopped() || !store.current(start.revision)
        fun waiting(phase: String) { store.update(start.revision) { it.copy(phase = phase, nextAttempt = now() + 15_000) } }
        if (!online()) { waiting("waiting_network"); return }
        try {
            for (retired in start.retired) {
                if (stopped()) return
                dnsFactory(retired).recover()
                store.completeCleanup(retired.id)
            }
            if (cancelled()) return
            val dns = profile?.let(dnsFactory)
            dns?.recover()
            if (!start.enabled || profile == null || dns == null) {
                store.update(start.revision) { it.copy(phase = "off", reason = "", failures = 0, nextAttempt = 0) }; return
            }
            val target = address()
            if (target.isNullOrEmpty()) { waiting("waiting_address"); return }
            if (cancelled()) return
            if (target != start.dnsAddress || now() >= start.dnsNext) {
                dns.updateAddress(target)
                if (!store.update(start.revision) { it.copy(dnsAddress = target, dnsNext = now() + 1_800_000) }) return
            }
            val selected = certificates.selected()
            val needsCertificate = selected == null || selected.domain != profile.dns.domain || selected.expires - now() <= 30L * 86400_000
            if (needsCertificate && (now() >= start.renewNext || selected == null || selected.domain != profile.dns.domain)) {
                if (!store.update(start.revision) { it.copy(phase = "issuing", reason = "") }) return
                val result = issue(profile, dns, ::cancelled)
                if (cancelled()) return
                if (!store.installIfCurrent(start.revision) {
                    certificates.install(result.domain, result.chain, result.key)
                    installed(certificates.state().fingerprint)
                }) return
            }
            store.update(start.revision) { it.copy(phase = "ready", reason = "", failures = 0,
                nextAttempt = now() + 15_000, renewNext = if (now() >= start.renewNext) now() + 43_200_000 else it.renewNext) }
        } catch (error: Exception) {
            if (cancelled()) return
            val failure = error as? CertificateFailure ?: CertificateFailure("storage")
            if (failure.reason == "cancelled") return
            store.update(start.revision) {
                val count = (it.failures + 1).coerceAtMost(1000)
                val delay = maxOf(backoff(count), failure.retryAfterSeconds * 1000)
                it.copy(phase = "retry", reason = failure.reason, failures = count, nextAttempt = now() + delay,
                    serverNotBefore = if (failure.retryAfterSeconds > 0) now() + failure.retryAfterSeconds * 1000 else it.serverNotBefore)
            }
        }
    }
    companion object {
        internal fun backoff(failures: Int): Long = when (failures) { 1 -> 60_000; 2 -> 120_000; 3 -> 300_000; 4 -> 900_000; else -> 3_600_000 }
    }
}
