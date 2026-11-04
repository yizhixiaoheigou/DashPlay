package com.dashplay.android

import kotlinx.serialization.Serializable

@Serializable
data class SavedPhone(val address: String, val name: String, val controllers: Set<String> = emptySet(),
    val paused: Boolean = false, val removed: Boolean = false, val manuallyAdded: Boolean = false)

@Serializable
data class PhoneState(val phones: List<SavedPhone> = emptyList(), val preferred: String? = null, val last: String? = null)

/** Durable invitation policy. Bonded means eligible for an attempt, never proof of Wi-Fi presence. */
class PhonePolicy(initial: PhoneState = PhoneState(), private val persist: (PhoneState) -> Unit = {}) {
    var state: PhoneState = initial; private set
    var disconnect: String? = null; private set
    private val declinedUntil = mutableMapOf<String, Long>()

    private fun save(next: PhoneState) { if (next != state) { persist(next); state = next } }
    fun discover(address: String, name: String) {
        if (state.phones.none { it.address == address })
            save(state.copy(phones = state.phones + SavedPhone(address, name.take(64))))
        else if (state.phones.any { it.address == address && !it.removed && it.name != name.take(64) })
            save(state.copy(phones = state.phones.map { if (it.address == address) it.copy(name = name.take(64)) else it }))
    }
    fun add(address: String, name: String) {
        val old = state.phones.firstOrNull { it.address == address }
        val phone = (old ?: SavedPhone(address, name)).copy(name = name.take(64), paused = false, removed = false, manuallyAdded = true)
        save(state.copy(phones = state.phones.filterNot { it.address == address } + phone, preferred = address))
        declinedUntil.remove(address)
        if (disconnect == address) disconnect = null
    }
    fun allowed(address: String, bonded: Set<String>): Boolean = address in bonded &&
        state.phones.any { it.address == address && !it.paused && !it.removed }

    fun select(bonded: Set<String>, now: Long): String? =
        (listOfNotNull(state.preferred, state.last) + state.phones.map { it.address }.sorted()).distinct()
            .firstOrNull { allowed(it, bonded) && now >= declinedUntil.getOrDefault(it, 0) }

    fun choose(address: String, bonded: Set<String>): Boolean {
        val phone = state.phones.firstOrNull { it.address == address && !it.removed } ?: return false
        if (address !in bonded) return false
        save(state.copy(preferred = address, phones = state.phones.map { if (it.address == address) phone.copy(paused = false) else it }))
        declinedUntil.remove(address)
        if (disconnect == address) disconnect = null
        return true
    }
    fun pause(address: String, remove: Boolean = false): Boolean {
        if (state.phones.none { it.address == address && !it.removed }) return false
        save(state.copy(phones = state.phones.map {
            if (it.address == address) it.copy(paused = true, removed = remove, name = if (remove) "" else it.name) else it
        }, preferred = state.preferred?.takeUnless { remove && it == address },
            last = state.last?.takeUnless { remove && it == address }))
        disconnect = address
        return true
    }
    fun decline(address: String, now: Long) { declinedUntil[address] = now + 60_000 }
    fun idle() { disconnect = null }
    fun boot() { idle(); declinedUntil.clear() }

    /** A cryptographically verified controller cannot be relabelled as the newly selected phone. */
    fun authorize(controller: String?, selected: String?, allowNew: Boolean, bonded: Set<String>): Boolean {
        if (controller.isNullOrBlank() || controller.length > 256 || controller.any { it.isISOControl() } ||
            selected == null || !allowed(selected, bonded)) return false
        val owner = state.phones.firstOrNull { controller in it.controllers }
        if (owner != null && owner.address != selected) return false
        if (owner == null && !allowNew) return false
        val phone = state.phones.first { it.address == selected }
        if (owner == null && phone.controllers.size >= 8) return false
        save(state.copy(preferred = selected, last = selected, phones = state.phones.map {
            if (it.address == selected) it.copy(controllers = it.controllers + controller) else it
        }))
        return true
    }
}
