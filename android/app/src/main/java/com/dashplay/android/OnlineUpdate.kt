package com.dashplay.android

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.bouncycastle.jce.provider.BouncyCastleProvider
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.KeyFactory
import java.security.MessageDigest
import java.security.Provider
import java.security.Security
import java.security.Signature
import java.security.spec.X509EncodedKeySpec
import java.util.Base64
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/**
 * Online update check and APK download aligned with scripts/onecloud/update.py:
 * - Tries GitHub first, then the configured mirror prefixes (default [DEFAULT_MIRRORS] in order, "none" for GitHub only).
 * - Reads the "android" section of the signed dashplay-update.json (the app has its own version: 0.3.0-beta.1),
 *   or, when the latest release predates that section, the newest APK asset of the release list.
 * - Verifies SHA-256, package name, signer certificate and monotonic version before handing to Android's installer.
 * - Checks once daily in the background when updateCheck is enabled; never installs automatically.
 */
object OnlineUpdate {
    const val OWNER = "yizhixiaoheigou"
    const val REPO = "DashPlay"
    /**
     * Built-in GitHub mirrors, tried in this order after a direct attempt. A single hard-coded mirror is not enough:
     * ghfast.top stopped answering in testing (2026-11-01, 15 s and 40 s timeouts from a network that reaches GitHub
     * directly), while gh-proxy.com answered in about 2 s. Every file is still checked against the signed manifest, so
     * trying several costs nothing and a dead or malicious mirror cannot change what gets installed.
     */
    val DEFAULT_MIRRORS = listOf("https://gh-proxy.com/", "https://ghproxy.net/", "https://ghfast.top/")
    const val DEFAULT_MIRROR = "https://gh-proxy.com/"
    const val LATEST_BASE = "https://github.com/$OWNER/$REPO/releases/latest/download/"
    const val API_LATEST = "https://api.github.com/repos/$OWNER/$REPO/releases/latest"
    const val API_LIST = "https://api.github.com/repos/$OWNER/$REPO/releases?per_page=20"
    const val MANIFEST = "dashplay-update.json"
    const val SIGNATURE = "dashplay-update.json.sig"
    // Ed25519 public key from scripts/onecloud/update-key.pem.
    private const val PUBLIC_KEY_BASE64 = "MCowBQYDK2VwAyEAoz2sMFK3TuIF7ArmkhTbsNv6EVhkrlW/iFszQWLPJZU="
    private val json = Json { ignoreUnknownKeys = true }
    private val executor = Executors.newSingleThreadScheduledExecutor { r -> Thread(r, "dashplay-online-update").apply { isDaemon = true } }
    private val generation = AtomicLong()
    private var future: ScheduledFuture<*>? = null

    @Serializable
    data class State(
        val checkedAt: Long = 0,
        val current: String = "",
        val latest: String = "",
        val notes: String = "",
        val available: Boolean = false,
        val apkUrl: String = "",
        val apkSha256: String = "",
        val apkSize: Long = 0,
        val error: String = "",
        val downloadedFile: String = "",
        val downloadedVersion: String = "",
    )

    fun mirrors(raw: String): List<String> {
        val trimmed = raw.trim()
        if (trimmed.equals("none", ignoreCase = true)) return emptyList()
        val list = if (trimmed.isEmpty()) DEFAULT_MIRRORS
        else trimmed.split(Regex("\\s+")).filter { it.isNotEmpty() }
        return list.map { if (it.endsWith("/")) it else "$it/" }
    }

    fun candidateUrls(url: String, mirrorSetting: String): List<String> =
        listOf(url) + mirrors(mirrorSetting).map { prefix -> prefix + url }

    fun versionKey(version: String?): List<Int>? {
        val cleaned = version?.trim()?.removePrefix("v")?.substringBefore("-")?.trim() ?: return null
        if (!cleaned.matches(Regex("\\d+(\\.\\d+)*"))) return null
        return cleaned.split(".").mapNotNull { it.toIntOrNull() }
    }

    /** True for a version that carries a pre-release suffix ("0.3.0-beta.1"), which sorts before its release. */
    fun isPreRelease(version: String?): Boolean =
        version?.trim()?.removePrefix("v")?.contains("-") == true

    /** True when the version is one the box and the app can both be installed from (numeric, optional -suffix). */
    fun validVersion(version: String?): Boolean = versionKey(version) != null

    fun newer(latest: String?, current: String?): Boolean {
        val l = versionKey(latest) ?: return false
        val c = versionKey(current) ?: return true
        for (i in 0 until maxOf(l.size, c.size)) {
            val lv = l.getOrElse(i) { 0 }
            val cv = c.getOrElse(i) { 0 }
            if (lv != cv) return lv > cv
        }
        // Same numbers: 0.3.0 is the release of 0.3.0-beta.1, so the beta is not newer than the release and
        // the release is newer than the beta. Two pre-releases: dev < alpha < beta < rc, then the number as a
        // number ("beta.10" > "beta.9"), the order scripts/android_version.py gives their versionCodes.
        val lPre = isPreRelease(latest)
        val cPre = isPreRelease(current)
        if (lPre != cPre) return cPre
        if (!lPre) return false
        val l2 = preRelease(latest); val c2 = preRelease(current)
        if (l2 == null || c2 == null) return suffix(latest).compareTo(suffix(current)) > 0
        return if (l2.first != c2.first) l2.first > c2.first else l2.second > c2.second
    }

    private val STAGES = mapOf("dev" to 1, "alpha" to 2, "beta" to 3, "rc" to 4)

    private fun preRelease(version: String?): Pair<Int, Int>? {
        val match = Regex("([a-z]+)\\.(\\d+)").matchEntire(suffix(version)) ?: return null
        val stage = STAGES[match.groupValues[1]] ?: return null
        return stage to (match.groupValues[2].toIntOrNull() ?: return null)
    }

    private fun suffix(version: String?): String =
        version?.trim()?.removePrefix("v")?.substringAfter("-", "").orEmpty().lowercase()

    /**
     * A version looks like an Android one (0.3.0-beta.1) rather than a box one (2026.10.31.3). The two channels
     * must never be compared: the box version started at 2026 and would always look newer than any app version.
     */
    fun looksLikeBoxVersion(version: String?): Boolean {
        val key = versionKey(version) ?: return false
        return key.firstOrNull() == 2026
    }

    /**
     * Ed25519 check of the signed manifest. Android already ships a provider called "BC"; it is an old BouncyCastle
     * without Ed25519, so "is a provider called BC there" is the wrong test — the bundled provider is registered under
     * its own name and used directly, otherwise the box's signature would never verify on a phone with Android's BC.
     */
    fun verifyEd25519(data: ByteArray, sig: ByteArray, publicKeyBase64: String = PUBLIC_KEY_BASE64): Boolean = runCatching {
        ensureBundledProvider()
        val spec = X509EncodedKeySpec(Base64.getDecoder().decode(publicKeyBase64))
        val kf = runCatching { KeyFactory.getInstance("Ed25519", BUNDLED) }.getOrElse { KeyFactory.getInstance("Ed25519") }
        val pub = kf.generatePublic(spec)
        val verifier = runCatching { Signature.getInstance("Ed25519", BUNDLED) }.getOrElse { Signature.getInstance("Ed25519") }
        verifier.initVerify(pub)
        verifier.update(data)
        verifier.verify(sig)
    }.getOrDefault(false)

    private const val BUNDLED = "DashPlayBC"

    /**
     * BouncyCastle's provider declares the name "BC", which Android has already taken with an old copy that has no
     * Ed25519. Wrapping it lets us look the new algorithms up by a name this app owns.
     */
    private class BundledProvider : Provider(BUNDLED, 1.0, "DashPlay bundled BouncyCastle") {
        private val delegate = BouncyCastleProvider()
        override fun getService(type: String?, algorithm: String?): Provider.Service? =
            delegate.getService(type, algorithm)
    }

    @Synchronized
    private fun ensureBundledProvider() {
        if (Security.getProvider(BUNDLED) == null) Security.addProvider(BundledProvider())
    }

    private fun stateFile(context: Context) = File(context.applicationContext.filesDir, "state/update-check.json")

    @Synchronized
    fun readState(context: Context): State {
        val current = currentVersion(context)
        val file = stateFile(context)
        if (!file.isFile) return State(current = current)
        return runCatching {
            val loaded = json.decodeFromString<State>(file.readText())
            loaded.copy(
                current = current,
                available = loaded.latest.isNotEmpty() && newer(loaded.latest, current),
            )
        }.getOrDefault(State(current = current))
    }

    @Synchronized
    private fun saveState(context: Context, state: State) {
        val file = stateFile(context)
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(json.encodeToString(state) + "\n")
        tmp.renameTo(file)
    }

    fun currentVersion(context: Context): String =
        runCatching { context.packageManager.getPackageInfo(context.packageName, 0).versionName.orEmpty() }.getOrDefault("")

    private fun fetchBytes(url: String, mirrorSetting: String, limit: Int): ByteArray {
        val errors = mutableListOf<String>()
        for (candidate in candidateUrls(url, mirrorSetting)) {
            var conn: HttpURLConnection? = null
            try {
                conn = (URL(candidate).openConnection() as HttpURLConnection).apply {
                    connectTimeout = 15_000
                    readTimeout = 20_000
                    instanceFollowRedirects = true
                    setRequestProperty("User-Agent", "DashPlay-Android-Update")
                }
                val code = conn.responseCode
                if (code != 200) throw IllegalStateException("HTTP $code")
                val out = ByteArrayOutputStream()
                conn.inputStream.use { input ->
                    val buf = ByteArray(16 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        if (out.size() + n > limit) throw IllegalArgumentException("响应内容超过大小限制")
                        out.write(buf, 0, n)
                    }
                }
                return out.toByteArray()
            } catch (e: Exception) {
                val host = runCatching { URL(candidate).host }.getOrDefault(candidate)
                errors += "$host: ${e.message ?: e.javaClass.simpleName}"
            } finally {
                conn?.disconnect()
            }
        }
        throw IllegalStateException("下载失败（${errors.joinToString("；").takeLast(240)}）。网络恢复后再试，或在下方填写可用的镜像加速地址。")
    }

    data class ReleaseInfo(
        val version: String,
        val notes: String,
        val apkUrl: String,
        val apkSha256: String = "",
        val apkSize: Long = 0,
    )

    /**
     * The signed release manifest carries the box version (2026.10.31.3) and, when the release ships an APK, an
     * "android" section with the app's own version (0.3.0-beta.1). Only the Android section may be compared with the
     * installed app: the box version would always win the comparison and point at a file that does not exist.
     */
    internal fun parseManifestOrApi(
        manifestBytes: ByteArray?,
        sigBytes: ByteArray?,
        apiBytes: ByteArray?,
        publicKeyBase64: String = PUBLIC_KEY_BASE64,
    ): ReleaseInfo {
        if (manifestBytes != null && sigBytes != null) {
            require(verifyEd25519(manifestBytes, sigBytes, publicKeyBase64)) { "更新清单的签名校验未通过，已忽略（可能是镜像或网络被篡改）。" }
            val obj = json.parseToJsonElement(String(manifestBytes, Charsets.UTF_8)).jsonObject
            val boxVersion = obj.text("version")
            require(versionKey(boxVersion) != null) { "更新清单版本格式无效。" }
            val androidObj = obj["android"]?.takeIf { it is JsonObject }?.jsonObject
                ?: throw IllegalArgumentException("这一版没有安卓安装包（清单里没有 android 段），请到发布页手动下载。")
            val version = androidObj.text("version")
            require(validVersion(version)) { "安卓版本格式无效。" }
            val apk = androidObj["apk"]?.takeIf { it is JsonObject }?.jsonObject
            val file = apk?.text("file").orEmpty()
            require(file.endsWith(".apk") && !file.contains('/') && !file.contains("..")) { "安卓安装包名称无效。" }
            return ReleaseInfo(
                version = version,
                notes = androidObj.text("notes").takeIf { it.isNotBlank() } ?: obj.text("notes"),
                apkUrl = baseUrl(androidObj.text("base"), boxVersion) + file,
                apkSha256 = apk?.text("sha256").orEmpty(),
                apkSize = apk?.long("size") ?: 0L,
            )
        }
        if (apiBytes != null) return parseReleaseList(apiBytes)
        throw IllegalStateException("未获取到版本信息。")
    }

    private fun JsonObject.text(name: String): String =
        (this[name] as? JsonPrimitive)?.takeIf { it.isString }?.content.orEmpty()

    private fun JsonObject.long(name: String): Long =
        (this[name] as? JsonPrimitive)?.content?.toLongOrNull() ?: 0L

    private fun JsonObject.flag(name: String): Boolean =
        (this[name] as? JsonPrimitive)?.content?.toBooleanStrictOrNull() ?: false

    private fun baseUrl(declared: String, boxVersion: String): String =
        declared.takeIf { it.isNotBlank() }?.let { if (it.endsWith("/")) it else "$it/" }
            ?: "https://github.com/$OWNER/$REPO/releases/download/v$boxVersion/"

    /**
     * An APK published by us. The DashPlay release page carries the app APK (DashPlay-android-<version>.apk); the box
     * packages are named dashplay-bundle / dashplay-jre / dashplay-luci-* and must never be offered to the app.
     */
    private fun apkAsset(name: String): Boolean {
        if (!name.endsWith(".apk", ignoreCase = true)) return false
        val lower = name.lowercase()
        if (lower.startsWith("dashplay-android-")) return true
        return !Regex("""^dashplay-(bundle|jre|luci)""").containsMatchIn(lower)
    }

    /** Version of the APK, from its file name (DashPlay-android-0.3.0-beta.1.apk) or else from the file itself. */
    internal fun versionFromApkName(name: String): String? {
        val match = Regex("""(?:^|[-_])v?(\d+\.\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?)\.apk$""", RegexOption.IGNORE_CASE)
            .find(name) ?: return null
        return match.groupValues[1].takeIf { validVersion(it) }
    }

    /** GitHub's /releases/latest or /releases list: the newest release that actually carries an APK asset. */
    internal fun parseReleaseList(apiBytes: ByteArray): ReleaseInfo {
        val root = String(apiBytes, Charsets.UTF_8).trim()
        val releases = json.parseToJsonElement(root).let { if (it is JsonArray) it else JsonArray(listOf(it)) }
        var best: ReleaseInfo? = null
        for (release in releases) {
            if (release !is JsonObject || release.flag("draft")) continue
            val tag = release.text("tag_name").removePrefix("v")
            val assets = (release["assets"] as? JsonArray) ?: JsonArray(emptyList())
            for (asset in assets) {
                if (asset !is JsonObject) continue
                val name = asset.text("name")
                if (!apkAsset(name)) continue
                val version = versionFromApkName(name) ?: tag.takeIf { validVersion(it) } ?: continue
                val digest = asset.text("digest")
                val candidate = ReleaseInfo(
                    version = version,
                    notes = release.text("body"),
                    apkUrl = asset.text("browser_download_url"),
                    apkSha256 = if (digest.startsWith("sha256:")) digest.removePrefix("sha256:").lowercase() else "",
                    apkSize = asset.long("size"),
                )
                if (candidate.apkUrl.isNotEmpty() && (best == null || newer(candidate.version, best.version))) best = candidate
            }
        }
        return best ?: throw IllegalStateException("发布页里没有安卓安装包。")
    }

    fun check(context: Context): State {
        val app = context.applicationContext
        val settings = Settings(app)
        val mirror = settings.updateMirror
        val current = currentVersion(app)
        val previous = readState(app)
        return try {
            val info = try {
                val m = fetchBytes(LATEST_BASE + MANIFEST, mirror, 1024 * 1024)
                val s = fetchBytes(LATEST_BASE + SIGNATURE, mirror, 4096)
                parseManifestOrApi(m, s, null)
            } catch (manifestError: Exception) {
                // A tampered manifest is a hard stop: never fall back to an unauthenticated source for it.
                if (manifestError is IllegalArgumentException && manifestError.message?.contains("签名") == true) {
                    throw manifestError
                }
                // The latest release may predate the android section: ask the release list for the newest APK.
                val api = fetchBytes(API_LIST, mirror, 2 * 1024 * 1024)
                parseManifestOrApi(null, null, api)
            }
            // Only an Android version is comparable with the installed app; a box version here is a bug, not an update.
            require(!looksLikeBoxVersion(info.version)) { "更新清单里的安卓版本仍是盒子版本号，请到发布页手动下载安装包。" }
            val next = State(
                checkedAt = System.currentTimeMillis(),
                current = current,
                latest = info.version,
                notes = info.notes.take(1000),
                available = newer(info.version, current),
                apkUrl = info.apkUrl,
                apkSha256 = info.apkSha256,
                apkSize = info.apkSize,
                error = "",
                downloadedFile = previous.downloadedFile,
                downloadedVersion = previous.downloadedVersion,
            )
            saveState(app, next)
            next
        } catch (e: Exception) {
            val failed = previous.copy(
                checkedAt = System.currentTimeMillis(),
                current = current,
                error = e.message ?: "检查更新失败，请检查网络或更换镜像加速地址。",
            )
            saveState(app, failed)
            failed
        }
    }

    internal fun download(context: Context): ApkUpdates.Candidate {
        val app = context.applicationContext
        val settings = Settings(app)
        var state = readState(app)
        if (state.latest.isEmpty() || state.apkUrl.isEmpty()) {
            state = check(app)
            if (state.error.isNotEmpty() && state.latest.isEmpty()) throw IllegalArgumentException(state.error)
        }
        require(state.available) { "当前已是最新版本（${state.current}）。" }
        val dir = File(app.cacheDir, "apk-updates").apply { mkdirs() }
        val target = File.createTempFile("update-", ".apk", dir)
        val verified = downloadFile(state, settings.updateMirror, target) { ApkUpdates.inspect(app, it) }
        saveState(app, state.copy(downloadedFile = target.name, downloadedVersion = verified.version, error = ""))
        return verified
    }

    /** Download/verification boundary, shared by the app and loopback HTTP regression tests. */
    internal fun downloadFile(state: State, mirrorSetting: String, target: File,
        inspect: (File) -> ApkUpdates.Candidate): ApkUpdates.Candidate {
        val errors = mutableListOf<String>()
        for (candidate in candidateUrls(state.apkUrl, mirrorSetting)) {
            var conn: HttpURLConnection? = null
            try {
                conn = (URL(candidate).openConnection() as HttpURLConnection).apply {
                    connectTimeout = 15_000
                    readTimeout = 60_000
                    instanceFollowRedirects = true
                    setRequestProperty("User-Agent", "DashPlay-Android-Update")
                }
                val code = conn.responseCode
                if (code != 200) throw IllegalStateException("HTTP $code")
                val digest = MessageDigest.getInstance("SHA-256")
                var total = 0L
                val max = if (state.apkSize > 0) state.apkSize else ApkUpdates.MAX_BYTES
                conn.inputStream.use { input ->
                    FileOutputStream(target).use { output ->
                        val buf = ByteArray(64 * 1024)
                        while (true) {
                            val n = input.read(buf)
                            if (n < 0) break
                            total += n
                            require(total <= max && total <= ApkUpdates.MAX_BYTES) { "下载的文件超过预期大小，已停止。" }
                            digest.update(buf, 0, n)
                            output.write(buf, 0, n)
                        }
                        output.fd.sync()
                    }
                }
                if (state.apkSize > 0 && total != state.apkSize) {
                    throw IllegalArgumentException("下载的文件大小与发布清单不符。")
                }
                val hex = digest.digest().joinToString("") { "%02x".format(it.toInt() and 255) }
                if (state.apkSha256.isNotEmpty() && !hex.equals(state.apkSha256, ignoreCase = true)) {
                    throw IllegalArgumentException("下载的文件 SHA-256 校验与签名清单不符，已拒绝安装。")
                }
                return inspect(target)
            } catch (e: Exception) {
                target.delete()
                if (e is IllegalArgumentException && (e.message?.contains("签名") == true || e.message?.contains("SHA-256") == true)) {
                    throw e
                }
                val host = runCatching { URL(candidate).host }.getOrDefault(candidate)
                errors += "$host: ${e.message ?: e.javaClass.simpleName}"
            } finally {
                conn?.disconnect()
            }
        }
        throw IllegalArgumentException("下载更新包失败（${errors.joinToString("；").takeLast(220)}）。可在下方更换镜像加速地址后重试。")
    }

    @Synchronized
    fun attach(context: Context): Closeable {
        val app = context.applicationContext
        val id = generation.incrementAndGet()
        future?.cancel(false)
        future = executor.scheduleWithFixedDelay({
            if (generation.get() != id) return@scheduleWithFixedDelay
            val settings = Settings(app)
            if (!settings.updateCheck) return@scheduleWithFixedDelay
            val state = readState(app)
            if (System.currentTimeMillis() - state.checkedAt >= 24L * 3600 * 1000) {
                runCatching { check(app) }
            }
        }, 2, 3600, TimeUnit.SECONDS)
        return Closeable {
            synchronized(this) {
                if (generation.compareAndSet(id, id + 1)) {
                    future?.cancel(false)
                    future = null
                }
            }
        }
    }

    fun describe(context: Context): JSONObject {
        val settings = Settings(context)
        val s = readState(context)
        return JSONObject()
            .put("checkedAt", s.checkedAt)
            .put("current", s.current)
            .put("latest", s.latest)
            .put("notes", s.notes)
            .put("available", s.available)
            .put("error", s.error)
            .put("updateCheck", settings.updateCheck)
            .put("updateMirror", settings.updateMirror)
            .put("defaultMirror", DEFAULT_MIRROR)
    }
}
