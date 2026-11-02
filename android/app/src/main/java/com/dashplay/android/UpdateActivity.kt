package com.dashplay.android

import android.app.Activity
import android.content.ClipData
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.TextView
import java.text.DateFormat
import java.util.Date

/** Online update check + mirror acceleration + local APK fallback. Android always owns final installation confirmation. */
class UpdateActivity : Activity() {
    private lateinit var onlineSummary: TextView
    private lateinit var checkButton: Button
    private lateinit var downloadButton: Button
    private lateinit var dailyCheck: CheckBox
    private lateinit var mirrorInput: EditText
    private lateinit var mirrorSaveButton: Button
    private lateinit var status: TextView
    private lateinit var choose: Button
    private lateinit var installButton: Button
    private var candidate: ApkUpdates.Candidate? = null
    private var working = false
    private var installerOpen = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = Ui(this)
        val settings = Settings(this)
        val root = ui.stack()
        root.isFocusableInTouchMode = true
        if (savedInstanceState == null) root.requestFocus()
        root.addView(ui.button(R.style.Button_Text, getString(R.string.back)) { finish() })
        root.addView(ui.text(R.style.Text_Title, "版本与更新"))
        @Suppress("DEPRECATION") val info = packageManager.getPackageInfo(packageName, 0)
        root.addView(ui.text(R.style.Text_Name, "当前版本：${info.versionName}"))

        root.addView(ui.text(R.style.Text_Head, "在线检查与更新"))
        onlineSummary = ui.text(R.style.Text_Small, "正在读取在线更新状态……")
        root.addView(onlineSummary)

        checkButton = ui.button(R.style.Button_Secondary, "检查在线更新") { checkOnline() }.apply { id = R.id.update_check_now }
        downloadButton = ui.button(R.style.Button_Primary, "在线下载并更新") { downloadOnline() }.apply { id = R.id.update_download }
        root.addView(checkButton, ui.matchWidth())
        root.addView(downloadButton, ui.matchWidth())

        dailyCheck = CheckBox(this).apply {
            id = R.id.update_daily
            text = "每天自动检查新版本（只提示，不自动安装）"
            minHeight = ui.dp(48)
            isChecked = settings.updateCheck
            setOnCheckedChangeListener { _, checked ->
                settings.updateCheck = checked
            }
        }
        root.addView(dailyCheck)

        val (mirrorBox, mirrorField) = ui.field(
            "镜像加速地址（留空依次试内置镜像，填 none 只连 GitHub，可用空格分隔多个）",
            savedInstanceState?.getString("mirror") ?: settings.updateMirror,
            password = false,
            fieldId = R.id.update_mirror,
        )
        mirrorInput = mirrorField
        root.addView(mirrorBox, ui.matchWidth())
        root.addView(ui.text(R.style.Text_Small, "当 GitHub 无法直接连接时，依次尝试此镜像前缀。发布包均经过签名与 SHA-256 校验，镜像无法篡改安装内容。"))
        mirrorSaveButton = ui.button(R.style.Button_Secondary, "保存镜像设置") {
            try {
                settings.updateMirror = mirrorInput.text.toString()
                status.text = "镜像加速设置已保存。"
            } catch (e: IllegalArgumentException) {
                status.text = e.message ?: "镜像地址格式无效。"
            }
        }.apply { id = R.id.update_mirror_save }
        root.addView(mirrorSaveButton, ui.matchWidth())

        root.addView(ui.text(R.style.Text_Head, "本地安装包更新"))
        root.addView(ui.text(R.style.Text_Small, "也可直接选择已下载的新版 DashPlay APK，在系统安装界面确认更新。覆盖安装会保留设置、配对和身份，无需先卸载。"))
        status = ui.text(R.style.Text_Small, savedInstanceState?.getString("status") ?: "尚未选择更新包。").apply {
            id = R.id.update_status; accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }; root.addView(status)
        choose = ui.button(R.style.Button_Secondary, "选择更新 APK") { pick() }.apply { id = R.id.update_choose }
        installButton = ui.button(R.style.Button_Primary, "安装更新") { install() }.apply { id = R.id.update_install }
        root.addView(choose, ui.matchWidth()); root.addView(installButton, ui.matchWidth())
        root.addView(ui.text(R.style.Text_Small, "更新会短暂停止接收服务。请先暂停 iPhone 连接，安装完成后重新打开 DashPlay。没有文件选择器的设备，可在系统文件管理器中打开 APK。"))
        setContentView(ui.page(root))
        installerOpen = savedInstanceState?.getBoolean("installerOpen") ?: false
        renderOnlineState(OnlineUpdate.readState(this))
        buttons()
        savedInstanceState?.getString("file")?.let { filename -> work({ ApkUpdates.inspect(this, ApkUpdates.file(this, filename)) }) }
        if (savedInstanceState == null) installerResult(intent)
    }

    override fun onResume() {
        super.onResume()
        if (::installButton.isInitialized) {
            renderOnlineState(OnlineUpdate.readState(this))
            buttons()
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString("file", candidate?.file?.name)
        outState.putString("status", status.text.toString())
        outState.putString("mirror", mirrorInput.text.toString())
        outState.putBoolean("installerOpen", installerOpen)
    }

    private fun renderOnlineState(s: OnlineUpdate.State) {
        val checkedText = if (s.checkedAt > 0) "上次检查：" + DateFormat.getDateTimeInstance().format(Date(s.checkedAt)) else "尚未检查过在线版本"
        val headline = when {
            s.error.isNotEmpty() -> "检查更新未成功：${s.error}"
            s.available -> "发现新版本：${s.latest}（当前 ${s.current}）" + (if (s.notes.isNotBlank()) "\n更新内容：${s.notes}" else "")
            s.latest.isNotEmpty() -> "已是最新版本（${s.latest}）"
            else -> "点击「检查在线更新」对比 GitHub 最新发布版本。"
        }
        onlineSummary.text = "$headline\n$checkedText"
        downloadButton.visibility = if (s.available) View.VISIBLE else View.GONE
    }

    private fun buttons() {
        val state = OnlineUpdate.readState(this)
        checkButton.isEnabled = !working && !installerOpen
        downloadButton.isEnabled = !working && !installerOpen && state.available
        mirrorSaveButton.isEnabled = !working && !installerOpen
        choose.isEnabled = !working && !installerOpen
        installButton.isEnabled = !working && !installerOpen && candidate != null
        installButton.text = if (Build.VERSION.SDK_INT >= 26 && candidate != null && !packageManager.canRequestPackageInstalls()) "允许安装更新" else "安装更新"
    }

    private fun checkOnline() {
        if (working) return
        runCatching { Settings(this).updateMirror = mirrorInput.text.toString() }.onFailure {
            status.text = it.message ?: "镜像地址格式无效。"
            return
        }
        working = true; buttons(); onlineSummary.text = "正在从 GitHub / 镜像检查最新版本……"
        Thread {
            val state = OnlineUpdate.check(this)
            runOnUiThread {
                working = false
                if (!isFinishing && !isDestroyed) {
                    renderOnlineState(state)
                    buttons()
                }
            }
        }.start()
    }

    private fun downloadOnline() {
        if (working) return
        if (DashPlayService.snapshot().sessionActive) {
            status.text = "请先在「iPhone」中暂停连接，再下载并安装更新。"
            return
        }
        runCatching { Settings(this).updateMirror = mirrorInput.text.toString() }.onFailure {
            status.text = it.message ?: "镜像地址格式无效。"
            return
        }
        candidate?.file?.delete(); candidate = null
        working = true; buttons(); status.text = "正在下载并校验新版 APK……"
        Thread {
            val result = runCatching { OnlineUpdate.download(this) }
            runOnUiThread {
                working = false
                if (isFinishing || isDestroyed) {
                    if (candidate == null) result.getOrNull()?.file?.delete()
                    return@runOnUiThread
                }
                renderOnlineState(OnlineUpdate.readState(this))
                result.onSuccess {
                    candidate = it
                    status.text = "已下载并校验 ${it.version}，正在打开安装器……"
                    buttons()
                    install()
                }.onFailure {
                    candidate = null
                    status.text = it.message ?: "在线下载失败，请检查网络或更换镜像地址。"
                    buttons()
                }
            }
        }.start()
    }

    private fun pick() {
        runCatching { startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).setType(ApkUpdates.MIME)
            .addCategory(Intent.CATEGORY_OPENABLE), 1) }.onFailure {
            status.text = "此设备没有文件选择器，请在系统文件管理器中打开新版 DashPlay APK。"
        }
    }

    private fun install() {
        val selected = candidate ?: return
        if (DashPlayService.snapshot().sessionActive) { status.text = "请先在「iPhone」中暂停连接，再安装更新。"; return }
        if (Build.VERSION.SDK_INT >= 26 && !packageManager.canRequestPackageInstalls()) {
            runCatching { startActivity(Intent(android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName"))) }
                .onFailure { status.text = "请在系统设置中允许 DashPlay 安装应用，然后返回重试。" }
            return
        }
        work({ ApkUpdates.inspect(this, selected.file) }) { checked ->
            if (DashPlayService.snapshot().sessionActive) status.text = "请先暂停 iPhone 连接，再安装更新。"
            else if (Build.VERSION.SDK_INT < 24) installLegacy(checked)
            else {
                val uri = ApkUpdates.uri(this, checked.file)
                // Content-URI installation is supported by Android 7+.
                @Suppress("DEPRECATION") val intent = Intent(Intent.ACTION_INSTALL_PACKAGE).setDataAndType(uri, ApkUpdates.MIME)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION).putExtra(Intent.EXTRA_RETURN_RESULT, true)
                intent.clipData = ClipData.newRawUri("DashPlay update", uri)
                runCatching { startActivityForResult(intent, 2); installerOpen = true; status.text = "请在系统界面确认安装。" }
                    .onFailure { status.text = "无法打开系统安装器，请在系统文件管理器中打开 APK。" }
            }
        }
    }

    private fun installLegacy(checked: ApkUpdates.Candidate) {
        working = true; buttons(); status.text = "正在准备系统安装……"
        Thread {
            val result = runCatching { LegacyApkInstaller.stage(applicationContext, checked.file) }
            runOnUiThread {
                working = false
                if (isFinishing || isDestroyed) {
                    result.getOrNull()?.let { LegacyApkInstaller.abandon(applicationContext, it) }
                    return@runOnUiThread
                }
                result.onSuccess { id ->
                    if (DashPlayService.snapshot().sessionActive) {
                        LegacyApkInstaller.abandon(this, id)
                        status.text = "请先暂停 iPhone 连接，再安装更新。"
                    } else runCatching {
                        LegacyApkInstaller.commit(this, id)
                        installerOpen = true; status.text = "请在系统界面确认安装。"
                    }.onFailure {
                        LegacyApkInstaller.abandon(this, id)
                        status.text = "无法准备系统安装，请重新选择安装包后重试。"
                    }
                }.onFailure { status.text = "无法准备系统安装，请检查剩余空间后重试。" }
                buttons()
            }
        }.start()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent); setIntent(intent); installerResult(intent)
    }

    @Suppress("DEPRECATION")
    private fun installerResult(intent: Intent) {
        if (intent.action != LegacyApkInstaller.RESULT) return
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirmation = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                runCatching {
                    requireNotNull(confirmation)
                    startActivity(confirmation)
                    installerOpen = true; status.text = "请在系统界面确认安装。"
                }.onFailure {
                    intent.getIntExtra(PackageInstaller.EXTRA_SESSION_ID, -1).takeIf { it >= 0 }
                        ?.let { LegacyApkInstaller.abandon(this, it) }
                    installerOpen = false; status.text = "无法打开系统安装界面，请重试。"
                }
            }
            PackageInstaller.STATUS_SUCCESS -> {
                installerOpen = false; candidate?.file?.delete(); candidate = null
                status.text = "更新已完成。"
            }
            else -> {
                installerOpen = false
                status.text = "安装未完成，当前版本和设置保持不变。"
            }
        }
        buttons()
    }

    @Deprecated("Activity result API without AndroidX")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 1 && resultCode == RESULT_OK) {
            val uri = data?.data ?: return
            candidate?.file?.delete(); candidate = null
            work({ ApkUpdates.prepare(this, uri) })
        } else if (requestCode == 2) {
            installerOpen = false
            if (resultCode == RESULT_OK) { candidate?.file?.delete(); candidate = null }
            status.text = if (resultCode == RESULT_OK) "更新已完成。" else "安装未完成，当前版本和设置保持不变。"
            buttons()
        }
    }

    private fun work(action: () -> ApkUpdates.Candidate, after: ((ApkUpdates.Candidate) -> Unit)? = null) {
        if (working) return
        working = true; buttons(); status.text = "正在校验安装包……"
        Thread {
            val result = runCatching(action)
            runOnUiThread {
                working = false
                if (isFinishing || isDestroyed) {
                    if (candidate == null) result.getOrNull()?.file?.delete()
                    return@runOnUiThread
                }
                result.onSuccess { candidate = it; if (!installerOpen) status.text = "可更新至 ${it.version}。签名与当前版本一致。"; after?.invoke(it) }
                    .onFailure { status.text = if (it is IllegalArgumentException) it.message else "读取安装包失败，请重新选择。"; candidate = null }
                buttons()
            }
        }.start()
    }
}
