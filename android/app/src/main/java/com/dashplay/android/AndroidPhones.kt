package com.dashplay.android

import android.bluetooth.BluetoothAdapter
import android.content.Context
import kotlinx.serialization.encodeToString
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/** Android's bonded devices plus app-owned connection policy; no hidden unpair API or ARP probing. */
class AndroidPhones private constructor(context: Context) {
    private val app = context.applicationContext
    private val settings = Settings(app)
    private val prefs = app.getSharedPreferences("dashplay", Context.MODE_PRIVATE)
    private val json = Json { ignoreUnknownKeys = true }
    private val loaded = runCatching {
        prefs.getString("phone_policy", null)?.let { json.decodeFromString<PhoneState>(it) }
            ?: PhoneState(preferred = settings.iphoneAddress)
    }
    private val initial = loaded.getOrDefault(PhoneState())
    private val policy = PhonePolicy(initial) { state ->
        check(prefs.edit().putString("phone_policy", json.encodeToString(state)).commit()) { "手机设置保存失败，请重试。" }
    }
    private val iap2 = UUID.fromString("00000000-deca-fade-deca-deafdecacafe")

    private fun bonded(): Map<String, String> = runCatching {
        BluetoothAdapter.getDefaultAdapter()?.bondedDevices.orEmpty().filter { device ->
            device.address == settings.iphoneAddress || policy.state.phones.any { it.address == device.address } ||
                device.name?.contains("iPhone", true) == true || device.uuids.orEmpty().any { it.uuid == iap2 }
        }.associate { it.address.uppercase() to (it.name ?: "iPhone").take(64) }
    }.getOrDefault(emptyMap())

    private fun current(): Set<String> {
        check(loaded.isSuccess) { "手机记录无法读取，已停止自动连接。" }
        val bonded = bonded()
        bonded.forEach { (address, name) -> policy.discover(address, name) }
        return bonded.keys
    }

    @Synchronized fun add(address: String, name: String) {
        check(loaded.isSuccess) { "手机记录无法读取，未覆盖原记录。" }
        require(address.matches(MAC))
        policy.add(address.uppercase(), name)
    }
    @Synchronized fun candidate(): String? = runCatching { policy.select(current(), android.os.SystemClock.elapsedRealtime()) }.getOrNull()
    @Synchronized fun canInvite(address: String): Boolean = runCatching { policy.allowed(address.uppercase(), current()) }.getOrDefault(false)
    @Synchronized fun pause(address: String, remove: Boolean = false): Boolean {
        check(loaded.isSuccess) { "手机记录无法读取，未覆盖原记录。" }
        return policy.pause(address.uppercase(), remove)
    }
    @Synchronized fun authorize(controller: String?, selected: String?, allowNew: Boolean): Boolean =
        policy.authorize(controller, selected, allowNew, current())

    @Synchronized fun describe(): JSONObject {
        val bonded = current()
        val receiver = DashPlayService.activeReceiver()
        val wiredLive = receiver?.wiredSessionActive() == true
        val rows = JSONArray()
        policy.state.phones.filter { !it.removed }.sortedBy { it.name }.forEach {
            rows.put(JSONObject().put("address", it.address).put("name", it.name).put("paused", it.paused)
                .put("bonded", it.address in bonded).put("active", !wiredLive && it.address == policy.active)
                .put("last", it.address == policy.state.last))
        }
        return JSONObject().put("phones", rows).put("wiredLive", wiredLive)
            .put("wiredName", if (wiredLive) (DashPlayService.snapshot().phoneName ?: "iPhone") else "")
    }

    /** Adapter to the shared supervisor. `present` means eligible to attempt Bluetooth on Android. */
    @Synchronized fun command(command: String, args: List<String>): String? {
        val bonded = current()
        val now = android.os.SystemClock.elapsedRealtime()
        fun ok(value: Boolean): String? = if (value) "ok" else null
        return when (command) {
            "boot" -> { policy.boot(); policy.select(bonded, now) }
            "select", "unplugged" -> policy.select(bonded, now)
            "present", "allowed" -> args.firstOrNull()?.let { ok(policy.allowed(it, bonded)) }
            "switch" -> args.firstOrNull()?.let { ok(policy.choose(it, bonded)) }
            "idle" -> { policy.idle(); "ok" }
            "decline" -> args.firstOrNull()?.let { policy.decline(it, now); "ok" }
            "disconnect-request" -> policy.active?.takeIf { !policy.allowed(it, bonded) } ?: policy.disconnect
            "admit" -> null // IP is not a phone identity; the verified controller gate decides ownership.
            "remember" -> ok(args.size == 3 && args[2].isNotBlank() && policy.active == args[1])
            "devices" -> {
                val rows = JSONArray()
                policy.state.phones.filter { !it.removed && it.address in bonded }.forEach {
                    rows.put(JSONObject().put("address", it.address).put("name", it.name)
                        .put("active", it.address == policy.active).put("last", it.address == policy.state.last))
                }
                rows.toString()
            }
            else -> null
        }
    }

    companion object {
        private val MAC = Regex("[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}")
        @Volatile private var instance: AndroidPhones? = null
        fun get(context: Context): AndroidPhones = instance ?: synchronized(this) {
            instance ?: AndroidPhones(context).also { instance = it }
        }
    }
}
