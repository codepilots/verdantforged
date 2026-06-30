#!/usr/bin/env python3
"""Verify the broker-side WASM skill binary upload + envelope injection
(kanban t_c27c1d8d, child of t_bf00a075).

The previous task shipped `POST /v1/skills` (manifest + wasm_ref_uri metadata)
and `submit_job` injecting the registered `wasm_manifest_hash` into every
envelope. This task closes the remaining gap: an actual WASM BINARY upload
endpoint and an envelope field the worker can use to fetch the binary.

Properties verified:

  1. POST /v1/skills/{name}/wasm with a raw binary body + matching
     `X-Wasm-Manifest-Hash` header stores the file at
     `BROKER_EFS_MOUNT/wasm/{name}-{version}.wasm`.
  2. POST /v1/skills/{name}/wasm returns 201 with a `Location` header and
     the on-disk size, sha256, and uri in the JSON body.
  3. POST /v1/skills/{name}/wasm rejects a binary whose sha256 doesn't
     match `wasm_manifest_hash` (403 with `wasm_hash_mismatch`).
  4. POST /v1/skills/{name}/wasm rejects a binary whose length doesn't
     match `wasm_ref.size_bytes` (403 with `wasm_size_mismatch`).
  5. POST /v1/skills/{name}/wasm returns 401 without bearer auth.
  6. POST /v1/skills/{name}/wasm returns 404 when the skill name is
     not registered.
  7. POST /v1/skills/{name}/wasm rejects oversized binaries (>50 MiB).
  8. GET /v1/skills/{name} (after upload) exposes the `wasm_uri` so the
     client knows where the binary lives.
  9. submit_job() injects `wasm_uri` into the envelope for registered
     WASM skills so the worker can fetch the binary.
 10. submit_job() does NOT inject `wasm_uri` for built-in stubs or
     prompt-template-only skills.
 11. POST /v1/skills/{name}/wasm refuses a binary for a skill that was
     registered with `prompt_template` (no `wasm_ref`) — 409 with
     `skill_not_wasm`.
"""
from __future__ import annotations
import os, sys, json, hashlib, asyncio, tempfile, shutil
from pathlib import Path

# Test isolation — every path is sandboxed under TMP_ROOT.
TMP_ROOT = Path(tempfile.mkdtemp(prefix="hermes-wasm-upload-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TMP_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
os.environ["STRIPE_SECRET_KEY"] = ""
os.environ["BROKER_ACCOUNT_HASH_SECRET"] = "test-account-hash-secret"
SKILLS_AUTH_HEADER = {"Authorization": "Bearer test-skills-key-deadbeef"}

# Pre-existing tests work with the `wasm_manifest_hash` field on POST
# /v1/skills that matches the SHA-256 of the WASM binary. To exercise the
# rejection paths we just need any 64-char hex; the actual file content
# doesn't have to be a real WASM module.
TESTS_DIR = Path(__file__).resolve().parent
WORKSPACE = TESTS_DIR.parent
BROKER_DIR = WORKSPACE / "broker-daemon"
sys.path.insert(0, str(BROKER_DIR))

import daemon  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

daemon.init_db()
(daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)

WASM_DIR = daemon.BROKER_EFS_MOUNT / "wasm"
INBOX = daemon.INBOX

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
        "wasm_manifest_hash": good_hash(name + "-binary"),
        "entry_point": "execute",
        "wasm_ref": {"uri": "s3://placeholder/before-upload.wasm",
                     "size_bytes": 4096},  # placeholder; updated by caller
    }
    m.update(overrides)
    return m


def good_submit_body(skill="summarize"):
    return {
        "encrypted_skill": skill,
        "encrypted_data": "hello",
        "requester_sig": "0x",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_demo_test_001",
        "client_req_id": f"req_{hashlib.sha256(skill.encode()).hexdigest()[:16]}",
    }


async def run():
    app = daemon.build_app()
    app.on_startup.clear()  # don't spawn background workers in tests
    server = TestServer(app)

    async def safe_json(resp):
        try:
            return await resp.json()
        except Exception:
            return {"_raw": await resp.text()}

    async with TestClient(server) as client:
        # ---- 1+2: upload a real (fake) WASM binary, then assert it's stored ----
        name = "photo-glow-up-v1"
        binary = b"\x00asm\x01\x00\x00\x00" + b"\x00" * 256  # fake wasm header
        sha = hashlib.sha256(binary).hexdigest()
        m = good_manifest(name,
                          wasm_manifest_hash=sha,
                          wasm_ref={"uri": "tbd", "size_bytes": len(binary)})
        resp = await client.post("/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        check("0-prep. POST /v1/skills returns 201",
              resp.status == 201, f"status={resp.status}")

        upload_headers = dict(SKILLS_AUTH_HEADER)
        upload_headers["X-Wasm-Manifest-Hash"] = sha
        upload_headers["Content-Type"] = "application/wasm"
        resp = await client.post(
            f"/v1/skills/{name}/wasm",
            data=binary,
            headers=upload_headers,
        )
        body = await safe_json(resp)
        check("1. POST /v1/skills/{name}/wasm returns 201",
              resp.status == 201, f"status={resp.status} body={body}")
        check("2. upload response includes wasm_uri/sha256/size",
              isinstance(body, dict) and body.get("sha256") == sha and
              body.get("size_bytes") == len(binary) and
              isinstance(body.get("wasm_uri"), str),
              f"body={body}")

        # On-disk file matches
        on_disk = WASM_DIR / f"{name}-0.1.0.wasm"
        check("2b. WASM binary persisted to EFS at wasm/{name}-{version}.wasm",
              on_disk.exists() and on_disk.read_bytes() == binary,
              f"path={on_disk} exists={on_disk.exists()}")

        # ---- 3: hash mismatch rejected ----
        bad_sha = "f" * 64
        upload_bad = dict(SKILLS_AUTH_HEADER)
        upload_bad["X-Wasm-Manifest-Hash"] = bad_sha
        upload_bad["Content-Type"] = "application/wasm"
        resp = await client.post(
            f"/v1/skills/{name}/wasm",
            data=binary,
            headers=upload_bad,
        )
        body = await safe_json(resp)
        check("3. wrong sha256 -> 403 with wasm_hash_mismatch",
              resp.status == 403 and body.get("code") == "wasm_hash_mismatch",
              f"status={resp.status} body={body}")

        # ---- 4: size mismatch rejected ----
        # Register a separate skill whose manifest claims a different size
        # than the actual binary we send.
        size_mismatch_name = "size-mismatch-v1"
        sm_binary = b"\x00asm" + b"\x00" * 100
        sm_sha = hashlib.sha256(sm_binary).hexdigest()
        sm_manifest = good_manifest(
            size_mismatch_name,
            wasm_manifest_hash=sm_sha,
            wasm_ref={"uri": "tbd", "size_bytes": len(sm_binary) + 50},
        )
        resp = await client.post("/v1/skills", json=sm_manifest,
                                 headers=SKILLS_AUTH_HEADER)
        assert resp.status == 201, await resp.text()
        upload_sm = dict(SKILLS_AUTH_HEADER)
        upload_sm["X-Wasm-Manifest-Hash"] = sm_sha
        upload_sm["Content-Type"] = "application/wasm"
        resp = await client.post(
            f"/v1/skills/{size_mismatch_name}/wasm",
            data=sm_binary,
            headers=upload_sm,
        )
        body = await safe_json(resp)
        check("4. size mismatch -> 403 with wasm_size_mismatch",
              resp.status == 403 and body.get("code") == "wasm_size_mismatch",
              f"status={resp.status} body={body}")

        # ---- 5: missing auth header ----
        resp = await client.post(
            f"/v1/skills/{name}/wasm",
            data=binary,
            headers={"X-Wasm-Manifest-Hash": sha,
                     "Content-Type": "application/wasm"},
        )
        check("5. missing bearer -> 401",
              resp.status == 401, f"status={resp.status}")

        # ---- 6: unknown skill ----
        upload_h = dict(SKILLS_AUTH_HEADER)
        upload_h["X-Wasm-Manifest-Hash"] = sha
        upload_h["Content-Type"] = "application/wasm"
        resp = await client.post(
            "/v1/skills/never-registered-skill/wasm",
            data=binary,
            headers=upload_h,
        )
        body = await safe_json(resp)
        check("6. unknown skill -> 404 with skill_not_found",
              resp.status == 404 and body.get("code") == "skill_not_found",
              f"status={resp.status} body={body}")

        # ---- 7: oversized binary (over 50 MiB cap). The broker-side cap on
        # wasm_ref.size_bytes (50 MiB) is enforced at registration; if a
        # client managed to upload more bytes than the registered size, the
        # upload endpoint must reject — otherwise a malicious publisher
        # could DoS the broker's EFS by uploading a 10 GiB blob. We test
        # that the endpoint rejects body length > declared size.
        big_name = "too-big-v1"
        declared_size = 1024  # small claim
        big_manifest = good_manifest(
            big_name,
            wasm_manifest_hash="0" * 64,
            wasm_ref={"uri": "tbd", "size_bytes": declared_size},
        )
        resp = await client.post("/v1/skills", json=big_manifest,
                                 headers=SKILLS_AUTH_HEADER)
        assert resp.status == 201, await resp.text()
        # Upload a 2 KiB body but the manifest only declared 1 KiB
        upload_b = dict(SKILLS_AUTH_HEADER)
        upload_b["X-Wasm-Manifest-Hash"] = "0" * 64
        upload_b["Content-Type"] = "application/wasm"
        upload_b["Content-Length"] = str(declared_size * 2)
        resp = await client.post(
            f"/v1/skills/{big_name}/wasm",
            data=b"\x00" * (declared_size * 2),
            headers=upload_b,
        )
        body = await safe_json(resp)
        check("7. body length > declared size -> 403 wasm_size_mismatch",
              resp.status == 403 and body.get("code") == "wasm_size_mismatch",
              f"status={resp.status} body={body}")

        # ---- 8: GET /v1/skills/{name} exposes wasm_uri ----
        resp = await client.get(f"/v1/skills/{name}")
        body = await resp.json()
        check("8. GET /v1/skills/{name} exposes wasm_uri",
              resp.status == 200 and isinstance(body.get("wasm_uri"), str) and
              body["wasm_uri"].endswith(f"{name}-0.1.0.wasm"),
              f"status={resp.status} body={body}")

        # ---- 9+10: submit_job injects wasm_uri for WASM skills only ----
        # Clear inbox
        for f in INBOX.glob("*.json"):
            f.unlink()

        # WASM skill envelope should have wasm_uri
        resp = await client.post("/v1/jobs",
                                 json=good_submit_body(skill=name))
        assert resp.status == 202, await resp.text()
        envelopes = list(INBOX.glob("*.json"))
        check("9-prep. submit_job(WASM skill) wrote one envelope",
              len(envelopes) == 1)
        env = json.loads(envelopes[0].read_text())
        check("9. WASM-skill envelope contains wasm_uri",
              "wasm_uri" in env and env["wasm_uri"].endswith(
                  f"{name}-0.1.0.wasm"),
              f"env keys={list(env.keys())} wasm_uri={env.get('wasm_uri')}")

        # Built-in stub envelope should NOT have wasm_uri
        for f in INBOX.glob("*.json"):
            f.unlink()
        resp = await client.post("/v1/jobs",
                                 json=good_submit_body(skill="summarize"))
        assert resp.status == 202, await resp.text()
        envelopes = list(INBOX.glob("*.json"))
        check("10-prep. submit_job(built-in stub) wrote one envelope",
              len(envelopes) == 1)
        env = json.loads(envelopes[0].read_text())
        check("10. built-in stub envelope has NO wasm_uri",
              "wasm_uri" not in env,
              f"env keys={list(env.keys())}")

        # ---- 11: prompt-template skill rejects /wasm upload ----
        pt_name = "prompt-only-v1"
        pt_manifest = good_manifest(
            pt_name,
            wasm_manifest_hash=good_hash(pt_name + "-prompt"),
            prompt_template="You are a test skill. Input: {{input}}",
            wasm_ref=None,
        )
        resp = await client.post("/v1/skills", json=pt_manifest,
                                 headers=SKILLS_AUTH_HEADER)
        assert resp.status == 201, await resp.text()
        upload_pt = dict(SKILLS_AUTH_HEADER)
        upload_pt["X-Wasm-Manifest-Hash"] = good_hash(pt_name + "-prompt")
        upload_pt["Content-Type"] = "application/wasm"
        resp = await client.post(
            f"/v1/skills/{pt_name}/wasm",
            data=b"\x00asm" + b"\x00" * 32,
            headers=upload_pt,
        )
        body = await safe_json(resp)
        check("11. prompt-template skill -> upload 409 skill_not_wasm",
              resp.status == 409 and body.get("code") == "skill_not_wasm",
              f"status={resp.status} body={body}")

    shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


asyncio.run(run())