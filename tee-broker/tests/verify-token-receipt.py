#!/usr/bin/env python3
"""Verify the token-receipt skill (Skill 2 — Stripe pillar).

Showpiece skill that generates a signed receipt for a completed job's
LLM token usage, demonstrating the "Pay As Token Burns" billing model.

Tests are offline (no AWS, no live broker). They exercise:

  A. Cost calculation — pure function in worker/poller.py.
     Lease prorate: $0.20 per 15-min slot (ceil to next 15-min boundary? or
     straight prorate?). Tokens: $0.001 per 1K tokens. Total = lease+tokens.

  B. Receipt builder — composes the receipt dict with cost breakdown,
     Ed25519 signs it, and returns it in the worker's result envelope.

  C. Daemon usage_context injection — when submit_job sees
     encrypted_skill == "token-receipt", the daemon looks up the referenced
     job_id's llm_tokens_used / started_at / finished_at / stripe_pi_id from
     the DB and injects them as `usage_context` in the worker envelope.

  D. End-to-end registration — POST /v1/skills with the token-receipt
     manifest succeeds and /v1/discover advertises it.

  E. Worker dispatch — when execute_in_envelope sees skill=="token-receipt"
     AND envelope has usage_context, it produces a receipt-shaped output
     (cost_breakdown + broker_signature) WITHOUT needing a real LLM call
     (the receipt itself is deterministic; we skip the LLM path).

Run: python3 tests/verify-token-receipt.py
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# ---- Environment setup --------------------------------------------------------
# Mirror verify-skill-registration.py: temp EFS mount, offline-safe defaults.
TEST_ROOT = Path(tempfile.mkdtemp(prefix="token-receipt-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"  # never actually called
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_REGION"] = "eu-west-1"
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
# Force demo mode — avoids `import stripe` (the SDK isn't installed locally
# for offline tests). Production deployment sets STRIPE_SECRET_KEY for real.
os.environ["STRIPE_SECRET_KEY"] = ""
SKILLS_AUTH_HEADER = {"Authorization": "Bearer test-skills-key-deadbeef"}

DAEMON_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/broker-daemon"
WORKER_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/worker"
sys.path.insert(0, DAEMON_DIR)
sys.path.insert(0, WORKER_DIR)

PASS = 0
FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        print(f"[PASS] {label}")
        PASS += 1
    else:
        print(f"[FAIL] {label}" + (f"  ({detail})" if detail else ""))
        FAIL += 1


# ---- A. Cost calculation (pure function) -------------------------------------

def test_cost_calc():
    """Pure-function tests for the cost calculation."""
    # Import here so we get the live module (after any later edits).
    from poller import compute_receipt_cost  # type: ignore
    # Simple 15-minute job, 1000 tokens
    r = compute_receipt_cost(total_tokens=1000, duration_seconds=15 * 60)
    check("A1. 15-min job, 1000 tokens: lease = $0.20",
          abs(r["lease_usd"] - 0.20) < 1e-9, f"lease_usd={r['lease_usd']}")
    check("A2. 15-min job, 1000 tokens: tokens = $0.001",
          abs(r["tokens_usd"] - 0.001) < 1e-9, f"tokens_usd={r['tokens_usd']}")
    check("A3. 15-min job, 1000 tokens: total = $0.201",
          abs(r["total_usd"] - 0.201) < 1e-9, f"total_usd={r['total_usd']}")
    # 30-minute job prorates to 2x lease
    r = compute_receipt_cost(total_tokens=0, duration_seconds=30 * 60)
    check("A4. 30-min job, 0 tokens: lease = $0.40",
          abs(r["lease_usd"] - 0.40) < 1e-9, f"lease_usd={r['lease_usd']}")
    check("A5. 30-min job, 0 tokens: tokens = $0.00",
          abs(r["tokens_usd"] - 0.0) < 1e-9, f"tokens_usd={r['tokens_usd']}")
    # 7.5 minutes = half a slot = $0.10 lease
    r = compute_receipt_cost(total_tokens=5000, duration_seconds=7 * 60 + 30)
    check("A6. 7.5-min job, 5K tokens: lease = $0.10",
          abs(r["lease_usd"] - 0.10) < 1e-9, f"lease_usd={r['lease_usd']}")
    check("A7. 7.5-min job, 5K tokens: tokens = $0.005",
          abs(r["tokens_usd"] - 0.005) < 1e-9, f"tokens_usd={r['tokens_usd']}")
    # 0-duration edge case (instant job) — should not error and lease should be 0
    r = compute_receipt_cost(total_tokens=100, duration_seconds=0)
    check("A8. 0-sec job: lease = $0.00",
          abs(r["lease_usd"] - 0.0) < 1e-9, f"lease_usd={r['lease_usd']}")
    check("A9. 0-sec job: tokens = $0.0001 (100 tokens)",
          abs(r["tokens_usd"] - 0.0001) < 1e-9, f"tokens_usd={r['tokens_usd']}")
    # Negative duration should clamp to 0, not error
    r = compute_receipt_cost(total_tokens=100, duration_seconds=-50)
    check("A10. negative duration clamps to lease=$0",
          abs(r["lease_usd"] - 0.0) < 1e-9, f"lease_usd={r['lease_usd']}")


def test_cost_calc_prompt_and_completion():
    """Receipt includes prompt/completion token breakdown, not just total."""
    from poller import compute_receipt_cost  # type: ignore
    r = compute_receipt_cost(prompt_tokens=300, completion_tokens=700,
                             total_tokens=1000, duration_seconds=900)
    check("A11. prompt_tokens in result",
          r["prompt_tokens"] == 300, f"prompt_tokens={r['prompt_tokens']}")
    check("A12. completion_tokens in result",
          r["completion_tokens"] == 700, f"completion_tokens={r['completion_tokens']}")
    check("A13. total_tokens in result",
          r["total_tokens"] == 1000, f"total_tokens={r['total_tokens']}")


# ---- B. Receipt builder + Ed25519 signature ----------------------------------

def test_receipt_build_and_sign():
    """Build a receipt, sign it, verify the signature, tamper-test fails."""
    from poller import build_token_receipt  # type: ignore
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)

    usage = {
        "prompt_tokens": 350,
        "completion_tokens": 175,
        "total_tokens": 525,
        "duration_seconds": 23,
        "stripe_pi_id": "pi_demo_billing",
        "started_at": "2026-06-27T22:00:00+00:00",
        "finished_at": "2026-06-27T22:00:23+00:00",
    }
    # Pass an ephemeral key so we don't need a writable /opt/worker.
    sk = Ed25519PrivateKey.generate()
    receipt = build_token_receipt(
        original_job_id="job_abc123",
        receipt_job_id="job_def456",
        usage=usage,
        signing_key=sk,
    )
    # Top-level shape
    check("B1. receipt has job_id (the original)",
          receipt.get("job_id") == "job_abc123",
          f"job_id={receipt.get('job_id')}")
    check("B2. receipt has receipt_job_id",
          receipt.get("receipt_job_id") == "job_def456")
    check("B3. receipt has token_breakdown",
          isinstance(receipt.get("token_breakdown"), dict))
    tb = receipt["token_breakdown"]
    check("B4. token_breakdown.prompt_tokens = 350",
          tb.get("prompt_tokens") == 350)
    check("B5. token_breakdown.completion_tokens = 175",
          tb.get("completion_tokens") == 175)
    check("B6. token_breakdown.total_tokens = 525",
          tb.get("total_tokens") == 525)
    check("B7. receipt has cost_breakdown",
          isinstance(receipt.get("cost_breakdown"), dict))
    cb = receipt["cost_breakdown"]
    check("B8. cost_breakdown.lease_usd > 0 for 23s job",
          cb.get("lease_usd", 0) > 0, f"lease_usd={cb.get('lease_usd')}")
    check("B9. cost_breakdown.total_usd > 0",
          cb.get("total_usd", 0) > 0, f"total_usd={cb.get('total_usd')}")
    check("B10. receipt has stripe_pi_id",
          receipt.get("stripe_pi_id") == "pi_demo_billing")
    check("B11. receipt has broker_signature (base64)",
          isinstance(receipt.get("broker_signature"), str) and len(receipt["broker_signature"]) > 0)
    check("B12. receipt has signed_at (ISO8601)",
          isinstance(receipt.get("signed_at"), str) and "T" in receipt.get("signed_at", ""))

    # Verify the signature against the worker's pubkey — this is what the
    # showpiece for judges is: anyone can check the receipt is signed by
    # the broker's enclave key.
    sig = base64.b64decode(receipt["broker_signature"])
    sig_payload = receipt["signed_payload"].encode()
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(receipt["broker_pubkey"]))
    try:
        pub.verify(sig, sig_payload)
        check("B13. broker_signature verifies against broker_pubkey", True)
    except Exception as e:
        check("B13. broker_signature verifies against broker_pubkey", False, str(e))

    # Tamper test: flip a digit in total_tokens. A verifier reconstructs the
    # canonical payload from the receipt fields and verifies the signature
    # against THAT reconstruction — not against the literal signed_payload
    # string (which a malicious party could keep intact while mutating the
    # fields). So we re-canonicalize the tampered receipt and check that
    # the signature no longer validates.
    tampered = json.loads(json.dumps(receipt))
    tampered["token_breakdown"]["total_tokens"] = tampered["token_breakdown"]["total_tokens"] + 1
    # Reconstruct the canonical payload the way a verifier would.
    verifier_payload_obj = {
        "job_id": tampered["job_id"],
        "receipt_job_id": tampered["receipt_job_id"],
        "token_breakdown": tampered["token_breakdown"],
        "cost_breakdown": tampered["cost_breakdown"],
        "stripe_pi_id": tampered["stripe_pi_id"],
        "signed_at": tampered["signed_at"],
    }
    verifier_payload = json.dumps(verifier_payload_obj, sort_keys=True).encode()
    try:
        pub.verify(base64.b64decode(tampered["broker_signature"]), verifier_payload)
        check("B14. tampered receipt FAILS signature check", False)
    except Exception:
        check("B14. tampered receipt FAILS signature check", True)


# ---- C. Daemon usage_context injection ---------------------------------------

async def _seed_job(daemon, job_id, llm_tokens_used=523, llm_calls=2,
                    stripe_pi_id="pi_demo_billing",
                    started_at=None, finished_at=None, state="completed"):
    """Insert a prior job row so submit_job has something to look up."""
    now = "2026-06-27T22:00:00+00:00"
    request_body = json.dumps({
        "encrypted_skill": "summarize",
        "encrypted_data": "test input",
        "stripe_pi_id": stripe_pi_id,
        "client_req_id": f"client-{job_id}",
    })
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, started_at, "
            "finished_at, state, request_body, llm_tokens_used, llm_calls) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, f"client-{job_id}", now,
             started_at or now, finished_at or now, state,
             request_body, llm_tokens_used, llm_calls),
        )


def test_daemon_usage_context_injection():
    """When submit_job receives a token-receipt job, it must look up the
    referenced prior job's usage and inject it as usage_context."""
    import daemon  # type: ignore
    daemon.init_db()
    # The handler expects /mnt/broker/jobs/inbox to exist so we can write
    # envelopes to it (submit_job writes the envelope synchronously).
    (daemon.BROKER_EFS_MOUNT / "jobs" / "inbox").mkdir(parents=True, exist_ok=True)
    (daemon.BROKER_EFS_MOUNT / "jobs" / "outbox").mkdir(parents=True, exist_ok=True)
    (daemon.BROKER_EFS_MOUNT / "logs").mkdir(parents=True, exist_ok=True)
    (daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)

    # Seed a prior job
    asyncio.run(_seed_job(daemon, "job_prior_aaa", llm_tokens_used=1234,
                          llm_calls=3, stripe_pi_id="pi_demo_billing",
                          started_at="2026-06-27T21:00:00+00:00",
                          finished_at="2026-06-27T21:00:42+00:00",
                          state="completed"))

    from aiohttp.test_utils import TestServer, TestClient

    async def _drive():
        app = daemon.build_app()
        app.on_startup.clear()
        server = TestServer(app)
        async with TestClient(server) as client:
            # Submit a token-receipt job referencing the prior job_id
            resp = await client.post("/v1/jobs", json={
                "client_req_id": "receipt-1",
                "encrypted_skill": "token-receipt",
                "encrypted_data": json.dumps({"job_id": "job_prior_aaa"}),
                "requester_sig": "0x",
                "result_pubkey": "0x",
                "stripe_pi_id": "pi_demo_billing",
            })
            check("C1. POST /v1/jobs (token-receipt) returns 202",
                  resp.status == 202,
                  f"status={resp.status}")
            body = await resp.json()
            receipt_job_id = body.get("job_id")
            check("C2. receipt job has a job_id",
                  isinstance(receipt_job_id, str) and receipt_job_id.startswith("job_"))
            # Read the envelope the daemon wrote to EFS inbox
            envelope_path = daemon.BROKER_EFS_MOUNT / "jobs" / "inbox" / f"{receipt_job_id}.json"
            check("C3. envelope written to inbox",
                  envelope_path.exists(), f"path={envelope_path}")
            env = json.loads(envelope_path.read_text())
            uc = env.get("usage_context")
            check("C4. envelope has usage_context",
                  isinstance(uc, dict), f"usage_context={uc}")
            if isinstance(uc, dict):
                check("C5. usage_context.job_id == prior job",
                      uc.get("job_id") == "job_prior_aaa")
                check("C6. usage_context.llm_tokens_used == 1234",
                      uc.get("llm_tokens_used") == 1234)
                check("C7. usage_context.llm_calls == 3",
                      uc.get("llm_calls") == 3)
                check("C8. usage_context.stripe_pi_id == pi_demo_billing",
                      uc.get("stripe_pi_id") == "pi_demo_billing")
                check("C9. usage_context.duration_seconds == 42",
                      uc.get("duration_seconds") == 42,
                      f"duration_seconds={uc.get('duration_seconds')}")

    asyncio.run(_drive())


def test_daemon_usage_context_missing_job():
    """When the referenced job doesn't exist, usage_context should be None
    so the worker skill can produce a clear 'not found' error."""
    import daemon  # type: ignore
    daemon.init_db()
    (daemon.BROKER_EFS_MOUNT / "jobs" / "inbox").mkdir(parents=True, exist_ok=True)
    (daemon.BROKER_EFS_MOUNT / "jobs" / "outbox").mkdir(parents=True, exist_ok=True)
    (daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)

    from aiohttp.test_utils import TestServer, TestClient

    async def _drive():
        app = daemon.build_app()
        app.on_startup.clear()
        server = TestServer(app)
        async with TestClient(server) as client:
            resp = await client.post("/v1/jobs", json={
                "client_req_id": "receipt-bad",
                "encrypted_skill": "token-receipt",
                "encrypted_data": json.dumps({"job_id": "job_does_not_exist"}),
                "requester_sig": "0x",
                "result_pubkey": "0x",
                "stripe_pi_id": "pi_demo_x",
            })
            check("C10. POST /v1/jobs with unknown prior job still accepts (202)",
                  resp.status == 202, f"status={resp.status}")
            body = await resp.json()
            receipt_job_id = body.get("job_id")
            envelope_path = daemon.BROKER_EFS_MOUNT / "jobs" / "inbox" / f"{receipt_job_id}.json"
            env = json.loads(envelope_path.read_text())
            check("C11. usage_context is None for unknown job_id",
                  env.get("usage_context") is None,
                  f"usage_context={env.get('usage_context')}")

    asyncio.run(_drive())


def test_daemon_usage_context_accepts_bare_string():
    """encrypted_data can also be a bare job_id string (not just JSON).
    The receipt spec uses bare strings in the judge demo."""
    import daemon  # type: ignore
    daemon.init_db()
    (daemon.BROKER_EFS_MOUNT / "jobs" / "inbox").mkdir(parents=True, exist_ok=True)
    (daemon.BROKER_EFS_MOUNT / "jobs" / "outbox").mkdir(parents=True, exist_ok=True)
    (daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)
    asyncio.run(_seed_job(daemon, "job_prior_bbb", llm_tokens_used=42,
                          stripe_pi_id="pi_demo_x"))

    from aiohttp.test_utils import TestServer, TestClient

    async def _drive():
        app = daemon.build_app()
        app.on_startup.clear()
        server = TestServer(app)
        async with TestClient(server) as client:
            resp = await client.post("/v1/jobs", json={
                "client_req_id": "receipt-bare",
                "encrypted_skill": "token-receipt",
                "encrypted_data": "job_prior_bbb",  # bare string
                "requester_sig": "0x",
                "result_pubkey": "0x",
                "stripe_pi_id": "pi_demo_x",
            })
            check("C12. bare string job_id still accepted (202)",
                  resp.status == 202, f"status={resp.status}")
            body = await resp.json()
            env = json.loads((daemon.BROKER_EFS_MOUNT / "jobs" / "inbox" /
                              f"{body['job_id']}.json").read_text())
            check("C13. usage_context populated from bare string",
                  isinstance(env.get("usage_context"), dict) and
                  env["usage_context"].get("llm_tokens_used") == 42)

    asyncio.run(_drive())


# ---- D. End-to-end registration ----------------------------------------------

def test_register_token_receipt_skill():
    """Register token-receipt via POST /v1/skills and confirm /v1/discover
    advertises it."""
    import daemon  # type: ignore
    daemon.init_db()
    (daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)
    (daemon.BROKER_EFS_MOUNT / "jobs" / "inbox").mkdir(parents=True, exist_ok=True)
    (daemon.BROKER_EFS_MOUNT / "jobs" / "outbox").mkdir(parents=True, exist_ok=True)

    from aiohttp.test_utils import TestServer, TestClient

    async def _drive():
        app = daemon.build_app()
        app.on_startup.clear()
        server = TestServer(app)
        async with TestClient(server) as client:
            manifest = {
                "name": "token-receipt",
                "version": "0.1.0",
                "description": "Generate a signed token-usage receipt for a completed job (Pay As Token Burns billing)",
                "wasm_manifest_hash": hashlib.sha256(b"token-receipt").hexdigest(),
                "entry_point": "main",
                "prompt_template": (
                    "You are the token-receipt skill. The input JSON has a "
                    "usage_context with llm_tokens_used, llm_calls, "
                    "duration_seconds, and stripe_pi_id. Format a human-"
                    "readable receipt."
                ),
                "resource_limits": {
                    "max_fuel": 5_000_000,
                    "max_duration_ms": 30_000,
                    "max_memory_mb": 128,
                },
            }
            resp = await client.post("/v1/skills", json=manifest, headers=SKILLS_AUTH_HEADER)
            check("D1. POST /v1/skills (token-receipt) returns 201",
                  resp.status == 201, f"status={resp.status}")
            body = await resp.json()
            check("D2. registered name == token-receipt",
                  body.get("name") == "token-receipt")
            check("D3. registered resource_limits.max_fuel == 5_000_000",
                  body.get("resource_limits", {}).get("max_fuel") == 5_000_000)

            # /v1/discover should advertise it
            resp = await client.get("/v1/discover")
            disc = await resp.json()
            check("D4. /v1/discover includes token-receipt",
                  "token-receipt" in disc.get("supported_skills", []),
                  f"supported_skills={disc.get('supported_skills')}")

            # GET /v1/skills/token-receipt should return it
            resp = await client.get("/v1/skills/token-receipt")
            fetched = await resp.json()
            check("D5. GET /v1/skills/token-receipt returns the manifest",
                  resp.status == 200 and fetched.get("name") == "token-receipt")

    asyncio.run(_drive())


# ---- E. Worker dispatch ------------------------------------------------------

def test_worker_dispatches_token_receipt():
    """When execute_in_envelope sees skill=='token-receipt' + a usage_context
    in the envelope, it should build the receipt (no LLM call needed) and
    embed it in the result with cost_breakdown + broker_signature."""
    import poller  # type: ignore
    from poller import execute_in_envelope  # type: ignore
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    # Use a tmp EFS mount so ARTIFACTS_DIR + KEY_DIR writes don't pollute
    # real paths. The poller reads INBOX.glob() but we call it directly,
    # so we only need the ARTIFACTS_DIR + KEY_DIR to be writable.
    tmp = Path(tempfile.mkdtemp(prefix="poller-tr-"))
    poller.ARTIFACTS_DIR = tmp / "artifacts"
    poller.KEY_DIR = tmp / "keys"
    # Chose to mutate poller.KEY_DIR (module global) over monkey-patching
    # _ensure_worker_signing_key so the test stays close to the production
    # code path. Module reload would be heavier.

    env = {
        "job_id": "job_receipt_worker",
        "encrypted_skill": "token-receipt",
        "encrypted_data": json.dumps({"job_id": "job_prior_aaa"}),
        "result_pubkey": "0x",  # demo: no encryption
        "requester_sig": "0x",
        "stripe_pi_id": "pi_demo_billing",
        # skill_hash required by the poller (VULN-S hardening from
        # t_bf00a075). Production envelopes always carry it.
        "skill_hash": hashlib.sha256(b"token-receipt").hexdigest(),
        "usage_context": {
            "job_id": "job_prior_aaa",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "llm_calls": 1,
            "duration_seconds": 12,
            "stripe_pi_id": "pi_demo_billing",
            "started_at": "2026-06-27T22:00:00+00:00",
            "finished_at": "2026-06-27T22:00:12+00:00",
        },
        # No llm_token / llm_proxy_url — token-receipt must NOT need the LLM
    }
    out = execute_in_envelope(env)
    check("E1. execute_in_envelope returns state=completed",
          out.get("state") == "completed", f"state={out.get('state')}")
    result = out.get("result", {})
    receipt = result.get("receipt")
    check("E2. result has receipt block", isinstance(receipt, dict),
          f"receipt={receipt}")
    if isinstance(receipt, dict):
        check("E3. receipt.job_id == prior job",
              receipt.get("job_id") == "job_prior_aaa")
        check("E4. receipt has token_breakdown with total_tokens",
              receipt.get("token_breakdown", {}).get("total_tokens") == 150)
        check("E5. receipt has cost_breakdown.total_usd > 0",
              receipt.get("cost_breakdown", {}).get("total_usd", 0) > 0)
        check("E6. receipt has broker_signature",
              isinstance(receipt.get("broker_signature"), str))
        # No LLM error — this skill doesn't need the LLM
        check("E7. no llm_error (token-receipt is deterministic)",
              "llm_error" not in result, f"llm_error={result.get('llm_error')}")
        check("E8. result.output has a human-readable receipt summary",
              isinstance(result.get("output"), str) and len(result["output"]) > 0)


def test_worker_dispatches_missing_usage_context():
    """When the envelope has skill='token-receipt' but no usage_context,
    the skill should produce a clear error receipt (no crash)."""
    import poller  # type: ignore
    from poller import execute_in_envelope  # type: ignore

    tmp = Path(tempfile.mkdtemp(prefix="poller-tr-missing-"))
    poller.ARTIFACTS_DIR = tmp / "artifacts"

    env = {
        "job_id": "job_receipt_missing",
        "encrypted_skill": "token-receipt",
        "encrypted_data": "job_does_not_exist",
        "result_pubkey": "0x",
        "requester_sig": "0x",
        "stripe_pi_id": "pi_demo_billing",
        # skill_hash required by the poller (VULN-S hardening from
        # t_bf00a075). Production envelopes always carry it.
        "skill_hash": hashlib.sha256(b"token-receipt").hexdigest(),
        # usage_context absent — daemon should have set it to None
    }
    out = execute_in_envelope(env)
    check("E9. missing usage_context: state=completed (graceful)",
          out.get("state") == "completed", f"state={out.get('state')}")
    result = out.get("result", {})
    receipt = result.get("receipt")
    check("E10. missing usage_context: receipt has error field",
          isinstance(receipt, dict) and "error" in receipt,
          f"receipt={receipt}")


# ---- Driver -----------------------------------------------------------------

def main():
    test_cost_calc()
    test_cost_calc_prompt_and_completion()
    test_receipt_build_and_sign()
    test_daemon_usage_context_injection()
    test_daemon_usage_context_missing_job()
    test_daemon_usage_context_accepts_bare_string()
    test_register_token_receipt_skill()
    test_worker_dispatches_token_receipt()
    test_worker_dispatches_missing_usage_context()
    print()
    print("=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print(f"Ad-hoc verification — token-receipt skill (Stripe pillar).")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()