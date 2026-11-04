package com.dashplay.android

import com.playport.server.WebHub
import io.ktor.server.cio.CIO
import io.ktor.server.engine.embeddedServer
import io.ktor.server.routing.routing
import io.ktor.server.routing.get
import io.ktor.server.routing.post
import io.ktor.server.response.respondText
import io.ktor.http.HttpStatusCode
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import java.net.InetAddress
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse

/** Real HTTP routing with explicit fixture management handlers; no Android preferences or device are changed. */
class HttpManagementTest {
    @Test(timeout=10000) fun managementSurvivesDisabledViewerAndUnavailableTlsAndRetainsOriginGuard() {
        val server = embeddedServer(CIO, host="127.0.0.1", port=0) {
            HttpBrowserRoutes.install(this, {
                routing {
                    get("/manage") { call.respondText("management fixture") }
                    post("/api/android/settings") {
                        val own = WebHub.ownPage(call.request.headers["Origin"],
                            "${call.request.local.serverHost}:${call.request.local.serverPort}", call.request.headers["Sec-Fetch-Site"])
                        call.respondText(if (own) "accepted fixture" else "forbidden", status=if (own) HttpStatusCode.OK else HttpStatusCode.Forbidden)
                    }
                }
            }, null)
        }.start(false)
        val backend = runBlocking { server.engine.resolvedConnectors().first().port }
        val front = BrowserFront(InetAddress.getByName("127.0.0.1"), 0, null, backend)
        front.start()
        val client = HttpClient.newHttpClient()
        val base = "http://127.0.0.1:${front.localPort}"
        fun request(path: String, origin: String? = null) = client.send(HttpRequest.newBuilder(URI(base+path)).apply {
            if (origin != null) { header("Origin", origin); POST(HttpRequest.BodyPublishers.ofString("{}")) } else GET()
        }.build(),HttpResponse.BodyHandlers.ofString())
        try {
            assertEquals(200,request("/manage").statusCode())
            assertEquals("management fixture",request("/manage").body())
            val root = request("/")
            assertEquals(302,root.statusCode())
            assertEquals("/manage",root.headers().firstValue("Location").orElse(""))
            assertEquals(404,request("/ws").statusCode())
            assertEquals(200,request("/api/android/settings",base).statusCode())
            assertEquals(403,request("/api/android/settings","http://unrelated.example").statusCode())
        } finally { front.close(); server.stop(0,1000) }
    }
}
