package com.dashplay.android

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import java.io.ByteArrayOutputStream
import java.text.DateFormat
import java.util.Date

/** Device-local certificate import. Selected private key bytes are never shown or copied to the clipboard. */
class TlsActivity : Activity() {
    private lateinit var domain: EditText
    private lateinit var status: TextView
    private lateinit var chainLabel: TextView
    private lateinit var keyLabel: TextView
    private val buttons = mutableListOf<Button>()
    private var chain = ""
    private var key = ""
    private var working = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = Ui(this)
        val root = ui.stack()
        // Opening settings should show status and actions, not automatically cover them with the keyboard.
        root.isFocusableInTouchMode = true
        if (savedInstanceState == null) root.requestFocus()
        root.addView(ui.button(R.style.Button_Text, getString(R.string.back)) { finish() })
        root.addView(ui.text(R.style.Text_Title, "自己的网址与证书"))
        root.addView(ui.button(R.style.Button_Secondary, "自动申请与续期") {
            startActivity(Intent(this, AutomaticTlsActivity::class.java))
        }.apply { id = R.id.tls_automatic })
        root.addView(ui.text(R.style.Text_Small, "导入域名对应的 PEM 证书链与私钥。域名需能解析到本机，证书需被车机信任；本次导入不会自动修改 DNS。"))
        root.addView(ui.text(R.style.Text_Small, getString(R.string.browser_scheme_hint)))
        val snapshot = DashPlayService.snapshot()
        root.addView(ui.text(R.style.Text_Small, "当前域名解析目标：" +
            (if (TeslaRootAccess.active) TeslaRootPlan.ADDRESS else snapshot.network?.address?.hostAddress ?: "等待网络")))
        status = ui.text(R.style.Text_Name, "正在读取证书状态……")
        root.addView(status)
        val (box, input) = ui.field("完整域名", savedInstanceState?.getString("domain").orEmpty(), false, R.id.tls_domain)
        domain = input; root.addView(box, ui.matchWidth())
        chain = savedInstanceState?.getString("chain").orEmpty()
        key = savedInstanceState?.getString("key").orEmpty()
        chainLabel = ui.text(R.style.Text_Small, ""); root.addView(chainLabel)
        keyLabel = ui.text(R.style.Text_Small, ""); root.addView(keyLabel)
        fun button(label: String, id: Int, action: () -> Unit) {
            val button = ui.button(R.style.Button_Secondary, label, action).apply { this.id = id }
            buttons += button; root.addView(button, ui.matchWidth())
        }
        button("选择证书链文件", R.id.tls_chain) { pick(1) }
        button("选择私钥文件", R.id.tls_key) { pick(2) }
        button("导入并启用", R.id.tls_import) {
            val name = domain.text.toString(); val selectedChain = chain; val selectedKey = key
            work {
                AutomaticTls.import(this, TlsImport(name, selectedChain, selectedKey))
                DashPlayService.restart(this)
                runOnUiThread { chain = ""; key = ""; labels() }
            }
        }
        button("启用已保存的证书", R.id.tls_enable) { work { AutomaticTls.enable(this, true); DashPlayService.restart(this) } }
        button("停用并使用本地证书", R.id.tls_disable) { work { AutomaticTls.enable(this, false); DashPlayService.restart(this) } }
        val settings = Settings(this)
        val ownNameCheck = android.widget.CheckBox(this).apply {
            text = "由本机告诉车机域名在哪（热点模式）"
            minHeight = ui.dp(48)
            isChecked = settings.ownNameOnHotspot
            setOnCheckedChangeListener { _, checked ->
                settings.ownNameOnHotspot = checked
                DashPlayService.restart(this@TlsActivity)
            }
        }
        root.addView(ownNameCheck)
        root.addView(ui.text(R.style.Text_Small, "为连接本机热点的设备提供域名解析，使车机在无互联网时仍能通过域名访问接收端。如果影响车机自带应用联网，可关闭此项。在非特斯拉模式下需点击下方按钮授权 Root 接管 53 端口（或已开启特斯拉联网检查 / USB 热点共享）。"))
        button("授权 Root 启用热点域名回答", View.generateViewId()) {
            work { settings.enableHotspotOwnNameRoot(); DashPlayService.restart(this) }
        }
        button("关闭热点域名 Root 规则", View.generateViewId()) {
            work { settings.hotspotOwnNameRoot = false; DashPlayService.restart(this) }
        }
        root.addView(ui.text(R.style.Text_Small, "证书只保存在本应用的私有目录。导入或切换后接收服务会重启，CarPlay 会短暂断开；停用会保留证书材料。若域名打不开，可用浏览器管理中的本地地址恢复。"))
        setContentView(ui.page(root)); labels()
    }
    override fun onResume() { super.onResume(); if (!working) refresh() }
    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString("domain", domain.text.toString()); outState.putString("chain", chain); outState.putString("key", key)
    }
    private fun labels() {
        chainLabel.text = if (chain.isEmpty()) "尚未选择证书链" else "证书链已选择"
        keyLabel.text = if (key.isEmpty()) "尚未选择私钥" else "私钥已选择"
    }
    private fun pick(request: Int) {
        runCatching { startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).setType("*/*")
            .addCategory(Intent.CATEGORY_OPENABLE), request) }.onFailure { status.text = "此设备没有文件选择器，请通过浏览器管理导入。" }
    }
    @Deprecated("Activity result API without AndroidX")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (resultCode != RESULT_OK || requestCode !in 1..2) return
        val uri = data?.data ?: return
        work {
            val limit = if (requestCode == 1) OwnTlsMaterial.MAX_CHAIN else OwnTlsMaterial.MAX_KEY
            val bytes = ByteArrayOutputStream()
            contentResolver.openInputStream(uri)?.use { input ->
                val buffer = ByteArray(4096)
                while (true) {
                    val count = input.read(buffer); if (count < 0) break
                    require(bytes.size() + count <= limit) { "文件过大。" }
                    bytes.write(buffer, 0, count)
                }
            } ?: throw IllegalArgumentException("无法读取文件。")
            require(bytes.size() > 0) { "文件为空。" }
            val text = bytes.toString("UTF-8")
            runOnUiThread { if (requestCode == 1) chain = text else key = text; labels() }
        }
    }
    private fun refresh() = work { }
    private fun work(action: () -> Unit) {
        if (working) return
        working = true; buttons.forEach { it.isEnabled = false }; status.text = "正在处理……"
        Thread {
            val failure = try { action(); null } catch (e: IllegalArgumentException) { e.message ?: "证书无效。" }
                catch (_: Exception) { "操作未完成，原有证书保留，请重试。" }
            val state = OwnTls.store(this).state()
            runOnUiThread {
                working = false
                if (!isFinishing && !isDestroyed) {
                    buttons.forEach { it.isEnabled = true }
                    status.text = failure ?: (state.message + if (state.expires > 0)
                        "\n有效期至：" + DateFormat.getDateInstance().format(Date(state.expires)) else "")
                    if (domain.text.isEmpty() && state.domain.isNotEmpty()) domain.setText(state.domain)
                }
            }
        }.start()
    }
}
