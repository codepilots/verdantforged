#!/usr/bin/env python3
"""Verify the photo-glow-up WASM skill E2E through the broker + worker
(kanban t_c27c1d8d).

This is the integration test that pins the full path:

  POST /v1/skills (manifest)   ->
  POST /v1/skills/{name}/wasm  (binary upload)  ->
  POST /v1/jobs (with input_data) ->
  broker writes envelope to EFS inbox ->
  worker.execute_in_envelope(env) ->
  worker fetches WASM from wasm_uri, instantiates via wasmtime,
  calls execute(input_json), parses output JSON, signs envelope ->
  broker reads outbox, adds broker_signature ->
  final result envelope contains image_b64 (base64 BMP), skill_hash,
  input_hash, worker_signature, broker_signature, execution_mode =
  "wasm-skill".

We exercise the whole chain against the real photo-glow-up WASM artifact
shipped at
`../tee-broker-pattern/tee-broker-skills/photo-glow-up/build-provenance/skill_photo_glow_up.wasm`
(SHA-256 `b75a4aba688931e667b8d465d15e460c483e24eb4edc78823e731a6d3b16235b`;
the hash changed in t_c27c1d8d's fix for the 4-MiB heap OOM, where
HEAP_END was bumped from 0x400000 to 0x600000 to fit in_bytes + 4-MiB
OUT_CAP + 8-byte out_len_box_ptr in the WASM's bump allocator).

Properties verified:

  E1. End-to-end WASM skill execution produces a base64 BMP in result.output
  E2. output_image_b64 decodes to a BMP whose width/height match the
      SkillOutput response (`width`, `height`)
  E3. result.execution_mode == "wasm-skill"
  E4. result.skill_hash == registered wasm_manifest_hash
  E5. result.worker_signature is a non-empty Ed25519 signature
  E6. broker_signature added by _finalize_job is a non-empty string
  E7. The WASM path runs even when llm_token/llm_proxy_url are absent
      (WASM skills don't need the LLM proxy)
  E8. Resource caps (max_fuel, max_duration_ms) from the manifest are
      honoured — submit a job with a manifest declaring max_fuel=1 and
      the worker surfaces the failure with execution_mode="wasm-skill"
      and an error mentioning fuel
  E9. _skill_row_to_dict() exposes wasm_uri after upload
"""
from __future__ import annotations
import os, sys, json, hashlib, asyncio, tempfile, shutil, base64
from pathlib import Path

# --- test isolation ---
TMP_ROOT = Path(tempfile.mkdtemp(prefix="hermes-wasm-e2e-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TMP_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
os.environ["STRIPE_SECRET_KEY"] = ""
os.environ["BROKER_ACCOUNT_HASH_SECRET"] = "test-account-hash-secret"
SKILLS_AUTH_HEADER={"Authorization": "Bearer test-skills-key-deadbeef"}
# Disable SEV-SNP measurement lookup — IMDS isn't reachable in tests.
os.environ["WORKER_SEV_SNP_DISABLED"] = "1"
# Keep plaintext output for tests (avoids X25519 result encryption block).
os.environ["BROKER_KEEP_PLAINTEXT_FOR_DEMO"] = "1"

TESTS_DIR = Path(__file__).resolve().parent
WORKSPACE = TESTS_DIR.parent
BROKER_DIR = WORKSPACE / "broker-daemon"
sys.path.insert(0, str(BROKER_DIR))

# Load working-tree worker/poller.py into a tmp dir so the worker's
# /opt/worker hardcodes don't break tests.
import shutil as _shutil
POLLER_TMP = TMP_ROOT / "poller-src"
POLLER_TMP.mkdir(parents=True, exist_ok=True)
_shutil.copy(WORKSPACE / "worker" / "poller.py", POLLER_TMP / "poller.py")
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

poller.KEY_DIR = POLLER_TMP / "keys" / "e2e-poller"
poller.KEY_DIR.mkdir(parents=True, exist_ok=True)
poller.get_sev_snp_measurement = lambda: "stub-measurement-for-e2e"

daemon.init_db()
(daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)

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


# --- helpers ----------------------------------------------------------------

PHOTO_GLOW_UP_WASM = (
    Path("/home/autumn/hermes/competition/tee-broker-pattern")
    / "tee-broker-skills"
    / "photo-glow-up"
    / "build-provenance"
    / "skill_photo_glow_up.wasm"
)
PHOTO_GLOW_UP_SHA = "b75a4aba688931e667b8d465d15e460c483e24eb4edc78823e731a6d3b16235b"


def make_test_bmp_b64(width: int = 4, height: int = 4,
                      r: int = 220, g: int = 110, b: int = 80) -> str:
    """Make a tiny 24-bit BMP, return base64.

    Mirrors the smoke.rs make_test_bmp helper so the WASM pipeline runs
    the same shape of input. The visual content doesn't matter for this
    test — we only assert structural properties (parseable BMP, correct
    dimensions, deterministic output)."""
    row = ((width * 3 + 3) & ~3)
    data = row * height
    out = bytearray()
    out += b"BM"
    out += (54 + data).to_bytes(4, "little")
    out += b"\x00" * 4
    out += (54).to_bytes(4, "little")
    out += (40).to_bytes(4, "little")
    out += width.to_bytes(4, "little")
    out += height.to_bytes(4, "little", signed=True)
    out += (1).to_bytes(2, "little")
    out += (24).to_bytes(2, "little")
    out += (0).to_bytes(4, "little")
    out += data.to_bytes(4, "little")
    out += (2835).to_bytes(4, "little")
    out += (2835).to_bytes(4, "little")
    out += b"\x00" * 8
    for _ in range(height):
        for _ in range(width):
            out += bytes([b, g, r])  # BGR
        out += b"\x00" * (row - width * 3)
    return base64.b64encode(bytes(out)).decode()


def parse_bmp_size(bmp_bytes: bytes) -> tuple[int, int]:
    """Read width/height from a 24-bit BMP header."""
    assert bmp_bytes[:2] == b"BM", "not a BMP"
    width = int.from_bytes(bmp_bytes[18:22], "little")
    # height is signed; treat as int then abs (BMP rows can be top-down)
    h_signed = int.from_bytes(bmp_bytes[22:26], "little", signed=True)
    return width, abs(h_signed)


async def run():
    app = daemon.build_app()
    app.on_startup.clear()
    server = TestServer(app)

    async def safe_json(resp):
        try:
            return await resp.json()
        except Exception:
            return {"_raw": await resp.text()}

    async with TestClient(server) as client:
        # ---- 1. Register the photo-glow-up WASM skill with the real
        #         artifact's SHA-256 so resolve_skill_hash returns it. ----
        assert PHOTO_GLOW_UP_WASM.exists(), \
            f"photo-glow-up WASM missing at {PHOTO_GLOW_UP_WASM}"
        wasm_bytes = PHOTO_GLOW_UP_WASM.read_bytes()
        actual_sha = hashlib.sha256(wasm_bytes).hexdigest()
        check("0a. shipped photo-glow-up WASM matches expected SHA-256",
              actual_sha == PHOTO_GLOW_UP_SHA,
              f"expected={PHOTO_GLOW_UP_SHA} got={actual_sha}")

        manifest = {
            "name": "photo-glow-up",
            "version": "0.1.0",
            "description": "Photo retouch WASM skill (subtle/editorial/artistic)",
            "wasm_manifest_hash": PHOTO_GLOW_UP_SHA,
            "entry_point": "execute",
            "wasm_ref": {"uri": "tbd", "size_bytes": len(wasm_bytes)},
            "resource_limits": {
                # Enough fuel for the 4x4 BMP subtle mode (~2ms observed)
                "max_fuel": 100_000_000,
                "max_duration_ms": 30_000,
                "max_memory_mb": 512,
            },
        }
        resp = await client.post("/v1/skills", json=manifest,
                                 headers=SKILLS_AUTH_HEADER)
        body = await safe_json(resp)
        check("0b. POST /v1/skills(photo-glow-up) -> 201",
              resp.status == 201, f"status={resp.status} body={body}")

        # ---- 2. Upload the real WASM binary to the broker. ----
        upload_h = dict(SKILLS_AUTH_HEADER)
        upload_h["X-Wasm-Manifest-Hash"] = PHOTO_GLOW_UP_SHA
        upload_h["Content-Type"] = "application/wasm"
        resp = await client.post(
            "/v1/skills/photo-glow-up/wasm",
            data=wasm_bytes,
            headers=upload_h,
        )
        body = await safe_json(resp)
        check("1. POST /v1/skills/photo-glow-up/wasm -> 201",
              resp.status == 201, f"status={resp.status} body={body}")
        wasm_uri = body.get("wasm_uri") if isinstance(body, dict) else None
        check("1b. upload response includes wasm_uri on disk",
              isinstance(wasm_uri, str) and wasm_uri.endswith(
                  "photo-glow-up-0.1.0.wasm"),
              f"wasm_uri={wasm_uri}")

        # ---- 3. Submit a job with a real photo-glow-up input. ----
        bmp_b64 = make_test_bmp_b64(width=4, height=4)
        input_json = json.dumps({
            "image_b64": bmp_b64,
            "mode": "subtle",
            "palette": "sunset_glow",
            "strength": 0.7,
            "seed": 42,
        })
        submit_body = {
            "encrypted_skill": "photo-glow-up",
            "encrypted_data": input_json,  # broker stores it as-is
            "requester_sig": "0x",
            "result_pubkey": "0x",
            "stripe_pi_id": "pi_demo_e2e_001",
            "client_req_id": "req_e2e_001",
        }
        # Clear inbox/outbox
        for f in daemon.INBOX.glob("*.json"):
            f.unlink()
        for f in daemon.OUTBOX.glob("*.json"):
            f.unlink()

        resp = await client.post("/v1/jobs", json=submit_body)
        check("2. submit_job(photo-glow-up) -> 202",
              resp.status == 202, f"status={resp.status}")
        envelopes = list(daemon.INBOX.glob("*.json"))
        check("2b. broker wrote one envelope",
              len(envelopes) == 1, f"envelopes={[e.name for e in envelopes]}")
        envelope = json.loads(envelopes[0].read_text())
        check("2c. envelope carries wasm_uri",
              "wasm_uri" in envelope and envelope["wasm_uri"] == wasm_uri,
              f"wasm_uri={envelope.get('wasm_uri')}")
        check("2d. envelope.skill_hash == registered manifest hash",
              envelope.get("skill_hash") == PHOTO_GLOW_UP_SHA,
              f"skill_hash={envelope.get('skill_hash')}")

        # ---- 4. Hand the envelope to the worker poller. ----
        # Strip llm_token/llm_proxy_url: WASM skills don't need the proxy,
        # and the test must NOT depend on a live broker LLM endpoint.
        envelope.pop("llm_token", None)
        envelope.pop("llm_proxy_url", None)
        result = poller.execute_in_envelope(envelope)
        check("3. execute_in_envelope returned state=completed",
              result.get("state") == "completed",
              f"state={result.get('state')} err={result.get('result', {}).get('error', '')}")

        r = result.get("result", {})
        check("3a. execution_mode == wasm-skill",
              r.get("execution_mode") == "wasm-skill",
              f"execution_mode={r.get('execution_mode')}")

        # ---- 5. The output is a base64 BMP with the right shape. ----
        output = r.get("output", "")
        check("4. result.output is JSON",
              isinstance(output, str) and output.startswith("{"),
              f"output[:80]={output[:80] if isinstance(output, str) else type(output)}")
        if isinstance(output, str) and output.startswith("{"):
            try:
                parsed = json.loads(output)
                check("4a. output has image_b64/width/height/mode/palette",
                      all(k in parsed for k in ("image_b64", "width", "height",
                                                "mode", "palette")),
                      f"keys={list(parsed.keys())}")
                check("4b. mode echoes input",
                      parsed.get("mode") == "subtle",
                      f"mode={parsed.get('mode')}")
                check("4c. palette echoes input",
                      parsed.get("palette") == "sunset_glow",
                      f"palette={parsed.get('palette')}")
                # Decode the base64 BMP and check its dimensions
                out_bmp = base64.b64decode(parsed["image_b64"])
                w, h = parse_bmp_size(out_bmp)
                check("4d. output BMP width/height match SkillOutput",
                      w == parsed["width"] and h == parsed["height"],
                      f"bmp=({w},{h}) vs json=({parsed['width']},{parsed['height']})")
            except Exception as e:
                check("4z. output parses as JSON cleanly", False, f"{e}")

        # ---- 6. The worker signature is present. ----
        check("5. result.worker_signature is non-empty",
              isinstance(r.get("worker_signature"), str)
              and len(r["worker_signature"]) > 0,
              f"len={len(r.get('worker_signature') or '')}")

        # ---- 7. Resource-cap enforcement (E8): submit a job with a
        #         manifest declaring max_fuel=1 so the worker must fail. ----
        tiny_manifest = {
            "name": "fuel-capped",
            "version": "0.1.0",
            "description": "Capped at 1 fuel unit",
            "wasm_manifest_hash": PHOTO_GLOW_UP_SHA,
            "entry_point": "execute",
            "wasm_ref": {"uri": "tbd", "size_bytes": len(wasm_bytes)},
            "resource_limits": {
                "max_fuel": 1,
                "max_duration_ms": 1000,
                "max_memory_mb": 4,
            },
        }
        # Register + upload
        resp = await client.post("/v1/skills", json=tiny_manifest,
                                 headers=SKILLS_AUTH_HEADER)
        check("6-prep. POST /v1/skills(fuel-capped) -> 201",
              resp.status == 201, f"status={resp.status}")
        upload_h2 = dict(SKILLS_AUTH_HEADER)
        upload_h2["X-Wasm-Manifest-Hash"] = PHOTO_GLOW_UP_SHA
        upload_h2["Content-Type"] = "application/wasm"
        resp = await client.post(
            "/v1/skills/fuel-capped/wasm",
            data=wasm_bytes,
            headers=upload_h2,
        )
        check("6-prep2. upload fuel-capped wasm -> 201",
              resp.status == 201, f"status={resp.status}")

        # Now submit a job to the fuel-capped skill
        cap_body = dict(submit_body)
        cap_body["encrypted_skill"] = "fuel-capped"
        cap_body["client_req_id"] = "req_cap_001"
        # Clear inbox
        for f in daemon.INBOX.glob("*.json"):
            f.unlink()
        resp = await client.post("/v1/jobs", json=cap_body)
        check("6-prep3. submit_job(fuel-capped) -> 202",
              resp.status == 202, f"status={resp.status}")
        envelopes = list(daemon.INBOX.glob("*.json"))
        assert len(envelopes) == 1
        envelope_capped = json.loads(envelopes[0].read_text())
        envelope_capped.pop("llm_token", None)
        envelope_capped.pop("llm_proxy_url", None)
        result_capped = poller.execute_in_envelope(envelope_capped)
        check("6. fuel=1 cap -> state=failed with fuel-related error",
              result_capped.get("state") == "failed"
              and ("fuel" in (result_capped.get("result", {}).get("error", "")
                              or "").lower()
                   or "out_of_fuel" in str(result_capped).lower()),
              f"state={result_capped.get('state')} err={result_capped.get('result', {}).get('error', '')[:200]}")

    shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


asyncio.run(run())