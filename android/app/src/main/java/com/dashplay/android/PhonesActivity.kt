package com.dashplay.android

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import java.util.concurrent.Executors

/** App records and system Bluetooth pairing are presented as distinct operations. */
class PhonesActivity : Activity() {
    private lateinit var ui: Ui
    private val handler = Handler(Looper.getMainLooper())
    private val refreshTask = Runnable { refresh() }
    private val worker = Executors.newSingleThreadExecutor()
    private var busy = false
    private var notice = ""
    private var shown = ""
    private var resumed = false
    override fun onCreate(state: Bundle?) { super.onCreate(state); ui = Ui(this) }
    override fun onResume() { super.onResume(); resumed = true; shown = ""; refresh() }
    override fun onPause() { resumed = false; handler.removeCallbacks(refreshTask); super.onPause() }
    override fun onDestroy() { worker.shutdown(); super.onDestroy() }
    private fun permission() = Build.VERSION.SDK_INT < 31 || checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED

    private fun action(address: String, action: String) {
        if (busy) return
        busy = true; notice = "正在处理…"; refresh()
        worker.execute {
            val error = runCatching { PhoneActions.apply(this, PhoneAction(address, action)) }.getOrElse { "操作未完成，请重试。" }
            handler.post {
                if (isDestroyed) return@post
                busy = false
                notice = error ?: when (action) {
                    "disconnect" -> "已暂停这部手机的自动连接；选择连接可恢复。"
                    "remove" -> "已从 DashPlay 移除。系统蓝牙配对仍保留，可在系统设置中取消。"
                    else -> "正在邀请 iPhone，请稍候。"
                }
                if (resumed) refresh()
            }
        }
    }
    private fun refresh() {
        val data = runCatching { AndroidPhones.get(this).describe() }.getOrNull()
        val key = "${data}:$notice:$busy:${permission()}"
        if (key != shown) {
            shown = key
            val focus = currentFocus?.tag
            val root = ui.stack()
            root.addView(ui.button(R.style.Button_Text, "返回") { finish() })
            root.addView(ui.text(R.style.Text_Title, "已添加的 iPhone"))
            root.addView(ui.text(R.style.Text_Small, "断开会暂停自动连接，重启后仍保持。移除只影响 DashPlay；取消系统蓝牙配对请进入系统设置。"))
            if (notice.isNotBlank()) root.addView(ui.text(R.style.Text_Name, notice))
            if (!permission()) {
                root.addView(ui.button(R.style.Button_Primary, "允许访问已配对设备") {
                    if (Build.VERSION.SDK_INT >= 31) requestPermissions(arrayOf(Manifest.permission.BLUETOOTH_CONNECT), 9)
                })
            }
            val rows = data?.optJSONArray("phones")
            val wiredLive = DashPlayService.snapshot().wiredActive
            val wiredName = DashPlayService.snapshot().phoneName ?: "iPhone"
            if (wiredLive) {
                root.addView(ui.text(R.style.Text_Head, "有线 · $wiredName"))
                root.addView(ui.text(R.style.Text_Small, "CarPlay 连接中 · 数据线连接"))
            }
            if (rows == null) root.addView(ui.text(R.style.Text_Name, "暂时无法读取手机记录，请重试。"))
            else if (rows.length() == 0 && !wiredLive) root.addView(ui.text(R.style.Text_Name, "尚未添加 iPhone"))
            else for (i in 0 until rows.length()) {
                val phone = rows.getJSONObject(i); val address = phone.getString("address"); val name = phone.getString("name")
                val status = when { !wiredLive && phone.getBoolean("active") -> "CarPlay 连接中"; phone.getBoolean("paused") -> "已暂停自动连接"; !phone.getBoolean("bonded") -> "未配对或缺少蓝牙权限"; else -> "已配对，等待连接" }
                root.addView(ui.text(R.style.Text_Head, name))
                root.addView(ui.text(R.style.Text_Small, "$status · 蓝牙 …${address.takeLast(5)}"))
                root.addView(ui.button(R.style.Button_Secondary, "连接") { action(address, "connect") }.apply { tag = "$address/connect"; isEnabled = !busy && phone.getBoolean("bonded") })
                if (!phone.getBoolean("paused")) root.addView(ui.button(R.style.Button_Secondary, "断开并暂停") { action(address, "disconnect") }.apply { tag = "$address/disconnect"; isEnabled = !busy })
                root.addView(ui.button(R.style.Button_Text, "从 DashPlay 移除") {
                    AlertDialog.Builder(this).setTitle("移除 $name？").setMessage("停止这部手机的自动连接，并从 DashPlay 列表移除；系统蓝牙配对会保留。")
                        .setNegativeButton("取消", null).setPositiveButton("移除") { _, _ -> action(address, "remove") }.show()
                }.apply { tag = "$address/remove"; isEnabled = !busy })
            }
            root.addView(ui.button(R.style.Button_Primary, "添加 iPhone") { startActivity(Intent(this, PairActivity::class.java)) }.apply { id = R.id.add_iphone })
            root.addView(ui.button(R.style.Button_Text, "系统蓝牙设置") { startActivity(Intent(android.provider.Settings.ACTION_BLUETOOTH_SETTINGS)) })
            setContentView(ui.page(root))
            focus?.let { root.findViewWithTag<View>(it)?.requestFocus() }
        }
        handler.removeCallbacks(refreshTask)
        if (resumed) handler.postDelayed(refreshTask, 2000)
    }
}
