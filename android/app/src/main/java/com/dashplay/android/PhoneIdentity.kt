package com.dashplay.android

/** Eligibility hints, not proof of CarPlay identity: the authenticated controller gate remains authoritative. */
internal object PhoneIdentity {
    fun remembered(phone: SavedPhone?, selected: Boolean): Boolean = selected || phone?.let {
        it.manuallyAdded || it.controllers.isNotEmpty() || it.name.contains("iPhone", ignoreCase = true)
    } == true

    fun eligible(lowEnergyOnly: Boolean, name: String?, hasIap2: Boolean, phone: SavedPhone?, selected: Boolean): Boolean =
        !lowEnergyOnly && (hasIap2 || name?.contains("iPhone", ignoreCase = true) == true || remembered(phone, selected))

    fun visible(phone: SavedPhone, lowEnergyOnly: Boolean, eligibleBond: Boolean, selected: Boolean): Boolean =
        !phone.removed && !lowEnergyOnly && (eligibleBond || remembered(phone, selected))
}
