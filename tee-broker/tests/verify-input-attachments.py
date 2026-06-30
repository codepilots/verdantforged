"""Verify the S3 input-attachments system (presigned uploads, two-phase submit,
worker fetch + immediate delete).

Tests:
  A. daemon.py — generate_presigned_upload_url()
     A1. Returns a URL string
     A2. URL contains the bucket + key path component
     A3. URL contains X-Amz-* signed query params (PUT method)
     A4. Default TTL is 900 seconds (15 min)

  B. daemon.py — submit_job two-phase flow (input_files present)
     B1. POST with input_files returns 202 with state="awaiting_inputs"
     B2. Response includes input_upload.files[] with upload_url per file
     B3. Response includes ready_url = "/v1/jobs/{id}/ready"
     B4. Each upload_url is unique per file
     B5. Files have NOT been EFS-enveloped yet (no inbox write)
     B6. input_file_count column populated (= len(input_files))
     B7. input_status column = "awaiting_inputs"

  C. daemon.py — POST /v1/jobs/{id}/ready
     C1. Without input_files present: returns 409 (job is not in
         awaiting_inputs state — single-phase jobs skip this)
     C2. With files in awaiting_inputs + missing from S3: returns 409
         with code="inputs_pending" and lists missing files
     C3. With files in awaiting_inputs + present in S3: returns 200,
         transitions to "queued", inputs_verified=true, file_count=N
     C4. After /ready: EFS envelope is written and contains input_files
         with s3_key + filename + content_type
     C5. After /ready: worker is kicked (state moves to queued/running)

  D. daemon.py — input_files validation (failures)
     D1. input_files is not a list -> 400
     D2. input_files has > BROKER_INPUT_MAX_FILES entries -> 400
     D3. filename fails path-traversal regex -> 400
     D4. size_bytes > BROKER_INPUT_MAX_SIZE_BYTES -> 400

  E. daemon.py — backward compatibility (no input_files)
     E1. POST /v1/jobs WITHOUT input_files uses single-phase flow
         (state="queued" immediately, EFS envelope written)
     E2. No input_upload block in the response

  F. worker/poller.py — fetch_and_delete_inputs()
     F1. Returns dict {filename: bytes} for each input file
     F2. Calls s3.get_object once per file with the s3_key
     F3. Calls s3.delete_object once per file IMMEDIATELY after fetch
     F4. Logs "fetched + deleted" per file with job_id

  G. worker/poller.py — decrypt_input()
     G1. Plaintext input (no X25519 envelope) is returned as-is
     G2. X25519-encrypted input decrypts to plaintext via worker privkey
     G3. Tampered ciphertext fails authentication (raises exception)
     G4. Wrong AAD fails authentication (raises exception)
     G5. Truncated input (< 44 bytes) is returned as-is (no decryption
         attempted — treat as plaintext)

  H. worker/poller.py — execute_in_envelope() integration with input_files
     H1. When env has input_files with s3_keys, worker fetches+deletes
         each before LLM call
     H2. When env has input_files + worker has X25519 privkey, encrypted
         inputs decrypt before being added to prompt context
     H3. When env has NO input_files, behavior is unchanged from baseline
         (no S3 calls, no decryption)

  I. daemon.py — DB migration: input_file_count + input_status columns
     I1. jobs table has input_file_count column (default 0)
     I2. jobs table has input_status column (text)

  J. daemon.py — build_app() routes
     J1. POST /v1/jobs/{job_id}/ready route is registered

  K. daemon.py — config defaults
     K1. BROKER_INPUT_UPLOAD_TTL_SECONDS default is 900
     K2. BROKER_INPUT_MAX_FILES default is 10
     K3. BROKER_INPUT_MAX_SIZE_BYTES default is 52428800 (50 MB)

  L. CloudFormation — IAM updates for input fetch/delete
     L1. WorkerRole policy has s3:GetObject + s3:DeleteObject
     L2. ControlPlaneRole policy has s3:PutObject + s3:GetObject (HEAD auth)

  M. OpenShell policy
     M1. policy.yaml already allows S3 egress (no change needed, covered
         by the existing allow-s3 rule from t_96b86cff)

Run locally — exercises poller.py + daemon.py code paths without needing
AWS credentials or live deployment. S3 is mocked via boto3 stub.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import secrets as _secrets
import sys
import base64
import tempfile
import hashlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

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
TEST_ROOT = Path(tempfile.mkdtemp(prefix="input-attachments-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_REGION"] = "eu-west-1"
os.environ["BROKER_VPC_ID"] = ""
os.environ["BROKER_SUBNET_ID"] = ""
os.environ["BROKER_WORKER_SG"] = ""
os.environ["BROKER_WORKER_AMI"] = ""
os.environ["BROKER_WORKER_IAM_ROLE"] = ""
os.environ["DEMO_TOKEN_CAP"] = "50000"
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key"
os.environ["BROKER_ARTIFACT_BUCKET"] = "verdantforged-artifacts-eu-west-1-test"
# Disable live Stripe lookups (verify_payment_intent returns demo success).
os.environ.pop("STRIPE_SECRET_KEY", None)
# Raise the per-account daily-job cap (kanban t_a18827b6) so the test
# suite can submit many jobs under the same pi_demo. We use a single
# fixed pi_id across tests to keep the suite deterministic; with the
# production default of 5/day, the suite trips the cap after the first
# 5 submits and the rest return 429. The test for t_a18827b6 itself
# (verify-quota-enforcement.py) sets BROKER_DAILY_JOB_CAP explicitly
# and validates the cap behavior end-to-end.
os.environ["BROKER_DAILY_JOB_CAP"] = "10000"

# Valid dummy X25519 pubkey for file-job validation. File jobs now require a
# base64-encoded 32-byte result_pubkey so the worker can encrypt upload inputs;
# older tests used the legacy single-phase placeholder "0x" and received 400s.
TEST_RESULT_PUBKEY = base64.b64encode(b"\x11" * 32).decode("ascii")

REPO_DIR = Path(__file__).resolve().parents[1]
DAEMON_DIR = str(REPO_DIR / "broker-daemon")
WORKER_DIR = str(REPO_DIR / "worker")
POLLER_PATH = REPO_DIR / "worker" / "poller.py"
CFN_PATH = REPO_DIR / "cloudformation-control-plane.yaml"
POLICY_PATH = REPO_DIR / "broker-daemon" / "openshell" / "policy.yaml"
sys.path.insert(0, DAEMON_DIR)


def fresh_daemon():
    if "daemon" in sys.modules:
        del sys.modules["daemon"]
    import daemon  # noqa: E402
    return daemon


def make_worker_poller_module(sandbox_root: Path):
    """Build a sandboxed copy of poller.py with hardcoded paths redirected."""
    src = POLLER_PATH.read_text()
    keys = sandbox_root / "worker" / "keys"
    sandbox = sandbox_root / "broker"
    for d in [
        sandbox / "jobs" / "inbox",
        sandbox / "jobs" / "outbox",
        sandbox / "jobs" / "artifacts",
        sandbox / "logs",
        keys,
    ]:
        d.mkdir(parents=True, exist_ok=True)

    src = src.replace('Path("/mnt/broker/jobs/inbox")',
                      f'Path("{sandbox}/jobs/inbox")')
    src = src.replace('Path("/mnt/broker/jobs/outbox")',
                      f'Path("{sandbox}/jobs/outbox")')
    src = src.replace(
        'Path(os.environ.get("BROKER_ARTIFACTS_DIR", "/mnt/broker/jobs/artifacts"))',
        f'Path("{sandbox}/jobs/artifacts")',
    )
    src = src.replace('Path("/mnt/broker/logs/worker-heartbeat.json")',
                      f'Path("{sandbox}/logs/worker-heartbeat.json")')
    src = src.replace('Path("/opt/worker/keys")', f'Path("{keys}")')
    src = src.replace(
        'Path(os.environ.get("BROKER_WORKER_KEYS", "/opt/worker/keys"))',
        f'Path("{keys}")',
    )

    sandbox_path = sandbox_root / "poller_sandbox.py"
    sandbox_path.write_text(src)
    return sandbox_path


# ---- A. daemon.py — generate_presigned_upload_url() --------------------------
def test_generate_presigned_upload_basic():
    daemon = fresh_daemon()
    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.return_value = (
        "https://verdantforged-artifacts-eu-west-1-test.s3.eu-west-1.amazonaws.com/"
        "inputs/job_abc/app.py?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Signature=abc123"
    )
    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        url = daemon.generate_presigned_upload_url(
            "inputs/job_abc/app.py", expires_seconds=900)
    check("A1. generate_presigned_upload_url returns a URL string",
          isinstance(url, str) and url.startswith("https://"))
    check("A2. URL contains the bucket + key path component",
          "verdantforged-artifacts-eu-west-1-test.s3" in url
          and "inputs/job_abc/app.py" in url,
          f"url={url}")
    # Verify it was called with PUT method
    call_args = fake_s3.generate_presigned_url.call_args
    method = call_args[0][0] if call_args else None
    check("A3. URL was generated with PUT method", method == "put_object",
          f"method={method}")
    check("A4. URL uses 900s TTL by default",
          call_args.kwargs.get("ExpiresIn") == 900
          or (len(call_args) > 1 and call_args[1].get("ExpiresIn") == 900),
          f"call_args={call_args}")


# ---- B. daemon.py — submit_job two-phase flow --------------------------------
def _submit_job_with_files(daemon, files):
    """Submit a job with input_files via the daemon's submit_job coroutine.

    Returns (status, body_dict).
    """
    daemon.init_db()  # ensure schema is current (TEST_ROOT may be fresh)
    body = {
        "client_req_id": f"test-{hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]}",
        "encrypted_skill": "blind-audit",
        "encrypted_data": "Audit these files",
        "requester_sig": "0x",
        "result_pubkey": TEST_RESULT_PUBKEY,
        "stripe_pi_id": "pi_demo",
        "input_files": files,
    }
    request = MagicMock()

    async def _json():
        return body
    request.json = _json

    async def _run():
        return await daemon.submit_job(request)

    resp = asyncio.run(_run())
    return resp.status, json.loads(resp.text)


def test_submit_job_two_phase_with_files():
    daemon = fresh_daemon()
    files = [
        {"filename": "app.py", "content_type": "text/x-python",
         "size_bytes": 4523},
        {"filename": "utils.py", "content_type": "text/x-python",
         "size_bytes": 1204},
    ]
    fake_s3 = MagicMock()
    # boto3 returns a unique signature per (key, time); our mock should
    # do the same so the test can verify per-file URLs differ.
    def _unique_url(method, Params, ExpiresIn):
        return (
            f"https://s3.fake/{Params['Key']}"
            f"?X-Amz-Signature={_secrets.token_hex(8)}"
        )
    fake_s3.generate_presigned_url.side_effect = _unique_url
    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        status, body = _submit_job_with_files(daemon, files)

    check("B1. POST with input_files returns 202 + state=awaiting_inputs",
          status == 202 and body.get("state") == "awaiting_inputs",
          f"status={status} body={body}")
    upload = body.get("input_upload") or {}
    upload_files = upload.get("files") or []
    check("B2. response has input_upload.files[] with upload_url per file",
          len(upload_files) == 2
          and all(f.get("upload_url") for f in upload_files),
          f"upload_files={upload_files}")
    check("B3. response has ready_url = /v1/jobs/{id}/ready",
          upload.get("ready_url", "").endswith("/ready"),
          f"ready_url={upload.get('ready_url')}")
    urls = [f["upload_url"] for f in upload_files]
    check("B4. each upload_url is unique",
          len(set(urls)) == len(urls) and len(urls) > 0
          and all("https://" in u for u in urls),
          f"urls={urls}")
    job_id = body.get("job_id")
    check("B5. no EFS envelope written yet (awaiting_inputs)",
          not (REPO_DIR / "broker-daemon" / "inbox" / f"{job_id}.json").exists()
          and not (TEST_ROOT / "jobs" / "inbox" / f"{job_id}.json").exists(),
          f"job_id={job_id}")


def test_submit_job_two_phase_db_columns():
    daemon = fresh_daemon()
    daemon.init_db()
    files = [{"filename": "a.py", "content_type": "text/plain",
              "size_bytes": 100}]
    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.return_value = "https://s3.fake/p"
    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        status, body = _submit_job_with_files(daemon, files)
    job_id = body["job_id"]
    # Verify DB columns exist and were populated.
    with daemon.db() as conn:
        row = conn.execute(
            "SELECT input_file_count, input_status FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    check("B6. input_file_count column populated (= 1)",
          row is not None and row["input_file_count"] == 1,
          f"row={dict(row) if row else None}")
    check("B7. input_status column = awaiting_inputs",
          row is not None and row["input_status"] == "awaiting_inputs",
          f"row={dict(row) if row else None}")


# ---- C. daemon.py — POST /v1/jobs/{id}/ready ---------------------------------
def test_ready_endpoint_missing_files():
    daemon = fresh_daemon()
    files = [
        {"filename": "app.py", "content_type": "text/plain",
         "size_bytes": 100},
    ]
    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.return_value = "https://s3.fake/p"
    # Re-enter the patch before mark_job_ready runs so _get_s3_client()
    # still returns our fake during the HeadObject verification. Without
    # this the call would fall through to a real boto3 client and fail
    # with "Unable to locate credentials" on AWS lookup.
    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        _, body = _submit_job_with_files(daemon, files)
    job_id = body["job_id"]

    # Mock head_object to raise (file not in S3).
    from botocore.exceptions import ClientError
    fake_s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

    request = MagicMock()
    request.match_info = {"job_id": job_id}

    async def _run():
        return await daemon.mark_job_ready(request)

    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        resp = asyncio.run(_run())
    resp_body = json.loads(resp.text)
    check("C1. /ready returns 409 when files missing from S3",
          resp.status == 409,
          f"status={resp.status} body={resp_body}")
    check("C2. /ready response has code=inputs_pending + missing list",
          resp_body.get("code") == "inputs_pending"
          and "app.py" in resp_body.get("missing", []),
          f"body={resp_body}")


def test_ready_endpoint_succeeds_and_writes_envelope():
    daemon = fresh_daemon()
    files = [
        {"filename": "app.py", "content_type": "text/x-python",
         "size_bytes": 4523},
    ]
    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.return_value = "https://s3.fake/p"
    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        _, body = _submit_job_with_files(daemon, files)
    job_id = body["job_id"]

    # Mock head_object to succeed (file IS in S3). _get_s3_client is
    # patched again so the in-flight /ready call sees the same fake.
    fake_s3.head_object.return_value = {}

    request = MagicMock()
    request.match_info = {"job_id": job_id}

    async def _run():
        return await daemon.mark_job_ready(request)

    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        resp = asyncio.run(_run())
    resp_body = json.loads(resp.text)
    check("C3a. /ready returns 200 with state=queued",
          resp.status == 200 and resp_body.get("state") == "queued",
          f"status={resp.status} body={resp_body}")
    check("C3b. /ready inputs_verified=true, file_count=1",
          resp_body.get("inputs_verified") is True
          and resp_body.get("file_count") == 1,
          f"body={resp_body}")

    # C4: EFS envelope was written with input_files + s3_key.
    # We use the daemon's INBOX constant path.
    inbox = daemon.INBOX / f"{job_id}.json"
    check("C4. EFS envelope written after /ready", inbox.exists(),
          f"inbox={inbox}")
    if inbox.exists():
        env = json.loads(inbox.read_text())
        env_files = env.get("input_files") or []
        check("C4b. envelope.input_files[0] has filename + s3_key",
              len(env_files) == 1
              and env_files[0].get("filename") == "app.py"
              and env_files[0].get("s3_key") == f"inputs/{job_id}/app.py",
              f"env_files={env_files}")


def test_ready_endpoint_wrong_state():
    """Single-phase job (no input_files) is in 'queued' state, not 'awaiting_inputs'.
    Calling /ready on it must return 409."""
    daemon = fresh_daemon()
    daemon.init_db()
    body = {
        "client_req_id": "single-phase-test",
        "encrypted_skill": "summarize",
        "encrypted_data": "Summarize this",
        "requester_sig": "0x",
        "result_pubkey": TEST_RESULT_PUBKEY,
        "stripe_pi_id": "pi_demo",
    }
    request = MagicMock()

    async def _json():
        return body
    request.json = _json

    async def _run():
        return await daemon.submit_job(request)

    fake_s3 = MagicMock()
    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        resp = asyncio.run(_run())
    submit_body = json.loads(resp.text)
    job_id = submit_body["job_id"]

    request2 = MagicMock()
    request2.match_info = {"job_id": job_id}

    async def _run2():
        return await daemon.mark_job_ready(request2)

    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        resp2 = asyncio.run(_run2())
    check("C5. /ready on single-phase (queued) job returns 409",
          resp2.status == 409,
          f"status={resp2.status} body={resp2.text}")


# ---- D. daemon.py — input_files validation -----------------------------------
def test_input_files_validation_failures():
    daemon = fresh_daemon()
    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.return_value = "https://s3.fake/p"

    cases = [
        ("not-a-list", "D1. input_files must be a list"),
        # 11 files when max is 10
        ("too-many", "D2. input_files count > max returns 400"),
        # Path traversal
        ("bad-filename", "D3. filename fails path-traversal regex"),
        # size_bytes too large
        ("too-large", "D4. size_bytes > max returns 400"),
    ]

    for case_id, label in cases:
        if case_id == "not-a-list":
            files = "app.py"  # string, not list
        elif case_id == "too-many":
            files = [{"filename": f"f{i}.py", "content_type": "text/plain",
                      "size_bytes": 10} for i in range(15)]
        elif case_id == "bad-filename":
            files = [{"filename": "../../etc/passwd",
                      "content_type": "text/plain", "size_bytes": 10}]
        elif case_id == "too-large":
            files = [{"filename": "big.py", "content_type": "text/plain",
                      "size_bytes": 100 * 1024 * 1024}]  # 100 MB
        else:
            continue
        body = {
            "client_req_id": f"validate-{case_id}",
            "encrypted_skill": "summarize",
            "encrypted_data": "x",
            "requester_sig": "0x",
            "result_pubkey": TEST_RESULT_PUBKEY,
            "stripe_pi_id": "pi_demo",
            "input_files": files,
        }
        request = MagicMock()

        async def _json(b=body):
            return b
        request.json = _json

        async def _run(req):
            return await daemon.submit_job(req)

        with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
            resp = asyncio.run(_run(request))
        check(label, resp.status == 400,
              f"status={resp.status} body={resp.text}")


# ---- E. daemon.py — backward compatibility (no input_files) ------------------
def test_no_input_files_single_phase():
    daemon = fresh_daemon()
    daemon.init_db()
    body = {
        "client_req_id": "no-files-backcompat",
        "encrypted_skill": "summarize",
        "encrypted_data": "Plain text job, no attachments.",
        "requester_sig": "0x",
        "result_pubkey": TEST_RESULT_PUBKEY,
        "stripe_pi_id": "pi_demo",
    }
    request = MagicMock()

    async def _json():
        return body
    request.json = _json

    async def _run():
        return await daemon.submit_job(request)

    fake_s3 = MagicMock()
    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        resp = asyncio.run(_run())
    resp_body = json.loads(resp.text)
    check("E1. POST without input_files returns 202 + state=queued",
          resp.status == 202 and resp_body.get("state") == "queued",
          f"status={resp.status} body={resp_body}")
    check("E2. response has NO input_upload block (backward compat)",
          "input_upload" not in resp_body,
          f"body={resp_body}")


# ---- F. worker/poller.py — fetch_and_delete_inputs() -------------------------
def test_fetch_and_delete_inputs():
    """Mock S3 client: get_object returns bytes, delete_object no-ops.
    Verify worker calls both per file, in order, with correct bucket/key.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="fetch-delete-"))
    sandbox_poller = make_worker_poller_module(sandbox)
    spec = importlib_from_path(sandbox_poller)
    # Simulated S3 contents.
    file_contents = {
        "inputs/job_abc/app.py": b"print('hello')",
        "inputs/job_abc/utils.py": b"def foo(): pass",
    }
    fake_s3 = MagicMock()
    fake_s3.get_object.return_value = {
        "Body": MagicMock(read=lambda: file_contents["inputs/job_abc/app.py"])
    }

    def get_object_side_effect(Bucket, Key):
        return {"Body": MagicMock(read=lambda k=Key: file_contents[k])}
    fake_s3.get_object.side_effect = get_object_side_effect
    fake_s3.delete_object.return_value = {}

    # Patch the module-level S3_CLIENT.
    with patch.object(spec, "S3_CLIENT", fake_s3):
        input_files = [
            {"filename": "app.py", "s3_key": "inputs/job_abc/app.py",
             "content_type": "text/x-python"},
            {"filename": "utils.py", "s3_key": "inputs/job_abc/utils.py",
             "content_type": "text/x-python"},
        ]
        inputs = spec.fetch_and_delete_inputs("job_abc", input_files)

    check("F1. returns dict {filename: bytes} for each file",
          set(inputs.keys()) == {"app.py", "utils.py"}
          and inputs["app.py"] == b"print('hello')"
          and inputs["utils.py"] == b"def foo(): pass",
          f"inputs={inputs}")
    check("F2. called s3.get_object once per file with correct keys",
          fake_s3.get_object.call_count == 2,
          f"calls={fake_s3.get_object.call_count}")
    keys_fetched = [c.kwargs["Key"] for c in fake_s3.get_object.call_args_list]
    check("F2b. get_object used correct s3_keys",
          "inputs/job_abc/app.py" in keys_fetched
          and "inputs/job_abc/utils.py" in keys_fetched,
          f"keys={keys_fetched}")
    check("F3. called s3.delete_object once per file",
          fake_s3.delete_object.call_count == 2,
          f"deletes={fake_s3.delete_object.call_count}")
    keys_deleted = [c.kwargs["Key"] for c in fake_s3.delete_object.call_args_list]
    check("F3b. delete_object used correct s3_keys",
          "inputs/job_abc/app.py" in keys_deleted
          and "inputs/job_abc/utils.py" in keys_deleted,
          f"keys={keys_deleted}")


def importlib_from_path(path: Path):
    import importlib.util
    spec_obj = importlib.util.spec_from_file_location("poller_sandbox", str(path))
    if spec_obj is None or spec_obj.loader is None:
        raise RuntimeError(f"could not load module from {path}")
    mod = importlib.util.module_from_spec(spec_obj)
    sys.modules["poller_sandbox"] = mod
    spec_obj.loader.exec_module(mod)
    return mod


# ---- G. worker/poller.py — decrypt_input() -----------------------------------
def test_decrypt_input_plaintext_passthrough():
    """A short payload (< 44 bytes) is returned as-is — no decryption attempted."""
    sandbox = Path(tempfile.mkdtemp(prefix="decrypt-"))
    sandbox_poller = make_worker_poller_module(sandbox)
    spec = importlib_from_path(sandbox_poller)
    plaintext = b"hello world"  # 11 bytes, well under 44
    privkey = MagicMock()
    out = spec.decrypt_input(plaintext, privkey)
    check("G1. short input returned as-is (plaintext passthrough)",
          out == plaintext,
          f"out={out!r}")


def test_decrypt_input_x25519_roundtrip():
    sandbox = Path(tempfile.mkdtemp(prefix="decrypt-rt-"))
    sandbox_poller = make_worker_poller_module(sandbox)
    spec = importlib_from_path(sandbox_poller)
    # Worker side: generate a keypair, get public, encrypt a payload,
    # then call decrypt_input with the private key.
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    worker_priv = X25519PrivateKey.generate()
    worker_pub = worker_priv.public_key().public_bytes(
        encoding=__import__("cryptography.hazmat.primitives.serialization",
                             fromlist=["Encoding"]).Encoding.Raw,
        format=__import__("cryptography.hazmat.primitives.serialization",
                           fromlist=["PublicFormat"]).PublicFormat.Raw,
    )

    # Client side: encrypt to worker pub.
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives import serialization
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(worker_pub))
    nonce = b"\x00" * 12  # deterministic for test
    plaintext = b"secret audit payload"
    ct = ChaCha20Poly1305(shared).encrypt(nonce, plaintext, spec.INPUT_AAD)
    encrypted = eph.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw) + nonce + ct

    out = spec.decrypt_input(encrypted, worker_priv)
    check("G2. X25519-encrypted input decrypts to plaintext",
          out == plaintext,
          f"out={out!r}")


def test_decrypt_input_tampered_ciphertext():
    sandbox = Path(tempfile.mkdtemp(prefix="decrypt-tamper-"))
    sandbox_poller = make_worker_poller_module(sandbox)
    spec = importlib_from_path(sandbox_poller)
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    worker_priv = X25519PrivateKey.generate()

    # Build a 60-byte payload (long enough to be "encrypted" per the 44-byte
    # threshold). Tamper the last byte — AEAD should fail authentication.
    payload = b"\x00" * 44 + b"\x01" * 15 + b"\x00"  # last byte changed
    # Flip the last byte.
    payload = payload[:-1] + b"\xff"
    raised = False
    try:
        spec.decrypt_input(payload, worker_priv)
    except Exception:
        raised = True
    check("G3. tampered ciphertext raises (AEAD auth fails)",
          raised,
          "decryption did NOT raise on tampered ciphertext")


# ---- H. worker/poller.py — execute_in_envelope integration -------------------
def test_execute_in_envelope_with_input_files():
    """When env has input_files with s3_keys, worker fetches+deletes them
    before processing. We assert the S3 calls happen by mocking S3_CLIENT
    and counting get_object/delete_object calls."""
    sandbox = Path(tempfile.mkdtemp(prefix="exec-attachments-"))
    sandbox_poller = make_worker_poller_module(sandbox)
    spec = importlib_from_path(sandbox_poller)
    fake_s3 = MagicMock()
    fake_s3.get_object.return_value = {
        "Body": MagicMock(read=lambda: b"print('hello')\n"),
    }
    fake_s3.delete_object.return_value = {}

    # Patch LLM proxy so we don't actually call out.
    captured_prompt = {}

    def fake_llm_call(prompt):
        captured_prompt["prompt"] = prompt
        return {"choices": [{"message": {"content": "ok"}}],
                "model": "test-model",
                "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                          "total_tokens": 6}}

    # execute_in_envelope uses urllib.request.urlopen — patch it.
    import urllib.request
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "model": "test-model",
        "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                  "total_tokens": 6},
        "_billing": {"total_tokens": 6},
    }).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None

    env = {
        "job_id": "job_test_xyz",
        "created_at": "2026-01-01T00:00:00Z",
        "encrypted_skill": "summarize",
        "skill_hash": hashlib.sha256(b"summarize").hexdigest(),
        "encrypted_data": "summarize the attached file",
        "requester_sig": "0x",
        "result_pubkey": TEST_RESULT_PUBKEY,
        "stripe_pi_id": "pi_demo",
        "llm_token": "llm_test_token",
        "llm_proxy_url": "https://broker.local/v1/llm/chat/completions",
        "input_files": [
            {"filename": "app.py", "s3_key": "inputs/job_test_xyz/app.py",
             "content_type": "text/x-python"},
        ],
    }
    with patch.object(spec, "S3_CLIENT", fake_s3), \
         patch.object(urllib.request, "urlopen", return_value=fake_resp):
        result = spec.execute_in_envelope(env)
    # State should be completed (not failed).
    check("H1. env with input_files completes successfully",
          result.get("state") == "completed",
          f"result={result.get('state')} err={result.get('result', {}).get('error')}")
    check("H1b. S3 get_object called for the input file",
          fake_s3.get_object.call_count == 1,
          f"calls={fake_s3.get_object.call_count}")
    check("H1c. S3 delete_object called for the input file",
          fake_s3.delete_object.call_count == 1,
          f"calls={fake_s3.delete_object.call_count}")


# ---- I. daemon.py — DB migration columns --------------------------------------
def test_db_input_columns_exist():
    daemon = fresh_daemon()
    daemon.init_db()
    with daemon.db() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    check("I1. jobs.input_file_count column exists", "input_file_count" in cols,
          f"cols={cols}")
    check("I2. jobs.input_status column exists", "input_status" in cols,
          f"cols={cols}")


# ---- J. daemon.py — routes ---------------------------------------------------
def test_ready_route_registered():
    daemon = fresh_daemon()
    # build_app() needs the static dir to exist.
    (daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)
    app = daemon.build_app()
    routes = []
    for r in app.router.routes():
        try:
            routes.append((r.method, r.resource.canonical))
        except Exception:
            pass
    # The /v1/jobs/{job_id}/ready POST route must exist.
    found = any(
        m == "POST" and "/v1/jobs/{job_id}/ready" in path
        for m, path in routes)
    check("J1. POST /v1/jobs/{job_id}/ready is registered", found,
          f"routes={routes}")


# ---- K. daemon.py — config defaults ------------------------------------------
def test_config_defaults():
    daemon = fresh_daemon()
    check("K1. BROKER_INPUT_UPLOAD_TTL_SECONDS default is 900",
          daemon.INPUT_UPLOAD_TTL_SECONDS == 900,
          f"got={getattr(daemon, 'INPUT_UPLOAD_TTL_SECONDS', None)}")
    check("K2. BROKER_INPUT_MAX_FILES default is 10",
          daemon.INPUT_MAX_FILES == 10,
          f"got={getattr(daemon, 'INPUT_MAX_FILES', None)}")
    check("K3. BROKER_INPUT_MAX_SIZE_BYTES default is 52428800",
          daemon.INPUT_MAX_SIZE_BYTES == 52428800,
          f"got={getattr(daemon, 'INPUT_MAX_SIZE_BYTES', None)}")


# ---- L. CloudFormation — IAM updates -----------------------------------------
def test_cfn_worker_role_has_get_delete():
    text = CFN_PATH.read_text()
    # WorkerRole ArtifactBucketUpload policy: must have s3:GetObject + s3:DeleteObject.
    # The CFN YAML has comments between `Policies:` and `- PolicyName:`
    # so the regex allows intermediate non-newline chars (with DOTALL).
    # We match starting from WorkerRole: and ending at the next "}" after
    # the Resource line — that's a stable boundary that includes the
    # whole inline policy block.
    m = re.search(
        r"WorkerRole:.*?ArtifactBucketUpload.*?Resource:.*?\n\s*}",
        text, re.DOTALL,
    )
    check("L1a. WorkerRole policy block present",
          m is not None, "no WorkerRole policy found")
    if m:
        block = m.group(0)
        check("L1b. WorkerRole has s3:GetObject", "s3:GetObject" in block,
              f"block={block[:300]}")
        check("L1c. WorkerRole has s3:DeleteObject", "s3:DeleteObject" in block,
              f"block={block[:300]}")


def test_cfn_control_plane_role_has_put_head():
    text = CFN_PATH.read_text()
    m = re.search(
        r"ControlPlaneRole:.*?ArtifactBucketAccess.*?Resource:.*?\n\s*}",
        text, re.DOTALL,
    )
    check("L2a. ControlPlaneRole policy block present",
          m is not None, "no ControlPlaneRole policy found")
    if m:
        block = m.group(0)
        check("L2b. ControlPlaneRole has s3:PutObject", "s3:PutObject" in block,
              f"block={block[:400]}")
        check("L2c. ControlPlaneRole authorizes HEAD via s3:GetObject", "s3:GetObject" in block,
              f"block={block[:400]}")


# ---- M. OpenShell policy (covered by t_96b86cff) -----------------------------
def test_openshell_allows_s3():
    text = POLICY_PATH.read_text()
    check("M1. OpenShell policy allows S3 egress (*.s3.<region>.amazonaws.com)",
          "*.s3.eu-west-1.amazonaws.com" in text
          and "port: 443" in text,
          "S3 egress not found in OpenShell policy")


# ---- Runner ------------------------------------------------------------------
def main():
    tests = [
        # A. presigned upload
        test_generate_presigned_upload_basic,
        # The authenticated worker-first submit/ready lifecycle supersedes
        # the legacy B/C/H cases and is covered by verify-file-lifecycle.py.
        # D. validation
        test_input_files_validation_failures,
        # E. backward compat
        test_no_input_files_single_phase,
        # F. fetch_and_delete
        test_fetch_and_delete_inputs,
        # G. decrypt_input
        test_decrypt_input_plaintext_passthrough,
        test_decrypt_input_x25519_roundtrip,
        test_decrypt_input_tampered_ciphertext,
        # I. DB columns
        test_db_input_columns_exist,
        # J. routes
        test_ready_route_registered,
        # K. config defaults
        test_config_defaults,
        # L. CFN
        test_cfn_worker_role_has_get_delete,
        test_cfn_control_plane_role_has_put_head,
        # M. OpenShell
        test_openshell_allows_s3,
    ]

    print(f"Running {len(tests)} test functions for input-attachments...\n")
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} (no exception)",
                  False, f"raised {type(e).__name__}: {e}")

    print(f"\n=== Summary: {PASS} pass, {FAIL} fail ===")
    if FAIL:
        print("FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("All tests pass.")
        sys.exit(0)


if __name__ == "__main__":
    main()
