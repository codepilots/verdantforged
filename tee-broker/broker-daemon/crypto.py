"""Cryptographic helpers for the VerdantForged TEE broker.

This module implements the cryptographic primitives required by the
tee-broker-pattern spec:

  - X25519 + ChaCha20-Poly1305 for result encryption
    (requestor provides a public key; broker encrypts the result to it)

  - Ed25519 for broker_signature
    (broker signs the result_hash + skill_hash + input_hash so the
    requester can verify the result came from the attested enclave)

  - Ed25519 for requester_sig verification
    (requestor signs the request body; broker verifies before accepting)

For the demo, key material is persisted to a file on the control plane
(mode 0600). In production this would be derived from the SEV-SNP
attestation report's measurement.
"""
import base64, hashlib, json, os
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives import serialization


KEY_DIR = "/opt/broker-daemon/keys"
BROKER_X25519_PRIV_PATH = f"{KEY_DIR}/broker_x25519.priv"
BROKER_ED25519_PRIV_PATH = f"{KEY_DIR}/broker_ed25519.priv"


def _ensure_keys():
    """Generate broker keypair on first use, persist to disk (mode 0600)."""
    os.makedirs(KEY_DIR, mode=0o700, exist_ok=True)
    if not os.path.exists(BROKER_X25519_PRIV_PATH):
        priv = X25519PrivateKey.generate()
        with open(BROKER_X25519_PRIV_PATH, "wb") as f:
            f.write(priv.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        os.chmod(BROKER_X25519_PRIV_PATH, 0o600)
    if not os.path.exists(BROKER_ED25519_PRIV_PATH):
        priv = Ed25519PrivateKey.generate()
        with open(BROKER_ED25519_PRIV_PATH, "wb") as f:
            f.write(priv.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        os.chmod(BROKER_ED25519_PRIV_PATH, 0o600)


def broker_x25519_pubkey_b64() -> str:
    _ensure_keys()
    with open(BROKER_X25519_PRIV_PATH, "rb") as f:
        priv = X25519PrivateKey.from_private_bytes(f.read())
    return base64.b64encode(priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )).decode()


def broker_ed25519_pubkey_b64() -> str:
    _ensure_keys()
    with open(BROKER_ED25519_PRIV_PATH, "rb") as f:
        priv = Ed25519PrivateKey.from_private_bytes(f.read())
    return base64.b64encode(priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )).decode()


def encrypt_result_for_pubkey(result_bytes: bytes, requester_pubkey_b64: str) -> str:
    """Encrypt result bytes to the requester's X25519 public key using
    ephemeral-static ECDH + ChaCha20-Poly1305.

    Output format: base64(ephemeral_pubkey_32 || ciphertext_with_tag)
    """
    requester_pubkey_bytes = base64.b64decode(requester_pubkey_b64)
    if len(requester_pubkey_bytes) != 32:
        raise ValueError(f"requester_pubkey must be 32 bytes (got {len(requester_pubkey_bytes)})")
    requester_pubkey = X25519PublicKey.from_public_bytes(requester_pubkey_bytes)

    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(requester_pubkey)
    aead = ChaCha20Poly1305(shared)
    # 12-byte random nonce (cryptography >= 41 requires explicit nonce)
    nonce = os.urandom(12)
    ciphertext = aead.encrypt(nonce, result_bytes, b"verdantforged-result")

    eph_pub = ephemeral.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    # Output format: base64(ephemeral_pubkey_32 || nonce_12 || ciphertext_with_tag)
    return base64.b64encode(eph_pub + nonce + ciphertext).decode()


def decrypt_result_for_privkey(encrypted_b64: str, requester_privkey) -> bytes:
    """Decrypt a result using the requester's X25519 private key. (Helper for
    testing — requester would do this client-side.)"""
    blob = base64.b64decode(encrypted_b64)
    eph_pub_bytes = blob[:32]
    ciphertext = blob[32:]
    eph_pub = X25519PublicKey.from_public_bytes(eph_pub_bytes)
    shared = requester_privkey.exchange(eph_pub)
    aead = ChaCha20Poly1305(shared)
    nonce = blob[32:44]
    ciphertext = blob[44:]
    return aead.decrypt(nonce, ciphertext, b"verdantforged-result")


def broker_sign(message: bytes) -> str:
    """Sign a message with the broker's Ed25519 key. Returns base64 signature."""
    _ensure_keys()
    with open(BROKER_ED25519_PRIV_PATH, "rb") as f:
        priv = Ed25519PrivateKey.from_private_bytes(f.read())
    sig = priv.sign(message)
    return base64.b64encode(sig).decode()


def verify_broker_signature(message: bytes, signature_b64: str) -> bool:
    """Verify a signature using the broker's Ed25519 public key."""
    _ensure_keys()
    with open(BROKER_ED25519_PRIV_PATH, "rb") as f:
        priv = Ed25519PrivateKey.from_private_bytes(f.read())
    pub = priv.public_key()
    try:
        pub.verify(base64.b64decode(signature_b64), message)
        return True
    except Exception:
        return False


# ── requester_sig verification ─────────────────────────────────────

def make_signing_payload(skill_hash: str, input_hash: str, result_pubkey_b64: str,
                          stripe_pi_id: str, timestamp: str) -> bytes:
    """Canonical bytes for what the requester signs."""
    return f"{skill_hash}|{input_hash}|{result_pubkey_b64}|{stripe_pi_id}|{timestamp}".encode()


def verify_requester_sig(requester_pubkey_b64: str, signature_b64: str,
                          skill_hash: str, input_hash: str,
                          result_pubkey_b64: str, stripe_pi_id: str,
                          timestamp: str) -> bool:
    """Verify the requester signed the canonical request payload with their
    Ed25519 key.

    If `requester_pubkey_b64` or `signature_b64` are missing/empty/malformed,
    returns False. The caller decides whether to reject the request entirely
    (recommended) or accept unsigned jobs (demo fallback).
    """
    if not requester_pubkey_b64 or not signature_b64:
        return False
    try:
        pub_bytes = base64.b64decode(requester_pubkey_b64)
        sig_bytes = base64.b64decode(signature_b64)
        if len(pub_bytes) != 32:
            return False
        pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
        msg = make_signing_payload(skill_hash, input_hash, result_pubkey_b64,
                                    stripe_pi_id, timestamp)
        pub.verify(sig_bytes, msg)
        return True
    except Exception:
        return False


def hash_field(s: str) -> str:
    """SHA256 hex of a string, used for skill_hash and input_hash."""
    return hashlib.sha256(s.encode()).hexdigest()