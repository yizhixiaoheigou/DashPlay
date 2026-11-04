package com.dashplay.android

import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.net.VpnService
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.widget.EditText
import android.widget.TextView
import java.io.File
import java.util.concurrent.Executors

/** A persistent, isolated address test. It never replaces a live cable VPN or stops somebody else's service. */
class TeslaAddressProbeActivity : Activity() {
    private val handler = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private lateinit var results: TextView
    private lateinit var answer: EditText
    private var vpn: UsbVpnService? = null
    private var bound = false
    private var page: TeslaAddressProbe.Page? = null
    private var lease: UsbVpnService.AddressLease? = null
    private val probePort = 8090
    private var markerValue = ""
    private var startedAt = 0L
    private var networkKey = ""
    private fun networkKey(): String = LocalNetwork.choose(this)?.let { "${Settings(this).network}:${it.name}:${it.address.hostAddress}" }.orEmpty()
    @Volatile private var peerSeen = false
    @Volatile private var attempt = 0L
    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            vpn = (service as? UsbVpnService.LocalBinder)?.service
            runProbe()
        }
        override fun onServiceDisconnected(name: ComponentName?) { vpn = null; stopProbe() }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = Ui(this); val root = ui.stack()
        root.addView(ui.button(R.style.Button_Text, "返回") { finish() })
        root.addView(ui.text(R.style.Text_Title, "免 Root 地址可达性测试"))
        root.addView(ui.text(R.style.Text, "先关闭 HTTP 兼容入口并断开 USB CarPlay，再开始测试。地址和测试页会保持到停止或离开本页；不会启动第二个 VPN 中断正在使用的 USB 连接。"))
        root.addView(ui.text(R.style.Text_Small, "本机热点与外部热点分开记录。普通外部热点通常没有通往这个虚拟地址的路由，同一个 Wi-Fi 不等于可达。本测试不修改路由器、Root 规则或声音。"))
        results = ui.text(R.style.Text_Small, "等待开始……"); root.addView(results)
        root.addView(ui.button(R.style.Button_Primary, "开始测试并授权 VPN") {
            val consent = VpnService.prepare(this)
            if (consent != null) startActivityForResult(consent, CONSENT) else begin()
        }.apply { id = R.id.probe_start })
        answer = EditText(this).apply { hint = "填入另一台设备页面上的标记"; isSingleLine = true }
        root.addView(answer)
        root.addView(ui.button(R.style.Button_Secondary, "核对客户端标记") {
            val valid = networkKey.isNotEmpty() && networkKey == networkKey() && lease?.isOpen == true && peerSeen && markerValue.isNotEmpty() &&
                AddressChallenge(markerValue, startedAt).verified(answer.text.toString().trim(), attempt, attempt, System.currentTimeMillis() - startedAt)
            results.text = if (valid) "客户端访问与标记核对通过；仅证明当前网络和端口可达，画面与 USB 共存另测。" else "未验证：需另一台设备实际打开页面并填入本轮标记，且测试通道仍开启。"
            if (valid) TeslaAddressProbe.journal(File(filesDir, "tesla-address-probe.log"), listOf(TeslaAddressProbe.Result("PEER", true,
                "network=${Settings(this).network}; port=$probePort; carrier=${TeslaAddressProbe.carrierName(this)}", markerValue)))
        })
        root.addView(ui.button(R.style.Button_Secondary, "停止并清理") { stopProbe() }.apply { id = R.id.probe_stop })
        setContentView(ui.page(root))
    }
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == CONSENT) {
            if (resultCode == RESULT_OK) begin() else results.text = "尚未允许 VPN，测试未启动。"
        }
    }
    private fun begin() {
        if (lease != null) { results.text = "本轮仍在运行，请先停止。"; return }
        if (Settings(this).compatHttp || Settings(this).teslaRootAccess) {
            results.text = "请先关闭 HTTP 兼容入口和 Root 地址，再进行独立地址测试。"; return
        }
        if (vpn != null) { runProbe(); return }
        if (!bound) bound = bindService(Intent(this, UsbVpnService::class.java).setAction(UsbVpnService.LOCAL_BIND), connection, Context.BIND_AUTO_CREATE)
        if (!bound) results.text = "无法连接 VPN 服务。"
    }
    private fun runProbe() {
        val service = vpn ?: return
        val id = ++attempt
        results.text = "正在建立地址……"
        worker.execute {
            val created = service.attachAddress(TeslaVpnAddress(), exclusive = true)
            val marker = java.util.UUID.randomUUID().toString().replace("-", "").take(16)
            val listener = if (created != null) TeslaAddressProbe.Page(marker, probePort) { peerSeen = true } else null
            handler.post {
                if (id != attempt || isFinishing) { listener?.close(); created?.close(); return@post }
                if (listener?.started != true || created == null) {
                    listener?.close(); created?.close()
                    results.text = "无法建立独立测试通道或监听地址；请检查 VPN 授权，并先断开 USB、关闭其他地址入口。"
                } else {
                    networkKey = networkKey()
                    lease = created; page = listener; markerValue = marker; startedAt = System.currentTimeMillis(); peerSeen = false
                    created.onClosed { handler.post { if (lease === created) { stopProbe(); results.text = "VPN 已撤销或地址通道已关闭，本轮证据失效。" } } }
                    listener.start()
                    results.text = "请用另一台设备打开 http://${TeslaAddressProbe.TEST_ADDRESS}:$probePort/，再填回页面标记。请保留 http:// 开头，不要改成 https://。保持本页开启。"
                }
            }
        }
    }
    private fun stopProbe() {
        attempt++; peerSeen = false
        page?.close(); page = null
        val owned = lease; lease = null; owned?.close()
        if (bound) { runCatching { unbindService(connection) }; bound = false }
        vpn = null
        if (::results.isInitialized) results.text = "已停止测试并释放本轮资源。"
    }
    override fun onDestroy() { stopProbe(); worker.shutdown(); super.onDestroy() }
    companion object { private const val CONSENT = 41 }
}
