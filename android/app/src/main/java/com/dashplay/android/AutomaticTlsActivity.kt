package com.dashplay.android

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.*
import java.text.DateFormat
import java.util.Date

class AutomaticTlsActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var feedback: TextView
    private lateinit var enabled: CheckBox
    private lateinit var terms: CheckBox
    private lateinit var provider: Spinner
    private lateinit var domain: EditText
    private lateinit var zone: EditText
    private lateinit var token: EditText
    private lateinit var zoneBox: View
    private lateinit var providerHint: TextView
    private val buttons = mutableListOf<Button>()
    private val handler = Handler(Looper.getMainLooper())
    private var working = false
    private var initialized = false
    private val poll = object : Runnable {
        override fun run() { if (!working) work(); handler.postDelayed(this, 5000) }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = Ui(this); val root = ui.stack()
        root.isFocusableInTouchMode = true; root.requestFocus()
        root.addView(ui.button(R.style.Button_Text, getString(R.string.back)) { finish() })
        root.addView(ui.text(R.style.Text_Title, "自动申请与续期"))
        root.addView(ui.text(R.style.Text_Small, "为这台设备使用独立域名。联网时自动维护 IPv4 解析、申请和续期证书；无 SIM 卡也可通过外部 Wi-Fi 或以太网上网，离线时保留已有证书。"))
        status = ui.text(R.style.Text_Name, "正在读取……"); root.addView(status)
        enabled = CheckBox(this).apply { text = "启用自动申请与续期"; id = R.id.auto_tls_enabled; minHeight = ui.dp(48) }
        root.addView(enabled)
        val providerLabel = ui.text(R.style.Text_Name, "DNS 服务商").apply { labelFor = R.id.auto_tls_provider }
        root.addView(providerLabel)
        provider = Spinner(this).apply {
            id = R.id.auto_tls_provider; minimumHeight = ui.dp(48)
            adapter = ArrayAdapter(this@AutomaticTlsActivity, android.R.layout.simple_spinner_dropdown_item, arrayOf("DuckDNS", "deSEC"))
        }; root.addView(provider, ui.matchWidth())
        fun field(label: String, id: Int, password: Boolean = false): Pair<View, EditText> {
            val pair = ui.field(label, "", password, id); root.addView(pair.first, ui.matchWidth()); return pair
        }
        domain = field("完整域名", R.id.auto_tls_domain).second
        val zoneField = field("deSEC DNS 区域", R.id.auto_tls_zone); zoneBox = zoneField.first; zone = zoneField.second
        token = field("DNS token（同一域名留空保留）", R.id.auto_tls_token, true).second
        providerHint = ui.text(R.style.Text_Small, ""); root.addView(providerHint)
        provider.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onNothingSelected(parent: AdapterView<*>?) = Unit
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                zoneBox.visibility = if (position == 1) View.VISIBLE else View.GONE
                providerHint.text = if (position == 0) "DuckDNS 域名须供本设备独用，不能与其他程序共用 TXT 验证记录。" else
                    "填写托管该域名的 DNS 区域，token 需有修改此域名 A 与 TXT 记录的权限。"
            }
        }
        terms = CheckBox(this).apply {
            id = R.id.auto_tls_terms; text = "我同意 Let's Encrypt 证书服务条款"; minHeight = ui.dp(48)
        }; root.addView(terms)
        root.addView(ui.button(R.style.Button_Text, "查看证书服务条款") {
            runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://letsencrypt.org/repository/"))) }
                .onFailure { feedback.text = "可在浏览器访问 https://letsencrypt.org/repository/ 查看条款。" }
        })
        root.addView(ui.text(R.style.Text_Small, "签发后的域名会出现在公开证书记录中。此功能维护 IPv4，不会清除已有 IPv6 解析；请勿为此域名配置指向其他设备的 AAAA 记录。"))
        fun button(label: String, id: Int, action: () -> Unit) {
            val b = ui.button(R.style.Button_Secondary, label, action).apply { this.id = id }
            buttons += b; root.addView(b, ui.matchWidth())
        }
        button("保存自动维护设置", R.id.auto_tls_save) {
            val request = AutomaticTlsUpdate(enabled.isChecked, if (provider.selectedItemPosition == 1) "desec" else "duckdns",
                domain.text.toString(), if (provider.selectedItemPosition == 1) zone.text.toString() else "",
                token.text.toString().takeIf { it.isNotBlank() }, terms.isChecked)
            work("已保存，后台会按网络状态继续；当前画面不会因此立即重启。") {
                AutomaticTls.store(this).configure(request)
                runOnUiThread { token.setText("") }
            }
        }
        button("重试自动维护", R.id.auto_tls_retry) { work("已请求重试；服务商要求的等待时间仍会保留。") { AutomaticTls.store(this).retry() } }
        feedback = ui.text(R.style.Text_Small, "").apply { accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE }
        root.addView(feedback)
        root.addView(ui.text(R.style.Text_Small, "接收服务运行时才会自动维护。续期证书将在 CarPlay 空闲时加载。关闭自动维护会保留当前证书；手动导入或切换证书会同时停止自动维护。未完成的旧验证记录将在联网后清理。"))
        enabled.setOnCheckedChangeListener { _, _ -> fields() }
        fields()
        setContentView(ui.page(root))
        if (savedInstanceState != null) {
            initialized = true
            enabled.isChecked = savedInstanceState.getBoolean("enabled"); terms.isChecked = savedInstanceState.getBoolean("terms")
            provider.setSelection(savedInstanceState.getInt("provider")); domain.setText(savedInstanceState.getString("domain"))
            zone.setText(savedInstanceState.getString("zone")); token.setText(savedInstanceState.getString("token"))
        }
    }
    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean("enabled", enabled.isChecked); outState.putBoolean("terms", terms.isChecked)
        outState.putInt("provider", provider.selectedItemPosition); outState.putString("domain", domain.text.toString())
        outState.putString("zone", zone.text.toString()); outState.putString("token", token.text.toString())
    }
    override fun onResume() { super.onResume(); handler.post(poll) }
    override fun onPause() { handler.removeCallbacks(poll); super.onPause() }
    private fun fields() {
        listOf(provider, domain, zone, token, terms).forEach { it.isEnabled = enabled.isChecked }
    }
    private fun work(success: String? = null, action: () -> Unit = {}) {
        if (working) return
        working = true; buttons.forEach { it.isEnabled = false }
        if (success != null) feedback.text = "正在保存……"
        Thread {
            var state: AutomaticTlsStore.State? = null
            val error = try { action(); state = AutomaticTls.store(this).state(); null }
                catch (e: IllegalArgumentException) { e.message ?: "设置无效。" }
                catch (_: Exception) { "无法读取或保存自动维护设置，原文件保留。" }
            runOnUiThread {
                working = false
                if (!isDestroyed && !isFinishing) {
                    buttons.forEach { it.isEnabled = true }
                    if (error != null) feedback.text = error else if (success != null) feedback.text = success
                    state?.let { s ->
                        status.text = s.message + if (s.nextAttempt > System.currentTimeMillis() && s.phase == "retry")
                            "\n下次尝试：" + DateFormat.getDateTimeInstance().format(Date(s.nextAttempt)) else ""
                        if (!initialized) {
                            initialized = true; enabled.isChecked = s.enabled; terms.isChecked = s.termsAccepted
                            provider.setSelection(if (s.provider == "desec") 1 else 0); domain.setText(s.domain); zone.setText(s.zone)
                        }
                    }
                }
            }
        }.start()
    }
}
