package com.dashplay.android

import android.app.Activity
import android.os.Bundle
import android.widget.TextView

/**
 * Ships what the APK actually links: one line per runtime dependency, taken from its POM or jar by
 * scripts/android-collect-licenses.py, plus the full text of each licence beside it.
 */
class LicensesActivity : Activity() {
    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        val ui = Ui(this)
        val root = ui.stack()
        root.addView(ui.button(R.style.Button_Text, getString(R.string.back)) { finish() })
        root.addView(ui.text(R.style.Text_Title, "开源许可"))
        root.addView(ui.text(R.style.Text_Small, "这台设备上实际运行的第三方组件与许可证。清单由构建脚本按真实依赖生成，全文在 APK 内，不需要联网。"))
        root.addView(ui.text(R.style.Text_Head, "第三方组件"))
        root.addView(ui.text(R.style.Text_Small, read("third-party.txt")))
        root.addView(ui.text(R.style.Text_Head, "许可证全文"))
        for (name in assets.list("licenses").orEmpty().filter { it != "third-party.txt" }.sorted()) {
            val body = read(name)
            if (body.isNotBlank()) {
                root.addView(ui.text(R.style.Text_Name, name))
                root.addView(ui.text(R.style.Text_Small, body))
            }
        }
        setContentView(ui.page(root))
    }

    private fun read(name: String): String =
        runCatching { assets.open("licenses/$name").use { it.readBytes().toString(Charsets.UTF_8) } }.getOrDefault("")
}
