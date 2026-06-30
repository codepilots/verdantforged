#!/usr/bin/env python3
"""Template for an end-to-end file-upload test against VerdantForged.

Fill in:
- task_prompt
- input file contents / filenames
- Stripe secret retrieval if you don't use the control-plane SSM path
- result key handling if you want to decrypt the returned artifact locally
"""

import base64
import json
import os
import secrets
import textwrap
import time
from dataclasses import dataclass

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

BROKER = os.environ.get("BROKER_URL", "https://verdant.codepilots.co.uk")

@dataclass
class FileItem:
    filename: str
    content_type: str
    plaintext: bytes


def encrypt_for_worker(job_id: str, worker_pubkey_b64: str, item: FileItem) -> bytes:
    worker_public = X25519PublicKey.from_public_bytes(base64.b64decode(worker_pubkey_b64))
    ephemeral = X25519PrivateKey.generate()
    ephemeral_pub = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    shared = ephemeral.exchange(worker_public)
    aad = f"verdantforged-file-v1\0input\0{job_id}\0{item.filename}".encode()
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"verdantforged-file-key-v1\0" + aad,
    ).derive(shared)
    nonce = os.urandom(12)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, item.plaintext, aad)
    return ephemeral_pub + nonce + ciphertext


def main():
    # Replace with your own task and inputs.
    task_prompt = textwrap.dedent(
        """
        You are a security-focused code reviewer. Inspect the attached files and
        return findings with severity, category, description, and fix.
        """
    ).strip()

    files = [
        FileItem("example.py", "text/x-python", b"print('hello')\n"),
        FileItem("notes.md", "text/markdown", b"# example\n"),
    ]

    # 1) Create job
    # job_payload = {...}
    # r = requests.post(f"{BROKER}/v1/jobs", json=job_payload)
    # job_id = r.json()["job_id"]

    # 2) Poll until awaiting_inputs and fetch upload URLs
    # 3) Encrypt and PUT each file
    # 4) POST /ready
    # 5) Poll until completed
    # 6) Fetch /artifacts and decrypt result
    raise SystemExit("Fill in broker-specific request details.")


if __name__ == "__main__":
    main()
