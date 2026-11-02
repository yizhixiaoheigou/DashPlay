package com.dashplay.android

import java.io.File
import java.io.FileOutputStream
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.PrivateKey
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.RSAPublicKeySpec
import org.bouncycastle.asn1.pkcs.PrivateKeyInfo
import org.bouncycastle.asn1.pkcs.PKCSObjectIdentifiers
import org.bouncycastle.asn1.pkcs.RSAPrivateKey

/** Reuse the ACME account after process restarts. A damaged existing key is never silently replaced. */
internal class AcmeAccountStore(private val directory: File,
    private val decodePrivate: (ByteArray) -> PrivateKey = { KeyFactory.getInstance("RSA").generatePrivate(PKCS8EncodedKeySpec(it)) }) {
    @Synchronized fun loadOrCreate(): KeyPair {
        val file = File(directory, "account.pk8")
        try {
            if (file.exists()) {
                require(file.length() in 256..16_384)
                val factory = KeyFactory.getInstance("RSA")
                val encoded = file.readBytes()
                // Android 5's RSA KeyFactory may return only RSAPrivateKey, even for a full PKCS#8 CRT key.
                // Read n/e from the stored standard encoding, not a provider-specific Java interface.
                val info = PrivateKeyInfo.getInstance(encoded)
                require(info.privateKeyAlgorithm.algorithm == PKCSObjectIdentifiers.rsaEncryption)
                val rsa = RSAPrivateKey.getInstance(info.parsePrivateKey())
                require(rsa.modulus.bitLength() >= 2048 && rsa.publicExponent.signum() > 0)
                return KeyPair(factory.generatePublic(RSAPublicKeySpec(rsa.modulus, rsa.publicExponent)), decodePrivate(encoded))
            }
            val key = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
            privateAtomicWrite(file, key.private.encoded)
            return key
        } catch (error: Exception) { throw CertificateFailure("account_storage", causeKind = error.javaClass.simpleName) }
    }
}

internal fun privateAtomicWrite(file: File, bytes: ByteArray) {
    val parent = file.parentFile ?: throw CertificateFailure("storage")
    check(parent.isDirectory || parent.mkdirs())
    val scratch = File.createTempFile(".write-", ".tmp", parent)
    try {
        check(scratch.setReadable(false, false) && scratch.setWritable(false, false) &&
            scratch.setReadable(true, true) && scratch.setWritable(true, true))
        FileOutputStream(scratch).use { it.write(bytes); it.fd.sync() }
        check(scratch.renameTo(file))
    } finally { scratch.delete() }
}
