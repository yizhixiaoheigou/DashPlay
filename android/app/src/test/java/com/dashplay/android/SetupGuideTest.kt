package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class SetupGuideTest {
    private fun problem(step: String, car: Car = Car.TESLA, network: Network = Network.MIFI,
        root: Boolean = false, answers: Map<String, String> = emptyMap(), link: IphoneLink = IphoneLink.WIRED,
        usb: Boolean = true) = SetupGuide.problem(step, car, network, link, usb, true, root, answers)

    @Test fun domainAndInternetCannotReplaceTeslaAddressPath() {
        val answers = mapOf(SetupGuide.TLS to "domain", SetupGuide.INTERNET to "upstream", SetupGuide.ACCESS to "none")
        assertNotNull(problem(SetupGuide.ACCESS, answers = answers))
        // Root on the receiver cannot manage an external AP's address path.
        assertNotNull(problem(SetupGuide.ACCESS, root = true, answers = mapOf(SetupGuide.ACCESS to "root")))
        assertNull(problem(SetupGuide.ACCESS, answers = mapOf(SetupGuide.ACCESS to "upstream")))
    }
    @Test fun theRootFreeAddressIsNeverMarkedSupportedBeforeItIsVerified() {
        val answers = mapOf(SetupGuide.ACCESS to "vpn_address")
        val problem = problem(SetupGuide.ACCESS, network = Network.THIS_HOTSPOT, answers = answers)
        assertNotNull("candidate only: no real-client evidence yet", problem)
        assertTrue(problem!!.contains("免 Root 地址测试"))
    }

    @Test fun ownHotspotRequiresAppRootForTeslaButOrdinaryCarsDoNotAsk() {
        val answers = mapOf(SetupGuide.ACCESS to "root")
        assertNotNull(problem(SetupGuide.ACCESS, network = Network.THIS_HOTSPOT, answers = answers))
        assertNull(problem(SetupGuide.ACCESS, network = Network.THIS_HOTSPOT, root = true, answers = answers))
        assertFalse(SetupGuide.included(SetupGuide.ACCESS, Car.OTHER, Network.THIS_HOTSPOT,
            IphoneLink.WIRED, true, true, emptyMap()))
    }
    @Test fun internetAndCertificateAreSeparateGates() {
        assertNull(problem(SetupGuide.INTERNET, answers = mapOf(SetupGuide.INTERNET to "offline")))
        assertNotNull(problem(SetupGuide.KEEP, answers = mapOf(SetupGuide.KEEP to "offline")))
        assertNotNull(problem(SetupGuide.TLS, answers = mapOf(SetupGuide.TLS to "local")))
        assertNull(problem(SetupGuide.TLS, car = Car.OTHER, answers = mapOf(SetupGuide.TLS to "local")))
        assertNotNull(problem(SetupGuide.DNS, answers = mapOf(SetupGuide.TLS to "domain")))
    }
    @Test fun offlineDnsCannotBePublicCacheOrAnUnprivilegedHotspot() {
        assertNotNull(problem(SetupGuide.DNS, answers = mapOf(SetupGuide.DNS to "public", SetupGuide.KEEP to "external")))
        assertNull(problem(SetupGuide.DNS, answers = mapOf(SetupGuide.DNS to "public", SetupGuide.KEEP to "online")))
        assertNotNull("ordinary car on offline LAN cannot use public DNS either",
            problem(SetupGuide.DNS, car = Car.OTHER, network = Network.THIS_HOTSPOT,
                answers = mapOf(SetupGuide.INTERNET to "offline", SetupGuide.TLS to "domain", SetupGuide.DNS to "public")))
        assertNull(problem(SetupGuide.DNS, car = Car.OTHER, network = Network.THIS_HOTSPOT,
            answers = mapOf(SetupGuide.INTERNET to "upstream", SetupGuide.TLS to "domain", SetupGuide.DNS to "public")))
        assertNull(problem(SetupGuide.DNS, answers = mapOf(SetupGuide.DNS to "upstream")))
        assertNotNull(problem(SetupGuide.DNS, network = Network.THIS_HOTSPOT,
            answers = mapOf(SetupGuide.DNS to "hotspot")))
        assertNull(problem(SetupGuide.DNS, network = Network.THIS_HOTSPOT, root = true,
            answers = mapOf(SetupGuide.DNS to "hotspot")))
    }

    @Test fun offlineHotspotReceiverCannotClaimRealCarInternet() {
        assertNotNull(problem(SetupGuide.KEEP, network = Network.THIS_HOTSPOT,
            answers = mapOf(SetupGuide.INTERNET to "offline", SetupGuide.KEEP to "online")))
        assertNull(problem(SetupGuide.KEEP, network = Network.THIS_HOTSPOT,
            answers = mapOf(SetupGuide.INTERNET to "usb", SetupGuide.KEEP to "online")))
    }
    @Test fun externalCarDnsDoesNotDependOnReceiverInternetIntent() {
        for (car in listOf(Car.OTHER, Car.TESLA)) {
            val answers = mapOf(SetupGuide.INTERNET to "offline", SetupGuide.KEEP to "online",
                SetupGuide.TLS to "domain", SetupGuide.DNS to "public")
            assertNull("external network can give the car DNS while receiver internet is not requested",
                problem(SetupGuide.DNS, car = car, network = Network.MIFI, answers = answers))
        }
    }
    @Test fun changingCarInternetRequiresDnsConfirmationAgain() {
        val answers = mutableMapOf(SetupGuide.KEEP to "online", SetupGuide.DNS to "public",
            SetupGuide.INTERNET to "usb", SetupGuide.TLS to "domain")
        SetupGuide.invalidate(SetupGuide.KEEP, answers)
        assertFalse(answers.containsKey(SetupGuide.DNS))
        assertEquals("usb", answers[SetupGuide.INTERNET])
        assertEquals("domain", answers[SetupGuide.TLS])
    }
    @Test fun wiredStillRequiresLanButDoesNotAskWirelessCredentials() {
        assertTrue(SetupGuide.included(SetupGuide.LAN, Car.OTHER, Network.MIFI, IphoneLink.WIRED, true, false, emptyMap()))
        assertNotNull(problem(SetupGuide.LAN, answers = mapOf(SetupGuide.LAN to "missing")))
        assertFalse(SetupGuide.included("wifi", Car.OTHER, Network.MIFI, IphoneLink.WIRED, true, false, emptyMap()))
    }
    @Test fun sameIphoneHotspotDoesNotPromiseAndroidWireless() {
        val answers = mapOf(SetupGuide.SOURCE to "same_iphone")
        for (link in listOf(IphoneLink.WIRELESS, IphoneLink.ANY))
            assertNotNull(problem("iphone", answers = answers, link = link))
        assertNull(problem("iphone", answers = answers))
        assertNotNull(problem("iphone", answers = answers, usb = false))
        assertTrue(SetupGuide.included("wifi", Car.OTHER, Network.THIS_HOTSPOT,
            IphoneLink.WIRELESS, true, true, answers))
        // Changing to the receiver's hotspot must not retain this external-network restriction.
        assertNull(problem("iphone", network = Network.THIS_HOTSPOT, answers = answers, link = IphoneLink.WIRELESS))
    }
    @Test fun wirelessCanStillBorrowUsbInternet() {
        val answers = mapOf(SetupGuide.INTERNET to "usb")
        assertTrue(SetupGuide.included("usb", Car.OTHER, Network.MIFI, IphoneLink.WIRELESS, true, true, answers))
        assertNotNull(problem(SetupGuide.INTERNET, usb = false, answers = answers))
    }
    @Test fun upstreamChangesInvalidateConfirmationsWithoutSilentlyChangingPreferences() {
        val answers = mutableMapOf(SetupGuide.SOURCE to "other", SetupGuide.LAN to "ready",
            SetupGuide.ACCESS to "upstream", SetupGuide.INTERNET to "upstream", SetupGuide.KEEP to "online",
            SetupGuide.TLS to "domain", SetupGuide.DNS to "ready")
        SetupGuide.invalidate("network", answers)
        assertFalse(answers.containsKey(SetupGuide.ACCESS))
        assertFalse(answers.containsKey(SetupGuide.DNS))
        SetupGuide.invalidate("car", answers)
        assertFalse(answers.containsKey(SetupGuide.SOURCE))
    }
    @Test fun summaryDoesNotFinishBeforeRequiredLocalFeaturesMatchTheAnswers() {
        val ready = SetupGuide.Readiness(true, true, true, false, false, true, true)
        val answers = mapOf(SetupGuide.KEEP to "online", SetupGuide.TLS to "domain", SetupGuide.DNS to "public")
        assertNull(SetupGuide.pending(Car.TESLA, Network.THIS_HOTSPOT, true, answers, ready))
        assertEquals(SetupGuide.Pending.USB, SetupGuide.pending(Car.TESLA, Network.THIS_HOTSPOT, true, answers, ready.copy(usb = false)))
        assertEquals(SetupGuide.Pending.TESLA_ACCESS, SetupGuide.pending(Car.TESLA, Network.THIS_HOTSPOT, true, answers, ready.copy(rootActive = false)))
        assertEquals(SetupGuide.Pending.OFFLINE_CHECKS, SetupGuide.pending(Car.TESLA, Network.THIS_HOTSPOT, true, answers, ready.copy(offlineChecks = true)))
        assertEquals(SetupGuide.Pending.TLS, SetupGuide.pending(Car.TESLA, Network.MIFI, false, answers, ready.copy(tlsUsable = false)))
    }
    @Test fun localCertificateChoiceDoesNotLeavePreviousOwnCertificateEnabled() {
        val ready = SetupGuide.Readiness(true, false, false, false, false, true, true)
        val answers = mapOf(SetupGuide.TLS to "local")
        assertEquals(SetupGuide.Pending.LOCAL_TLS, SetupGuide.pending(Car.OTHER, Network.MIFI, false, answers, ready))
        assertNull(SetupGuide.pending(Car.OTHER, Network.MIFI, false, answers, ready.copy(tlsEnabled = false)))
    }
    @Test fun offlineHotspotDnsMustActuallyRunBeforeCompletion() {
        val answers = mapOf(SetupGuide.TLS to "domain", SetupGuide.DNS to "hotspot")
        val ready = SetupGuide.Readiness(true, false, false, false, false, true, true)
        assertEquals(SetupGuide.Pending.DNS, SetupGuide.pending(Car.OTHER, Network.THIS_HOTSPOT, false, answers, ready))
        assertNull(SetupGuide.pending(Car.OTHER, Network.THIS_HOTSPOT, false, answers, ready.copy(dnsActive = true)))
    }

    @Test fun usbPageOptOutUpdatesSummaryAndInvalidatesTeslaInternetConfirmation() {
        val answers = mutableMapOf(SetupGuide.INTERNET to "usb", SetupGuide.KEEP to "online")
        SetupGuide.reconcileUsbChoice(answers, false)
        assertEquals("offline", answers[SetupGuide.INTERNET])
        assertFalse(answers.containsKey(SetupGuide.KEEP))
        answers[SetupGuide.INTERNET] = "upstream"
        SetupGuide.reconcileUsbChoice(answers, false)
        assertEquals("upstream", answers[SetupGuide.INTERNET])
        SetupGuide.reconcileUsbChoice(answers, true)
        assertEquals("usb", answers[SetupGuide.INTERNET])
    }

    @Test fun httpRouteDoesNotRequireRootDomainOrDnsButStillRequiresLanAndKeep() {
        val answers=mapOf(SetupGuide.ACCESS to "http",SetupGuide.LAN to "ready",SetupGuide.KEEP to "verified")
        for (network in listOf(Network.THIS_HOTSPOT,Network.MIFI)) {
            assertNull(problem(SetupGuide.ACCESS,network=network,answers=answers))
            assertNull(problem(SetupGuide.KEEP,network=network,answers=answers))
            for (step in listOf(SetupGuide.TLS,SetupGuide.DNS))
                assertFalse(SetupGuide.included(step,Car.TESLA,network,IphoneLink.WIRELESS,false,true,answers))
            assertTrue(SetupGuide.included(SetupGuide.HTTP_CHECK,Car.TESLA,network,IphoneLink.WIRELESS,false,true,answers))
        }
        assertNotNull(problem(SetupGuide.LAN,answers=answers+(SetupGuide.LAN to "missing")))
        assertNotNull(problem(SetupGuide.KEEP,answers=answers+(SetupGuide.KEEP to "offline")))
        assertNotNull(problem(SetupGuide.HTTP_CHECK,answers=answers))
        assertNull(problem(SetupGuide.HTTP_CHECK,answers=answers+(SetupGuide.HTTP_CHECK to "ready")))
    }
    @Test fun httpSummaryChecksRuntimeAndUsbInsteadOfOldCertificateFlags() {
        val answers=mapOf(SetupGuide.ACCESS to "http",SetupGuide.TLS to "domain",SetupGuide.DNS to "hotspot")
        val ready=SetupGuide.Readiness(true,false,false,false,false,false,false,httpReady=true)
        assertNull(SetupGuide.pending(Car.TESLA,Network.THIS_HOTSPOT,false,answers,ready))
        assertEquals(SetupGuide.Pending.HTTP,SetupGuide.pending(Car.TESLA,Network.THIS_HOTSPOT,false,answers,ready.copy(httpReady=false)))
        assertEquals(SetupGuide.Pending.USB,SetupGuide.pending(Car.TESLA,Network.MIFI,true,answers,ready.copy(usb=false)))
    }
    @Test fun routeSwitchAndRuntimeChangeInvalidateOnlyRelevantConfirmations() {
        val answers=mutableMapOf(SetupGuide.ACCESS to "http",SetupGuide.HTTP_CHECK to "ready",SetupGuide.HTTP_PROOF to "generation-1",
            SetupGuide.TLS to "domain",SetupGuide.DNS to "public",SetupGuide.SOURCE to "other",SetupGuide.STARTUP to "manual")
        SetupGuide.reconcileHttpConfirmation(answers,"generation-1",true)
        assertEquals("ready",answers[SetupGuide.HTTP_CHECK])
        SetupGuide.reconcileHttpConfirmation(answers,"generation-2",true)
        assertFalse(answers.containsKey(SetupGuide.HTTP_CHECK))
        answers[SetupGuide.HTTP_CHECK]="ready"; answers[SetupGuide.HTTP_PROOF]="generation-2"
        SetupGuide.reconcileHttpConfirmation(answers,"generation-2",false)
        assertFalse(answers.containsKey(SetupGuide.HTTP_CHECK))
        SetupGuide.invalidate(SetupGuide.ACCESS,answers)
        assertFalse(answers.containsKey(SetupGuide.TLS));assertFalse(answers.containsKey(SetupGuide.DNS))
        assertEquals("other",answers[SetupGuide.SOURCE]);assertEquals("manual",answers[SetupGuide.STARTUP])
    }
    @Test fun missingRuntimeProofCannotSurviveDraftRestore() {
        val answers=mutableMapOf(SetupGuide.HTTP_CHECK to "ready")
        SetupGuide.reconcileHttpConfirmation(answers,null,true)
        assertFalse(answers.containsKey(SetupGuide.HTTP_CHECK))
    }
    @Test fun startupIntentDoesNotClaimColdBootWasTested() {
        assertNotNull(problem(SetupGuide.STARTUP))
        assertNull(problem(SetupGuide.STARTUP,answers=mapOf(SetupGuide.STARTUP to "manual")))
        assertNull(problem(SetupGuide.STARTUP,answers=mapOf(SetupGuide.STARTUP to "automatic")))
    }

}
