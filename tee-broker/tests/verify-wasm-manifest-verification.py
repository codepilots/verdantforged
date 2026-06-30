#!/usr/bin/env python3
"""Verify WASM manifest verification on the broker and worker.

Properties verified (per kanban task t_bf00a075):

Broker daemon (broker-daemon/daemon.py):
  1. resolve_skill_hash() returns the registered wasm_manifest_hash for a
     WASM skill that has been POSTed to /v1/skills.
  2. resolve_skill_hash() falls back to sha256(name) for a built-in stub
     that has no entry in the `skills` table.
  3. resolve_skill_hash() falls back to sha256(name) for an unknown skill.
  4. submit_job() writes an envelope to the inbox with a `skill_hash` field
     that equals resolve_skill_hash()'s output.
  5. submit_job() writes the registered hash when the skill is registered.
  6. submit_job() writes the name-hash when the skill is a built-in stub.

Worker poller (worker/poller.py):
  7. execute_in_envelope() succeeds when the envelope's skill_hash is a
     well-formed 64-char hex string (even if it doesn't match
     sha256(encrypted_skill) — the envelope is authoritative for the
     registered WASM manifest hash, while the worker recomputes the name
     hash separately for the signed payload).
  8. execute_in_envelope() rejects envelopes where skill_hash is missing
     or malformed (returns state="failed" with reason "skill_hash_missing"
     / "skill_hash_malformed").
  9. execute_in_envelope() accepts the registered hash from the envelope
     (the worker's check is "well-formed 64-char hex", not "matches
     sha256(name)" — that's deliberate, see the task plan).
"""
import os, sys, json, hashlib, tempfile, shutil, asyncio
from pathlib import Path

# Set up temp env BEFORE importing daemon
TMP_ROOT = Path(tempfile.mkdtemp(prefix="hermes-wasm-manifest-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TMP_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"  # never actually called
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
# Uncommitted daemon work added real-Stripe PaymentIntent verification.
# Keep STRIPE_SECRET_KEY empty so the daemon operates in demo mode (no
# `stripe` library needed) and we can isolate the skill_hash behaviour.
os.environ["STRIPE_SECRET_KEY"] = ""
os.environ["BROKER_ACCOUNT_HASH_SECRET"] = "test-account-hash-secret"
SKILLS_AUTH_HEADER = {"Authorization": "Bearer test-skills-key-deadbeef"}

# Resolve the daemon module from the workspace
TESTS_DIR = Path(__file__).resolve().parent
WORKSPACE = TESTS_DIR.parent
BROKER_DIR = WORKSPACE / "broker-daemon"
sys.path.insert(0, str(BROKER_DIR))

# Load the working-tree worker/poller.py into a tmp dir so we can import it
# without the poller's /opt/worker hardcodes interfering. We DO use the
# working tree (not the committed version) because t_bf00a075 patches the
# working tree to add the skill_hash well-formedness check.
POLLER_TMP = TMP_ROOT / "poller-src"
POLLER_TMP.mkdir(parents=True, exist_ok=True)
shutil.copy(WORKSPACE / "worker" / "poller.py", POLLER_TMP / "poller.py")
os.environ["BROKER_WORKER_KEYS"] = str(POLLER_TMP / "keys")
os.environ["BROKER_EFS_LOGS"] = str(POLLER_TMP / "logs")
os.environ["BROKER_ARTIFACTS_DIR"] = str(POLLER_TMP / "artifacts")
(POLLER_TMP / "keys").mkdir(parents=True, exist_ok=True)
(POLLER_TMP / "logs").mkdir(parents=True, exist_ok=True)
(POLLER_TMP / "artifacts").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(POLLER_TMP))

import daemon  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402
import poller  # noqa: E402

# The committed poller hardcodes KEY_DIR = /opt/worker/keys — no env override
# existed yet. Redirect it to a tmp dir so the test runs as an unprivileged
# user. (This is testing-only — production workers run as root and use the
# real /opt/worker/keys.)
poller.KEY_DIR = POLLER_TMP / "keys" / "committed-poller"
poller.KEY_DIR.mkdir(parents=True, exist_ok=True)

# Initialise the DB against the temp dir
daemon.init_db()
(daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)

# Shortcut paths used by the tests
INBOX = TMP_ROOT / "jobs" / "inbox"
INBOX.mkdir(parents=True, exist_ok=True)

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        print(f"[PASS] {label}")
        PASS += 1
    else:
        print(f"[FAIL] {label}  {detail}")
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
        "wasm_ref": {"uri": "s3://bucket/test.wasm", "size_bytes": 4096},
    }
    m.update(overrides)
    return m


def good_submit_body(skill="summarize"):
    """A valid submit body. requester_sig='0x' skips signature verification
    (demo fallback) so we don't have to mint Ed25519 keys."""
    return {
        "encrypted_skill": skill,
        "encrypted_data": "hello world",
        "requester_sig": "0x",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_demo_test_001",
        "client_req_id": f"req_{hashlib.sha256(skill.encode()).hexdigest()[:16]}",
    }


async def run():
    # === Task 1: resolve_skill_hash helper ===

    with daemon.db() as conn:
        h = daemon.resolve_skill_hash("never-registered-skill", conn)
    check("3. unknown skill -> fallback to sha256(name)",
          h == hashlib.sha256(b"never-registered-skill").hexdigest(),
          f"got={h}")

    with daemon.db() as conn:
        h = daemon.resolve_skill_hash("summarize", conn)
    check("2. built-in stub -> fallback to sha256(name)",
          h == hashlib.sha256(b"summarize").hexdigest(),
          f"got={h}")

    # Spin up the test server for /v1/skills registration
    app = daemon.build_app()
    app.on_startup.clear()
    server = TestServer(app)
    async with TestClient(server) as client:
        # Register a WASM skill
        m = good_manifest("photo-glow-up-v1",
                          wasm_manifest_hash="1a2d2dad912c2be2d0da0465f70e375890f79e5f37703427563dbac095ef9b08",
                          description="photo retouch WASM skill")
        resp = await client.post("/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        check("0-prep. POST /v1/skills returns 201 for WASM skill",
              resp.status == 201, f"status={resp.status} body={body}")

        with daemon.db() as conn:
            h = daemon.resolve_skill_hash("photo-glow-up-v1", conn)
        check("1. registered WASM skill -> wasm_manifest_hash",
              h == "1a2d2dad912c2be2d0da0465f70e375890f79e5f37703427563dbac095ef9b08",
              f"got={h}")

        # === Task 2: envelope injection ===

        # Clear inbox so we can assert exactly one envelope was written
        for f in INBOX.glob("*.json"):
            f.unlink()

        # Built-in stub path
        body = good_submit_body(skill="summarize")
        resp = await client.post("/v1/jobs", json=body)
        check("4-prep. submit_job(summarize) -> 202",
              resp.status == 202, f"status={resp.status}")
        envelopes = list(INBOX.glob("*.json"))
        check("4. submit_job wrote exactly one envelope",
              len(envelopes) == 1, f"envelopes={[e.name for e in envelopes]}")
        env = json.loads(envelopes[0].read_text())
        check("4b. envelope has skill_hash field",
              "skill_hash" in env, f"envelope keys={list(env.keys())}")
        check("4c. built-in stub envelope skill_hash = sha256(name)",
              env.get("skill_hash") == hashlib.sha256(b"summarize").hexdigest(),
              f"got={env.get('skill_hash')}")

        # Registered WASM skill path
        for f in INBOX.glob("*.json"):
            f.unlink()
        body = good_submit_body(skill="photo-glow-up-v1")
        resp = await client.post("/v1/jobs", json=body)
        check("5-prep. submit_job(photo-glow-up-v1) -> 202",
              resp.status == 202, f"status={resp.status}")
        envelopes = list(INBOX.glob("*.json"))
        check("5. submit_job wrote one envelope for registered skill",
              len(envelopes) == 1)
        env = json.loads(envelopes[0].read_text())
        check("5b. registered envelope skill_hash = wasm_manifest_hash",
              env.get("skill_hash") == "1a2d2dad912c2be2d0da0465f70e375890f79e5f37703427563dbac095ef9b08",
              f"got={env.get('skill_hash')}")

        # === Task 3: worker poller ===
        # Tests run against the pure execute_in_envelope() function so we
        # don't need a live inbox watcher.

        # 7. Valid skill_hash → succeeds (state="completed")
        env_match = {
            "job_id": "job_test_match",
            "encrypted_skill": "summarize",
            "encrypted_data": "hello",
            "result_pubkey": "0x",
            "stripe_pi_id": "pi_test",
            "skill_hash": hashlib.sha256(b"summarize").hexdigest(),
            "llm_token": "",  # forces "no LLM path" but still completes
            "llm_proxy_url": "",
        }
        result = poller.execute_in_envelope(env_match)
        check("7. valid skill_hash -> state=completed",
              result["state"] == "completed",
              f"state={result['state']} keys={list(result.keys())}")

        # 8a. Missing skill_hash → fails
        env_missing = {
            "job_id": "job_test_missing",
            "encrypted_skill": "summarize",
            "encrypted_data": "hello",
            "result_pubkey": "0x",
            "stripe_pi_id": "pi_test",
            # no skill_hash
            "llm_token": "",
            "llm_proxy_url": "",
        }
        result = poller.execute_in_envelope(env_missing)
        check("8a. missing skill_hash -> state=failed",
              result["state"] == "failed",
              f"state={result['state']}")
        check("8a-reason. failure reason is skill_hash_missing",
              "skill_hash_missing" in (result.get("result", {}).get("error", "")
                                       or ""),
              f"error={result.get('result', {}).get('error')}")

        # 8b. Malformed skill_hash → fails
        env_bad = {
            "job_id": "job_test_bad",
            "encrypted_skill": "summarize",
            "encrypted_data": "hello",
            "result_pubkey": "0x",
            "stripe_pi_id": "pi_test",
            "skill_hash": "not-hex",
            "llm_token": "",
            "llm_proxy_url": "",
        }
        result = poller.execute_in_envelope(env_bad)
        check("8b. malformed skill_hash -> state=failed",
              result["state"] == "failed",
              f"state={result['state']}")
        check("8b-reason. failure reason is skill_hash_malformed",
              "skill_hash_malformed" in (result.get("result", {}).get("error", "")
                                         or ""),
              f"error={result.get('result', {}).get('error')}")

        # 9. Registered hash from envelope accepted even when != sha256(name)
        env_reg = {
            "job_id": "job_test_registered",
            "encrypted_skill": "photo-glow-up-v1",
            "encrypted_data": "hello",
            "result_pubkey": "0x",
            "stripe_pi_id": "pi_test",
            "skill_hash": "1a2d2dad912c2be2d0da0465f70e375890f79e5f37703427563dbac095ef9b08",
            "llm_token": "",
            "llm_proxy_url": "",
        }
        result = poller.execute_in_envelope(env_reg)
        check("9. registered hash in envelope accepted even if != sha256(name)",
              result["state"] == "completed",
              f"state={result['state']} error={result.get('result', {}).get('error', '')}")

    # Cleanup
    shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print(f"Ad-hoc verification -- WASM manifest verification on broker + worker.")
    sys.exit(0 if FAIL == 0 else 1)


asyncio.run(run())