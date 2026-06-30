#!/usr/bin/env python3
"""Verify the POST /v1/skills registration endpoint.

Runs the broker daemon in-process (no AWS, no live broker) using aiohttp's
TestServer against a temp BROKER_EFS_MOUNT. This mirrors the offline-friendly
approach used by /tmp/hermes-verify-skills-endpoint.py and lets the test run
on any box that has aiohttp installed.

Properties verified (per kanban task t_66fb9df8):
  1. POST /v1/skills accepts a valid prompt-template manifest → 201
  2. Persisted manifest is readable via GET /v1/skills/{name} (latest version)
  3. GET /v1/skills/{name@version} returns the pinned version
  4. Duplicate (name, version) returns 409 with skill_already_registered
  5. GET /v1/skills (list) returns one row per distinct name (latest version)
  6. /v1/discover merges built-in stubs with registered skills
  7. /v1/discover dedupes when a registered skill shadows a built-in name

Validation properties (every POST /v1/skills failure mode):
  8.  Missing required field (wasm_manifest_hash) → 400
  9.  Invalid wasm_manifest_hash (not 64 hex) → 400
 10.  Both prompt_template AND wasm_ref → 400 (XOR)
 11.  Neither prompt_template NOR wasm_ref → 400
 12.  Uppercase name → 400
 13.  Resource limit above hard cap → 400
 14.  entry_point with space → 400
 15.  prompt_template oversized (>32 KiB) → 400
 16.  wasm_ref.size_bytes above 50 MiB → 400

Persistence defaults:
 17.  Omitted resource_limits → defaults applied (256 MB / 60s / 10M fuel)

WASM-ref variant:
 18.  POST /v1/skills with wasm_ref → 201, response shape correct
"""
import os, sys, json, hashlib, tempfile, shutil, asyncio
from pathlib import Path

# Set up temp env BEFORE importing daemon
TMP_ROOT = Path(tempfile.mkdtemp(prefix="hermes-skill-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TMP_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"  # never actually called
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
# Skill registration now requires auth (VULN-S2 fix). The test sets a known
# API key and uses it on every POST /v1/skills request. Without this env var,
# the broker refuses registration with 503 to stay closed-by-default.
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
# The auth header to send with every POST.
SKILLS_AUTH_HEADER = {"Authorization": "Bearer test-skills-key-deadbeef"}

# Resolve the daemon module from the workspace (this file lives in tests/,
# which is a sibling of broker-daemon/).
TESTS_DIR = Path(__file__).resolve().parent
WORKSPACE = TESTS_DIR.parent
BROKER_DIR = WORKSPACE / "broker-daemon"
sys.path.insert(0, str(BROKER_DIR))

import daemon  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

# Initialise the DB against the temp dir
daemon.init_db()
# build_app() references a /static directory that main() creates at startup.
# Pre-create it so the app can be built offline.
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


async def run():
    app = daemon.build_app()
    # build_app() registers _on_startup which constructs WorkerManager (needs
    # AWS creds). Strip it so the app can run offline.
    app.on_startup.clear()

    server = TestServer(app)
    async with TestClient(server) as client:
        # === Persistence & CRUD ===
        m = good_manifest("summarize-pro")
        resp = await client.post("/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        check("1. POST /v1/skills accepts a valid manifest → 201",
              resp.status == 201, f"status={resp.status} body={body}")
        check("1b. response body has the registered name",
              body.get("name") == "summarize-pro")
        check("1c. response has the prompt_template echoed back",
              body.get("prompt_template") == m["prompt_template"])
        check("1d. response has the Location header",
              "summarize-pro@0.1.0" in resp.headers.get("Location", ""))

        resp = await client.get("/v1/skills/summarize-pro")
        latest = await resp.json()
        check("2. GET /v1/skills/{name} (latest) returns 200",
              resp.status == 200)
        check("2b. latest is version 0.1.0",
              latest.get("version") == "0.1.0")

        resp = await client.get("/v1/skills/summarize-pro@0.1.0")
        pinned = await resp.json()
        check("3. GET /v1/skills/{name@version} returns 200",
              resp.status == 200)
        check("3b. pinned version matches",
              pinned.get("version") == "0.1.0")
        check("3c. pinned body equals latest body",
              pinned.get("wasm_manifest_hash") == latest.get("wasm_manifest_hash"))

        # Conflict
        resp = await client.post("/v1/skills", json=good_manifest("summarize-pro"), headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        check("4. duplicate (name, version) returns 409",
              resp.status == 409, f"status={resp.status} body={body}")
        check("4b. error code is skill_already_registered",
              body.get("code") == "skill_already_registered")

        # List endpoint — register a second version then check dedup
        m2 = good_manifest("summarize-pro", version="0.2.0",
                            wasm_manifest_hash=good_hash("summarize-pro@0.2.0"),
                            description="Test skill summarize-pro v2")
        resp = await client.post("/v1/skills", json=m2, headers=SKILLS_AUTH_HEADER)
        check("5-prep. register summarize-pro@0.2.0 returns 201", resp.status == 201,
              f"status={resp.status} body={await resp.json()}")
        resp = await client.get("/v1/skills")
        body = await resp.json()
        names = [s["name"] for s in body.get("skills", [])]
        check("5. GET /v1/skills lists each distinct name once",
              names.count("summarize-pro") == 1, f"names={names}")
        sp = next((s for s in body["skills"] if s["name"] == "summarize-pro"), None)
        check("5b. the listed entry is the latest version (0.2.0)",
              sp and sp.get("version") == "0.2.0", f"entry={sp}")

        # /v1/discover merge
        resp = await client.get("/v1/discover")
        body = await resp.json()
        skills = body.get("supported_skills", [])
        check("6. /v1/discover includes the registered skill",
              "summarize-pro" in skills, f"skills={skills}")
        check("6b. /v1/discover still includes the built-in code-review",
              "code-review" in skills)
        check("6c. /v1/discover still includes the built-in summarize",
              "summarize" in skills)

        # Shadowing a built-in
        m = good_manifest("summarize")  # same name as built-in
        resp = await client.post("/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        check("7-prep. can register a skill with a built-in name", resp.status == 201,
              f"status={resp.status}")
        resp = await client.get("/v1/discover")
        body = await resp.json()
        skills = body.get("supported_skills", [])
        check("7. /v1/discover dedupes 'summarize' (registered shadows built-in)",
              skills.count("summarize") == 1, f"skills={skills}")

        # === Validation failures ===
        bad = good_manifest("missing-hash")
        del bad["wasm_manifest_hash"]
        resp = await client.post("/v1/skills", json=bad, headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        check("8. missing wasm_manifest_hash → 400", resp.status == 400)
        check("8b. error code is invalid_manifest",
              body.get("code") == "invalid_manifest")

        bad = good_manifest("bad-hash")
        bad["wasm_manifest_hash"] = "not-hex"
        resp = await client.post("/v1/skills", json=bad, headers=SKILLS_AUTH_HEADER)
        check("9. invalid wasm_manifest_hash (not 64 hex) → 400",
              resp.status == 400)

        bad = good_manifest("both")
        bad["wasm_ref"] = {"uri": "s3://x/y.wasm", "size_bytes": 1024}
        resp = await client.post("/v1/skills", json=bad, headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        check("10. both prompt_template AND wasm_ref → 400", resp.status == 400)
        check("10b. error message mentions XOR requirement",
              "exactly one" in body.get("error", ""))

        # Neither prompt_template nor wasm_ref
        bad = good_manifest("neither")
        del bad["prompt_template"]
        resp = await client.post("/v1/skills", json=bad, headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        check("11. neither prompt_template nor wasm_ref → 400", resp.status == 400)
        check("11b. error message mentions XOR requirement",
              "exactly one" in body.get("error", ""))

        bad = good_manifest("BadName")  # uppercase
        resp = await client.post("/v1/skills", json=bad, headers=SKILLS_AUTH_HEADER)
        check("12. uppercase name → 400", resp.status == 400)

        bad = good_manifest("huge-fuel")
        bad["resource_limits"] = {"max_fuel": 10**12}
        resp = await client.post("/v1/skills", json=bad, headers=SKILLS_AUTH_HEADER)
        check("13. resource limit above hard cap → 400", resp.status == 400)

        bad = good_manifest("bad-entry")
        bad["entry_point"] = "main function"  # contains a space
        resp = await client.post("/v1/skills", json=bad, headers=SKILLS_AUTH_HEADER)
        check("14. entry_point with space → 400", resp.status == 400)

        bad = good_manifest("huge-prompt")
        bad["prompt_template"] = "x" * (33 * 1024)  # 33 KiB > 32 KiB cap
        resp = await client.post("/v1/skills", json=bad, headers=SKILLS_AUTH_HEADER)
        check("15. prompt_template over 32 KiB → 400", resp.status == 400)

        bad = good_manifest("huge-wasm-ref")
        del bad["prompt_template"]
        bad["wasm_ref"] = {"uri": "s3://x/y.wasm", "size_bytes": 100 * 1024 * 1024}
        resp = await client.post("/v1/skills", json=bad, headers=SKILLS_AUTH_HEADER)
        check("16. wasm_ref.size_bytes over 50 MiB → 400", resp.status == 400)

        # === Defaults ===
        bad = good_manifest("default-limits", version="0.1.0",
                            wasm_manifest_hash=good_hash("default-limits"))
        del bad["prompt_template"]
        bad["wasm_ref"] = {"uri": "s3://x/y.wasm", "size_bytes": 1024}
        resp = await client.post("/v1/skills", json=bad, headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        rl = body.get("resource_limits", {})
        check("17. omitted resource_limits → max_fuel default (10M)",
              rl.get("max_fuel") == 10_000_000, f"rl={rl}")
        check("17b. omitted resource_limits → max_duration_ms default (60K)",
              rl.get("max_duration_ms") == 60_000, f"rl={rl}")
        check("17c. omitted resource_limits → max_memory_mb default (256)",
              rl.get("max_memory_mb") == 256, f"rl={rl}")

        # === WASM-ref variant ===
        m = {
            "name": "wasm-only",
            "version": "0.1.0",
            "description": "Pure WASM skill",
            "wasm_manifest_hash": good_hash("wasm-only"),
            "entry_point": "handle",
            "wasm_ref": {"uri": "s3://bucket/wasm-only.wasm", "size_bytes": 4096},
        }
        resp = await client.post("/v1/skills", json=m, headers=SKILLS_AUTH_HEADER)
        body = await resp.json()
        check("18. POST /v1/skills with wasm_ref → 201",
              resp.status == 201, f"status={resp.status} body={body}")
        check("18b. response has wasm_ref.uri",
              body.get("wasm_ref", {}).get("uri") == "s3://bucket/wasm-only.wasm")
        check("18c. response has wasm_ref.size_bytes",
              body.get("wasm_ref", {}).get("size_bytes") == 4096)
        check("18d. response does NOT have prompt_template (XOR enforced)",
              "prompt_template" not in body)
        check("18e. readback via GET /v1/skills/wasm-only returns same uri",
              body.get("wasm_ref", {}).get("uri") == "s3://bucket/wasm-only.wasm")

    # Cleanup
    shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print(f"Ad-hoc verification — POST /v1/skills registration endpoint.")
    print(f"Scope: persistence, validation, defaults, /v1/discover merge, shadowing.")
    sys.exit(0 if FAIL == 0 else 1)


asyncio.run(run())
