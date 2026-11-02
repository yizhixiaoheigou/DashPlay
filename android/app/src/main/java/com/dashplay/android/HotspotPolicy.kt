package com.dashplay.android

/** Only channels supported by the current CarPlay invitation format (2.4/5 GHz). */
object HotspotPolicy {
    fun channel(frequency: Int): Int? = when {
        frequency == 2484 -> 14
        frequency in 2412..2472 && (frequency - 2407) % 5 == 0 -> (frequency - 2407) / 5
        frequency in 5160..5885 && (frequency - 5000) % 5 == 0 -> (frequency - 5000) / 5
        else -> null
    }

    fun owns(owner: Boolean, name: String?, password: String?, expectedName: String, expectedPassword: String): Boolean =
        owner && name == expectedName && password == expectedPassword
}
