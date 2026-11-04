package com.dashplay.android

/** Allowlisted event words only: never persist arbitrary phone log text or field values. */
internal object PhoneConnectionEvent {
    private val process = Regex("\\b(accessoryd|iap2d|carkitd|airplayd|configd)\\b", RegexOption.IGNORE_CASE)
    private val relevant = Regex("carplay|directlink|\\bncm\\b|iap2", RegexOption.IGNORE_CASE)
    private val words = ("carplay directlink ncm iap2 usb wired wireless network interface address ipv6 tcp socket " +
        "start starting started stop stopped session connection connect connected connecting disconnected " +
        "available unavailable enabled disabled enable disable ready waiting wait pending accepted rejected " +
        "success succeeded failed failure error timeout timed out cancelled canceled invalid missing busy " +
        "not no cannot unable already active inactive is was true false authentication authenticated " +
        "transport registered unregistered attach attached detach detached create created destroy destroyed " +
        "route routing added removed changed change found unknown denied refused supported unsupported " +
        "request requested received send sent begin end ended reset retry retrying duplicate " +
        "bonjour discovery discover discovered service local remote link up down before after reason status").split(' ').toSet()
    private val tokens = Regex("[A-Za-z][A-Za-z0-9_]*")
    private val code = Regex("\\b(?:error|err|status|reason|code)\\s*[=:]?\\s*(-?\\d{1,10})\\b", RegexOption.IGNORE_CASE)
    private val negativeCode = Regex("(?<![A-Za-z0-9_])-\\d{1,10}(?![A-Za-z0-9_])")
    private val interfaceName = Regex("\\ben\\d{1,3}\\b")
    private val errno = Regex("\\b(?:ENETDOWN|ENETUNREACH|EHOSTUNREACH|ENXIO|ETIMEDOUT|ECONNREFUSED|EADDRNOTAVAIL|EHOSTDOWN|ECONNRESET)\\b")

    fun summarize(line: String): String? {
        val owner = process.find(line)?.value?.lowercase() ?: return null
        if (!relevant.containsMatchIn(line)) return null
        val selected = tokens.findAll(line).map { it.value.lowercase() }.filter { it in words }.take(45).toList()
        if (selected.isEmpty()) return null
        val codes = (code.findAll(line).map { it.groupValues[1] } + negativeCode.findAll(line).map { it.value }).distinct().take(3).toList()
        val interfaces = interfaceName.findAll(line).map { it.value }.distinct().take(3).toList()
        val errors = errno.findAll(line).map { it.value }.distinct().take(3).toList()
        return "$owner words=${selected.joinToString(" ")}" +
            (if (codes.isEmpty()) "" else " codes=${codes.joinToString(",")}") +
            (if (interfaces.isEmpty()) "" else " interfaces=${interfaces.joinToString(",")}") +
            (if (errors.isEmpty()) "" else " errors=${errors.joinToString(",")}")
    }
}
