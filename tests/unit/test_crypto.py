"""
Unit tests for crypto.py - Encryption and decryption
"""
import pytest
import os

from yank.common.crypto import encrypt, decrypt, generate_key, derive_key


class TestEncryptDecrypt:
    """Tests for encrypt/decrypt functions"""

    def test_encrypt_decrypt_roundtrip(self, encryption_key):
        plaintext = b"Hello, World! This is secret data."
        ciphertext = encrypt(plaintext, encryption_key)

        # Ciphertext should be different from plaintext
        assert ciphertext != plaintext

        # Decrypt should recover original
        decrypted = decrypt(ciphertext, encryption_key)
        assert decrypted == plaintext

    def test_encrypt_empty_data(self, encryption_key):
        plaintext = b""
        ciphertext = encrypt(plaintext, encryption_key)
        decrypted = decrypt(ciphertext, encryption_key)
        assert decrypted == plaintext

    def test_encrypt_large_data(self, encryption_key):
        # Test with 1MB of data
        plaintext = os.urandom(1024 * 1024)
        ciphertext = encrypt(plaintext, encryption_key)
        decrypted = decrypt(ciphertext, encryption_key)
        assert decrypted == plaintext

    def test_wrong_key_fails(self, encryption_key):
        plaintext = b"Secret message"
        ciphertext = encrypt(plaintext, encryption_key)

        # Try to decrypt with wrong key
        wrong_key = b"wrong_key_0123456789abcdef01234"
        with pytest.raises(Exception):
            decrypt(ciphertext, wrong_key)

    def test_tampered_ciphertext_fails(self, encryption_key):
        plaintext = b"Secret message"
        ciphertext = encrypt(plaintext, encryption_key)

        # Tamper with ciphertext
        tampered = bytearray(ciphertext)
        tampered[-1] ^= 0xFF  # Flip bits in last byte
        tampered = bytes(tampered)

        with pytest.raises(Exception):
            decrypt(tampered, encryption_key)

    def test_different_encryptions_produce_different_ciphertext(self, encryption_key):
        plaintext = b"Same message"

        ciphertext1 = encrypt(plaintext, encryption_key)
        ciphertext2 = encrypt(plaintext, encryption_key)

        # Should produce different ciphertext due to random nonce
        assert ciphertext1 != ciphertext2

        # But both should decrypt to same plaintext
        assert decrypt(ciphertext1, encryption_key) == plaintext
        assert decrypt(ciphertext2, encryption_key) == plaintext


class TestKeyGeneration:
    """Tests for key generation functions"""

    def test_generate_key_length(self):
        key = generate_key()
        assert len(key) == 32  # 256 bits

    def test_generate_key_random(self):
        key1 = generate_key()
        key2 = generate_key()
        assert key1 != key2

    def test_derive_key_deterministic(self):
        password = "my_secure_password"
        salt = b"fixed_salt_12345"

        key1 = derive_key(password, salt)
        key2 = derive_key(password, salt)

        # Same inputs should produce same key
        assert key1 == key2

    def test_derive_key_different_salts(self):
        password = "my_secure_password"
        salt1 = b"salt_one_1234567"
        salt2 = b"salt_two_7654321"

        key1 = derive_key(password, salt1)
        key2 = derive_key(password, salt2)

        # Different salts should produce different keys
        assert key1 != key2

    def test_derive_key_length(self):
        key, salt = derive_key("password", b"salt12345678901")
        assert len(key) == 32  # 256 bits


class TestEdgeCases:
    """Edge case tests"""

    def test_binary_data_with_null_bytes(self, encryption_key):
        plaintext = b"\x00\x01\x02\x00\x00\xff\xfe\x00"
        ciphertext = encrypt(plaintext, encryption_key)
        decrypted = decrypt(ciphertext, encryption_key)
        assert decrypted == plaintext

    def test_unicode_data(self, encryption_key):
        text = "Hello \u4e16\u754c \U0001F600"  # Chinese + emoji
        plaintext = text.encode('utf-8')
        ciphertext = encrypt(plaintext, encryption_key)
        decrypted = decrypt(ciphertext, encryption_key)
        assert decrypted.decode('utf-8') == text
