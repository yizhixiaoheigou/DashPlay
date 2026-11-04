package com.dashplay.android

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.Toast

/** Physical access grants the owner a management link, independently of the car's viewer token. */
class ManagementActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = Ui(this)
        val root = ui.stack()
        root.addView(ui.button(R.style.Button_Text, getString(R.string.back)) { finish() })
        root.addView(ui.text(R.style.Text_Title, getString(R.string.manage_title)))
        root.addView(ui.text(R.style.Text_Small, getString(R.string.manage_help)))
        root.addView(ui.text(R.style.Text_Small, getString(R.string.browser_scheme_hint)))
        val address = DashPlayService.snapshot().directAddress
        if (address == null) {
            root.addView(ui.text(R.style.Text_Name, getString(R.string.manage_no_network)))
            root.addView(ui.button(R.style.Button_Secondary, getString(R.string.wifi_open_settings)) { Intents.openHotspotSettings(this) })
            root.addView(ui.button(R.style.Button_Primary, getString(R.string.manage_refresh)) { recreate() })
        } else {
            val link = "http://$address/manage"
            root.addView(ui.text(R.style.Text_Name, link).apply { setTextIsSelectable(true) })
            root.addView(ImageView(this).apply {
                setImageBitmap(ui.qr(link, ui.dp(240)))
                contentDescription = getString(R.string.manage_qr)
            }, LinearLayout.LayoutParams(ui.dp(240), ui.dp(240)))
            root.addView(ui.text(R.style.Text_Small, getString(R.string.manage_private)))
            root.addView(ui.button(R.style.Button_Primary, getString(R.string.manage_copy)) {
                (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(ClipData.newPlainText("DashPlay", link))
                Toast.makeText(this, R.string.manage_copied, Toast.LENGTH_SHORT).show()
            })
            root.addView(ui.button(R.style.Button_Secondary, getString(R.string.manage_open)) {
                if (runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(link))) }.isFailure)
                    Toast.makeText(this, R.string.manage_no_browser, Toast.LENGTH_LONG).show()
            })
            val direct = DashPlayService.snapshot().directAddress
            if (direct != null && direct != address) {
                root.addView(ui.text(R.style.Text_Small, "可用本地 HTTP 管理地址恢复，无需域名或证书。"))
                root.addView(ui.text(R.style.Text_Name, "http://$direct/manage").apply { setTextIsSelectable(true) })
                root.addView(ui.button(R.style.Button_Secondary, "复制本地管理入口") {
                    (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(
                        ClipData.newPlainText("DashPlay", "http://$direct/manage"))
                    Toast.makeText(this, R.string.manage_copied, Toast.LENGTH_SHORT).show()
                })
            }
        }
        root.addView(ui.text(R.style.Text_Small, "车上出问题后，可导出运行日志发给帮你排查的人；凭据、密码和私钥已去除。"))
        root.addView(ui.button(R.style.Button_Secondary, "导出运行日志") { exportLog() })
        setContentView(ui.page(root))
    }

    private fun exportLog() {
        val intent = Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
            .setType("text/plain").putExtra(Intent.EXTRA_TITLE, "DashPlay-log.txt")
        // TV boxes may have no document picker: the browser page downloads the same file.
        if (runCatching { startActivityForResult(intent, EXPORT_LOG) }.isFailure)
            Toast.makeText(this, "本机没有文件保存界面，请在浏览器管理页点「下载运行日志」。", Toast.LENGTH_LONG).show()
    }

    @Deprecated("Android 5 compatible activity result")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        val target = data?.data
        if (requestCode != EXPORT_LOG || resultCode != RESULT_OK || target == null) return
        Thread({
            val text = DriveLog.export(java.io.File(filesDir, "logs")).ifEmpty { "（还没有日志：DashPlay 服务尚未运行）\n" }
            val ok = runCatching { contentResolver.openOutputStream(target)!!.use { it.write(text.toByteArray(Charsets.UTF_8)) } }.isSuccess
            runOnUiThread { Toast.makeText(this, if (ok) "运行日志已保存。" else "保存失败，请换一个位置重试。", Toast.LENGTH_LONG).show() }
        }, "export-log").start()
    }

    private companion object { const val EXPORT_LOG = 41 }
}
