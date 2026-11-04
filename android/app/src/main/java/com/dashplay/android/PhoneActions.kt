package com.dashplay.android

import android.content.Context
import com.playport.server.PhoneList
import kotlinx.serialization.Serializable

@Serializable
data class PhoneAction(val address: String = "", val action: String = "")

object PhoneActions {
    /** Returns a user-facing error; successful disconnects are consumed by the shared supervisor. */
    fun apply(context: Context, request: PhoneAction): String? {
        val address = PhoneList.normalize(request.address)
        if (!PhoneList.ADDRESS.matches(address)) return "手机标识无效。"
        val phones = AndroidPhones.get(context)
        return when (request.action) {
            "connect" -> {
                val receiver = DashPlayService.activeReceiver() ?: return "请先启动接收服务。"
                WirelessReadiness.problem(context)?.let { return it }
                if (!receiver.config.wireless) return "请先完成无线连接的网络与蓝牙设置。"
                when (receiver.switchPhone(address)) {
                    null -> null
                    "cable" -> "当前仅支持数据线，请先完成无线网络设置或拔掉数据线。"
                    "not-paired" -> "这部手机尚未配对或已从 DashPlay 移除，请重新添加。"
                    "busy" -> "正在切换，请稍后重试。"
                    else -> "接收服务暂时无法切换，请稍后重试。"
                }
            }
            "disconnect", "remove" -> if (phones.pause(address, request.action == "remove")) null else "手机记录不存在。"
            else -> "不支持这项手机操作。"
        }
    }
}
