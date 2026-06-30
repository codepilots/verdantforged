"""Verify the 4 hackathon-critical security fixes from SECURITY_REVIEW.md.

Run locally — exercises daemon.py + poller.py code paths without needing a
live broker or worker.

Tests:
  A. SSRF webhook blocklist (VULN-S1)
     - 169.254.169.254 (AWS IMDS) -> reject
     - 10.0.0.5 (RFC1918)          -> reject
     - 192.168.1.1 (RFC1918)        -> reject
     - 127.0.0.1 (loopback)         -> reject
     - localhost hostname          -> reject
     - 8.8.8.8 (public)            -> accept
     - example.com hostname        -> accept
     - http:// (non-HTTPS)         -> reject
     - missing webhook_url         -> accept (webhook is optional)

  B. Skill registration bearer-token auth (VULN-S2)
     - Without BROKER_SKILLS_API_KEY env: registration REJECTED
     - With BROKER_SKILLS_API_KEY env:
       - missing Authorization header -> rejected
       - wrong Bearer token            -> rejected
       - correct Bearer token          -> accepted (validate_skill_manifest returns ok)
     - GET /v1/skills (list) public, no auth needed

  C. Plaintext output redaction (VULN-S3)
     - When result_encrypted is set + BROKER_KEEP_PLAINTEXT_FOR_DEMO unset/0:
       result['output'] == "[encrypted — see result_encrypted]"
     - When result_encrypted is set + BROKER_KEEP_PLAINTEXT_FOR_DEMO=1:
       result['output'] retains plaintext

  D. Skill routes wired (CQ-6 regression)
     - GET  /v1/skills         -> registered in build_app()
     - GET  /v1/skills/{ref}   -> registered in build_app()
     - POST /v1/skills         -> registered in build_app()

Run with any Python that has aiohttp+boto3+cryptography available.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import base64
import tempfile
import subprocess
from pathlib import Path

# ---- Test environment setup --------------------------------------------------
# Use a temp EFS mount so daemon.py import doesn't fail (it tries to mkdir
# $BROKER_EFS_MOUNT/logs at import time). We override the env BEFORE importing
# daemon.
TEST_ROOT = Path(tempfile.mkdtemp(prefix="secfix-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_REGION"] = "eu-west-1"
os.environ["BROKER_VPC_ID"] = ""
os.environ["BROKER_SUBNET_ID"] = ""
os.environ["BROKER_WORKER_SG"] = ""
os.environ["BROKER_WORKER_AMI"] = ""
os.environ["BROKER_WORKER_IAM_ROLE"] = ""
os.environ["DEMO_TOKEN_CAP"] = "50000"

DAEMON_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/broker-daemon"
WORKER_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/worker"
sys.path.insert(0, DAEMON_DIR)

PASS = 0
FAIL = 0
FAILURES: list[str] = []


# _bump_* helpers tolerate being called from nested failure handlers so a
# crash in one section doesn't make subsequent FAIL += raise UnboundLocalError.
def _bump_fail():
    global FAIL
    FAIL += 1


def _bump_pass():
    global PASS
    PASS += 1


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        print(f"[PASS] {label}")
        PASS += 1
    else:
        print(f"[FAIL] {label}" + (f"  ({detail})" if detail else ""))
        FAIL += 1
        FAILURES.append(label)


def fresh_daemon():
    """Re-import daemon with current env vars (some are read at import time)."""
    if "daemon" in sys.modules:
        del sys.modules["daemon"]
    import daemon  # noqa: E402
    return daemon


# ---- A. SSRF webhook blocklist -----------------------------------------------
def test_webhook_ssrf() -> None:
    """validate_submit must reject private/link-loopback webhook URLs."""
    daemon = fresh_daemon()
    validate_submit = daemon.validate_submit

    base = {
        "client_req_id": "x",
        "encrypted_skill": "summarize",
        "encrypted_data": "data",
        "requester_sig": "0x",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_test",
    }

    # Private/link-local IPs that MUST be rejected
    for bad_url in [
        "https://169.254.169.254/latest/meta-data/",      # AWS IMDS
        "https://10.0.0.5/hook",                           # RFC1918 10/8
        "https://172.16.0.1/hook",                         # RFC1918 172.16/12
        "https://192.168.1.1/hook",                        # RFC1918 192.168/16
        "https://127.0.0.1:8080/hook",                     # loopback
        "http://8.8.8.8/hook",                             # public IP but HTTP not HTTPS
        "http://169.254.169.254/",                         # IMDS + HTTP
    ]:
        body = {**base, "webhook_url": bad_url}
        ok, err = validate_submit(body)
        check(f"A1. reject {bad_url}", not ok,
              f"got ok=True err={err!r}" if ok else "")

    # Hostname that resolves to a private IP (localhost -> 127.0.0.1)
    body = {**base, "webhook_url": "https://localhost/hook"}
    ok, err = validate_submit(body)
    check("A2. reject https://localhost (resolves to 127.0.0.1)",
          not ok, f"got ok=True err={err!r}" if ok else "")

    # Public IPs/hostnames that MUST be accepted
    for ok_url in [
        "https://8.8.8.8/hook",
        "https://example.com/hook",
        "https://api.github.com/hook",
    ]:
        body = {**base, "webhook_url": ok_url}
        ok, err = validate_submit(body)
        check(f"A3. accept {ok_url}", ok,
              f"got ok=False err={err!r}" if not ok else "")

    # Empty webhook_url (optional) MUST be accepted
    body = {**base, "webhook_url": ""}
    ok, err = validate_submit(body)
    check("A4. accept empty webhook_url (optional field)", ok,
          f"got ok=False err={err!r}" if not ok else "")

    # Missing webhook_url MUST be accepted
    body = {**base}
    body.pop("webhook_url", None)
    ok, err = validate_submit(body)
    check("A5. accept missing webhook_url key", ok,
          f"got ok=False err={err!r}" if not ok else "")

    # Garbage URLs
    body = {**base, "webhook_url": "not-a-url-at-all"}
    ok, err = validate_submit(body)
    check("A6. reject garbage URL 'not-a-url-at-all'", not ok,
          f"got ok=True err={err!r}" if ok else "")


# ---- B. Skill registration bearer-token auth ---------------------------------
def test_skill_auth() -> None:
    """POST /v1/skills must require a valid Bearer token when API key is set,
    and must reject registrations when no key is configured."""
    from aiohttp.test_utils import make_mocked_request

    manifest = {
        "name": "secfix-test-skill",
        "version": "1.0.0",
        "description": "unit test",
        "wasm_manifest_hash": "a" * 64,
        "entry_point": "main",
        "prompt_template": "Summarize the following input.",
    }

    # ---- B5: missing API key => registration REJECTED ----
    os.environ.pop("BROKER_SKILLS_API_KEY", None)
    daemon = fresh_daemon()
    check("B5a. BROKER_SKILLS_API_KEY falsy when env unset",
          not daemon.BROKER_SKILLS_API_KEY,
          f"got {daemon.BROKER_SKILLS_API_KEY!r}")

    async def call_register(req):
        # Patch request.json to return our manifest
        async def _json():
            return manifest
        req.json = _json
        return await daemon.register_skill(req)

    async def run_no_key():
        daemon.init_db()  # ensure skills table exists for the list_skills check
        req = make_mocked_request("POST", "/v1/skills")
        resp = await call_register(req)
        check("B5b. POST /v1/skills rejected when no BROKER_SKILLS_API_KEY",
              resp.status in (401, 503),
              f"got {resp.status} (must refuse open registration when no key)")

        # GET /v1/skills (list) is public regardless of API key
        req = make_mocked_request("GET", "/v1/skills")
        resp = await daemon.list_skills(req)
        check("B5c. GET /v1/skills (list) public even with no API key",
              resp.status == 200, f"got {resp.status}")

    asyncio.run(run_no_key())

    # ---- B1-B4: API key set => require matching Bearer ----
    os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
    daemon = fresh_daemon()
    check("B0. BROKER_SKILLS_API_KEY loaded from env",
          daemon.BROKER_SKILLS_API_KEY == "test-skills-key-deadbeef",
          f"got {daemon.BROKER_SKILLS_API_KEY!r}")

    async def run_with_key():
        # No auth header
        req = make_mocked_request("POST", "/v1/skills")
        resp = await call_register(req)
        check("B1. POST /v1/skills with no Authorization -> 401",
              resp.status == 401, f"got {resp.status}")

        # Wrong token
        req = make_mocked_request(
            "POST", "/v1/skills",
            headers={"Authorization": "Bearer wrong-token"},
        )
        resp = await call_register(req)
        check("B2. POST /v1/skills with wrong Bearer -> 401",
              resp.status == 401, f"got {resp.status}")

        # Malformed Authorization header
        req = make_mocked_request(
            "POST", "/v1/skills",
            headers={"Authorization": "NotBearer foo"},
        )
        resp = await call_register(req)
        check("B2b. POST /v1/skills with non-Bearer scheme -> 401",
              resp.status == 401, f"got {resp.status}")

        # Correct token — should register (201)
        req = make_mocked_request(
            "POST", "/v1/skills",
            headers={"Authorization": "Bearer test-skills-key-deadbeef"},
        )
        resp = await call_register(req)
        check("B3. POST /v1/skills with correct Bearer -> 201",
              resp.status == 201, f"got {resp.status}")

        # GET /v1/skills (list) public
        req = make_mocked_request("GET", "/v1/skills")
        resp = await daemon.list_skills(req)
        check("B4. GET /v1/skills (list) public with key set",
              resp.status == 200, f"got {resp.status}")

    asyncio.run(run_with_key())


# ---- C. Plaintext output redaction (BROKER_KEEP_PLAINTEXT_FOR_DEMO) ----------
def test_plaintext_redaction() -> None:
    """When result_encrypted is set, result['output'] must be redacted unless
    BROKER_KEEP_PLAINTEXT_FOR_DEMO=1."""
    # Create the sandbox dirs in the PARENT process so we can pass the
    # paths to the subprocess via env vars (BROKER_WORKER_KEYS /
    # BROKER_EFS_LOGS) instead of string-rewriting the poller source.
    # This is the new contract since t_ab320c7b — KEY_DIR and LOGS now
    # honour these env vars (originally they were hardcoded constants).
    _tmp_efs = tempfile.mkdtemp(prefix="poller-test-")
    _sandbox = _tmp_efs + "/broker"
    _keys = _tmp_efs + "/worker/keys"
    Path(_sandbox + "/jobs/inbox").mkdir(parents=True)
    Path(_sandbox + "/jobs/outbox").mkdir(parents=True)
    Path(_sandbox + "/logs").mkdir(parents=True)
    Path(_keys).mkdir(parents=True)
    # Run in subprocess because poller.py reads BROKER_KEEP_PLAINTEXT_FOR_DEMO
    # at module import time AND tries to mkdir /mnt/broker at import time.
    # We rewrite poller.py paths via importlib so it doesn't touch /mnt/broker.
    test_script = r"""
import sys, os, json, base64, tempfile, hashlib
from pathlib import Path

# Build a sandboxed copy of poller.py with the hardcoded /mnt/broker +
# /opt/worker paths redirected to a tmp dir. This lets us exec the real
# module source (rather than reimplementing it in the test) without
# requiring root or touching real worker state.
src_path = "/home/autumn/hermes/tee-broker-deploy/worker/poller.py"
src_path = "/home/autumn/hermes/competition/tee-broker-deploy/worker/poller.py"
with open(src_path) as f:
    src = f.read()

tmp_efs = tempfile.mkdtemp(prefix="poller-test-")
sandbox = tmp_efs + "/broker"
keys = tmp_efs + "/worker/keys"
Path(sandbox + "/jobs/inbox").mkdir(parents=True)
Path(sandbox + "/jobs/outbox").mkdir(parents=True)
Path(sandbox + "/logs").mkdir(parents=True)
Path(keys).mkdir(parents=True)

# Rewrite the hardcoded Path constants to point inside our sandbox.
# Key insight: KEY_DIR and LOGS now read BROKER_WORKER_KEYS / BROKER_EFS_LOGS
# env vars (testability fix from t_ab320c7b) — we set those env vars below
# in the subprocess rather than rewriting the source. /mnt/broker paths are
# still hardcoded constants and DO need string replacement.
src = src.replace('Path("/mnt/broker/jobs/inbox")',  'Path("{sj}/jobs/inbox")'.format(sj=sandbox))
src = src.replace('Path("/mnt/broker/jobs/outbox")', 'Path("{sj}/jobs/outbox")'.format(sj=sandbox))
src = src.replace('Path("/mnt/broker/logs/worker-heartbeat.json")',
                  'Path("{sj}/logs/worker-heartbeat.json")'.format(sj=sandbox))

# Stub LLM endpoint so we don't make a real network call
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"

sandbox_path = tmp_efs + "/poller_sandbox.py"
with open(sandbox_path, "w") as f:
    f.write(src)

sys.path.insert(0, tmp_efs)
import poller_sandbox as mod  # noqa: E402

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization

req_priv = X25519PrivateKey.generate()
req_pub_b64 = base64.b64encode(
    req_priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
).decode()

env = {
    "job_id": "job_test_redact",
    "encrypted_skill": "summarize",
    "encrypted_data": "redaction test data",
    "result_pubkey": req_pub_b64,
    "stripe_pi_id": "pi_redact_test",
    "execution_mode": "llm-stub",
    # skill_hash is required by the poller (VULN-S hardening from
    # t_bf00a075). The broker always emits it in production envelopes;
    # this test mirrors that so the LLM-call path executes and the
    # redaction / encryption branches actually run.
    "skill_hash": hashlib.sha256(b"summarize").hexdigest(),
}
result = mod.execute_in_envelope(env)
inner = result.get("result", {})
print("RESULT_JSON:" + json.dumps({
    "output": inner.get("output"),
    "has_result_encrypted": "result_encrypted" in inner,
    "result_encryption_error": inner.get("result_encryption_error"),
    "demo_flag": os.environ.get("BROKER_KEEP_PLAINTEXT_FOR_DEMO", "0"),
}))
"""

    # First: default (flag unset / "0") -> redaction should apply
    proc = subprocess.run(
        [sys.executable, "-c", test_script],
        env={**os.environ, "BROKER_KEEP_PLAINTEXT_FOR_DEMO": "0",
             # KEY_DIR / LOGS now read these env vars (t_ab320c7b testability
             # fix) so the sandbox subprocess can use the tmp keys dir.
             # Format the paths with .format() because `keys` / `sandbox`
             # live inside the test_script raw string (subprocess scope),
             # not the parent process — explicit interpolation is required.
             "BROKER_WORKER_KEYS": _keys,
             "BROKER_EFS_LOGS": _sandbox + "/logs"},
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        print(f"[FAIL] C1 subprocess crashed: rc={proc.returncode}")
        print(f"  stdout: {proc.stdout!r}")
        print(f"  stderr: {proc.stderr!r}")
        FAIL += 1
        FAILURES.append("C1 (subprocess crashed)")
        return
    result_line = next(
        (l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON:")), ""
    )
    payload = json.loads(result_line[len("RESULT_JSON:"):])
    check("C1. result_encrypted is set (encryption succeeded)",
          payload.get("has_result_encrypted") is True,
          f"encryption_error={payload.get('result_encryption_error')!r}")
    check("C2. output redacted when BROKER_KEEP_PLAINTEXT_FOR_DEMO=0",
          payload.get("output") == "[encrypted — see result_encrypted]",
          f"got output={payload.get('output')!r}")

    # Second: flag=1 -> plaintext should be retained
    proc = subprocess.run(
        [sys.executable, "-c", test_script],
        env={**os.environ, "BROKER_KEEP_PLAINTEXT_FOR_DEMO": "1",
             "BROKER_WORKER_KEYS": _keys,
             "BROKER_EFS_LOGS": _sandbox + "/logs"},
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        print(f"[FAIL] C3 subprocess crashed: rc={proc.returncode}")
        print(f"  stdout: {proc.stdout!r}")
        print(f"  stderr: {proc.stderr!r}")
        FAIL += 1
        FAILURES.append("C3 (subprocess crashed)")
        return
    result_line = next(
        (l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON:")), ""
    )
    payload = json.loads(result_line[len("RESULT_JSON:"):])
    check("C3. output plaintext retained when BROKER_KEEP_PLAINTEXT_FOR_DEMO=1",
          payload.get("output") != "[encrypted — see result_encrypted]"
          and payload.get("output") is not None
          and payload.get("output") != "",
          f"got output={payload.get('output')!r}")


# ---- D. Skill routes wired (CQ-6 regression) ---------------------------------
def test_skill_routes_wired() -> None:
    """build_app() must register /v1/skills, /v1/skills/{ref} routes."""
    daemon = fresh_daemon()
    # build_app() requires the static directory to exist (used by add_static).
    # Create it under the test EFS mount before constructing the app.
    static_dir = daemon.BROKER_EFS_MOUNT / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app = daemon.build_app()
    routes = []
    for r in app.router.routes():
        try:
            canonical = r.resource.canonical if r.resource else "?"
        except Exception:
            canonical = "?"
        routes.append((r.method, canonical))

    check("D1. POST /v1/skills route registered",
          any(m == "POST" and "/v1/skills" == p for m, p in routes),
          f"routes={routes}")
    check("D2. GET /v1/skills route registered",
          any(m == "GET" and "/v1/skills" == p for m, p in routes),
          f"routes={routes}")
    check("D3. GET /v1/skills/{{ref}} route registered",
          any(m == "GET" and "/v1/skills/{ref}" == p for m, p in routes),
          f"routes={routes}")


# ---- Main --------------------------------------------------------------------
def main() -> int:
    print("=== A. SSRF webhook blocklist (VULN-S1) ===")
    test_webhook_ssrf()
    print()
    print("=== B. Skill registration bearer-token auth (VULN-S2) ===")
    test_skill_auth()
    print()
    print("=== C. Plaintext output redaction (VULN-S3) ===")
    test_plaintext_redaction()
    print()
    print("=== D. Skill routes wired (CQ-6 regression) ===")
    test_skill_routes_wired()
    print()
    print(f"=== Summary: PASS={PASS} FAIL={FAIL} ===")
    if FAIL:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())