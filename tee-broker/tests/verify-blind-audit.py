#!/usr/bin/env python3
"""Verify Showcase Skill 3: blind-audit.

The blind-audit skill is the crypto pillar of the hackathon demo: source
code is encrypted to the worker's X25519 public key, decrypted inside the
TEE, audited by the LLM, encrypted back to the requester, and signed.

This test file verifies all four moving parts without needing a live
broker on AWS:

  1. Worker X25519 pubkey persistence + publishing to EFS
  2. /v1/discover surfaces the worker's enclave_pubkey
  3. Skill manifest declares `decrypt_input: true` and the column is
     persisted + returned
  4. submit_job includes `skill_decrypt_input` in the worker envelope
  5. poller.execute_in_envelope decrypts the input when the flag is set

The tests run offline (no AWS, no live broker) using aiohttp's TestServer
against a temp BROKER_EFS_MOUNT, mirroring verify-skill-registration.py.
"""
import os, sys, json, hashlib, base64, tempfile, shutil, asyncio
from pathlib import Path

# Set up temp env BEFORE importing daemon / poller modules
TMP_ROOT = Path(tempfile.mkdtemp(prefix="hermes-blind-audit-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TMP_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
# Skill registration needs an auth key (VULN-S2 fix)
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-blind-audit"
# Force demo mode so verify_payment_intent short-circuits with (True,
# "stripe_disabled", 0). Matches verify-attestation-verifier.py:54 and
# verify-input-attachments.py:140 — without this, a host that exports
# STRIPE_SECRET_KEY=*** would short-circuit to the live-mode cost guard
# in daemon.py:1938 and reject every submit_job with "insufficient
# payment" (since the demo pi_test_* PIs don't exist in real Stripe).
# Chose pop-then-empty-set rather than pop-then-delete so subsequent
# daemon.STRIPE_SECRET_KEY reads see a consistent empty string.
os.environ.pop("STRIPE_SECRET_KEY", None)
os.environ["STRIPE_SECRET_KEY"] = ""
SKILLS_AUTH_HEADER={"Authorization": "Bearer test-skills-key-blind-audit"}
# Point worker key + log dirs at the temp mount so the poller does not try
# to write to /opt/worker (which is not writable as a non-root test user).
TMP_KEYS = TMP_ROOT / "worker-keys"
TMP_KEYS.mkdir(parents=True, exist_ok=True)
os.environ["BROKER_WORKER_KEYS"] = str(TMP_KEYS)
os.environ["BROKER_EFS_LOGS"] = str(TMP_ROOT / "logs")

TESTS_DIR = Path(__file__).resolve().parent
WORKSPACE = TESTS_DIR.parent
BROKER_DIR = WORKSPACE / "broker-daemon"
WORKER_DIR = WORKSPACE / "worker"
sys.path.insert(0, str(BROKER_DIR))
sys.path.insert(0, str(WORKER_DIR))

import daemon  # noqa: E402
import poller  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

# Initialise the DB against the temp dir
daemon.init_db()
(daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)
# Mirror the directories the daemon expects.
(daemon.BROKER_EFS_MOUNT / "jobs" / "inbox").mkdir(parents=True, exist_ok=True)
(daemon.BROKER_EFS_MOUNT / "jobs" / "outbox").mkdir(parents=True, exist_ok=True)
(daemon.BROKER_EFS_MOUNT / "logs").mkdir(parents=True, exist_ok=True)

# cryptography imports (same as poller.py) for client-side encrypt/decrypt
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives import serialization


PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        print(f"[PASS] {label}")
        PASS += 1
    else:
        print(f"[FAIL] {label}  ({detail})")
        FAIL += 1


def good_hash(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


def good_manifest(name, **overrides):
    m = {
        "name": name,
        "version": "0.1.0",
        "description": f"Test skill {name}",
        "wasm_manifest_hash": good_hash(name),
        "entry_point": "main",
        "prompt_template": "You are a test skill. Input: {{input}}",
    }
    m.update(overrides)
    return m


def client_encrypt_to_pubkey(plaintext: bytes, worker_pubkey_b64: str) -> str:
    """Client-side: ephemeral X25519 + ChaCha20-Poly1305 to the worker pubkey.

    Output: base64(ephemeral_pubkey_32 || nonce_12 || ciphertext_with_tag).
    Matches the format the poller will recognise and decrypt.
    """
    worker_pub = X25519PublicKey.from_public_bytes(
        base64.b64decode(worker_pubkey_b64))
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(worker_pub)
    nonce = os.urandom(12)
    ciphertext = ChaCha20Poly1305(shared).encrypt(
        nonce, plaintext, b"verdantforged-input")
    eph_pub = eph.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return base64.b64encode(eph_pub + nonce + ciphertext).decode()


# =======================================================================
# 1. Worker X25519 pubkey persistence + publishing to EFS
# =======================================================================

def test_worker_publishes_x25519_pubkey_to_efs():
    """When the worker starts, it publishes its X25519 pubkey to EFS so
    the daemon can serve it via /v1/discover."""
    # Force a fresh key by deleting any persisted state
    keys_file = TMP_ROOT / "logs" / "worker-keys.json"
    if keys_file.exists():
        keys_file.unlink()

    # Call the worker's key publishing helper
    poller.publish_worker_keys()

    check("1a. worker-keys.json is created on EFS",
          keys_file.exists())
    if not keys_file.exists():
        return

    body = json.loads(keys_file.read_text())
    check("1b. worker-keys.json contains x25519_pubkey_b64 (32 bytes decoded)",
          len(base64.b64decode(body["x25519_pubkey_b64"])) == 32)
    check("1c. worker-keys.json contains ed25519_pubkey_b64 (32 bytes decoded)",
          len(base64.b64decode(body["ed25519_pubkey_b64"])) == 32)
    check("1d. worker-keys.json has created_at timestamp",
          "created_at" in body and body["created_at"])
    check("1e. worker-keys.json has a key_id field",
          "key_id" in body and body["key_id"])


def test_worker_keys_idempotent():
    """A second call to publish_worker_keys does NOT overwrite the existing
    keys (rotation would break clients already encrypting to the old pubkey).
    """
    keys_file = TMP_ROOT / "logs" / "worker-keys.json"
    original = keys_file.read_text()

    poller.publish_worker_keys()

    after = keys_file.read_text()
    check("1f. publish_worker_keys is idempotent — does not rotate keys",
          original == after)


# =======================================================================
# 2. /v1/discover surfaces enclave_pubkey
# =======================================================================

async def test_discover_includes_enclave_pubkey():
    """The daemon reads worker-keys.json and surfaces the X25519 pubkey as
    enclave_pubkey in /v1/discover."""
    # First, register the blind-audit skill so the discover list advertises it
    app = daemon.build_app()
    app.on_startup.clear()

    server = TestServer(app)
    async with TestClient(server) as client:
        # Register blind-audit with decrypt_input=true
        m = good_manifest(
            "blind-audit",
            description="Blind security audit — source code is encrypted, "
                        "even the broker can't read it",
            prompt_template=(
                "You are a security auditor. Carefully review the source "
                "code below. Identify:\n"
                "1. Injection vulnerabilities (SQLi, command injection, "
                "   XSS, template injection)\n"
                "2. Authentication / authorization flaws\n"
                "3. Cryptographic weaknesses (weak primitives, bad randomness, "
                "   missing auth)\n"
                "4. Input validation gaps\n"
                "Rate each finding severity 1-5. Source code:\n\n{{input}}"
            ),
            resource_limits={
                "max_fuel": 10_000_000,
                "max_duration_ms": 60_000,
                "max_memory_mb": 256,
            },
            decrypt_input=True,
        )
        resp = await client.post(
            "/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        check("2-prep. register blind-audit returns 201",
              resp.status == 201,
              f"status={resp.status} body={await resp.json()}")

        # Hit /v1/discover
        resp = await client.get("/v1/discover")
        body = await resp.json()
        att = body.get("attestation", {})
        check("2. /v1/discover has attestation.enclave_pubkey",
              "enclave_pubkey" in att)
        check("2b. enclave_pubkey is a non-empty base64 string",
              isinstance(att.get("enclave_pubkey"), str)
              and len(att.get("enclave_pubkey", "")) > 0)
        check("2c. enclave_pubkey decodes to 32 bytes (X25519 public key)",
              len(base64.b64decode(att["enclave_pubkey"])) == 32
              if att.get("enclave_pubkey") else False,
              f"enclave_pubkey={att.get('enclave_pubkey', '')[:40]}...")
        check("2d. enclave_pubkey matches what the worker published",
              att.get("enclave_pubkey") ==
              json.loads(
                  (TMP_ROOT / "logs" / "worker-keys.json").read_text()
              )["x25519_pubkey_b64"])
        keys = json.loads((TMP_ROOT / "logs" / "worker-keys.json").read_text())
        check("2e. /v1/discover surfaces worker_ed25519_pubkey",
              att.get("worker_ed25519_pubkey") == keys.get("ed25519_pubkey_b64"),
              f"att={att.get('worker_ed25519_pubkey')} keys={keys.get('ed25519_pubkey_b64')}")
        check("2f. /v1/discover surfaces nemoclaw_version",
              att.get("nemoclaw_version") == keys.get("nemoclaw_version"),
              f"att={att.get('nemoclaw_version')} keys={keys.get('nemoclaw_version')}")
        check("2g. /v1/discover surfaces nemoclaw_image",
              att.get("nemoclaw_image") == keys.get("nemoclaw_image"),
              f"att={att.get('nemoclaw_image')} keys={keys.get('nemoclaw_image')}")
        check("2h. /v1/discover surfaces nemoclaw_image_digest",
              att.get("nemoclaw_image_digest") == keys.get("nemoclaw_image_digest"),
              f"att={att.get('nemoclaw_image_digest')} keys={keys.get('nemoclaw_image_digest')}")
        check("2i. /v1/discover lists blind-audit in supported_skills",
              "blind-audit" in body.get("supported_skills", []),
              f"skills={body.get('supported_skills', [])}")


# =======================================================================
# 3. Skill manifest accepts decrypt_input flag, persisted + returned
# =======================================================================

async def test_skill_manifest_decrypt_input_flag():
    """Skill manifests can declare `decrypt_input: true` to tell the poller
    to decrypt the encrypted_data before sending it to the LLM."""
    app = daemon.build_app()
    app.on_startup.clear()
    server = TestServer(app)

    async with TestClient(server) as client:
        # 3a. decrypt_input defaults to False
        m = good_manifest("plain-skill",
                          wasm_manifest_hash=good_hash("plain-skill"))
        resp = await client.post(
            "/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        check("3a. omitted decrypt_input defaults to False",
              body.get("decrypt_input") is False,
              f"body={body}")

        # 3b. decrypt_input=true is accepted
        m = good_manifest("encrypted-skill",
                          wasm_manifest_hash=good_hash("encrypted-skill"),
                          decrypt_input=True)
        resp = await client.post(
            "/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        check("3b. decrypt_input=true is accepted and returned",
              resp.status == 201 and body.get("decrypt_input") is True,
              f"status={resp.status} body={body}")

        # 3c. decrypt_input=false is accepted (and different from true)
        m = good_manifest("false-flag-skill",
                          wasm_manifest_hash=good_hash("false-flag-skill"),
                          decrypt_input=False)
        resp = await client.post(
            "/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        check("3c. decrypt_input=false is accepted and returned",
              resp.status == 201 and body.get("decrypt_input") is False)

        # 3d. decrypt_input must be a boolean (not "yes" / 1 / etc.)
        m = good_manifest("bad-flag-skill",
                          wasm_manifest_hash=good_hash("bad-flag-skill"),
                          decrypt_input="yes")
        resp = await client.post(
            "/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        check("3d. decrypt_input='yes' (string) → 400",
              resp.status == 400,
              f"status={resp.status}")

        # 3e. GET /v1/skills/{name} returns decrypt_input
        resp = await client.get("/v1/skills/encrypted-skill")
        body = await resp.json()
        check("3e. GET /v1/skills/encrypted-skill returns decrypt_input=true",
              body.get("decrypt_input") is True)


# =======================================================================
# 4. submit_job includes skill_decrypt_input in the worker envelope
# =======================================================================

async def test_submit_job_envelope_carries_decrypt_flag():
    """submit_job looks up the registered skill, reads decrypt_input, and
    includes it in the envelope written to EFS inbox for the worker."""
    app = daemon.build_app()
    app.on_startup.clear()
    server = TestServer(app)
    async with TestClient(server) as client:
        # Register a skill with decrypt_input=true
        m = good_manifest("enc-flag-skill",
                          wasm_manifest_hash=good_hash("enc-flag-skill"),
                          decrypt_input=True)
        resp = await client.post(
            "/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        assert resp.status == 201, f"setup failed: {await resp.json()}"

        # Generate a real X25519 result pubkey (so the job is accepted)
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        req_priv = X25519PrivateKey.generate()
        req_pub_b64 = base64.b64encode(
            req_priv.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw)).decode()

        # Submit a job
        body = {
            "client_req_id": f"audit-{int.from_bytes(os.urandom(4), 'big')}",
            "encrypted_skill": "enc-flag-skill",
            "encrypted_data": "dummy-encrypted-data",
            "requester_sig": "0x",
            "result_pubkey": req_pub_b64,
            "stripe_pi_id": "pi_test_envelope",
        }
        resp = await client.post("/v1/jobs", json=body)
        check("4-prep. submit_job for enc-flag-skill returns 202",
              resp.status == 202,
              f"status={resp.status} body={await resp.json()}")
        if resp.status != 202:
            return
        first_resp = await resp.json()
        job_id = first_resp["job_id"]

        # Inspect the envelope in the inbox
        envelope_path = TMP_ROOT / "jobs" / "inbox" / f"{job_id}.json"
        check("4. envelope file exists in EFS inbox",
              envelope_path.exists())
        if not envelope_path.exists():
            return

        envelope = json.loads(envelope_path.read_text())
        check("4b. envelope has skill_decrypt_input=true",
              envelope.get("skill_decrypt_input") is True,
              f"envelope={envelope}")

        # Now register a plaintext skill and submit a job for it
        m = good_manifest("plain-skill-2",
                          wasm_manifest_hash=good_hash("plain-skill-2"))
        resp = await client.post(
            "/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        assert resp.status == 201

        # NOTE: do NOT reuse the variable name `body` for the response
        # dict — it shadows the request body we built at line 324, and
        # the second submit below would re-send the response as the
        # request (daemon would 400 with "missing fields: encrypted_data,
        # requester_sig, result_pubkey, stripe_pi_id"). Build a fresh
        # request body so both submits carry the full field set.
        plain_body = {
            "client_req_id": f"plain-{int.from_bytes(os.urandom(4), 'big')}",
            "encrypted_skill": "plain-skill-2",
            "encrypted_data": body["encrypted_data"],
            "requester_sig": body["requester_sig"],
            "result_pubkey": body["result_pubkey"],
            "stripe_pi_id": body["stripe_pi_id"],
        }
        resp = await client.post("/v1/jobs", json=plain_body)
        check("4c. submit_job for plain-skill-2 returns 202",
              resp.status == 202,
              f"status={resp.status} body={await resp.json()}")
        plain_resp = await resp.json()
        if "job_id" not in plain_resp:
            return
        job_id = plain_resp["job_id"]
        envelope_path = TMP_ROOT / "jobs" / "inbox" / f"{job_id}.json"
        envelope = json.loads(envelope_path.read_text())
        check("4d. envelope has skill_decrypt_input=false (default)",
              envelope.get("skill_decrypt_input") is False,
              f"envelope={envelope}")


# =======================================================================
# 5. poller.execute_in_envelope decrypts input when skill_decrypt_input=True
# =======================================================================

def test_poller_decrypts_input_when_flag_set():
    """The poller decrypts encrypted_data with the worker's X25519 privkey
    when skill_decrypt_input is true, then sends the plaintext to the LLM."""
    # Generate a "worker" keypair locally (the worker's actual key dir on
    # disk uses /opt/worker/keys — we monkey-patch the path for the test
    # by setting KEY_DIR in poller module).
    import cryptography.hazmat.primitives.serialization as ser
    worker_priv = X25519PrivateKey.generate()
    worker_pub_b64 = base64.b64encode(
        worker_priv.public_key().public_bytes(
            encoding=ser.Encoding.Raw, format=ser.PublicFormat.Raw)).decode()

    # Override the worker encryption key path so the poller uses OUR key
    test_key_dir = TMP_ROOT / "test-worker-keys"
    test_key_dir.mkdir(parents=True, exist_ok=True)
    test_priv_path = test_key_dir / "worker_encryption.priv"
    test_priv_path.write_bytes(worker_priv.private_bytes(
        encoding=ser.Encoding.Raw,
        format=ser.PrivateFormat.Raw,
        encryption_algorithm=ser.NoEncryption()))

    # Patch poller.KEY_DIR to point at our test key directory
    original_key_dir = poller.KEY_DIR
    poller.KEY_DIR = test_key_dir
    try:
        # Encrypt some plaintext to the worker pubkey
        plaintext = b"def hello():\n    return eval(input('> '))"
        encrypted = client_encrypt_to_pubkey(plaintext, worker_pub_b64)

        # skill_hash: the broker always emits this on every envelope (see
        # broker-daemon/daemon.py submit_job — resolve_skill_hash runs
        # unconditionally). The poller's skill_hash_missing guard at the
        # top of execute_in_envelope refuses to dispatch without it,
        # so we MUST populate it here for the no-llm-token / no-path
        # branch to actually run. Use SHA256(skill_name) to match the
        # broker's "latest-version-wins, falls back to sha256(name)"
        # policy for built-in stubs and unregistered skills.
        skill_hash_envelope = hashlib.sha256(b"blind-audit").hexdigest()

        env = {
            "job_id": f"job_blind_{int.from_bytes(os.urandom(4), 'big')}",
            "encrypted_skill": "blind-audit",
            "encrypted_data": encrypted,
            "result_pubkey": "0x",  # skip output encryption for this test
            "stripe_pi_id": "pi_blind",
            # No llm_token / llm_proxy_url — execute_in_envelope will fall
            # into the "no-path" branch and return a stub output, but the
            # input decryption happens BEFORE the LLM call, so we can
            # verify it by checking the llm_output / llm_error path.
            "llm_token": "",
            "llm_proxy_url": "",
            "skill_decrypt_input": True,
            "skill_hash": skill_hash_envelope,
        }
        result = poller.execute_in_envelope(env)
        check("5. execute_in_envelope returned a result dict",
              isinstance(result, dict) and "result" in result)
        if not result or "result" not in result:
            return
        r = result["result"]

        # We can verify decryption worked by checking that input_hash is
        # the hash of the PLAINTEXT, not the ciphertext (poller hashes
        # the post-decryption data string).
        expected_input_hash = hashlib.sha256(plaintext).hexdigest()
        check("5b. input_hash matches SHA256 of plaintext (decryption worked)",
              r.get("input_hash") == expected_input_hash,
              f"got={r.get('input_hash')} expected={expected_input_hash}")
        # The execution should have failed at the LLM call (no token), but
        # the plaintext should have been recovered first. We don't make
        # assertions about the LLM path — just that the hash is right.
        check("5c. result envelope has execution_mode (no LLM path expected)",
              r.get("execution_mode") in ("no-path", "broker-llm-proxy",
                                           "broker-proxy-failed"),
              f"mode={r.get('execution_mode')}")
    finally:
        poller.KEY_DIR = original_key_dir


def test_poller_does_not_decrypt_when_flag_unset():
    """Without skill_decrypt_input=True, the poller passes encrypted_data
    through verbatim (existing behaviour)."""
    plaintext = b"def hello():\n    return 'world'"
    # Encrypt it but DON'T set the flag — poller should hash the
    # ciphertext, not the plaintext.
    # Use a dummy keypair that the poller does NOT have, so the blob is
    # structurally valid but cannot be decrypted.
    other_priv = X25519PrivateKey.generate()
    other_pub_b64 = base64.b64encode(
        other_priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)).decode()
    encrypted = client_encrypt_to_pubkey(plaintext, other_pub_b64)

    env = {
        "job_id": f"job_passthrough_{int.from_bytes(os.urandom(4), 'big')}",
        "encrypted_skill": "summarize",  # built-in, no decrypt_input
        "encrypted_data": encrypted,
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_passthrough",
        "llm_token": "",
        "llm_proxy_url": "",
        # skill_decrypt_input NOT set (defaults to falsy)
        # skill_hash: emitted by the broker unconditionally on every
        # envelope (see test_poller_decrypts_input_when_flag_set for the
        # full rationale).
        "skill_hash": hashlib.sha256(b"summarize").hexdigest(),
    }
    result = poller.execute_in_envelope(env)
    r = result.get("result", {})
    expected_input_hash = hashlib.sha256(encrypted.encode()).hexdigest()
    check("6. without decrypt_input flag, input_hash is SHA256(ciphertext)",
          r.get("input_hash") == expected_input_hash,
          f"got={r.get('input_hash')} expected={expected_input_hash}")
    # And of course it should NOT match the plaintext hash
    plaintext_hash = hashlib.sha256(plaintext).hexdigest()
    check("6b. input_hash is NOT the plaintext hash (no silent decryption)",
          r.get("input_hash") != plaintext_hash)


def test_poller_handles_unencrypted_input_with_flag_set():
    """If skill_decrypt_input=true but encrypted_data is plaintext (not a
    valid ciphertext blob), the poller should pass it through and NOT crash
    (graceful degradation — flag is opt-in)."""
    env = {
        "job_id": f"job_plain_{int.from_bytes(os.urandom(4), 'big')}",
        "encrypted_skill": "blind-audit",
        "encrypted_data": "this is plaintext, not ciphertext",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_plain",
        "llm_token": "",
        "llm_proxy_url": "",
        "skill_decrypt_input": True,
        # skill_hash: see test_poller_decrypts_input_when_flag_set.
        "skill_hash": hashlib.sha256(b"blind-audit").hexdigest(),
    }
    result = poller.execute_in_envelope(env)
    r = result.get("result", {})
    # Plaintext gets hashed as-is
    expected = hashlib.sha256(b"this is plaintext, not ciphertext").hexdigest()
    check("7. decrypt_input flag with plaintext input → passthrough",
          r.get("input_hash") == expected,
          f"got={r.get('input_hash')} expected={expected}")
    # No exception raised — the test reaching here means it survived.


# =======================================================================
# Async runner
# =======================================================================

async def run():
    await test_discover_includes_enclave_pubkey()
    await test_skill_manifest_decrypt_input_flag()
    await test_submit_job_envelope_carries_decrypt_flag()


def main():
    # Sync tests first
    test_worker_publishes_x25519_pubkey_to_efs()
    test_worker_keys_idempotent()

    # Async tests
    asyncio.run(run())

    # More sync tests
    test_poller_decrypts_input_when_flag_set()
    test_poller_does_not_decrypt_when_flag_unset()
    test_poller_handles_unencrypted_input_with_flag_set()

    # Cleanup
    shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print(f"Ad-hoc verification — Showcase Skill 3: blind-audit.")
    print(f"Scope: worker X25519 pubkey publish, /v1/discover, skill "
          f"manifest decrypt_input flag, envelope plumbing, poller "
          f"decryption.")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()