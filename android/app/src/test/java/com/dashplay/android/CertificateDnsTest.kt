package com.dashplay.android

import java.io.File
import java.net.URI
import java.net.URLDecoder
import java.nio.file.Files
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class CertificateDnsTest {
    private val value = "A".repeat(43)
    private val secret = "fixture-private-token-12345678"
    @Test fun desecNewRecordsHonorTheDomainsMinimumTtl() = temporary { journal ->
        var writes = 0
        val dns = CertificateDns(CertificateDnsConfig("desec", "box.example.test", secret, "example.test"),
            CertificateHttp { method, url, _, body ->
                if (method == "PUT") {
                    val record = Json.parseToJsonElement(body!!.toString(Charsets.UTF_8)).jsonArray.single().jsonObject
                    assertEquals(4800, record["ttl"]!!.jsonPrimitive.int); writes++
                    CertificateReply(200, emptyMap(), "[]".toByteArray())
                } else if (url == "https://desec.io/api/v1/domains/example.test/")
                    CertificateReply(200, emptyMap(), """{"name":"example.test","minimum_ttl":4800}""".toByteArray())
                else CertificateReply(404, emptyMap(), ByteArray(0))
            }, journal)
        dns.updateAddress("192.0.2.1"); dns.present("_acme-challenge.box.example.test", value)
        assertEquals(2, writes)
    }
    private fun failWith(reason: String, block: () -> Unit) {
        try { block(); fail("expected rejection") } catch (error: CertificateFailure) {
            assertEquals(reason,error.reason); assertFalse(error.toString().contains(secret))
        }
    }
    private fun temporary(block: (File) -> Unit) {
        val directory=Files.createTempDirectory("dashplay-dns-").toFile()
        try { block(File(directory,"pending.json")) } finally { directory.deleteRecursively() }
    }
    @Test fun duckDnsAlwaysSuppliesAddressAndTxtCleanupNeverClearsAddressRecords() = temporary { journal ->
        val queries=mutableListOf<Map<String,String>>()
        val http=CertificateHttp { method,url,headers,body ->
            assertEquals("GET",method);assertNull(body);assertTrue(headers.isEmpty())
            assertEquals("www.duckdns.org",URI(url).host)
            val fields=URI(url).rawQuery.split('&').associate {
                val pair=it.split('=',limit=2); URLDecoder.decode(pair[0],"UTF-8") to URLDecoder.decode(pair[1],"UTF-8")
            }
            assertEquals(secret,fields["token"]);queries+=fields
            if (fields.containsKey("txt") && fields["clear"]!="true") assertTrue(journal.exists())
            CertificateReply(200,emptyMap(),"OK\n".toByteArray())
        }
        val config=CertificateDnsConfig("duckdns","dashplay-fixture.duckdns.org",secret)
        assertFalse(config.toString().contains(secret))
        val dns=CertificateDns(config,http,journal)
        dns.updateAddress("192.0.2.1")
        dns.present("_acme-challenge.${config.domain}",value)
        assertTrue(dns.cleanupPending())
        CertificateDns(config,http,journal).recover()
        assertFalse(journal.exists());assertEquals(3,queries.size)
        assertEquals("192.0.2.1",queries[0]["ip"])
        assertEquals(value,queries[1]["txt"]);assertEquals(value,queries[2]["txt"])
        assertEquals("true",queries[2]["clear"]);assertFalse(queries[0].containsKey("clear"))
    }
    @Test fun desecPreservesOtherTxtRecordsAndOnlyChangesTheConfiguredARecord() = temporary { journal ->
        var records=listOf("\"other-owner\"");val writes=mutableListOf<JsonObject>()
        val http=CertificateHttp { method,url,headers,body ->
            assertEquals("Token $secret",headers["Authorization"])
            if (url == "https://desec.io/api/v1/domains/example.test/") {
                assertEquals("GET", method)
                return@CertificateHttp CertificateReply(200, emptyMap(), """{"name":"example.test","minimum_ttl":3600}""".toByteArray())
            }
            assertTrue(url.startsWith("https://desec.io/api/v1/domains/example.test/rrsets/"))
            if (method=="GET") {
                assertTrue(url.endsWith("_acme-challenge.box/TXT/"))
                CertificateReply(200,emptyMap(),buildJsonObject {
                    put("subname","_acme-challenge.box");put("type","TXT");put("ttl",3600)
                    put("records",buildJsonArray { records.forEach { add(it) } })
                }.toString().toByteArray())
            } else {
                assertEquals("PUT",method)
                val payload=Json.parseToJsonElement(body!!.toString(Charsets.UTF_8)).jsonArray
                assertEquals(1,payload.size)
                val record=payload.single().jsonObject;writes+=record
                if (record.string("type")=="TXT") records=record["records"]!!.jsonArray.map { it.jsonPrimitive.content }
                CertificateReply(200,emptyMap(),"[]".toByteArray())
            }
        }
        val dns=CertificateDns(CertificateDnsConfig("desec","box.example.test",secret,"example.test"),http,journal)
        dns.updateAddress("100.96.0.1")
        assertEquals(3600,writes[0]["ttl"]!!.jsonPrimitive.int)
        assertEquals("box",writes[0].string("subname"));assertEquals("A",writes[0].string("type"))
        assertEquals("100.96.0.1",writes[0]["records"]!!.jsonArray.single().jsonPrimitive.content)
        dns.present("_acme-challenge.box.example.test",value)
        assertEquals(listOf("\"other-owner\"","\"$value\""),records)
        records=records+"\"added-after-present\""
        dns.remove("_acme-challenge.box.example.test",value)
        assertEquals(listOf("\"other-owner\"","\"added-after-present\""),records)
        assertEquals(3600,writes.last()["ttl"]!!.jsonPrimitive.int)
        assertFalse(journal.exists())
    }
    @Test fun interruptedPublicationCanBeCleanedOnRestartAndFailedCleanupKeepsJournal() = temporary { journal ->
        var deny=true
        val http=CertificateHttp { _,_,_,_ -> CertificateReply(if(deny)403 else 200,emptyMap(),"OK".toByteArray()) }
        val config=CertificateDnsConfig("duckdns","dashplay-fixture.duckdns.org",secret)
        val dns=CertificateDns(config,http,journal)
        failWith("dns_denied") { dns.present("_acme-challenge.${config.domain}",value) }
        assertTrue(journal.exists())
        failWith("dns_denied") { dns.recover() };assertTrue(journal.exists())
        deny=false;CertificateDns(config,http,journal).recover();assertFalse(journal.exists())
    }
    @Test fun wrongOrUnjournaledChallengeCannotRemoveDns() = temporary { journal ->
        var requests=0
        val dns=CertificateDns(CertificateDnsConfig("duckdns","dashplay-fixture.duckdns.org",secret),CertificateHttp { _,_,_,_ ->
            requests++;CertificateReply(200,emptyMap(),"OK".toByteArray())
        },journal)
        dns.remove("_acme-challenge.dashplay-fixture.duckdns.org",value);assertEquals(0,requests)
        failWith("dns_identifier") { dns.remove("_acme-challenge.foreign.test",value) }
        journal.writeText("damaged fixture journal")
        failWith("dns_journal") { dns.recover() }
        assertEquals(0,requests);assertEquals("damaged fixture journal",journal.readText())
    }
    @Test fun domainZoneTokenAndExplicitIpv4AreValidatedBeforeAnyRequest() = temporary { journal ->
        val configs=listOf(CertificateDnsConfig("duckdns","child.name.duckdns.org",secret),
            CertificateDnsConfig("desec","box.other.test",secret,"example.test"),
            CertificateDnsConfig("desec","box.example.test","bad token","example.test"),
            CertificateDnsConfig("unsupported","box.example.test",secret))
        for(config in configs) try { config.checked();fail() } catch (_:IllegalArgumentException) { }
        var count=0
        val dns=CertificateDns(CertificateDnsConfig("duckdns","fixture.duckdns.org",secret),CertificateHttp { _,_,_,_ ->
            count++;CertificateReply(200,emptyMap(),"OK".toByteArray())
        },journal)
        for(address in listOf("","::1","example.test","192.0.2.256","192.0.2.1&clear=true","0.0.0.0","127.0.0.1","224.0.0.1","169.254.1.1"))
            try { dns.updateAddress(address);fail() } catch (_:IllegalArgumentException) { }
        assertEquals(0,count)
    }
    @Test fun malformedExistingRecordCannotBeSilentlyOverwritten() = temporary { journal ->
        var writes=0
        val dns=CertificateDns(CertificateDnsConfig("desec","box.example.test",secret,"example.test"),CertificateHttp { method,_,_,_ ->
            if(method!="GET")writes++
            CertificateReply(200,emptyMap(),"""{"subname":"_acme-challenge.box","type":"TXT","ttl":900}""".toByteArray())
        },journal)
        failWith("dns_response") { dns.present("_acme-challenge.box.example.test",value) }
        failWith("dns_response") { dns.recover() }
        assertEquals(0,writes);assertTrue(journal.exists())
    }
}
