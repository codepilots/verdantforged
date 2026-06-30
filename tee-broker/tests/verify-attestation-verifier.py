#!/usr/bin/env python3
"""Verify the attestation-verifier skill (Skill 1 — NVIDIA/AMD pillar).

Showpiece skill that takes a broker's attestation block (from /v1/discover)
and produces a human-readable, signed verification verdict.

Tests are offline (no AWS, no live broker). They exercise:

  A. Verdict builder — pure function that parses an attestation block,
     decides pass/fail, formats details, and signs the canonical verdict
     JSON with Ed25519.

  B. Worker dispatch — when execute_in_envelope sees
     skill == "attestation-verifier", it short-circuits to the
     verdict builder (no LLM call) and embeds a structured verdict in
     the result with broker_signature. Mirror of the token-receipt
     deterministic dispatch path.

  C. End-to-end registration — POST /v1/skills with the
     attestation-verifier manifest succeeds and /v1/discover
     advertises it.

  D. Prompt-precedence — the registered prompt_template overrides the
     hardcoded fallback in the worker's skill_prompts dict. Pin so
     the dispatch wiring stays consistent.

  E. Error handling — non-JSON input, missing fields, and stub
     measurements all produce a clear "fail" verdict with details,
     never a crash.

Run: python3 tests/verify-attestation-verifier.py
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# ---- Environment setup --------------------------------------------------------
# Same offline-safe defaults as the other verify-*.py suites.
TEST_ROOT = Path(tempfile.mkdtemp(prefix="attestation-verifier-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"  # never actually called
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_REGION"] = "eu-west-1"
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
os.environ["STRIPE_SECRET_KEY"] = ""  # demo mode (no stripe SDK import needed)
SKILLS_AUTH_HEADER = {"Authorization": "Bearer test-skills-key-deadbeef"}

DAEMON_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/broker-daemon"
WORKER_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/worker"
sys.path.insert(0, DAEMON_DIR)
sys.path.insert(0, WORKER_DIR)

# Redirect poller KEY_DIR before any verdict-builder test runs. The poller
# hardcodes /opt/worker which isn't writable in the test sandbox; the
# worker-dispatch tests already override it locally, but the verdict-builder
# tests run first and would fail on the first sign attempt.
import poller as _poller  # type: ignore  # noqa: E402
_POLLER_KEY_DIR = Path(tempfile.mkdtemp(prefix="poller-av-keys-"))
_poller.KEY_DIR = _POLLER_KEY_DIR

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


# ---- A. Verdict builder (pure function) --------------------------------------

def test_verdict_builder_pass():
    """A real-looking attestation block (non-stub measurement + cert chain)
    produces verdict=pass with details."""
    from poller import build_attestation_verdict  # type: ignore
    attestation = {
        "tee_type": "amd-sev-snp",
        "measurement": "ab" * 48,  # 96-char hex (48 bytes)
        "report": base64.b64encode(b"x" * 1184).decode(),
        "cert_chain": [base64.b64encode(b"vcek").decode(),
                       base64.b64encode(b"ask").decode(),
                       base64.b64encode(b"ark").decode()],
        "chip_id": "AMD-Milan-1234",
        "policy_hash": hashlib.sha256(b"openshell-policy-v1").hexdigest(),
    }
    v = build_attestation_verdict(attestation)
    check("A1. verdict == pass for real attestation",
          v["verdict"] == "pass", f"verdict={v.get('verdict')}")
    check("A2. details is a non-empty string",
          isinstance(v["details"], str) and len(v["details"]) > 0)
    check("A3. measurement echoed back",
          v["measurement"] == attestation["measurement"])
    check("A4. chip_id echoed back",
          v["chip_id"] == "AMD-Milan-1234")
    check("A5. cert_chain_len == 3",
          v.get("cert_chain_len") == 3, f"got {v.get('cert_chain_len')}")
    check("A6. policy_hash matches",
          v.get("policy_hash") == attestation["policy_hash"])
    check("A7. broker_signature is base64 Ed25519 sig (88 chars)",
          isinstance(v.get("broker_signature"), str)
          and len(v["broker_signature"]) >= 80,
          f"sig={v.get('broker_signature')}")
    check("A8. signed_at present (ISO-8601 UTC)",
          isinstance(v.get("signed_at"), str)
          and "T" in v["signed_at"])


def test_verdict_builder_stub_measurement():
    """A stub measurement (no real TEE) yields verdict=fail with a clear
    explanation that the broker is unattested."""
    from poller import build_attestation_verdict  # type: ignore
    attestation = {
        "tee_type": "amd-sev-snp",
        "measurement": "stub-no-measurement",
        "report": "",
        "cert_chain": [],
        "chip_id": "",
        "policy_hash": "",
    }
    v = build_attestation_verdict(attestation)
    check("A9. stub measurement → verdict=fail",
          v["verdict"] == "fail", f"verdict={v.get('verdict')}")
    check("A10. details mentions 'unattested' or 'stub'",
          "stub" in v["details"].lower() or "unattested" in v["details"].lower(),
          f"details={v['details']}")
    check("A11. broker_signature still present on FAIL verdicts",
          isinstance(v.get("broker_signature"), str)
          and len(v["broker_signature"]) >= 80)


def test_verdict_builder_signature_verifies():
    """The broker_signature field actually verifies against the canonical
    JSON payload with Ed25519 — proves the signing is real, not a stub."""
    from poller import build_attestation_verdict  # type: ignore
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    attestation = {
        "tee_type": "amd-sev-snp",
        "measurement": "deadbeef" * 12,
        "report": base64.b64encode(b"y" * 1184).decode(),
        "cert_chain": [base64.b64encode(b"vcek").decode()],
        "chip_id": "AMD-Milan-9999",
        "policy_hash": "f" * 64,
    }
    v = build_attestation_verdict(attestation)
    # Reconstruct the signed payload — the verdict dict WITHOUT the
    # three envelope-only fields (broker_signature, broker_pubkey,
    # signed_payload). signed_at IS part of the signed claim (the
    # helper includes it in the canonical payload).
    # Must use ensure_ascii=False to match the helper's canonicalisation
    # (otherwise Python escapes → as \u2192 and the byte string differs).
    signed_payload = json.dumps(
        {k: v[k] for k in sorted(v.keys())
         if k not in ("broker_signature", "broker_pubkey", "signed_payload")},
        sort_keys=True, ensure_ascii=False
    ).encode()
    sig = base64.b64decode(v["broker_signature"])
    pub = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(v["broker_pubkey"]))
    try:
        pub.verify(sig, signed_payload)
        sig_ok = True
    except Exception:
        sig_ok = False
    check("A12. broker_signature verifies over canonical verdict JSON",
          sig_ok)


def test_verdict_builder_garbage_input():
    """Non-JSON or empty input doesn't crash — produces verdict=fail."""
    from poller import build_attestation_verdict  # type: ignore
    # Test 1: empty input
    v = build_attestation_verdict({})
    check("A13. empty attestation → verdict=fail (graceful)",
          v["verdict"] == "fail")
    check("A14. empty attestation still has a signature",
          isinstance(v.get("broker_signature"), str)
          and len(v["broker_signature"]) >= 80)
    # Test 2: missing measurement
    v = build_attestation_verdict({"tee_type": "amd-sev-snp"})
    check("A15. missing measurement → verdict=fail",
          v["verdict"] == "fail")
    # Test 3: cert_chain length != 3 → cert_chain_len recorded, still pass
    # if measurement is real
    v = build_attestation_verdict({
        "measurement": "a" * 96,
        "cert_chain": [base64.b64encode(b"x").decode()],  # 1 cert
        "tee_type": "amd-sev-snp",
    })
    check("A16. cert_chain_len reflected in verdict even when !=3",
          v.get("cert_chain_len") == 1)
    check("A17. cert_chain_present reflects whether ANY chain was supplied",
          v.get("cert_chain_present") is True)


# ---- B. Worker dispatch ------------------------------------------------------

def test_worker_dispatches_attestation_verifier():
    """When execute_in_envelope sees skill='attestation-verifier' AND a
    real attestation block in encrypted_data, it short-circuits to the
    deterministic verdict builder (no LLM call) and embeds the signed
    verdict in result.attestation_verdict."""
    import poller  # type: ignore
    from poller import execute_in_envelope  # type: ignore

    tmp = Path(tempfile.mkdtemp(prefix="poller-av-"))
    poller.ARTIFACTS_DIR = tmp / "artifacts"
    poller.KEY_DIR = tmp / "keys"

    attestation = {
        "tee_type": "amd-sev-snp",
        "measurement": "ab" * 48,
        "report": base64.b64encode(b"r" * 1184).decode(),
        "cert_chain": [base64.b64encode(b"a").decode(),
                       base64.b64encode(b"b").decode()],
        "chip_id": "AMD-Milan-5555",
        "policy_hash": hashlib.sha256(b"test-policy").hexdigest(),
    }
    env = {
        "job_id": "job_av_worker_pass",
        "encrypted_skill": "attestation-verifier",
        "encrypted_data": json.dumps(attestation),
        "result_pubkey": "0x",  # demo: no encryption
        "requester_sig": "0x",
        "stripe_pi_id": "pi_demo_verify",
        "skill_hash": hashlib.sha256(b"attestation-verifier").hexdigest(),
    }
    out = execute_in_envelope(env)
    check("B1. execute_in_envelope returns state=completed",
          out.get("state") == "completed", f"state={out.get('state')}")
    result = out.get("result", {})
    av = result.get("attestation_verdict")
    check("B2. result has attestation_verdict block",
          isinstance(av, dict), f"av={av}")
    if isinstance(av, dict):
        check("B3. verdict == pass for real attestation",
              av.get("verdict") == "pass",
              f"verdict={av.get('verdict')}")
        check("B4. chip_id echoed",
              av.get("chip_id") == "AMD-Milan-5555")
        check("B5. measurement echoed",
              av.get("measurement") == "ab" * 48)
        check("B6. broker_signature present",
              isinstance(av.get("broker_signature"), str)
              and len(av["broker_signature"]) >= 80)
    check("B7. no llm_error (attestation-verifier is deterministic)",
          "llm_error" not in result, f"llm_error={result.get('llm_error')}")
    check("B8. execution_mode == attestation-verifier-deterministic",
          result.get("execution_mode") == "attestation-verifier-deterministic",
          f"got {result.get('execution_mode')}")
    check("B9. result.output has a human-readable verdict summary",
          isinstance(result.get("output"), str) and len(result["output"]) > 0)


def test_worker_dispatches_stub_measurement():
    """A stub measurement yields verdict=fail in the worker, not a crash."""
    import poller  # type: ignore
    from poller import execute_in_envelope  # type: ignore

    tmp = Path(tempfile.mkdtemp(prefix="poller-av-stub-"))
    poller.ARTIFACTS_DIR = tmp / "artifacts"
    poller.KEY_DIR = tmp / "keys"

    env = {
        "job_id": "job_av_worker_stub",
        "encrypted_skill": "attestation-verifier",
        "encrypted_data": json.dumps({
            "tee_type": "amd-sev-snp",
            "measurement": "stub-no-measurement",
            "report": "",
            "cert_chain": [],
            "chip_id": "",
            "policy_hash": "",
        }),
        "result_pubkey": "0x",
        "requester_sig": "0x",
        "stripe_pi_id": "pi_demo_verify",
        "skill_hash": hashlib.sha256(b"attestation-verifier").hexdigest(),
    }
    out = execute_in_envelope(env)
    check("B10. stub measurement → state=completed (graceful)",
          out.get("state") == "completed")
    av = out.get("result", {}).get("attestation_verdict", {})
    check("B11. stub measurement → verdict=fail",
          av.get("verdict") == "fail", f"verdict={av.get('verdict')}")
    check("B12. details mentions stub/unattested",
          "stub" in av.get("details", "").lower()
          or "unattested" in av.get("details", "").lower())


def test_worker_dispatches_malformed_input():
    """Non-JSON input doesn't crash the worker."""
    import poller  # type: ignore
    from poller import execute_in_envelope  # type: ignore

    tmp = Path(tempfile.mkdtemp(prefix="poller-av-bad-"))
    poller.ARTIFACTS_DIR = tmp / "artifacts"
    poller.KEY_DIR = tmp / "keys"

    env = {
        "job_id": "job_av_worker_bad",
        "encrypted_skill": "attestation-verifier",
        "encrypted_data": "this is not json",
        "result_pubkey": "0x",
        "requester_sig": "0x",
        "stripe_pi_id": "pi_demo_verify",
        "skill_hash": hashlib.sha256(b"attestation-verifier").hexdigest(),
    }
    out = execute_in_envelope(env)
    check("B13. non-JSON input → state=completed (graceful)",
          out.get("state") == "completed")
    av = out.get("result", {}).get("attestation_verdict", {})
    check("B14. non-JSON input → verdict=fail",
          av.get("verdict") == "fail")
    check("B15. details mentions parse/invalid JSON",
          "json" in av.get("details", "").lower()
          or "parse" in av.get("details", "").lower())


# ---- C. End-to-end registration ----------------------------------------------

def test_register_attestation_verifier():
    """Register attestation-verifier via POST /v1/skills, confirm
    /v1/discover advertises it, and confirm /v1/jobs writes an
    envelope with skill_prompt byte-equal to the registered
    prompt_template. Combines C1-C6 (registration) and C7-C10
    (dispatch wiring) into one test to share the same DB.
    """
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
            PROMPT = (
                "You are attestation-verifier (NVIDIA pillar). The input is a "
                "JSON attestation block. Return a JSON verdict.\n\n"
                "Attestation block: {data}"
            )
            manifest = {
                "name": "attestation-verifier",
                "version": "0.1.0",
                "description": ("Verify a TEE broker's SEV-SNP attestation "
                                "report and produce a signed verdict"),
                "wasm_manifest_hash": hashlib.sha256(b"attestation-verifier").hexdigest(),
                "entry_point": "main",
                "prompt_template": PROMPT,
                "resource_limits": {
                    "max_fuel": 5_000_000,
                    "max_duration_ms": 30_000,
                    "max_memory_mb": 128,
                },
            }
            resp = await client.post("/v1/skills", json=manifest,
                                     headers=SKILLS_AUTH_HEADER)
            check("C1. POST /v1/skills (attestation-verifier) returns 201",
                  resp.status == 201, f"status={resp.status}")
            body = await resp.json()
            check("C2. registered name == attestation-verifier",
                  body.get("name") == "attestation-verifier")
            check("C3. registered resource_limits.max_fuel == 5_000_000",
                  body.get("resource_limits", {}).get("max_fuel") == 5_000_000)
            check("C4. registered resource_limits.max_duration_ms == 30_000",
                  body.get("resource_limits", {}).get("max_duration_ms") == 30_000)

            # /v1/discover should advertise it
            resp = await client.get("/v1/discover")
            disc = await resp.json()
            check("C5. /v1/discover includes attestation-verifier",
                  "attestation-verifier" in disc.get("supported_skills", []),
                  f"supported_skills={disc.get('supported_skills')}")

            # GET /v1/skills/attestation-verifier should return it
            resp = await client.get("/v1/skills/attestation-verifier")
            fetched = await resp.json()
            check("C6. GET /v1/skills/attestation-verifier returns the manifest",
                  resp.status == 200
                  and fetched.get("name") == "attestation-verifier")

            # Dispatch wiring — POST /v1/jobs for attestation-verifier
            # writes an envelope with skill_prompt equal to the
            # registered prompt_template byte-for-byte.
            body = {
                "client_req_id": "av-dispatch",
                "encrypted_skill": "attestation-verifier",
                "encrypted_data": json.dumps({
                    "tee_type": "amd-sev-snp",
                    "measurement": "ab" * 48,
                }),
                "requester_sig": "0x",
                "result_pubkey": "0x",
                "stripe_pi_id": "pi_demo_verify",
            }
            resp = await client.post("/v1/jobs", json=body)
            check("C7. POST /v1/jobs (attestation-verifier) → 202",
                  resp.status == 202, f"body={await resp.json()}")
            job_id = (await resp.json())["job_id"]
            envelope_p = (daemon.BROKER_EFS_MOUNT / "jobs" / "inbox"
                          / f"{job_id}.json")
            check("C8. envelope written to inbox",
                  envelope_p.exists(), f"path={envelope_p}")
            env = json.loads(envelope_p.read_text())
            check("C9. envelope has skill_prompt field",
                  "skill_prompt" in env,
                  f"envelope keys={list(env.keys())}")
            check("C10. skill_prompt matches registered prompt_template",
                  env.get("skill_prompt") == PROMPT,
                  f"got first 60 chars: {env.get('skill_prompt', '')[:60]!r}")

    asyncio.run(_drive())


# (Old split version removed — dispatch wiring now lives in the
# registration test above to share one DB.)


# ---- D. Prompt-precedence pin ------------------------------------------------

def test_prompt_precedence_no_override_takes_deterministic():
    """When NO `skill_prompt` is in the envelope, the deterministic
    verdict path runs (skill == 'attestation-verifier' falls through
    to build_attestation_verdict, not the LLM ladder). Pins the
    `if skill == "attestation-verifier" and not env.get("skill_prompt")`
    gate in worker/poller.py:1828.
    """
    import poller  # type: ignore
    from poller import execute_in_envelope  # type: ignore

    tmp = Path(tempfile.mkdtemp(prefix="poller-av-det-"))
    poller.ARTIFACTS_DIR = tmp / "artifacts"
    poller.KEY_DIR = tmp / "keys"

    env = {
        "job_id": "job_av_deterministic",
        "encrypted_skill": "attestation-verifier",
        "encrypted_data": json.dumps({
            "tee_type": "amd-sev-snp",
            "measurement": "ab" * 48,
            "chip_id": "AMD-Milan-Det",
        }),
        "result_pubkey": "0x",
        "requester_sig": "0x",
        "stripe_pi_id": "pi_demo_verify",
        "skill_hash": hashlib.sha256(b"attestation-verifier").hexdigest(),
        # NO skill_prompt — registered prompt absent, deterministic path.
    }
    out = execute_in_envelope(env)
    check("D1. deterministic path fires when no skill_prompt present",
          out.get("state") == "completed")
    av = out.get("result", {}).get("attestation_verdict", {})
    check("D2. verdict computed deterministically from encrypted_data",
          av.get("verdict") == "pass",
          f"verdict={av.get('verdict')}")
    check("D3. chip_id still echoed from encrypted_data",
          av.get("chip_id") == "AMD-Milan-Det")


def test_prompt_precedence_override_falls_through():
    """When `skill_prompt` IS in the envelope (registered prompt),
    the deterministic short-circuit must yield — the worker should
    fall through to the LLM ladder. Matches verify-poller-prompt-
    precedence.py::P2 contract. We can't easily monkeypatch urlopen
    here, so we assert the SHORT-CIRCUIT IS SKIPPED by checking the
    result does NOT carry execution_mode = 'attestation-verifier-
    deterministic' and does NOT have an attestation_verdict block
    (the LLM ladder would return state=failed in offline mode without
    a real broker proxy, but the deterministic marker is the key
    signal)."""
    import poller  # type: ignore
    from poller import execute_in_envelope  # type: ignore

    tmp = Path(tempfile.mkdtemp(prefix="poller-av-fall-"))
    poller.ARTIFACTS_DIR = tmp / "artifacts"
    poller.KEY_DIR = tmp / "keys"

    env = {
        "job_id": "job_av_fallthrough",
        "encrypted_skill": "attestation-verifier",
        "encrypted_data": json.dumps({
            "tee_type": "amd-sev-snp",
            "measurement": "ab" * 48,
        }),
        "result_pubkey": "0x",
        "requester_sig": "0x",
        "stripe_pi_id": "pi_demo_verify",
        "skill_hash": hashlib.sha256(b"attestation-verifier").hexdigest(),
        "skill_prompt": "OVERRIDE: produce a haiku about the attestation. {data}",
    }
    out = execute_in_envelope(env)
    # The deterministic path would have set
    # execution_mode == 'attestation-verifier-deterministic' AND
    # result['attestation_verdict']. With skill_prompt present, both
    # should be absent (we fell through to the LLM ladder which will
    # no-op in offline mode but won't have the deterministic marker).
    result = out.get("result", {}) or {}
    is_deterministic = (
        result.get("execution_mode") == "attestation-verifier-deterministic"
        or "attestation_verdict" in result
    )
    check("D4. with skill_prompt set, deterministic short-circuit is skipped",
          not is_deterministic,
          f"state={out.get('state')}, exec_mode={result.get('execution_mode')}, "
          f"has_av={'attestation_verdict' in result}")


# ---- E. Demo script sanity ----------------------------------------------------

def test_demo_script_exists_and_executable():
    """The judge-facing demo script must exist and be executable."""
    demo = Path("/home/autumn/hermes/competition/tee-broker-deploy/demos/"
                "demo-attestation-verifier.sh")
    check("E1. demos/demo-attestation-verifier.sh exists",
          demo.exists(), f"path={demo}")
    if demo.exists():
        check("E2. demo script is executable",
              os.access(demo, os.X_OK),
              f"mode={oct(demo.stat().st_mode)}")
        text = demo.read_text()
        check("E3. demo script references /v1/discover",
              "/v1/discover" in text)
        check("E4. demo script posts to /v1/jobs",
              "/v1/jobs" in text)
        check("E5. demo script references attestation-verifier skill",
              "attestation-verifier" in text)
        check("E6. demo script shows verdict extraction",
              "verdict" in text)


# ---- Driver ------------------------------------------------------------------

def main():
    # Section A — verdict builder
    test_verdict_builder_pass()
    test_verdict_builder_stub_measurement()
    test_verdict_builder_signature_verifies()
    test_verdict_builder_garbage_input()
    # Section B — worker dispatch
    test_worker_dispatches_attestation_verifier()
    test_worker_dispatches_stub_measurement()
    test_worker_dispatches_malformed_input()
    # Section C — registration + dispatch wiring
    test_register_attestation_verifier()
    # Section D — prompt precedence
    test_prompt_precedence_no_override_takes_deterministic()
    test_prompt_precedence_override_falls_through()
    # Section E — demo script
    test_demo_script_exists_and_executable()

    print()
    print("=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print("Ad-hoc verification — attestation-verifier skill (NVIDIA pillar).")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()