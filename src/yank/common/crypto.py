"""
Encryption module for clipboard-sync

Uses AES-256-GCM for authenticated encryption.
"""
import os
import hashlib
import secrets
from typing import Tuple

# Use cryptography library if available, fallback to basic implementation
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False
    print("WARNING: cryptography not installed. Run: pip install cryptography")


# Constants
NONCE_SIZE = 12  # 96 bits for GCM
TAG_SIZE = 16    # 128-bit authentication tag
KEY_SIZE = 32    # 256-bit key


def derive_key(shared_secret: str, salt: bytes = None) -> Tuple[bytes, bytes]:
    """
    Derive an encryption key from a shared secret using PBKDF2

    Args:
        shared_secret: The shared secret string
        salt: Optional salt bytes (generated if not provided)

    Returns:
        (key, salt) tuple
    """
    if salt is None:
        salt = os.urandom(16)

    # Use PBKDF2 with SHA256
    key = hashlib.pbkdf2_hmac(
        'sha256',
        shared_secret.encode('utf-8'),
        salt,
        iterations=100000,
        dklen=KEY_SIZE
    )

    return key, salt


def generate_key() -> bytes:
    """Generate a random 256-bit key"""
    return secrets.token_bytes(KEY_SIZE)


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """
    Encrypt data using AES-256-GCM

    Args:
        plaintext: Data to encrypt
        key: 32-byte encryption key

    Returns:
        nonce (12 bytes) + ciphertext + tag (16 bytes)
    """
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError("cryptography library required for encryption")

    nonce = os.urandom(NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    # Return nonce + ciphertext (tag is appended by AESGCM)
    return nonce + ciphertext


def decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """
    Decrypt data using AES-256-GCM

    Args:
        ciphertext: nonce (12 bytes) + encrypted data + tag (16 bytes)
        key: 32-byte encryption key

    Returns:
        Decrypted plaintext

    Raises:
        ValueError: If authentication fails (tampered data)
    """
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError("cryptography library required for decryption")

    if len(ciphertext) < NONCE_SIZE + TAG_SIZE:
        raise ValueError("Ciphertext too short")

    nonce = ciphertext[:NONCE_SIZE]
    actual_ciphertext = ciphertext[NONCE_SIZE:]

    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, actual_ciphertext, None)
        return plaintext
    except Exception as e:
        raise ValueError(f"Decryption failed - data may be corrupted or tampered: {e}")


def generate_pin() -> str:
    """Generate a 6-digit PIN for pairing"""
    return f"{secrets.randbelow(1000000):06d}"


def hash_token(token: str) -> str:
    """Hash a token for storage (don't store raw tokens)"""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()
