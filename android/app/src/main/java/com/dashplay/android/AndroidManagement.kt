package com.dashplay.android

import android.content.Context
import com.playport.server.WebHub
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.withCharset
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationCall
import io.ktor.server.request.receiveChannel
import io.ktor.server.response.respondText
import io.ktor.server.routing.get
import io.ktor.server.routing.post
import io.ktor.server.routing.routing
import io.ktor.utils.io.readAvailable
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.OutputStream
import java.security.MessageDigest

/** LAN management, including before identity/setup. Viewer authorization never grants management access. */
class AndroidManagement(private val context: Context, private val restart: () -> Unit) {
    private val settings = Settings(context)
    private val json = Json { ignoreUnknownKeys = false }

    fun install(app: Application) {
        app.routing {
            get("/manage") { page(call, "index.html", ContentType.Text.Html) }
            get("/manage/app.js") { page(call, "app.js", ContentType.Application.JavaScript) }
            get("/manage/style.css") { page(call, "style.css", ContentType.Text.CSS) }
            get("/api/android/settings") {
                if (authorized(call)) call.respondText(state().toString(), ContentType.Application.Json)
            }
            post("/api/android/settings") {
                if (!authorized(call)) return@post
                try {
                    val bytes = ByteArrayOutputStream()
                    receiveBounded(call, bytes, 8192)
                    val update = json.decodeFromString<ManagementSettings>(bytes.toString("UTF-8"))
                    require(update.setupDone != true || IdentityImport.present(context)) { "请先导入 CarPlay 身份。" }
                    withContext(Dispatchers.IO) { settings.apply(update) }
                    call.respondText(state().put("saved", true).toString(), ContentType.Application.Json)
                    restart()
                } catch (_: UploadTooLarge) {
                    error(call, "设置内容过大。", HttpStatusCode.PayloadTooLarge)
                } catch (failure: IllegalArgumentException) {
                    // Serialization errors can contain supplied secrets; return our validation messages only.
                    error(call, if (failure is kotlinx.serialization.SerializationException) "设置格式不正确。" else
                        failure.message ?: "设置无效。", HttpStatusCode.BadRequest)
                } catch (_: IllegalStateException) { error(call, "设置未保存，请重试。", HttpStatusCode.Conflict) }
            }
            post("/api/android/identity") {
                if (!authorized(call)) return@post
                val upload = File.createTempFile("identity-web-", ".zip", context.cacheDir)
                try {
                    withContext(Dispatchers.IO) {
                        upload.outputStream().use { receiveBounded(call, it, IdentityImport.MAX_ARCHIVE_BYTES) }
                    }
                    val failure = withContext(Dispatchers.IO) { IdentityImport.fromArchive(context, upload) }
                    if (failure != null) error(call, failure, HttpStatusCode.BadRequest)
                    else {
                        call.respondText("{\"imported\":true}", ContentType.Application.Json)
                        restart()
                    }
                } catch (_: UploadTooLarge) {
                    error(call, "身份文件应在 32 MB 以内。", HttpStatusCode.PayloadTooLarge)
                } finally { upload.delete() }
            }
            get("/api/android/tls") {
                if (authorized(call)) call.respondText(withContext(Dispatchers.IO) { tlsState().toString() }, ContentType.Application.Json)
            }
            // The car can install this instead of clicking through a warning. Public certificates only: the
            // private key never leaves its directory, and the response carries no credential.
            get("/api/android/ca.crt") {
                if (!authorized(call)) return@get
                val chain = withContext(Dispatchers.IO) { OwnTls.store(context).selected()?.chain ?: localChain() }
                if (chain.isNullOrEmpty()) error(call, "尚未启用证书。", HttpStatusCode.NotFound)
                else {
                    call.response.headers.append(HttpHeaders.ContentDisposition, "attachment; filename=\"dashplay-ca.crt\"")
                    call.respondText(chain.joinToString("") { pem(it) }, ContentType.Text.Plain)
                }
            }
            get("/api/android/tls/automatic") {
                if (authorized(call)) call.respondText(withContext(Dispatchers.IO) { automaticTlsState().toString() }, ContentType.Application.Json)
            }
            post("/api/android/tls/automatic") {
                if (!authorized(call)) return@post
                try {
                    val bytes = ByteArrayOutputStream(); receiveBounded(call, bytes, 8192)
                    val request = json.decodeFromString<AutomaticTlsUpdate>(bytes.toString("UTF-8"))
                    withContext(Dispatchers.IO) { AutomaticTls.store(context).configure(request) }
                    call.respondText(automaticTlsState().toString(), ContentType.Application.Json)
                } catch (_: UploadTooLarge) { error(call, "设置内容过大。", HttpStatusCode.PayloadTooLarge) }
                catch (failure: IllegalArgumentException) {
                    error(call, if (failure is kotlinx.serialization.SerializationException) "自动维护设置格式不正确。" else
                        failure.message ?: "自动维护设置无效。", HttpStatusCode.BadRequest)
                } catch (_: Exception) { error(call, "自动维护设置未保存，原文件保留。", HttpStatusCode.Conflict) }
            }
            post("/api/android/tls/automatic/retry") {
                if (!authorized(call)) return@post
                try {
                    withContext(Dispatchers.IO) { AutomaticTls.store(context).retry() }
                    call.respondText(automaticTlsState().toString(), ContentType.Application.Json)
                } catch (_: Exception) { error(call, "无法读取自动维护设置。", HttpStatusCode.Conflict) }
            }
            post("/api/android/tls/import") {
                if (!authorized(call)) return@post
                try {
                    val bytes = ByteArrayOutputStream()
                    receiveBounded(call, bytes, 131_072)
                    val request = json.decodeFromString<TlsImport>(bytes.toString("UTF-8"))
                    withContext(Dispatchers.IO) { AutomaticTls.import(context, request) }
                    call.respondText(tlsState().toString(), ContentType.Application.Json)
                    restart()
                } catch (_: UploadTooLarge) { error(call, "证书文件过大。", HttpStatusCode.PayloadTooLarge) }
                catch (failure: IllegalArgumentException) {
                    error(call, if (failure is kotlinx.serialization.SerializationException) "证书导入格式不正确。" else
                        failure.message ?: "证书无效。", HttpStatusCode.BadRequest)
                } catch (_: Exception) { error(call, "证书未保存，原有证书保留。", HttpStatusCode.Conflict) }
            }
            post("/api/android/tls/enable") {
                if (!authorized(call)) return@post
                try {
                    val bytes = ByteArrayOutputStream()
                    receiveBounded(call, bytes, 256)
                    val request = json.decodeFromString<TlsEnable>(bytes.toString("UTF-8"))
                    withContext(Dispatchers.IO) { AutomaticTls.enable(context, request.enabled) }
                    call.respondText(tlsState().toString(), ContentType.Application.Json)
                    restart()
                } catch (_: UploadTooLarge) { error(call, "请求过大。", HttpStatusCode.PayloadTooLarge) }
                catch (_: Exception) { error(call, "证书切换失败，请确认已导入有效证书。", HttpStatusCode.BadRequest) }
            }
            get("/api/android/phones") {
                if (authorized(call)) call.respondText(withContext(Dispatchers.IO) {
                    AndroidPhones.get(context).describe().toString()
                }, ContentType.Application.Json)
            }
            post("/api/android/phones/action") {
                if (!authorized(call)) return@post
                try {
                    val bytes = ByteArrayOutputStream()
                    receiveBounded(call, bytes, 1024)
                    val request = json.decodeFromString<PhoneAction>(bytes.toString("UTF-8"))
                    val message = withContext(Dispatchers.IO) { PhoneActions.apply(context, request) }
                    if (message == null) call.respondText("{\"accepted\":true}", ContentType.Application.Json)
                    else error(call, message, HttpStatusCode.Conflict)
                } catch (_: UploadTooLarge) { error(call, "请求内容过大。", HttpStatusCode.PayloadTooLarge) }
                catch (_: IllegalArgumentException) { error(call, "手机操作格式无效。", HttpStatusCode.BadRequest) }
                catch (_: IllegalStateException) { error(call, "手机设置保存失败，请重试。", HttpStatusCode.Conflict) }
            }
            post("/api/android/restart") {
                if (!authorized(call)) return@post
                call.respondText("{\"restarting\":true}", ContentType.Application.Json)
                restart()
            }
            post("/api/android/tesla/checks") {
                if (!authorized(call)) return@post
                try {
                    val bytes = ByteArrayOutputStream()
                    receiveBounded(call, bytes, 128)
                    val enabled = JSONObject(bytes.toString("UTF-8")).get("enabled")
                    require(enabled is Boolean)
                    withContext(Dispatchers.IO) { settings.teslaOfflineChecks = enabled }
                    call.respondText("{\"accepted\":true}", ContentType.Application.Json)
                    restart()
                } catch (_: UploadTooLarge) { error(call, "请求内容过大。", HttpStatusCode.PayloadTooLarge) }
                catch (_: Exception) { error(call, "请先在安卓设备上启用 Root 访问地址，或检查设置后重试。", HttpStatusCode.BadRequest) }
            }
            post("/api/android/tesla/disable") {
                if (!authorized(call)) return@post
                withContext(Dispatchers.IO) { settings.teslaRootAccess = false }
                call.respondText("{\"accepted\":true}", ContentType.Application.Json)
                restart()
            }
            get("/api/android/update") {
                if (authorized(call)) call.respondText(withContext(Dispatchers.IO) { OnlineUpdate.describe(context).toString() }, ContentType.Application.Json)
            }
            post("/api/android/update/check") {
                if (!authorized(call)) return@post
                val result = withContext(Dispatchers.IO) {
                    OnlineUpdate.check(context)
                    OnlineUpdate.describe(context)
                }
                call.respondText(result.toString(), ContentType.Application.Json)
            }
            get("/api/android/diagnostics") {
                if (!authorized(call)) return@get
                // Deliberately use structured observations, not raw protocol logs that may contain credentials.
                call.response.headers.append("Content-Disposition", "attachment; filename=DashPlay-diagnostics.json")
                call.respondText(state().toString(2), ContentType.Application.Json)
            }
            get("/api/android/logs") {
                if (!authorized(call)) return@get
                // Redacted when written (DriveLog): no tokens, passwords or private keys.
                val text = withContext(Dispatchers.IO) { DriveLog.export(java.io.File(context.filesDir, "logs")) }
                call.response.headers.append("Content-Disposition", "attachment; filename=DashPlay-log.txt")
                call.respondText(text.ifEmpty { "（还没有日志）\n" }, ContentType.Text.Plain.withCharset(Charsets.UTF_8))
            }
        }
    }

    private suspend fun page(call: ApplicationCall, name: String, type: ContentType) {
        call.response.headers.append("Cache-Control", "no-store")
        call.response.headers.append("Referrer-Policy", "no-referrer")
        call.response.headers.append("X-Content-Type-Options", "nosniff")
        call.response.headers.append("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        call.respondText(context.assets.open("manage/$name").bufferedReader().use { it.readText() }, type)
    }

    private suspend fun authorized(call: ApplicationCall): Boolean {
        call.response.headers.append("Cache-Control", "no-store")
        val own = WebHub.ownPage(call.request.headers["Origin"],
            "${call.request.local.serverHost}:${call.request.local.serverPort}", call.request.headers["Sec-Fetch-Site"])
        val token = call.request.headers["X-DashPlay-Admin"].orEmpty()
        val valid = token.length == 32 && MessageDigest.isEqual(token.toByteArray(), settings.adminToken.toByteArray())
        if (!own || !valid) {
            error(call, "请使用设备设置中提供的管理链接。", HttpStatusCode.Forbidden)
            return false
        }
        return true
    }

    private fun state(): JSONObject {
        val snapshot = DashPlayService.snapshot()
        val hotspot = settings.hotspot()
        val version = runCatching { context.packageManager.getPackageInfo(context.packageName, 0).versionName }.getOrDefault("")
        return JSONObject().put("version", version).put("status", snapshot.status.name)
            .put("identity", IdentityImport.present(context)).put("sessionActive", snapshot.sessionActive)
            .put("localHotspot", JSONObject().put("state", DashPlayService.managedHotspot?.phase?.name ?: "OFF")
                .put("message", DashPlayService.managedHotspot?.message.orEmpty())
                .put("ssid", DashPlayService.managedHotspot?.connection?.ssid.orEmpty())
                .put("channel", DashPlayService.managedHotspot?.connection?.channel ?: 0))
            .put("usb", JSONObject().put("enabled", settings.usbEnabled).put("activeMode", settings.usesUsb)
                .put("state", AndroidWiredController.status).put("message", AndroidWiredController.detail)
                .put("internetAllowed", settings.usbInternetAllowed).put("internetReady", settings.usesUsb && settings.usbInternet && UsbVpnService.internetReady)
                .put("internetMessage", if (!settings.usbInternet) "USB 借网未启用" else if (!settings.usesUsb) "等待启用数据线连接" else UsbVpnService.internetMessage))
            .put("teslaAccess", TeslaRootAccess.describe().put("enabled", settings.teslaRootAccess).put("checksEnabled", settings.teslaOfflineChecks))
            .put("usbShare", UsbShareAccess.describe().put("enabled", settings.usbHotspotShare).put("allowed", settings.usbHotspotShareAllowed))
            .put("ownTls", tlsState())
            .put("update", OnlineUpdate.describe(context))
            .put("viewers", snapshot.viewers).put("capabilities", DeviceCapabilities.describe(context))
            .put("settings", JSONObject().put("deviceName", settings.deviceName)
                .put("autoHotspot", settings.autoHotspot).put("car", settings.car.name).put("network", settings.network.name)
                .put("usbInternet", settings.usbInternet)
                .put("usbHotspotShare", settings.usbHotspotShare)
                .put("iphoneLink", settings.iphoneLink.name).put("ssid", hotspot?.ssid.orEmpty())
                .put("uplinkSsid", settings.uplink()?.ssid.orEmpty())
                .put("hasUplinkPassword", !settings.uplink()?.passphrase.isNullOrEmpty())
                .put("hasPassword", !hotspot?.passphrase.isNullOrEmpty()).put("wifiSecurity", settings.wifiSecurity.name)
                .put("wifiChannel", settings.wifiChannel).put("audioOutsideCarPlay", settings.audioOutsideCarPlay)
                .put("idleReleaseSeconds", settings.idleReleaseSeconds).put("startAtBoot", settings.startAtBoot)
                .put("setupDone", settings.setupDone).put("ownNameOnHotspot", settings.ownNameOnHotspot)
                .put("hotspotOwnNameRoot", settings.hotspotOwnNameRoot)
                .put("updateCheck", settings.updateCheck).put("updateMirror", settings.updateMirror))
    }

    private fun localChain(): List<java.security.cert.X509Certificate>? = runCatching {
        val stateDir = File(context.filesDir, "state").toPath()
        val tlsState = if (settings.teslaRootAccess && settings.car == Car.TESLA) stateDir.resolve("tls-tesla") else stateDir
        val file = tlsState.resolve("tls-keystore.p12").toFile()
        if (!file.isFile) return null
        val ks = java.security.KeyStore.getInstance("PKCS12")
        file.inputStream().use { ks.load(it, "playport".toCharArray()) }
        ks.getCertificateChain("playport")?.filterIsInstance<java.security.cert.X509Certificate>()?.takeIf { it.isNotEmpty() }
    }.getOrNull()

    private fun tlsState(): JSONObject {
        val snapshot = DashPlayService.snapshot()
        return JSONObject(json.encodeToString(OwnTls.store(context).state()))
            .put("automatic", automaticTlsState())
            .put("servingDomain", snapshot.ownDomain.orEmpty())
            .put("directAddress", snapshot.directAddress.orEmpty())
            .put("dnsTarget", if (TeslaRootAccess.active) TeslaRootPlan.ADDRESS else snapshot.network?.address?.hostAddress.orEmpty())
    }

    private fun automaticTlsState(): JSONObject = runCatching {
        JSONObject(json.encodeToString(AutomaticTls.store(context).state()))
    }.getOrElse { JSONObject().put("phase", "storage_error").put("message", "自动维护设置无法读取，原文件保留。") }

    private suspend fun receiveBounded(call: ApplicationCall, output: OutputStream, maximum: Long) {
        val length = call.request.headers["Content-Length"]?.toLongOrNull()
        if (length != null && length > maximum) throw UploadTooLarge()
        withTimeout(60_000) {
            val channel = call.receiveChannel()
            val buffer = ByteArray(8192)
            var total = 0L
            while (true) {
                val count = channel.readAvailable(buffer, 0, buffer.size)
                if (count < 0) break
                total += count
                if (total > maximum) throw UploadTooLarge()
                output.write(buffer, 0, count)
            }
        }
    }

    private suspend fun error(call: ApplicationCall, message: String, status: HttpStatusCode) {
        call.respondText(JSONObject().put("error", message).toString(), ContentType.Application.Json, status)
    }
    private class UploadTooLarge : Exception()
}

/** PEM of a public certificate: the same text a car imports; never includes a private key. */
private fun pem(certificate: java.security.cert.X509Certificate): String {
    val body = java.util.Base64.getMimeEncoder(64, "\n".toByteArray()).encodeToString(certificate.encoded)
    return "-----BEGIN CERTIFICATE-----\n" + body + "\n-----END CERTIFICATE-----\n"
}
