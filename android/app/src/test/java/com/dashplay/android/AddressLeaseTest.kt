package com.dashplay.android
import org.junit.Assert.*
import org.junit.Test

class AddressLeaseTest {
    @Test fun `address lease closes once and reports revoke to still-bound runtime listeners`() {
        var released=0;var revoked=0;var changed=0
        val lease=UsbVpnService.AddressLease("100.96.0.1",false){released++}
        lease.onClosed { revoked++ };lease.onChanged { changed++ }
        lease.changed();assertEquals(1,changed);assertTrue(lease.isOpen)
        lease.release();lease.release();lease.changed()
        assertEquals(1,released);assertEquals(1,revoked);assertEquals(1,changed);assertFalse(lease.isOpen)
        lease.onClosed { revoked++ };assertEquals(2,revoked)
    }
}
