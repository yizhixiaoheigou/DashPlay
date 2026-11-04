package com.dashplay.android

import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Test

class PhoneIdentityTest {
    private val address = "02:00:00:00:00:01"
    @Test fun anOldAutomaticallyDiscoveredPhoneClassEntryIsNotEvidence() {
        val old = Json.decodeFromString<SavedPhone>("""{"address":"$address","name":"Android phone"}""")
        assertFalse(PhoneIdentity.eligible(false, old.name, false, old, false))
        assertFalse(PhoneIdentity.remembered(old, false))
    }
    @Test fun customNamedIphoneCanBeDiscoveredByServiceOrRememberedAuthentication() {
        assertTrue(PhoneIdentity.eligible(false, "Personal phone", true, null, false))
        assertTrue(PhoneIdentity.eligible(false, "Renamed", false, SavedPhone(address,"Old",setOf("verified")), false))
        assertTrue(PhoneIdentity.eligible(false, "iPhone", false, null, false))
    }
    @Test fun explicitChoicesSurviveRestartAndAnotherPhoneBecomingPreferred() {
        val policy=PhonePolicy();policy.add(address,"Custom name");policy.add("02:00:00:00:00:02","Second")
        val saved=Json.decodeFromString<PhoneState>(Json.encodeToString(policy.state)).phones.first()
        assertTrue(saved.manuallyAdded)
        assertTrue(PhoneIdentity.eligible(false,"Renamed",false,saved,false))
        assertTrue(PhoneIdentity.eligible(false,"Legacy selected",false,null,true))
    }
    @Test fun lowEnergyOnlyAccessoriesAreNeverWirelessCarplayCandidates() {
        val accessory = SavedPhone(address,"remote",manuallyAdded=true)
        assertFalse(PhoneIdentity.eligible(true,"iPhone remote",true,accessory,true))
        assertFalse(PhoneIdentity.visible(accessory,true,false,true))
    }
    @Test fun unpairedVerifiedPhonesStayVisibleButRemovedAndUnverifiedRecordsDoNot() {
        val phone = SavedPhone(address,"Custom name",setOf("verified"))
        assertTrue(PhoneIdentity.visible(phone,false,false,false))
        assertFalse(PhoneIdentity.visible(phone.copy(removed=true),false,true,true))
        assertFalse(PhoneIdentity.visible(SavedPhone(address,"Android phone"),false,false,false))
    }
    @Test fun discoveryDoesNotConvertAnUnverifiedRecordIntoAUserChoice() {
        val policy=PhonePolicy();policy.discover(address,"Android phone")
        assertFalse(policy.state.phones.single().manuallyAdded)
        assertFalse(PhoneIdentity.remembered(policy.state.phones.single(),false))
    }
}
