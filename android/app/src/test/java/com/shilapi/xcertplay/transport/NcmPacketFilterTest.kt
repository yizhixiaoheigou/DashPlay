package com.shilapi.xcertplay.transport

import org.junit.Assert.*
import org.junit.Test

class NcmPacketFilterTest {
    private fun config(id: Int, control: Int = 3, capabilities: Int = 0xbb): ByteArray =
        byteArrayOf(9, 2, 24, 0, 1, id.toByte(), 0, 0x80.toByte(), 50,
            9, 4, control.toByte(), 0, 1, 2, 0x0d, 0, 0,
            6, 0x24, 0x1a, 0, 1, capabilities.toByte())

    @Test fun supportedInterfaceReceivesDirectedBroadcastAndIpv6Multicast() {
        val mask = NcmPacketFilter.receiveMask(config(6), 6, 3)
        assertEquals(0x0e, mask)
        assertEquals(0, mask!! and 1) // no promiscuous reception
    }

    @Test fun inactiveConfigurationCannotSupplyCapabilities() {
        val raw = config(5) + config(6, capabilities = 0xba)
        assertNull(NcmPacketFilter.receiveMask(raw, 6, 3))
        assertEquals(0x0e, NcmPacketFilter.receiveMask(raw, 5, 3))
    }

    @Test fun activeConfigurationCanEnableSupportAbsentFromFirstConfiguration() {
        val raw = config(5, capabilities = 0xba) + config(6)
        assertEquals(0x0e, NcmPacketFilter.receiveMask(raw, 6, 3))
    }

    @Test fun unrelatedInterfaceOrAlternateCannotEnableFiltering() {
        assertNull(NcmPacketFilter.receiveMask(config(6, control = 5), 6, 3))
        val alternate = config(6).also { it[12] = 1 }
        assertNull(NcmPacketFilter.receiveMask(alternate, 6, 3))
        val data = config(6).also { it[14] = 0x0a }
        assertNull(NcmPacketFilter.receiveMask(data, 6, 3))
    }

    @Test fun missingOrTruncatedCapabilitiesDoNotSendAnUnsupportedRequest() {
        assertNull(NcmPacketFilter.receiveMask(byteArrayOf(), 6, 3))
        assertNull(NcmPacketFilter.receiveMask(config(6).copyOf(23), 6, 3))
        val short = config(6).copyOf(23).also { it[2] = 23; it[18] = 5 }
        assertNull(NcmPacketFilter.receiveMask(short, 6, 3))
    }

    @Test fun malformedDescriptorAndConfigurationBoundaryAreRejected() {
        val zero = config(6).also { it[9] = 0 }
        assertNull(NcmPacketFilter.receiveMask(zero, 6, 3))
        val outside = config(6).also { it[2] = 18 }
        assertNull(NcmPacketFilter.receiveMask(outside, 6, 3))
        val crossing = config(6).also { it[2] = 23 }
        assertNull(NcmPacketFilter.receiveMask(crossing, 6, 3))
    }
}
