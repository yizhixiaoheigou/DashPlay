package com.dashplay.android

/** Only a newer standalone build signed by the currently installed publisher is an update. */
internal object ApkUpdatePolicy {
    data class Package(val name: String, val version: Long, val signers: Set<String>, val split: Boolean = false)
    fun check(installed: Package, candidate: Package) {
        require(candidate.name == installed.name) { "这不是 DashPlay 的安装包。" }
        require(!candidate.split) { "请选择完整 APK 安装包。" }
        require(installed.signers.isNotEmpty() && candidate.signers == installed.signers) {
            "安装包的签名与当前版本不同，不能覆盖更新。请向提供者获取同一签名的版本。"
        }
        require(candidate.version > installed.version) { "请选择比当前版本更新的安装包。" }
    }
}
