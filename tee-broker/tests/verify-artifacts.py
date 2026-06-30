"""Verify the result-pack / artifact system.

Tests:
  A. worker/poller.py — write_artifacts()
     A1. writes output.txt + manifest.json under artifacts dir
     A2. manifest contains filename, content_type, sha256, size_bytes, role per file
     A3. total_size_bytes in manifest matches sum of file sizes
     A4. sha256 hashes are computed over actual file bytes
     A5. creates nested subdirs for filenames with path separators (e.g. "code/main.py")
     A6. primary output is always written as output.txt with role=primary
     A7. accepts both str and bytes for file data
     A8. empty files list only writes output.txt (no artifacts beyond primary)

  B. worker/poller.py — execute_in_envelope() integration
     B1. when env["artifacts"] is empty/absent, result envelope has no "artifacts" key
     B2. when env["artifacts"] is provided, result envelope has "artifacts" with
         manifest_path, count, total_size_bytes, files[]

  C. broker-daemon/daemon.py — get_job_artifacts handler
     C1. returns 404 when no artifacts exist for the job
     C2. returns the manifest JSON when artifacts exist

  D. broker-daemon/daemon.py — get_job_artifact_file handler
     D1. returns 404 for filenames not in the manifest
     D2. serves a file with the content-type from the manifest
     D3. rejects path-traversal attempts (../, encoded slashes)
     D4. rejects files in the job dir that are NOT in the manifest

  E. broker-daemon/daemon.py — get_job() enhancement
     E1. when the job has no artifacts, response is unchanged
     E2. when the job has artifacts, response.result.artifacts includes
         manifest_url and download_urls (one per file)

  F. broker-daemon/daemon.py — _deliver_webhook() artifact_urls
     F1. webhook payload includes artifact_urls.manifest and .files when artifacts present
     F2. webhook payload's artifact_urls is None when no artifacts

  G. broker-daemon/daemon.py — DB migration / _finalize_job
     G1. jobs.artifact_count column exists (default 0)
     G2. _finalize_job writes artifact_count to the row when result has artifacts

  H. broker-daemon/daemon.py — build_app() route wiring
     H1. GET /v1/jobs/{job_id}/artifacts is registered
     H2. GET /v1/jobs/{job_id}/artifacts/{filename} is registered

Run locally — exercises daemon.py + poller.py code paths without needing a
live broker or worker.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import base64
import tempfile
import hashlib
import subprocess
from pathlib import Path

PASS = 0
FAIL = 0
FAILURES: list[str] = []


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


# ---- Test environment setup --------------------------------------------------
# Use temp EFS mount so daemon.py import doesn't fail.
TEST_ROOT = Path(tempfile.mkdtemp(prefix="artifacts-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_REGION"] = "eu-west-1"
os.environ["BROKER_VPC_ID"] = ""
os.environ["BROKER_SUBNET_ID"] = ""
os.environ["BROKER_WORKER_SG"] = ""
os.environ["BROKER_WORKER_AMI"] = ""
os.environ["BROKER_WORKER_IAM_ROLE"] = ""
os.environ["DEMO_TOKEN_CAP"] = "50000"
# Default key for skill auth (irrelevant to artifact tests but daemon imports it)
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key"

DAEMON_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/broker-daemon"
WORKER_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/worker"
sys.path.insert(0, DAEMON_DIR)


def fresh_daemon():
    if "daemon" in sys.modules:
        del sys.modules["daemon"]
    import daemon  # noqa: E402
    return daemon


# ---- A. worker/poller.py — write_artifacts() ---------------------------------
def test_write_artifacts_basic() -> None:
    """write_artifacts() writes output.txt + manifest.json with correct hashes."""
    # Build a sandboxed copy of poller.py with hardcoded paths redirected.
    src_path = os.path.join(WORKER_DIR, "poller.py")
    with open(src_path) as f:
        src = f.read()

    tmp_efs = tempfile.mkdtemp(prefix="poller-art-test-")
    sandbox = os.path.join(tmp_efs, "broker")
    keys = os.path.join(tmp_efs, "worker", "keys")
    Path(sandbox, "jobs", "inbox").mkdir(parents=True)
    Path(sandbox, "jobs", "outbox").mkdir(parents=True)
    Path(sandbox, "jobs", "artifacts").mkdir(parents=True)
    Path(sandbox, "logs").mkdir(parents=True)
    Path(keys).mkdir(parents=True)

    src = src.replace('Path("/mnt/broker/jobs/inbox")',
                      f'Path("{sandbox}/jobs/inbox")')
    src = src.replace('Path("/mnt/broker/jobs/outbox")',
                      f'Path("{sandbox}/jobs/outbox")')
    src = src.replace('Path(os.environ.get("BROKER_ARTIFACTS_DIR", "/mnt/broker/jobs/artifacts"))',
                      f'Path("{sandbox}/jobs/artifacts")')
    src = src.replace('Path("/mnt/broker/logs/worker-heartbeat.json")',
                      f'Path("{sandbox}/logs/worker-heartbeat.json")')
    src = src.replace('Path("/opt/worker/keys")', f'Path("{keys}")')

    sandbox_path = os.path.join(tmp_efs, "poller_sandbox.py")
    with open(sandbox_path, "w") as f:
        f.write(src)

    sys.path.insert(0, tmp_efs)
    import poller_sandbox as mod  # noqa: E402

    # ---- A1+A2+A6: write artifacts, check manifest ----
    job_id = "job_art_test_1"
    primary = "hello world\n"
    files = [
        {"filename": "image.bmp", "content_type": "image/bmp",
         "data": b"\x42\x4d" + b"\x00" * 100, "role": "artifact"},
        {"filename": "report.pdf", "content_type": "application/pdf",
         "data": b"%PDF-1.4\n%fake pdf bytes\n", "role": "artifact"},
    ]
    manifest = mod.write_artifacts(job_id, files, primary)

    art_dir = Path(sandbox) / "jobs" / "artifacts" / job_id
    check("A1. manifest.json exists in artifacts dir",
          (art_dir / "manifest.json").exists())
    check("A1b. output.txt exists in artifacts dir",
          (art_dir / "output.txt").exists())
    check("A1c. image.bmp exists in artifacts dir",
          (art_dir / "image.bmp").exists())
    check("A1d. report.pdf exists in artifacts dir",
          (art_dir / "report.pdf").exists())

    # Manifest entries
    entries = {a["filename"]: a for a in manifest["artifacts"]}
    check("A2. manifest has 3 entries (output.txt + 2 artifacts)",
          len(manifest["artifacts"]) == 3,
          f"got {len(manifest['artifacts'])}")
    check("A2b. manifest entry for output.txt has role=primary",
          entries.get("output.txt", {}).get("role") == "primary",
          f"got {entries.get('output.txt', {}).get('role')!r}")
    check("A2c. manifest entry has content_type field",
          all("content_type" in a for a in manifest["artifacts"]))
    check("A2d. manifest entry has sha256 field (64 hex chars)",
          all(len(a.get("sha256", "")) == 64 for a in manifest["artifacts"]))
    check("A2e. manifest entry has size_bytes field",
          all(isinstance(a.get("size_bytes"), int) for a in manifest["artifacts"]))

    # A3: total_size_bytes
    expected_total = sum(a["size_bytes"] for a in manifest["artifacts"])
    check("A3. total_size_bytes == sum of file sizes",
          manifest["total_size_bytes"] == expected_total,
          f"got {manifest['total_size_bytes']} expected {expected_total}")

    # A4: sha256 over actual bytes
    expected_sha = hashlib.sha256(b"\x42\x4d" + b"\x00" * 100).hexdigest()
    check("A4. image.bmp sha256 matches actual file bytes",
          entries["image.bmp"]["sha256"] == expected_sha,
          f"got {entries['image.bmp']['sha256']} expected {expected_sha}")

    # A5: nested subdirs
    job_id_nested = "job_art_nested"
    nested_files = [
        {"filename": "code/main.py", "content_type": "text/x-python",
         "data": "print('hi')\n", "role": "artifact"},
    ]
    mod.write_artifacts(job_id_nested, nested_files, "primary text")
    check("A5. nested dir created for code/main.py",
          (Path(sandbox) / "jobs" / "artifacts" / job_id_nested / "code" / "main.py").exists())

    # A7: bytes and str data
    job_id_mixed = "job_art_mixed"
    mixed_files = [
        {"filename": "a.txt", "content_type": "text/plain", "data": "text data", "role": "artifact"},
        {"filename": "b.bin", "content_type": "application/octet-stream",
         "data": b"\x00\x01\x02", "role": "artifact"},
    ]
    mod.write_artifacts(job_id_mixed, mixed_files, "primary")
    check("A7a. str data written correctly",
          (Path(sandbox) / "jobs" / "artifacts" / job_id_mixed / "a.txt").read_text() == "text data")
    check("A7b. bytes data written correctly",
          (Path(sandbox) / "jobs" / "artifacts" / job_id_mixed / "b.bin").read_bytes() == b"\x00\x01\x02")

    # A8: empty files list
    job_id_empty = "job_art_empty"
    manifest_empty = mod.write_artifacts(job_id_empty, [], "just primary")
    check("A8. empty files list only writes output.txt (1 manifest entry)",
          len(manifest_empty["artifacts"]) == 1
          and manifest_empty["artifacts"][0]["filename"] == "output.txt",
          f"got {len(manifest_empty['artifacts'])} entries")


# ---- B. worker/poller.py — execute_in_envelope() integration -----------------
def test_execute_in_envelope_artifacts() -> None:
    """execute_in_envelope() emits result['artifacts'] when env['artifacts'] set."""
    src_path = os.path.join(WORKER_DIR, "poller.py")
    with open(src_path) as f:
        src = f.read()

    tmp_efs = tempfile.mkdtemp(prefix="poller-art2-test-")
    sandbox = os.path.join(tmp_efs, "broker")
    keys = os.path.join(tmp_efs, "worker", "keys")
    Path(sandbox, "jobs", "inbox").mkdir(parents=True)
    Path(sandbox, "jobs", "outbox").mkdir(parents=True)
    Path(sandbox, "jobs", "artifacts").mkdir(parents=True)
    Path(sandbox, "logs").mkdir(parents=True)
    Path(keys).mkdir(parents=True)

    src = src.replace('Path("/mnt/broker/jobs/inbox")',
                      f'Path("{sandbox}/jobs/inbox")')
    src = src.replace('Path("/mnt/broker/jobs/outbox")',
                      f'Path("{sandbox}/jobs/outbox")')
    src = src.replace('Path(os.environ.get("BROKER_ARTIFACTS_DIR", "/mnt/broker/jobs/artifacts"))',
                      f'Path("{sandbox}/jobs/artifacts")')
    src = src.replace('Path("/mnt/broker/logs/worker-heartbeat.json")',
                      f'Path("{sandbox}/logs/worker-heartbeat.json")')
    src = src.replace('Path("/opt/worker/keys")', f'Path("{keys}")')

    # Force LLM to fail fast — we don't want to make a real call.
    os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"

    sandbox_path = os.path.join(tmp_efs, "poller_sandbox.py")
    with open(sandbox_path, "w") as f:
        f.write(src)

    sys.path.insert(0, tmp_efs)
    import poller_sandbox as mod  # noqa: E402

    # B1: no artifacts in env -> no "artifacts" key in result
    env_no_art = {
        "job_id": "job_no_artifacts",
        "encrypted_skill": "summarize",
        "encrypted_data": "data",
        "result_pubkey": "",
        "stripe_pi_id": "pi_test",
    }
    result1 = mod.execute_in_envelope(env_no_art)
    check("B1. no artifacts key in result when env has no artifacts",
          "artifacts" not in result1["result"],
          f"result keys: {list(result1['result'].keys())}")

    # B2: artifacts in env -> artifacts key in result
    env_with_art = {
        "job_id": "job_with_artifacts",
        "encrypted_skill": "summarize",
        "encrypted_data": "data",
        "result_pubkey": "",
        "stripe_pi_id": "pi_test",
        "artifacts": [
            {"filename": "out.txt", "content_type": "text/plain",
             "data": "hi", "role": "artifact"},
        ],
    }
    result2 = mod.execute_in_envelope(env_with_art)
    res2 = result2["result"]
    check("B2a. artifacts key present in result when env has artifacts",
          "artifacts" in res2)
    if "artifacts" in res2:
        arts = res2["artifacts"]
        check("B2b. result.artifacts has manifest_path field",
              "manifest_path" in arts)
        check("B2c. result.artifacts has count field",
              arts.get("count") == 1,
              f"got count={arts.get('count')}")
        check("B2d. result.artifacts has total_size_bytes field",
              "total_size_bytes" in arts)
        check("B2e. result.artifacts.files list has filename/content_type/sha256/size_bytes",
              len(arts.get("files", [])) == 1
              and all(k in arts["files"][0] for k in
                      ("filename", "content_type", "sha256", "size_bytes")))


# ---- C. broker-daemon/daemon.py — get_job_artifacts ---------------------------
def test_get_job_artifacts_handler() -> None:
    daemon = fresh_daemon()
    daemon.init_db()

    # Write a fake manifest into the artifact dir
    job_id = "job_daemon_art"
    art_dir = daemon.ARTIFACTS_DIR / job_id
    art_dir.mkdir(parents=True, exist_ok=True)
    fake_manifest = {
        "job_id": job_id,
        "artifacts": [
            {"filename": "output.txt", "content_type": "text/plain",
             "size_bytes": 5, "sha256": "x" * 64, "role": "primary"},
        ],
        "total_size_bytes": 5,
    }
    (art_dir / "manifest.json").write_text(json.dumps(fake_manifest))

    from aiohttp.test_utils import make_mocked_request

    async def run():
        # C1: unknown job -> 404
        req = make_mocked_request("GET", "/v1/jobs/job_nonexistent/artifacts")
        # route match_info keys
        req.match_info["job_id"] = "job_nonexistent"
        resp = await daemon.get_job_artifacts(req)
        check("C1. 404 for job with no artifacts",
              resp.status == 404, f"got {resp.status}")

        # C2: existing job -> returns manifest JSON
        req = make_mocked_request("GET", f"/v1/jobs/{job_id}/artifacts")
        req.match_info["job_id"] = job_id
        resp = await daemon.get_job_artifacts(req)
        check("C2a. 200 for job with artifacts", resp.status == 200,
              f"got {resp.status}")
        body = json.loads(resp.text)
        check("C2b. manifest body has artifacts list",
              isinstance(body.get("artifacts"), list)
              and len(body["artifacts"]) == 1)

    asyncio.run(run())


# ---- D. broker-daemon/daemon.py — get_job_artifact_file -----------------------
def test_get_job_artifact_file_handler() -> None:
    daemon = fresh_daemon()
    daemon.init_db()

    job_id = "job_daemon_file"
    art_dir = daemon.ARTIFACTS_DIR / job_id
    art_dir.mkdir(parents=True, exist_ok=True)
    file_bytes = b"hello image bytes"
    (art_dir / "image.bin").write_bytes(file_bytes)
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    fake_manifest = {
        "job_id": job_id,
        "artifacts": [
            {"filename": "image.bin", "content_type": "application/octet-stream",
             "size_bytes": len(file_bytes), "sha256": file_sha, "role": "artifact"},
        ],
        "total_size_bytes": len(file_bytes),
    }
    (art_dir / "manifest.json").write_text(json.dumps(fake_manifest))

    from aiohttp.test_utils import make_mocked_request

    async def run():
        # D1: filename not in manifest -> 404
        req = make_mocked_request("GET", f"/v1/jobs/{job_id}/artifacts/secret.txt")
        req.match_info["job_id"] = job_id
        req.match_info["filename"] = "secret.txt"
        resp = await daemon.get_job_artifact_file(req)
        check("D1. 404 for filename not in manifest", resp.status == 404,
              f"got {resp.status}")

        # D2: filename in manifest -> 200 with correct content-type
        req = make_mocked_request("GET", f"/v1/jobs/{job_id}/artifacts/image.bin")
        req.match_info["job_id"] = job_id
        req.match_info["filename"] = "image.bin"
        resp = await daemon.get_job_artifact_file(req)
        check("D2a. 200 for filename in manifest", resp.status == 200,
              f"got {resp.status}")
        check("D2b. Content-Type header matches manifest",
              resp.headers.get("Content-Type", "").startswith("application/octet-stream"),
              f"got {resp.headers.get('Content-Type')!r}")
        # FileResponse is a streaming response (no in-memory body) — verify the
        # underlying file path points at the artifact we wrote.
        served_path = getattr(resp, "_path", None) or getattr(resp, "path", None)
        expected_path = (daemon.ARTIFACTS_DIR / job_id / "image.bin").resolve()
        check("D2c. FileResponse serves the artifact file",
              served_path is not None and Path(served_path).resolve() == expected_path,
              f"got served_path={served_path!r}")

        # D3: path traversal attempts
        # aiohttp route matching doesn't decode ".." but we test via filename arg
        for traversal in ["../etc/passwd", "../../secret", "foo/../../bar"]:
            req = make_mocked_request(
                "GET", f"/v1/jobs/{job_id}/artifacts/{traversal}")
            req.match_info["job_id"] = job_id
            req.match_info["filename"] = traversal
            resp = await daemon.get_job_artifact_file(req)
            check(f"D3. reject path traversal '{traversal}' -> 404",
                  resp.status == 404, f"got {resp.status}")

        # D4: file present in job dir but NOT in manifest -> 404
        (art_dir / "not_in_manifest.bin").write_bytes(b"sneaky")
        req = make_mocked_request(
            "GET", f"/v1/jobs/{job_id}/artifacts/not_in_manifest.bin")
        req.match_info["job_id"] = job_id
        req.match_info["filename"] = "not_in_manifest.bin"
        resp = await daemon.get_job_artifact_file(req)
        check("D4. 404 for file in dir but not in manifest",
              resp.status == 404, f"got {resp.status}")

    asyncio.run(run())


# ---- E. broker-daemon/daemon.py — get_job() enhancement ----------------------
def test_get_job_includes_artifact_urls() -> None:
    daemon = fresh_daemon()
    daemon.init_db()

    job_id = "job_getjob"
    request_body = json.dumps({
        "encrypted_skill": "summarize",
        "encrypted_data": "x",
        "result_pubkey": "",
    })
    artifacts_summary = {
        "manifest_path": f"/mnt/broker/jobs/artifacts/{job_id}/manifest.json",
        "count": 2,
        "total_size_bytes": 200,
        "files": [
            {"filename": "a.png", "content_type": "image/png",
             "sha256": "x" * 64, "size_bytes": 100},
            {"filename": "b.txt", "content_type": "text/plain",
             "sha256": "y" * 64, "size_bytes": 100},
        ],
    }
    result_blob = json.dumps({
        "output": "hello",
        "artifacts": artifacts_summary,
    })
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
            "request_body, result) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, "req_getjob", "2026-01-01T00:00:00", "completed",
             request_body, result_blob),
        )

    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", f"/v1/jobs/{job_id}")
    req.match_info["job_id"] = job_id
    resp = asyncio.run(daemon.get_job(req))
    check("E1. get_job returns 200 for existing job",
          resp.status == 200, f"got {resp.status}")
    body = json.loads(resp.text)
    res = body.get("result", {})
    arts = res.get("artifacts")
    check("E2a. result.artifacts includes manifest_url",
          arts and arts.get("manifest_url") == f"/v1/jobs/{job_id}/artifacts",
          f"got {arts.get('manifest_url')!r}" if arts else "no arts")
    check("E2b. result.artifacts.download_urls has entry for a.png",
          arts and arts.get("download_urls", {}).get("a.png")
          == f"/v1/jobs/{job_id}/artifacts/a.png",
          f"got {arts.get('download_urls')!r}" if arts else "no arts")
    check("E2c. result.artifacts.download_urls has entry for b.txt",
          arts and arts.get("download_urls", {}).get("b.txt")
          == f"/v1/jobs/{job_id}/artifacts/b.txt")


# ---- E3. get_job without artifacts: no download_urls ----------------------------
def test_get_job_without_artifacts() -> None:
    daemon = fresh_daemon()
    daemon.init_db()
    job_id = "job_no_art"
    request_body = json.dumps({"encrypted_skill": "summarize"})
    result_blob = json.dumps({"output": "plain only"})
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
            "request_body, result) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, "req_no_art", "2026-01-01T00:00:00", "completed",
             request_body, result_blob),
        )
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", f"/v1/jobs/{job_id}")
    req.match_info["job_id"] = job_id
    resp = asyncio.run(daemon.get_job(req))
    body = json.loads(resp.text)
    res = body.get("result", {})
    check("E3. result without artifacts has no download_urls",
          "artifacts" not in res or "download_urls" not in res.get("artifacts", {}))


# ---- F. broker-daemon/daemon.py — _deliver_webhook artifact_urls --------------
def test_deliver_webhook_artifact_urls() -> None:
    daemon = fresh_daemon()
    daemon.init_db()

    # Insert a job with a webhook URL pointing to a localhost test endpoint.
    # We use a netcat-style: capture the request via a real HTTP server in this
    # process using aiohttp's test server. Instead, monkey-patch aiohttp.ClientSession
    # to capture the JSON posted.

    captured: list[dict] = []

    class FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None):
            captured.append({"url": url, "json": json})
            return FakeResp()

    # ---- F1: with artifacts ----
    job_id = "job_hook_art"
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
            "request_body, webhook_url) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, "req_hook_art", "2026-01-01T00:00:00", "completed",
             "{}", "https://example.com/hook"),
        )
    payload_with = {
        "job_id": job_id,
        "state": "completed",
        "result": {
            "output": "x",
            "artifacts": {
                "count": 1,
                "files": [
                    {"filename": "img.png", "content_type": "image/png",
                     "sha256": "z" * 64, "size_bytes": 50},
                ],
            },
        },
    }

    orig_session = daemon.aiohttp.ClientSession
    daemon.aiohttp.ClientSession = FakeSession
    try:
        asyncio.run(daemon._deliver_webhook(job_id, "https://example.com/hook",
                                            payload_with, "completed"))
    finally:
        daemon.aiohttp.ClientSession = orig_session

    check("F1a. webhook captured 1 POST",
          len(captured) == 1, f"got {len(captured)}")
    if captured:
        body = captured[0]["json"]
        check("F1b. webhook body has artifact_urls key",
              "artifact_urls" in body)
        au = body.get("artifact_urls")
        check("F1c. artifact_urls.manifest is a URL",
              isinstance(au, dict) and "/v1/jobs/" in au.get("manifest", ""),
              f"got {au!r}" if au else "no au")
        check("F1d. artifact_urls.files has img.png entry",
              isinstance(au, dict) and "img.png" in au.get("files", {}),
              f"got files={au.get('files') if au else None}")

    # ---- F2: without artifacts ----
    captured.clear()
    job_id2 = "job_hook_no_art"
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
            "request_body, webhook_url) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id2, "req_hook_no", "2026-01-01T00:00:00", "completed",
             "{}", "https://example.com/hook"),
        )
    payload_no = {"job_id": job_id2, "state": "completed", "result": {"output": "x"}}

    daemon.aiohttp.ClientSession = FakeSession
    try:
        asyncio.run(daemon._deliver_webhook(job_id2, "https://example.com/hook",
                                            payload_no, "completed"))
    finally:
        daemon.aiohttp.ClientSession = orig_session

    check("F2. webhook artifact_urls is None when no artifacts",
          len(captured) == 1 and captured[0]["json"].get("artifact_urls") is None,
          f"got artifact_urls={captured[0]['json'].get('artifact_urls') if captured else None!r}")


# ---- G. broker-daemon/daemon.py — DB migration / _finalize_job ---------------
def test_db_artifact_count_migration() -> None:
    daemon = fresh_daemon()
    daemon.init_db()
    # G1: column exists with default 0
    with daemon.db() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    check("G1. jobs.artifact_count column exists",
          "artifact_count" in cols, f"cols={cols}")

    # G2: _finalize_job writes artifact_count
    job_id = "job_finalize_art"
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
            "request_body) VALUES (?, ?, ?, ?, ?)",
            (job_id, "req_finalize", "2026-01-01T00:00:00", "queued", "{}"),
        )
    payload = {
        "job_id": job_id,
        "state": "completed",
        "result": {
            "output": "ok",
            "artifacts": {
                "count": 3,
                "files": [
                    {"filename": "a", "content_type": "text/plain", "sha256": "x" * 64, "size_bytes": 1},
                    {"filename": "b", "content_type": "text/plain", "sha256": "y" * 64, "size_bytes": 1},
                    {"filename": "c", "content_type": "text/plain", "sha256": "z" * 64, "size_bytes": 1},
                ],
            },
        },
    }
    # Stub webhook delivery — we only care about DB write here.
    orig_deliver = daemon._deliver_webhook

    async def no_webhook(*args, **kwargs):
        return None

    daemon._deliver_webhook = no_webhook
    # worker_mgr is a module-level reference set in _on_startup; for tests we
    # inject a no-op stand-in so _finalize_job doesn't try to talk to EC2.
    orig_wm = getattr(daemon, "worker_mgr", None)

    class FakeWM:
        async def note_job_finished(self):
            return None

    daemon.worker_mgr = FakeWM()
    try:
        asyncio.run(daemon._finalize_job(job_id, payload))
    finally:
        daemon._deliver_webhook = orig_deliver
        if orig_wm is not None:
            daemon.worker_mgr = orig_wm

    with daemon.db() as conn:
        row = conn.execute(
            "SELECT artifact_count, state FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    check("G2. _finalize_job wrote artifact_count=3 to DB",
          row["artifact_count"] == 3,
          f"got artifact_count={row['artifact_count']}, state={row['state']}")


# ---- H. broker-daemon/daemon.py — build_app() route wiring -------------------
def test_artifact_routes_wired() -> None:
    daemon = fresh_daemon()
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

    check("H1. GET /v1/jobs/{job_id}/artifacts route registered",
          any(m == "GET" and p == "/v1/jobs/{job_id}/artifacts" for m, p in routes),
          f"routes={routes}")
    check("H2. GET /v1/jobs/{job_id}/artifacts/{filename} route registered",
          any(m == "GET"
              and p == "/v1/jobs/{job_id}/artifacts/{filename}"
              for m, p in routes),
          f"routes={routes}")


# ---- Main --------------------------------------------------------------------
def main() -> int:
    print("=== A. write_artifacts() basic behavior ===")
    test_write_artifacts_basic()
    print()
    print("=== B. execute_in_envelope() artifact integration ===")
    test_execute_in_envelope_artifacts()
    print()
    print("=== C. get_job_artifacts handler ===")
    test_get_job_artifacts_handler()
    print()
    print("=== D. get_job_artifact_file handler ===")
    test_get_job_artifact_file_handler()
    print()
    print("=== E. get_job() includes artifact URLs ===")
    test_get_job_includes_artifact_urls()
    test_get_job_without_artifacts()
    print()
    print("=== F. _deliver_webhook artifact_urls ===")
    test_deliver_webhook_artifact_urls()
    print()
    print("=== G. DB artifact_count migration ===")
    test_db_artifact_count_migration()
    print()
    print("=== H. build_app() artifact routes ===")
    test_artifact_routes_wired()
    print()
    print(f"=== Summary: PASS={PASS} FAIL={FAIL} ===")
    if FAIL:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())