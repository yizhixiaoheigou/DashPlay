package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

/** The Android app must keep the N1/OpenWrt receiver's sound semantics (scripts/onecloud/audio_routes.py). */
class AudioPolicyTest {
    @Test fun alertAlwaysStaysInCarPlay() {
        for (outside in listOf(false, true)) for (tesla in listOf(false, true))
            assertEquals("carplay", AudioPolicy.routes(outside, tesla)["alert"])
    }

    @Test fun defaultKeepsEverythingInCarPlay() {
        val routes = AudioPolicy.routes(outsideCarPlay = false, tesla = false)
        assertEquals(setOf("media", "navigation", "alert", "siri", "calls"), routes.keys)
        assertTrue(routes.values.all { it == "carplay" })
    }

    @Test fun teslaSendsMusicAndCallsToBluetoothButKeepsNavigation() {
        val routes = AudioPolicy.routes(outsideCarPlay = false, tesla = true)
        assertEquals("bluetooth", routes["media"]); assertEquals("bluetooth", routes["siri"])
        assertEquals("bluetooth", routes["calls"]); assertEquals("carplay", routes["navigation"])
    }

    @Test fun leavingCarPlaySendsNavigationAwayToo() {
        val routes = AudioPolicy.routes(outsideCarPlay = true, tesla = false)
        assertEquals("bluetooth", routes["media"]); assertEquals("bluetooth", routes["navigation"])
        assertEquals("bluetooth", routes["calls"]); assertEquals("carplay", routes["alert"])
    }

    @Test fun webFlagsMatchRunReceiver() {
        // run-receiver.py: carMusic also for Tesla mode, carVoice only when calls/Siri left CarPlay.
        assertFalse(AudioPolicy.webCarMusic(outsideCarPlay = false, tesla = false))
        assertTrue(AudioPolicy.webCarMusic(outsideCarPlay = false, tesla = true))
        assertTrue(AudioPolicy.webCarMusic(outsideCarPlay = true, tesla = false))
        assertFalse(AudioPolicy.webCarVoice(outsideCarPlay = false, tesla = true))
        assertTrue(AudioPolicy.webCarVoice(outsideCarPlay = true, tesla = false))
    }

    @Test fun platformCableRoleSupportsSwitchingBetweenWiredAndWirelessRows() {
        val dir = java.nio.file.Files.createTempDirectory("android-cable-role")
        val marker = dir.resolve("cable-carplay-off")
        val role = com.playport.server.CableRole(marker, static = false).also { it.init() }
        val server = com.playport.server.CarPlayServer(
            com.playport.server.ServerConfig(
                bindAddress = "127.0.0.1",
                airPlayPort = java.net.ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1")).use { it.localPort },
                identityDir = dir.resolve("identity").also { java.nio.file.Files.createDirectories(it) },
                stateDir = dir.resolve("state").also { java.nio.file.Files.createDirectories(it) },
                wireless = true,
                wifiSsid = "DashPlay",
                wifiPassphrase = "password123",
            ),
            platformPresence = { cmd, _ ->
                when (cmd) {
                    "devices" -> """[{"address":"02:00:00:00:00:0A","name":"Phone A","active":false}]"""
                    "switch" -> "ok"
                    else -> null
                }
            },
            platformCableRole = role,
        )
        var reselected = 0
        server.platformWiredReselect = { reselected++ }
        try {
            server.setCablePhone(true)
            assertEquals(null, server.switchPhone("02:00:00:00:00:0A"))
            assertFalse(role.carPlayOnCable())
            assertEquals(1, reselected)
            server.cableWithoutCarPlay()
            assertEquals(null, server.switchToCable())
            assertTrue(role.carPlayOnCable())
            assertEquals(2, reselected)
        } finally {
            server.close()
            dir.toFile().deleteRecursively()
        }
    }
}