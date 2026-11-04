package com.dashplay.android

import com.playport.server.*
import com.shilapi.xcertplay.airplay.*
import java.net.Socket
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** Synthetic sessions exercise the production listener; no MFi or phone connection is claimed. */
class LivePhoneStateTest {
    @get:Rule val tmp = TemporaryFolder()
    private val a = "02:00:00:00:00:0A"
    private val b = "02:00:00:00:00:0B"
    private fun field(s: CarPlayServer, name: String) = s.javaClass.getDeclaredField(name).also { it.isAccessible = true }
    private fun set(s: CarPlayServer, name: String, value: Any?) = field(s, name).set(s, value)
    private fun listener(s: CarPlayServer) = field(s, "listener").get(s) as AirPlaySessionListener
    private fun session(s: CarPlayServer) = AirPlaySession(Socket(), s.airPlayConfig, s.identity,
        PairingStore(), null, listener(s), object : AirPlayMediaHandler {})
    private fun rows(s: CarPlayServer): JsonObject {
        val method = s.javaClass.getDeclaredMethod("withWiredRow", String::class.java).also { it.isAccessible = true }
        return Json.parseToJsonElement(method.invoke(s, PhoneList.body(true,
            """[{"address":"$a","active":false},{"address":"$b","active":true}]""")) as String).jsonObject
    }
    private fun active(s: CarPlayServer) = rows(s)["phones"]!!.jsonArray.filter {
        it.jsonObject["active"]!!.jsonPrimitive.boolean
    }.map { it.jsonObject["address"]!!.jsonPrimitive.content }
    private fun run(block: (CarPlayServer) -> Unit) {
        val root = tmp.newFolder().toPath()
        CarPlayServer(ServerConfig(identityDir=root, stateDir=root, bindAddress="127.0.0.1", wireless=false)).use {
            it.platformWiredReselect = {}
            block(it)
        }
    }
    @Test fun negotiationDoesNotDisplayAnActiveWirelessPhone() = run { s ->
        val pending = session(s); set(s, "activeSession", pending); set(s, "selectedPhone", a)
        assertTrue(active(s).isEmpty())
        pending.close()
    }
    @Test fun wiredControlConnectionIsNotLiveUntilRecord() = run { s ->
        val pending = session(s); set(s, "activeSession", pending); set(s, "wiredSession", pending)
        assertFalse(s.wiredSessionActive())
        listener(s).onSessionActive(pending)
        assertTrue(s.wiredSessionActive())
        assertTrue(active(s).isEmpty())
        pending.close(); assertFalse(s.wiredSessionActive())
    }
    @Test fun actualOwnerDoesNotFollowLaterSelectionIntent() = run { s ->
        val live = session(s); set(s, "activeSession", live); set(s, "selectedPhone", a)
        listener(s).onSessionActive(live); set(s, "selectedPhone", b)
        assertEquals(listOf(a), active(s))
        live.close(); assertTrue(active(s).isEmpty())
    }
    @Test fun oldRecordAndCloseCannotReplaceTheNewSession() = run { s ->
        val old = session(s); val next = session(s)
        set(s, "activeSession", old); set(s, "selectedPhone", a); listener(s).onSessionActive(old)
        set(s, "activeSession", next); set(s, "selectedPhone", b); listener(s).onSessionActive(next)
        listener(s).onSessionActive(old); old.close()
        assertSame(next, field(s, "activeSession").get(s))
        assertEquals(listOf(b), active(s))
        next.close()
    }
    @Test fun setupAuthorizationWaitsForRecordAndKeepsItsAuthenticatedOwner() {
        val root = tmp.newFolder().toPath()
        var allowed = true
        CarPlayServer(ServerConfig(identityDir=root, stateDir=root, bindAddress="127.0.0.1", wireless=false),
            platformController = { _, _, _ -> allowed }).use { s ->
            val pending = session(s); set(s, "activeSession", pending); set(s, "selectedPhone", a)
            assertTrue(listener(s).onSetupAllowed(pending))
            assertNull(s.wirelessSessionPhone()); assertTrue(active(s).isEmpty())
            listener(s).onDeviceInfo(pending, AirPlayDeviceInfo("Test", "", "", ""))
            assertFalse(s.hub.sessionIsActive)
            set(s, "selectedPhone", b)
            listener(s).onSessionActive(pending)
            assertEquals(a, s.wirelessSessionPhone()); assertTrue(s.hub.sessionIsActive)
            pending.close()
            allowed = false
            val rejected = session(s); set(s, "activeSession", rejected)
            assertFalse(listener(s).onSetupAllowed(rejected))
            listener(s).onSessionActive(rejected)
            assertNull(s.wirelessSessionPhone()); assertFalse(s.hub.sessionIsActive)
            rejected.close()
        }
    }
    @Test fun staleInfoCannotChangeTheNewSessionState() = run { s ->
        val old = session(s); val next = session(s)
        set(s, "activeSession", next); set(s, "selectedPhone", b); listener(s).onSessionActive(next)
        listener(s).onDeviceInfo(old, AirPlayDeviceInfo("Old", "", "", ""))
        assertTrue(s.hub.sessionIsActive); assertEquals(b, s.wirelessSessionPhone())
        old.close(); next.close()
    }
}
