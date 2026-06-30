"""Verify the dispatch wiring that connects POST /v1/skills to the worker.

When a registered prompt_template skill is invoked via /v1/jobs, the envelope
written to the worker's EFS inbox MUST include the resolved prompt_template as
``skill_prompt``. The worker poller prefers ``env["skill_prompt"]`` over its
hardcoded ``skill_prompts`` dict, so any registered prompt reaches the LLM.

This is the architectural gap flagged in the kanban t_ab320c7b comment: the
hardcoded skill_prompts dict in worker/poller.py was the ONLY path for any
skill — POST /v1/skills stored a prompt_template but it never reached the
worker. This test pins the broker side of the fix.

Properties verified:
  E1. submit_job() with an unknown skill writes envelope WITHOUT skill_prompt
      (nothing registered, hardcoded fallback expected)
  E2. After registering a prompt_template skill, submit_job() writes an envelope
      that includes the resolved prompt_template under "skill_prompt"
  E3. The skill_prompt in the envelope matches the registered prompt_template
      byte-for-byte (no template-substitution, no truncation)
  E4. submit_job() also works for the 3 built-in stubs (back-compat: built-ins
      don't get a skill_prompt because they're not in the skills table; the
      worker falls back to its hardcoded dict for these)
  E5. WASM-only skills (no prompt_template) write envelope WITHOUT skill_prompt
      (prompt-only dispatch path is intentional)
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# --- test env setup (must run BEFORE importing daemon) -----------------------
TEST_ROOT = Path(tempfile.mkdtemp(prefix="dispatch-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_REGION"] = "eu-west-1"
os.environ["BROKER_VPC_ID"] = ""
os.environ["BROKER_SUBNET_ID"] = ""
os.environ["BROKER_WORKER_SG"] = ""
os.environ["BROKER_WORKER_AMI"] = ""
os.environ["BROKER_WORKER_IAM_ROLE"] = ""
# Auth on POST /v1/skills (VULN-S2)
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
SKILLS_AUTH_HEADER = {"Authorization": "Bearer test-skills-key-deadbeef"}

DAEMON_DIR = Path(__file__).resolve().parent.parent / "broker-daemon"
sys.path.insert(0, str(DAEMON_DIR))

from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

import daemon  # noqa: E402

daemon.init_db()
(daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        print(f"[PASS] {label}")
        PASS += 1
    else:
        print(f"[FAIL] {label}  {detail}")
        FAIL += 1


def good_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def good_manifest(name: str, **overrides) -> dict:
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


def envelope_path(job_id: str) -> Path:
    return daemon.INBOX / f"{job_id}.json"


async def run() -> None:
    app = daemon.build_app()
    app.on_startup.clear()  # strip WorkerManager (needs AWS creds)

    server = TestServer(app)
    async with TestClient(server) as client:
        # -----------------------------------------------------------------
        # E1: unknown skill, no registration yet — no skill_prompt in envelope
        # -----------------------------------------------------------------
        body = {
            "client_req_id": "e1-unknown",
            "encrypted_skill": "totally-unknown-skill",
            "encrypted_data": "hello",
            "requester_sig": "0x",
            "result_pubkey": "0x",
            "stripe_pi_id": "pi_test_e1",
        }
        resp = await client.post("/v1/jobs", json=body)
        check("E1-prep. POST /v1/jobs unknown skill → 202",
              resp.status == 202, f"status={resp.status}")
        job_id = (await resp.json())["job_id"]
        env = json.loads(envelope_path(job_id).read_text())
        check("E1. unknown-skill envelope has NO skill_prompt field",
              "skill_prompt" not in env,
              f"envelope keys={list(env.keys())}")

        # -----------------------------------------------------------------
        # E4: built-in stub — also no skill_prompt in envelope (back-compat)
        # -----------------------------------------------------------------
        body = {
            "client_req_id": "e4-builtin",
            "encrypted_skill": "summarize",  # one of the BUILTIN_SKILLS
            "encrypted_data": "hello",
            "requester_sig": "0x",
            "result_pubkey": "0x",
            "stripe_pi_id": "pi_test_e4",
        }
        resp = await client.post("/v1/jobs", json=body)
        check("E4-prep. POST /v1/jobs built-in summarize → 202",
              resp.status == 202)
        job_id = (await resp.json())["job_id"]
        env = json.loads(envelope_path(job_id).read_text())
        check("E4. built-in stub envelope has NO skill_prompt field",
              "skill_prompt" not in env,
              "built-ins come from the worker's hardcoded dict, not the DB")

        # -----------------------------------------------------------------
        # E5: WASM-only skill — no prompt_template in DB → no skill_prompt
        # -----------------------------------------------------------------
        wasm_manifest = good_manifest(
            "wasm-only-test", version="0.1.0",
            wasm_manifest_hash=good_hash("wasm-only-test"))
        del wasm_manifest["prompt_template"]
        wasm_manifest["wasm_ref"] = {"uri": "s3://x/y.wasm", "size_bytes": 4096}
        resp = await client.post("/v1/skills", json=wasm_manifest,
                                 headers=SKILLS_AUTH_HEADER)
        check("E5-prep. register WASM-only skill → 201",
              resp.status == 201, f"status={resp.status}")

        body = {
            "client_req_id": "e5-wasm",
            "encrypted_skill": "wasm-only-test",
            "encrypted_data": "hello",
            "requester_sig": "0x",
            "result_pubkey": "0x",
            "stripe_pi_id": "pi_test_e5",
        }
        resp = await client.post("/v1/jobs", json=body)
        check("E5-prep2. POST /v1/jobs wasm-only → 202", resp.status == 202)
        job_id = (await resp.json())["job_id"]
        env = json.loads(envelope_path(job_id).read_text())
        check("E5. WASM-only envelope has NO skill_prompt field",
              "skill_prompt" not in env,
              "prompt-only dispatch path is intentional for wasm_ref skills")

        # -----------------------------------------------------------------
        # E2+E3: register a prompt-template skill, then submit — skill_prompt
        #        in envelope must equal the registered template byte-for-byte.
        # -----------------------------------------------------------------
        PROMPT = (
            "You are attestation-verifier. Parse the JSON input and emit:\n"
            '{"verdict": "pass"|"fail", "details": "...", "chip_id": "..."}\n'
            "Input: {{input}}"
        )
        m = good_manifest("attestation-verifier",
                          wasm_manifest_hash=good_hash("attestation-verifier"),
                          description="Verify a TEE broker's attestation report",
                          prompt_template=PROMPT)
        resp = await client.post("/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        check("E2-prep. register attestation-verifier → 201",
              resp.status == 201, f"status={resp.status} body={await resp.json()}")

        body = {
            "client_req_id": "e2-prompt",
            "encrypted_skill": "attestation-verifier",
            "encrypted_data": '{"tee_type":"amd-sev-snp","measurement":"abc"}',
            "requester_sig": "0x",
            "result_pubkey": "0x",
            "stripe_pi_id": "pi_test_e2",
        }
        resp = await client.post("/v1/jobs", json=body)
        check("E2. POST /v1/jobs registered skill → 202", resp.status == 202,
              f"body={await resp.json()}")
        job_id = (await resp.json())["job_id"]
        env = json.loads(envelope_path(job_id).read_text())
        check("E2b. envelope has skill_prompt field",
              "skill_prompt" in env,
              f"envelope keys={list(env.keys())}")
        check("E3. skill_prompt matches registered prompt_template byte-for-byte",
              env.get("skill_prompt") == PROMPT,
              f"got first 60 chars: {env.get('skill_prompt', '')[:60]!r}")
        # E3b: no template substitution happened ({{input}} still present)
        check("E3b. skill_prompt is NOT pre-rendered (template intact)",
              "{{input}}" in env.get("skill_prompt", ""),
              "template substitution must happen in the worker, not the broker")

    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print("Ad-hoc verification — dispatch wiring (POST /v1/skills → envelope.skill_prompt).")
    sys.exit(0 if FAIL == 0 else 1)


asyncio.run(run())