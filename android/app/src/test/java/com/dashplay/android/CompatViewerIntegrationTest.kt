package com.dashplay.android

import com.playport.server.CarPlayServer
import com.playport.server.ServerConfig
import io.ktor.server.cio.CIO
import io.ktor.server.engine.embeddedServer
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.net.http.WebSocket
import java.nio.ByteBuffer
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Real loopback HTTP/WebSocket, production CarPlayServer lifecycle and WebHub. No phone, identity or decoded video is simulated as real. */
class CompatViewerIntegrationTest {
    @get:Rule val tmp = TemporaryFolder()
    @Test(timeout = 15000) fun `HTTP viewer skips compressed media and an abrupt last close releases stills`() {
        val directory = tmp.root.toPath()
        val receiver = CarPlayServer(ServerConfig(identityDir=directory,stateDir=directory,accessToken="fixture-token",
            bindAddress="127.0.0.1",airPlayPort=0,wireless=false))
        val hub = receiver.hub
        val joined = CountDownLatch(1); val left = CountDownLatch(1); val image = CountDownLatch(1)
        val kinds = CopyOnWriteArrayList<Int>()
        hub.onStillsChange = { if (hub.stillsViewers > 0) joined.countDown() else left.countDown() }
        // Match Android: install the producer observer before starting the receiver.
        receiver.start()
        val server = embeddedServer(CIO, host="127.0.0.1", port=0) { hub.install(this,directory,viewerOnly=true) }.start(false)
        val backendPort = runBlocking { server.engine.resolvedConnectors().first().port }
        val front = BrowserFront(java.net.InetAddress.getByName("127.0.0.1"), 0, 1, backendPort)
        front.start()
        val port = front.localPort
        val client = HttpClient.newHttpClient()
        var socket: WebSocket? = null
        try {
            val response = client.send(HttpRequest.newBuilder(URI("http://127.0.0.1:$port/api/phones")).GET().build(),HttpResponse.BodyHandlers.ofString())
            assertEquals(404,response.statusCode())
            for (path in listOf("/manage", "/api/android/settings", "/api/android/identity", "/api/android/tls/import", "/api/android/update/install")) {
                for (method in listOf("GET", "POST")) {
                    val request = HttpRequest.newBuilder(URI("http://127.0.0.1:$port$path"))
                        .method(method, HttpRequest.BodyPublishers.noBody()).build()
                    assertEquals("$method $path must remain private", 404, client.send(request, HttpResponse.BodyHandlers.ofString()).statusCode())
                }
            }
            socket = client.newWebSocketBuilder().header("Origin","http://127.0.0.1:$port")
                .buildAsync(URI("ws://127.0.0.1:$port/ws?token=fixture-token"),object:WebSocket.Listener {
                    override fun onOpen(ws:WebSocket){ws.request(1)}
                    override fun onText(ws:WebSocket,data:CharSequence,last:Boolean):java.util.concurrent.CompletionStage<*>? { ws.request(1);return null }
                    override fun onBinary(ws:WebSocket,data:ByteBuffer,last:Boolean):java.util.concurrent.CompletionStage<*>? {
                        val kind=data.get(0).toInt() and 255;kinds.add(kind);if(kind==7)image.countDown();ws.request(1);return null
                    }
                }).get(5,TimeUnit.SECONDS)
            assertTrue(joined.await(2,TimeUnit.SECONDS))
            hub.broadcastVideoConfig(110,byteArrayOf(0,0,0,1,0x67,1))
            hub.broadcastVideoFrame(110,true,byteArrayOf(0,0,0,1,0x65,1))
            hub.broadcastImage(1,1,2,2,0,byteArrayOf(0xff.toByte(),0xd8.toByte(),0xff.toByte(),0xd9.toByte()))
            assertTrue(image.await(3,TimeUnit.SECONDS))
            assertFalse("HTTP must never receive compressed config",kinds.contains(1))
            assertFalse("HTTP must never receive compressed frames",kinds.contains(2))
            socket.abort()
            assertTrue("abrupt disconnect must notify producer",left.await(3,TimeUnit.SECONDS))
            assertEquals(0,hub.stillsViewers)
        } finally { socket?.abort();front.close();server.stop(0,1000);receiver.close() }
    }
}
