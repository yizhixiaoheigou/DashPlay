package com.dashplay.android

import android.app.Activity
import android.app.Instrumentation
import android.os.Bundle
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject

/** Read-only outbound compatibility probe. Never registers accounts, writes DNS, or requests certificates. */
class CertificateNetworkProbe : Instrumentation() {
    private var checksProbe = false
    private var internetProbe = false
    private var internetLanPort = 0
    private var internetShare = false
    private var internetManager = false
    private var managerCrash = false
    private var managerRecovery = false
    private var checksInterface = "dp-tesla0"
    private var vpnCrashControl = false
    private var packetBridgeProbe = false
    private var usbBootProbe = false
    private var usbWizardProbe = false
    private var usbDefaultsProbe = false
    private var wifiSetupProbe = false
    private var wifiWizardProbe = false
    override fun onCreate(arguments: Bundle?) {
        super.onCreate(arguments); checksProbe = arguments?.getString("probe") == "tesla-checks"
        packetBridgeProbe = arguments?.getString("probe") == "packet-bridge"
        usbBootProbe = arguments?.getString("probe") == "usb-boot"
        usbDefaultsProbe = arguments?.getString("probe") == "usb-defaults"
        usbWizardProbe = arguments?.getString("probe") == "usb-wizard"
        wifiSetupProbe = arguments?.getString("probe") == "wifi-setup"
        wifiWizardProbe = arguments?.getString("probe") == "wifi-wizard"
        internetProbe = arguments?.getString("probe") == "usb-internet"
        internetLanPort = arguments?.getString("lanPort")?.toIntOrNull() ?: 0
        internetShare = arguments?.getString("share") == "true"
        internetManager = arguments?.getString("manager") == "true"
        managerCrash = arguments?.getString("crash") == "true"
        managerRecovery = arguments?.getString("probe") == "usb-share-recover"
        checksInterface = arguments?.getString("checksInterface") ?: "dp-tesla0"
        vpnCrashControl = arguments?.getString("vpnCrashControl") == "true"
        start()
    }
    override fun onStart() {
        val results = Bundle()
        try {
            if (usbWizardProbe) { UsbWizardProbe.run(this, results); finish(Activity.RESULT_OK, results); return }
            if (usbDefaultsProbe) { UsbDefaultsProbe.run(targetContext, results); finish(Activity.RESULT_OK, results); return }
            if (wifiWizardProbe) { WifiWizardProbe.run(this, results); finish(Activity.RESULT_OK, results); return }
            if (wifiSetupProbe) { WifiSetupProbe.run(targetContext, results); finish(Activity.RESULT_OK, results); return }
            if (usbBootProbe) {
                BootReceiverProbe.run(targetContext, results)
                UsbBootProbe.run(targetContext, results)
                finish(Activity.RESULT_OK, results); return
            }
            if (packetBridgeProbe) { PacketBridgeProbe.run(results); finish(Activity.RESULT_OK, results); return }
            if (managerRecovery) { UsbShareManagerProbe.recoverAfterCrash(targetContext, results); finish(Activity.RESULT_OK, results); return }
            if (internetProbe) { UsbInternetProbe.run(this, results, internetLanPort, internetShare, internetManager, managerCrash, vpnCrashControl); finish(Activity.RESULT_OK, results); return }
            if (checksProbe) { TeslaChecksProbe.run(this, results, checksInterface); finish(Activity.RESULT_OK, results); return }
            val accountDirectory = java.io.File(targetContext.cacheDir, "certificate-account-probe").apply { mkdirs() }
            try {
                results.putString("account_stage", "create")
                val account = AcmeAccountStore(accountDirectory).loadOrCreate()
                results.putString("account_stage", "reload")
                val reopened = AcmeAccountStore(accountDirectory).loadOrCreate()
                results.putString("account_stage", "sign")
                check(account.public.encoded.contentEquals(reopened.public.encoded))
                val payload = "read-only-crypto-probe".toByteArray()
                val signature = java.security.Signature.getInstance("SHA256withRSA").run {
                    initSign(reopened.private); update(payload); sign()
                }
                check(java.security.Signature.getInstance("SHA256withRSA").run {
                    initVerify(account.public); update(payload); verify(signature)
                })
                results.putString("account_crypto", "ok (local only)")
            } finally { java.io.File(accountDirectory, "account.pk8").delete(); accountDirectory.delete() }
            val http = UrlCertificateHttp()
            results.putString("acme_dns", java.net.InetAddress.getAllByName("acme-v02.api.letsencrypt.org").joinToString { it.hostAddress.orEmpty() })
            // Public, fixed host only: diagnose emulator transport without exposing a credential-bearing URL.
            try {
                java.net.Socket().use { it.connect(java.net.InetSocketAddress("acme-v02.api.letsencrypt.org", 443), 10_000) }
                results.putString("acme_tcp", "ok")
            } catch (error: Exception) { results.putString("acme_tcp", error.javaClass.simpleName + ": " + error.message) }
            val control = java.net.URL(AcmeClient.PRODUCTION).openConnection() as javax.net.ssl.HttpsURLConnection
            try {
                control.connectTimeout = 10_000; control.readTimeout = 15_000
                results.putString("platform_https", control.responseCode.toString())
            } catch (error: Exception) {
                results.putString("platform_https", generateSequence(error as Throwable?) { it.cause }.take(5)
                    .joinToString(" / ") { it.javaClass.simpleName + ": " + it.message })
            } finally { control.disconnect() }
            val directory = http.request("GET", AcmeClient.PRODUCTION, emptyMap(), null)
            check(directory.code == 200)
            val value = Json.parseToJsonElement(directory.text()).jsonObject
            check(value.string("newOrder").startsWith("https://acme-v02.api.letsencrypt.org/"))
            results.putString("acme_directory", "ok")
            val duck = http.request("GET", "https://www.duckdns.org/spec.jsp", emptyMap(), null)
            check(duck.code == 200)
            results.putString("duckdns_https", "ok")
            val desec = http.request("GET", "https://desec.io/api/v1/domains/", emptyMap(), null)
            check(desec.code == 401 || desec.code == 403)
            results.putString("desec_https", "ok (unauthenticated read rejected)")
            TeslaChecksProbe.local(results)
            val query = TeslaChecksProbe.query("example.org")
            val dns = TeslaCheckDns(targetContext, "dashplay-probe-none").exchange(query)
            check(dns != null && TeslaCheckProtocol.validResponse(query, dns) && TeslaCheckProtocol.word(dns, 2) and 15 == 0)
            results.putString("system_dns_forward", "ok (read-only example.org)")
            results.putBoolean("success", true)
            finish(Activity.RESULT_OK, results)
        } catch (error: Exception) {
            results.putBoolean("success", false)
            results.putString("reason", if (error is CertificateFailure) error.reason else error.javaClass.simpleName)
            if (internetProbe || managerRecovery || packetBridgeProbe || usbBootProbe) {
                results.putString("probe_detail", error.message.orEmpty().take(200))
                android.util.Log.e("UsbInternetProbe", "emulator probe failed", error)
            }
            if (error is CertificateFailure) results.putString("cause_kind", error.causeKind)
            finish(Activity.RESULT_CANCELED, results)
        }
    }
}
