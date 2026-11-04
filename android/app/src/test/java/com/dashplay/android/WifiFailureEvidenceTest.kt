package com.dashplay.android

import org.junit.Assert.*
import org.junit.Test

class WifiFailureEvidenceTest {
    class Config(private val status: Any?) { fun getNetworkSelectionStatus(): Any? = status }
    class Status(private val temporary: Boolean, private val reason: Int) {
        fun isNetworkTemporaryDisabled() = temporary
        fun getNetworkSelectionDisableReason() = reason
        companion object { const val NETWORK_SELECTION_DISABLED_ASSOCIATION_REJECTION = 42 }
    }
    class RenamedStatus {
        fun isNetworkTemporaryDisabled() = true
        fun getNetworkSelectionDisableReason() = 73
        companion object { const val DISABLED_ASSOCIATION_REJECTION = 73 }
    }
    class UnknownReason {
        fun isNetworkTemporaryDisabled() = true
        fun getNetworkSelectionDisableReason() = 2
    }
    class BlockedConfig { fun getNetworkSelectionStatus(): Any = throw SecurityException("unavailable") }

    @Test fun onlyExplicitTemporaryAssociationRejectionAllowsRecovery() {
        assertEquals(true, WifiFailureEvidence.temporaryAssociationRejected(Config(Status(true,42))))
        assertEquals(false, WifiFailureEvidence.temporaryAssociationRejected(Config(Status(false,42))))
        assertEquals(false, WifiFailureEvidence.temporaryAssociationRejected(Config(Status(true,43))))
    }
    @Test fun platformReasonNamesAreReadWithoutAssumingNumericValues() {
        assertEquals(true, WifiFailureEvidence.temporaryAssociationRejected(Config(RenamedStatus())))
        assertNull(WifiFailureEvidence.temporaryAssociationRejected(Config(UnknownReason())))
    }
    @Test fun missingBlockedAndNullObservationsNeverAuthorizeRecovery() {
        assertNull(WifiFailureEvidence.temporaryAssociationRejected(Any()))
        assertNull(WifiFailureEvidence.temporaryAssociationRejected(BlockedConfig()))
        assertNull(WifiFailureEvidence.temporaryAssociationRejected(Config(null)))
    }
}
