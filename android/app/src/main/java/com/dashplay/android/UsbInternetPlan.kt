package com.dashplay.android

/** Explicit public IPv4 routes leave local networks on their original interfaces, including on API 21. */
internal data class UsbInternetPlan(val address: Int, val dns: Int, val routes: List<Cidr>) {
    data class Cidr(val address: Int, val prefix: Int) {
        init { require(prefix in 0..32 && address == (address and mask(prefix))) }
        fun contains(ip: Int) = (ip and mask(prefix)) == address
        val text get() = UsbTetherProtocol.address(address)
        companion object { fun mask(prefix: Int) = if (prefix == 0) 0 else -1 shl (32 - prefix) }
    }
    companion object {
        fun create(local: List<Cidr>): UsbInternetPlan {
            val reserved = listOf(Cidr(0, 8), Cidr(0x0a000000, 8), Cidr(0x64400000, 10), Cidr(0x7f000000, 8),
                Cidr(0xa9fe0000.toInt(), 16), Cidr(0xac100000.toInt(), 12), Cidr(0xc0000000.toInt(), 24),
                Cidr(0xc0a80000.toInt(), 16), Cidr(0xc6120000.toInt(), 15), Cidr(0xe0000000.toInt(), 3))
            val address = listOf(0xc0000002.toInt(), 0xc6120002.toInt(), 0xc6130002.toInt()).firstOrNull { candidate ->
                local.none { it.contains(candidate) || it.contains(candidate - 1) }
            } ?: throw IllegalArgumentException("本地网络与 USB 上网通道地址冲突，请先关闭其他虚拟网络。")
            val exclusions = (reserved + local).distinct()
            val routes = mutableListOf<Cidr>()
            fun visit(block: Cidr) {
                if (exclusions.any { it.prefix <= block.prefix && it.contains(block.address) }) return
                if (exclusions.none { block.prefix <= it.prefix && block.contains(it.address) }) { routes += block; return }
                val next = block.prefix + 1
                visit(Cidr(block.address, next)); visit(Cidr(block.address or (1 shl (32 - next)), next))
            }
            visit(Cidr(0, 0))
            routes += Cidr(address - 1, 32)
            return UsbInternetPlan(address, address - 1, routes)
        }
    }
}
