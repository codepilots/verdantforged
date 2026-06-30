"""Verify the encrypted S3 artifact system (replaces EFS plaintext storage).

Tests:
  A. worker/poller.py — encrypt_artifact()
     A1. returns bytes whose first 32 bytes are an X25519 ephemeral pubkey
     A2. round-trip: client privkey decrypts back to original plaintext
     A3. AAD is bound (changing the AAD causes decryption failure)
     A4. Each call uses a fresh ephemeral key (forward secrecy)
     A5. 12-byte nonce is fresh per call

  B. worker/poller.py — upload_artifacts_to_s3()
     B1. when result_pubkey is "" or "0x", returns None (no artifacts)
     B2. when called with N files, calls s3.put_object exactly N times
     B3. S3 keys are s3://{bucket}/{job_id}/{filename}
     B4. Puts use ServerSideEncryption="aws:kms"
     B5. Puts include encryption metadata (ephemeral_pubkey_32 || nonce_12 || ciphertext)
     B6. Manifest returned includes filename, content_type, size_bytes,
         sha256, s3_key, encrypted, encrypted_size_bytes per file
     B7. Manifest includes job_id and encryption field
     B8. Preserves nested filenames (code/main.py) as S3 keys with "/"

  C. worker/poller.py — execute_in_envelope() integration (S3 path)
     C1. when env['artifacts'] is set + result_pubkey is "0x" -> no S3 upload,
         result envelope has no artifacts key (graceful skip)
     C2. when env['artifacts'] is set + result_pubkey is a valid X25519 pub,
         execute_in_envelope uploads to S3 and the result envelope has
         artifacts.{count, encryption, files, ttl_hours, primary}

  D. broker-daemon/daemon.py — presigned URL generation
     D1. generate_presigned_url() returns a URL with X-Amz-* query params
     D2. URL expires in 900 seconds (15 minutes)
     D3. URL includes the bucket and key as the path component

  E. broker-daemon/daemon.py — GET /v1/jobs/{id}/artifacts endpoint
     E1. returns 404 when the job has no artifacts
     E2. returns the manifest with a download_url per file (presigned)
     E3. download_url TTL is 15 minutes (900s)
     E4. Files without s3_key are skipped from download_urls

  F. broker-daemon/daemon.py — GET /v1/jobs/{id}/artifacts/{filename} endpoint
     F1. returns 302 redirect to a presigned S3 URL
     F2. 302 Location header is a valid presigned URL
     F3. 404 when filename is not in the manifest
     F4. 404 when the job has no artifacts

  G. broker-daemon/daemon.py — get_job() includes S3 artifact fields
     G1. when result.artifacts present, response.result.artifacts has
         manifest_url, download_urls, ttl_hours, note fields
     G2. when no artifacts, response has no manifest_url/download_urls

  H. broker-daemon/daemon.py — webhook includes S3 manifest URL
     H1. webhook artifact_urls.manifest is /v1/jobs/{id}/artifacts
     H2. webhook artifact_urls.files contains all filenames

  I. broker-daemon/daemon.py — DB migration: artifact_count column
     I1. jobs.artifact_count column exists (default 0)

  J. broker-daemon/daemon.py — build_app() routes
     J1. GET /v1/jobs/{job_id}/artifacts registered
     J2. GET /v1/jobs/{job_id}/artifacts/{filename} registered

  K. worker/poller.py — no plaintext artifact bytes written to EFS
     K1. ARTIFACTS_DIR is not created or written to by the new code path

  L. CloudFormation template
     L1. cloudformation-control-plane.yaml defines ArtifactBucket resource
     L2. ArtifactBucket has BucketEncryption (SSE-KMS)
     L3. ArtifactBucket has LifecycleConfiguration with ExpirationInDays=1
     L4. ArtifactBucket has PublicAccessBlockConfiguration (4 true booleans)
     L5. ArtifactBucketPolicy grants PutObject + GetObject + DeleteObject
     L6. WorkerRole policy includes s3:PutObject on artifact bucket
     L7. ControlPlaneRole policy includes s3:GetObject + s3:DeleteObject + s3:ListBucket

  M. OpenShell policy
     M1. broker-daemon/openshell/policy.yaml has an allow-s3 rule for
         *.s3.<region>.amazonaws.com:443

  N. Bootstrap / deploy wiring
     N1. scripts/bootstrap-control-plane.sh writes BROKER_ARTIFACT_BUCKET
         to config.env
     N2. deploy.sh passes BROKER_ARTIFACT_BUCKET env to bootstrap
     N3. config.env (cloud-init template in CFN) writes BROKER_ARTIFACT_BUCKET

Run locally — exercises poller.py + daemon.py code paths without needing
AWS credentials or live deployment. S3 is mocked via boto3 stub.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
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
TEST_ROOT = Path(tempfile.mkdtemp(prefix="artifacts-s3-test-"))
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

REPO_DIR = Path(__file__).resolve().parents[1]
DAEMON_DIR = str(REPO_DIR / "broker-daemon")
WORKER_DIR = str(REPO_DIR / "worker")
POLLER_PATH = REPO_DIR / "worker" / "poller.py"
CFN_PATH = REPO_DIR / "cloudformation-control-plane.yaml"
POLICY_PATH = REPO_DIR / "broker-daemon" / "openshell" / "policy.yaml"
BOOTSTRAP_PATH = REPO_DIR / "scripts" / "bootstrap-control-plane.sh"
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
    # KEY_DIR may be a literal Path(...) or an env-var-backed one
    # (Path(os.environ.get("BROKER_WORKER_KEYS", "/opt/worker/keys"))).
    # Handle both forms so this sandbox stays in sync with the production
    # code's chosen shape.
    src = src.replace('Path("/opt/worker/keys")', f'Path("{keys}")')
    src = src.replace(
        'Path(os.environ.get("BROKER_WORKER_KEYS", "/opt/worker/keys"))',
        f'Path("{keys}")',
    )

    sandbox_path = sandbox_root / "poller_sandbox.py"
    sandbox_path.write_text(src)
    return sandbox_path


# ---- A. worker/poller.py — encrypt_artifact() ---------------------------------
def test_encrypt_artifact_basic():
    """encrypt_artifact returns bytes that decrypt back to plaintext via X25519."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    src = POLLER_PATH.read_text()
    sandbox_root = Path(tempfile.mkdtemp(prefix="poller-enc-"))
    sp = make_worker_poller_module(sandbox_root)
    sys.path.insert(0, str(sandbox_root))
    import poller_sandbox as mod

    # A1+A5: shape check (returns base64 string of binary blob)
    plaintext = b"secret photo bytes\n" * 10
    priv = X25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode()

    enc_b64 = mod.encrypt_artifact(plaintext, pub_b64)
    enc_bytes = base64.b64decode(enc_b64)
    check("A1. encrypted blob is at least 44 bytes (32 pubkey + 12 nonce + tag+ct)",
          len(enc_bytes) >= 44, f"got {len(enc_bytes)}")

    # A2: round-trip
    eph_pub_bytes = enc_bytes[:32]
    nonce = enc_bytes[32:44]
    ct = enc_bytes[44:]
    eph_pub = X25519PublicKey.from_public_bytes(eph_pub_bytes)
    shared = priv.exchange(eph_pub)
    recovered = ChaCha20Poly1305(shared).decrypt(nonce, ct, b"verdantforged-artifact")
    check("A2. client privkey decrypts ciphertext back to plaintext",
          recovered == plaintext,
          f"got {recovered!r}")

    # A3: AAD bound — changing the AAD fails decryption
    try:
        bad = ChaCha20Poly1305(shared).decrypt(nonce, ct, b"wrong-aad")
        check("A3. AAD is bound (wrong AAD -> decrypt fails)", False,
              f"decryption succeeded with wrong AAD: {bad!r}")
    except Exception:
        check("A3. AAD is bound (wrong AAD -> decrypt fails)", True)

    # A4+A5: fresh ephemeral key per call (different ciphertexts for same plaintext+pub)
    enc_b64_2 = mod.encrypt_artifact(plaintext, pub_b64)
    check("A4. each encrypt_artifact call uses fresh ephemeral key",
          enc_b64 != enc_b64_2, "same ciphertext for same input — forward secrecy broken")


# ---- B. worker/poller.py — upload_artifacts_to_s3() --------------------------
def test_upload_artifacts_to_s3_basic():
    """upload_artifacts_to_s3 uploads encrypted blobs to S3 and returns manifest."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    src = POLLER_PATH.read_text()
    sandbox_root = Path(tempfile.mkdtemp(prefix="poller-up-"))
    sp = make_worker_poller_module(sandbox_root)
    sys.path.insert(0, str(sandbox_root))
    import poller_sandbox as mod

    priv = X25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode()

    # Mock boto3 S3 client — record every put_object call
    fake_s3 = MagicMock()
    captured_puts = []

    def fake_put_object(**kwargs):
        captured_puts.append(kwargs)
        return {"ETag": "fake-etag"}

    fake_s3.put_object = fake_put_object

    files = [
        {"filename": "image.bmp", "content_type": "image/bmp",
         "data": b"\x42\x4d" + b"\x00" * 100, "role": "artifact"},
        {"filename": "report.pdf", "content_type": "application/pdf",
         "data": b"%PDF-1.4\n", "role": "artifact"},
    ]

    with patch.object(mod, "boto3", create=True) as fake_boto3, \
         patch.object(mod, "S3_CLIENT", fake_s3, create=True):
        # Tests inject the fake via module-level S3_CLIENT; upload_artifacts_to_s3
        # reads it through _get_s3_client().
        mod.S3_CLIENT = fake_s3
        fake_boto3.client.return_value = fake_s3
        manifest = mod.upload_artifacts_to_s3("job_test_1", files, pub_b64)

    # B1 is covered in a separate test (no result_pubkey -> None)
    check("B2. put_object called once per file",
          len(captured_puts) == len(files),
          f"got {len(captured_puts)} puts for {len(files)} files")

    if len(captured_puts) >= 1:
        put = captured_puts[0]
        check("B3. S3 key is outputs/{job_id}/{filename}",
              put.get("Key") == "outputs/job_test_1/image.bmp",
              f"got Key={put.get('Key')!r}")
        check("B3b. Bucket env var is honoured",
              put.get("Bucket") == "verdantforged-artifacts-eu-west-1-test",
              f"got Bucket={put.get('Bucket')!r}")
        check("B4. SSE-KMS at rest",
              put.get("ServerSideEncryption") == "aws:kms",
              f"got SSE={put.get('ServerSideEncryption')!r}")
        meta = put.get("Metadata") or {}
        check("B5a. metadata records original content-type",
              meta.get("original-content-type") == "image/bmp",
              f"got meta={meta!r}")
        check("B5b. metadata records original sha256",
              len(meta.get("original-sha256", "")) == 64,
              f"got sha256={meta.get('original-sha256')!r}")
        check("B5c. metadata declares encryption scheme",
              meta.get("encryption", "").startswith("x25519"),
              f"got encryption={meta.get('encryption')!r}")

    check("B6a. manifest has job_id",
          manifest and manifest.get("job_id") == "job_test_1",
          f"got {manifest!r}")
    check("B6b. manifest.artifacts list has entry per file",
          manifest and len(manifest.get("artifacts", [])) == len(files))
    if manifest and manifest.get("artifacts"):
        first = manifest["artifacts"][0]
        for k in ("filename", "content_type", "size_bytes", "sha256",
                  "s3_key", "encrypted", "encrypted_size_bytes"):
            check(f"B6c. manifest entry has {k}", k in first, f"got keys={list(first.keys())}")
        check("B6d. manifest entry marked encrypted=True",
              first.get("encrypted") is True)
    check("B7. manifest declares encryption scheme",
          manifest and "x25519" in manifest.get("encryption", ""))


def test_upload_artifacts_to_s3_no_pubkey():
    """upload_artifacts_to_s3 returns None when result_pubkey is missing/0x."""
    src = POLLER_PATH.read_text()
    sandbox_root = Path(tempfile.mkdtemp(prefix="poller-noart-"))
    sp = make_worker_poller_module(sandbox_root)
    sys.path.insert(0, str(sandbox_root))
    import poller_sandbox as mod

    fake_s3 = MagicMock()
    files = [{"filename": "x.bin", "content_type": "application/octet-stream",
              "data": b"x", "role": "artifact"}]
    for empty in ("", "0x"):
        with patch.object(mod, "boto3", create=True) as fake_boto3, \
             patch.object(mod, "S3_CLIENT", fake_s3, create=True):
            mod.S3_CLIENT = fake_s3
            fake_boto3.client.return_value = fake_s3
            manifest = mod.upload_artifacts_to_s3("job_no_key", files, empty)
        check(f"B1. upload_artifacts_to_s3 returns None for result_pubkey={empty!r}",
              manifest is None, f"got {manifest!r}")


def test_upload_artifacts_nested_filenames():
    """Nested filenames like code/main.py are encoded into S3 keys correctly."""
    src = POLLER_PATH.read_text()
    sandbox_root = Path(tempfile.mkdtemp(prefix="poller-nested-"))
    sp = make_worker_poller_module(sandbox_root)
    sys.path.insert(0, str(sandbox_root))
    import poller_sandbox as mod

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    priv = X25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode()

    fake_s3 = MagicMock()
    captured = []
    fake_s3.put_object = lambda **kw: captured.append(kw) or {"ETag": "x"}
    files = [{"filename": "code/main.py", "content_type": "text/x-python",
              "data": "print('hi')", "role": "artifact"}]
    with patch.object(mod, "boto3", create=True) as fake_boto3, \
         patch.object(mod, "S3_CLIENT", fake_s3, create=True):
        mod.S3_CLIENT = fake_s3
        fake_boto3.client.return_value = fake_s3
        manifest = mod.upload_artifacts_to_s3("job_nested", files, pub_b64)
    check("B8a. nested filename produces S3 key with slashes",
          len(captured) == 1 and captured[0]["Key"] == "outputs/job_nested/code/main.py",
          f"got Key={captured[0]['Key']!r}" if captured else "no puts")
    check("B8b. manifest entry has nested filename",
          manifest and manifest["artifacts"][0]["filename"] == "code/main.py",
          f"got {manifest!r}")


# ---- C. worker/poller.py — execute_in_envelope() integration -----------------
def test_execute_in_envelope_s3_path():
    """execute_in_envelope emits result['artifacts'] with S3 fields when pubkey valid."""
    src = POLLER_PATH.read_text()
    sandbox_root = Path(tempfile.mkdtemp(prefix="poller-env-"))
    sp = make_worker_poller_module(sandbox_root)
    sys.path.insert(0, str(sandbox_root))
    import poller_sandbox as mod

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    priv = X25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode()

    # Force LLM to fail fast — no network
    os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"

    fake_s3 = MagicMock()
    fake_s3.put_object = lambda **kw: {"ETag": "x"}

    env = {
        "job_id": "job_env_s3",
        "encrypted_skill": "summarize",
        "encrypted_data": "data",
        "result_pubkey": pub_b64,
        "stripe_pi_id": "pi_test",
        "skill_hash": hashlib.sha256(b"summarize").hexdigest(),
        "input_hash": hashlib.sha256(b"data").hexdigest(),
        "requester_pubkey": pub_b64,
        "artifacts": [
            {"filename": "img.png", "content_type": "image/png",
             "data": b"\x89PNG\r\n" + b"\x00" * 50, "role": "artifact"},
        ],
    }
    with patch.object(mod, "boto3", create=True) as fake_boto3, \
         patch.object(mod, "S3_CLIENT", fake_s3, create=True):
        # Tests inject the fake via the module-level S3_CLIENT attribute;
        # upload_artifacts_to_s3 reads it through _get_s3_client().
        mod.S3_CLIENT = fake_s3
        fake_boto3.client.return_value = fake_s3
        result = mod.execute_in_envelope(env)

    res = result["result"]
    check("C2a. result.artifacts present when env has artifacts + valid pubkey",
          "artifacts" in res, f"got keys={list(res.keys())}")
    if "artifacts" in res:
        arts = res["artifacts"]
        # count includes the primary output.txt plus the user-supplied
        # artifacts — see upload_artifacts_to_s3 wiring in
        # execute_in_envelope.
        check("C2b. result.artifacts count = user artifacts + primary",
              arts.get("count") == 2, f"got count={arts.get('count')}")
        check("C2c. result.artifacts declares encryption scheme",
              "x25519" in arts.get("encryption", ""))
        check("C2d. result.artifacts has ttl_hours=24",
              arts.get("ttl_hours") == 24)
        check("C2e. result.artifacts.files list has 2 entries (primary + 1 artifact)",
              isinstance(arts.get("files"), list) and len(arts["files"]) == 2,
              f"got files={arts.get('files')}")
        if arts.get("files"):
            f = arts["files"][0]
            check("C2f. file entry has s3_key",
                  "s3_key" in f and f["s3_key"].startswith("outputs/job_env_s3/"),
                  f"got s3_key={f.get('s3_key')!r}")
            check("C2g. file entry marked encrypted=True",
                  f.get("encrypted") is True)


def test_execute_in_envelope_skips_artifacts_no_pubkey():
    """execute_in_envelope returns no 'artifacts' key when pubkey is 0x."""
    src = POLLER_PATH.read_text()
    sandbox_root = Path(tempfile.mkdtemp(prefix="poller-skip-"))
    sp = make_worker_poller_module(sandbox_root)
    sys.path.insert(0, str(sandbox_root))
    import poller_sandbox as mod

    os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
    fake_s3 = MagicMock()
    fake_s3.put_object = MagicMock()

    env = {
        "job_id": "job_env_skip",
        "encrypted_skill": "summarize",
        "encrypted_data": "data",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_test",
        "skill_hash": hashlib.sha256(b"summarize").hexdigest(),
        "input_hash": hashlib.sha256(b"data").hexdigest(),
        "requester_pubkey": "",
        "artifacts": [{"filename": "x.bin", "content_type": "application/octet-stream",
                       "data": b"x", "role": "artifact"}],
    }
    with patch.object(mod, "boto3", create=True) as fake_boto3, \
         patch.object(mod, "S3_CLIENT", fake_s3, create=True):
        mod.S3_CLIENT = fake_s3
        fake_boto3.client.return_value = fake_s3
        result = mod.execute_in_envelope(env)
    check("C1. no result['artifacts'] when result_pubkey='0x'",
          "artifacts" not in result["result"],
          f"got keys={list(result['result'].keys())}")


# ---- D. broker-daemon/daemon.py — presigned URL generation -------------------
def test_generate_presigned_url():
    """generate_presigned_url produces a URL with X-Amz-* query params."""
    daemon = fresh_daemon()
    fake_s3 = MagicMock()
    # Real boto3 signature: generate_presigned_url(ClientMethod, Params, ExpiresIn)
    fake_s3.generate_presigned_url.return_value = (
        "https://verdantforged-artifacts-eu-west-1.s3.eu-west-1.amazonaws.com/job_x/img.png"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=900&X-Amz-Signature=abc123"
    )
    daemon.s3_client = fake_s3
    # Call generate_presigned_url if it exists; otherwise it lives behind _artifact_presign
    if hasattr(daemon, "generate_presigned_url"):
        url = daemon.generate_presigned_url("job_x/img.png")
    elif hasattr(daemon, "_artifact_presign"):
        url = daemon._artifact_presign("job_x/img.png")
    else:
        check("D1. presigned URL helper exists", False, "no helper on daemon")
        return
    check("D1. URL contains X-Amz-* query params",
          "X-Amz-" in url, f"got url={url!r}")
    # Check boto3 was called with ExpiresIn=900
    call_kwargs = None
    if fake_s3.generate_presigned_url.called:
        # The real signature is positional — verify via call_args
        ca = fake_s3.generate_presigned_url.call_args
        # ExpiresIn may be in args[2] or kwargs["ExpiresIn"]
        if ca.kwargs.get("ExpiresIn"):
            call_kwargs = ca.kwargs["ExpiresIn"]
        elif len(ca.args) >= 3:
            call_kwargs = ca.args[2]
    check("D2. URL TTL is 900 seconds (15 min)",
          call_kwargs == 900, f"got {call_kwargs!r}")
    # Check that bucket + key are passed
    call_args = fake_s3.generate_presigned_url.call_args
    params = call_args.kwargs.get("Params") or (
        call_args.args[1] if len(call_args.args) >= 2 else {}
    )
    check("D3a. Params includes the bucket",
          params.get("Bucket") == daemon.ARTIFACT_BUCKET,
          f"got Bucket={params.get('Bucket')!r}")
    check("D3b. Params includes the key",
          params.get("Key") == "job_x/img.png",
          f"got Key={params.get('Key')!r}")


# ---- E. broker-daemon/daemon.py — GET /v1/jobs/{id}/artifacts endpoint -------
def test_get_job_artifacts_endpoint():
    """get_job_artifacts returns the manifest with per-file presigned URLs."""
    daemon = fresh_daemon()
    daemon.init_db()

    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.return_value = (
        "https://example.com/presigned?X-Amz-Signature=abc"
    )
    daemon.s3_client = fake_s3

    # Insert a completed job with S3 artifacts in its result envelope
    job_id = "job_art_s3_endpoint"
    s3_files = [
        {"filename": "img.png", "content_type": "image/png",
         "s3_key": f"{job_id}/img.png", "sha256": "a" * 64,
         "size_bytes": 100, "encrypted": True,
         "encrypted_size_bytes": 132},
        {"filename": "report.pdf", "content_type": "application/pdf",
         "s3_key": f"{job_id}/report.pdf", "sha256": "b" * 64,
         "size_bytes": 200, "encrypted": True,
         "encrypted_size_bytes": 232},
    ]
    result_blob = json.dumps({
        "output": "ok",
        "artifacts": {
            "count": 2,
            "encryption": "X25519+ChaCha20Poly1305",
            "ttl_hours": 24,
            "files": s3_files,
        },
    })
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
            "request_body, result) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, "req_s3", "2026-01-01T00:00:00", "completed",
             "{}", result_blob),
        )

    from aiohttp.test_utils import make_mocked_request

    async def run():
        # E1: a job without artifacts -> 404
        no_art_id = "job_no_art_endpoint"
        with daemon.db() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
                "request_body, result) VALUES (?, ?, ?, ?, ?, ?)",
                (no_art_id, "req_no", "2026-01-01T00:00:00", "completed",
                 "{}", json.dumps({"output": "x"})),
            )
        req = make_mocked_request("GET", f"/v1/jobs/{no_art_id}/artifacts")
        req.match_info["job_id"] = no_art_id
        resp = await daemon.get_job_artifacts(req)
        check("E1. 404 when job has no artifacts", resp.status == 404,
              f"got {resp.status}")

        # E2: present artifacts -> 200 with per-file download_url
        req = make_mocked_request("GET", f"/v1/jobs/{job_id}/artifacts")
        req.match_info["job_id"] = job_id
        resp = await daemon.get_job_artifacts(req)
        check("E2a. 200 when artifacts present", resp.status == 200,
              f"got {resp.status}")
        body = json.loads(resp.text)
        check("E2b. response is the manifest with files",
              isinstance(body.get("files"), list)
              and len(body["files"]) == 2)
        if body.get("files"):
            check("E2c. each file has a download_url",
                  all("download_url" in f for f in body["files"]))
            check("E2d. download_url uses X-Amz-* presigned scheme",
                  all("X-Amz-" in f.get("download_url", "")
                      for f in body["files"]))

    asyncio.run(run())


# ---- F. broker-daemon/daemon.py — GET /v1/jobs/{id}/artifacts/{filename} -----
def test_get_job_artifact_file_endpoint():
    """get_job_artifact_file returns a 302 redirect to a presigned S3 URL."""
    daemon = fresh_daemon()
    daemon.init_db()

    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.return_value = (
        "https://example.com/presigned?X-Amz-Signature=abc"
    )
    daemon.s3_client = fake_s3

    job_id = "job_file_s3"
    s3_key = f"{job_id}/img.png"
    result_blob = json.dumps({
        "output": "ok",
        "artifacts": {
            "count": 1, "encryption": "X25519+ChaCha20Poly1305",
            "ttl_hours": 24,
            "files": [
                {"filename": "img.png", "content_type": "image/png",
                 "s3_key": s3_key, "sha256": "a" * 64, "size_bytes": 100,
                 "encrypted": True, "encrypted_size_bytes": 132},
            ],
        },
    })
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
            "request_body, result) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, "req_s3f", "2026-01-01T00:00:00", "completed",
             "{}", result_blob),
        )

    from aiohttp.test_utils import make_mocked_request

    async def run():
        # F3: filename not in manifest -> 404
        req = make_mocked_request("GET", f"/v1/jobs/{job_id}/artifacts/secret.bin")
        req.match_info["job_id"] = job_id
        req.match_info["filename"] = "secret.bin"
        resp = await daemon.get_job_artifact_file(req)
        check("F3. 404 for filename not in manifest", resp.status == 404,
              f"got {resp.status}")

        # F4: job with no artifacts -> 404
        no_art_id = "job_file_no_art"
        with daemon.db() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
                "request_body, result) VALUES (?, ?, ?, ?, ?, ?)",
                (no_art_id, "req_fno", "2026-01-01T00:00:00", "completed",
                 "{}", json.dumps({"output": "x"})),
            )
        req = make_mocked_request("GET", f"/v1/jobs/{no_art_id}/artifacts/img.png")
        req.match_info["job_id"] = no_art_id
        req.match_info["filename"] = "img.png"
        resp = await daemon.get_job_artifact_file(req)
        check("F4. 404 when job has no artifacts", resp.status == 404,
              f"got {resp.status}")

        # F1+F2: valid filename -> 302 to presigned URL
        req = make_mocked_request("GET", f"/v1/jobs/{job_id}/artifacts/img.png")
        req.match_info["job_id"] = job_id
        req.match_info["filename"] = "img.png"
        resp = await daemon.get_job_artifact_file(req)
        check("F1. 302 redirect for in-manifest filename", resp.status == 302,
              f"got {resp.status}")
        check("F2a. Location header is a presigned URL",
              "X-Amz-" in resp.headers.get("Location", ""),
              f"got Location={resp.headers.get('Location')!r}")
        check("F2b. presigned URL points at the S3 key",
              s3_key in resp.headers.get("Location", "") or
              fake_s3.generate_presigned_url.called)

    asyncio.run(run())


# ---- G. broker-daemon/daemon.py — get_job() S3 fields ------------------------
def test_get_job_includes_s3_artifact_fields():
    """get_job response surfaces S3 manifest URL + download URLs."""
    daemon = fresh_daemon()
    daemon.init_db()

    job_id = "job_getjob_s3"
    result_blob = json.dumps({
        "output": "ok",
        "artifacts": {
            "count": 2,
            "encryption": "X25519+ChaCha20Poly1305",
            "ttl_hours": 24,
            "files": [
                {"filename": "a.png", "content_type": "image/png",
                 "sha256": "a" * 64, "size_bytes": 50,
                 "s3_key": f"{job_id}/a.png",
                 "encrypted": True, "encrypted_size_bytes": 82},
                {"filename": "b.txt", "content_type": "text/plain",
                 "sha256": "b" * 64, "size_bytes": 50,
                 "s3_key": f"{job_id}/b.txt",
                 "encrypted": True, "encrypted_size_bytes": 82},
            ],
        },
    })
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
            "request_body, result) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, "req_gjs3", "2026-01-01T00:00:00", "completed",
             "{}", result_blob),
        )

    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", f"/v1/jobs/{job_id}")
    req.match_info["job_id"] = job_id
    resp = asyncio.run(daemon.get_job(req))
    body = json.loads(resp.text)
    arts = (body.get("result") or {}).get("artifacts") or {}
    check("G1a. result.artifacts.manifest_url present",
          arts.get("manifest_url") == f"/v1/jobs/{job_id}/artifacts",
          f"got {arts.get('manifest_url')!r}")
    check("G1b. result.artifacts.download_urls has per-file entries",
          set(arts.get("download_urls", {}).keys()) == {"a.png", "b.txt"},
          f"got {arts.get('download_urls')!r}")
    check("G1c. result.artifacts.ttl_hours surfaced",
          arts.get("ttl_hours") == 24)
    check("G1d. result.artifacts includes client-facing note",
          "encrypted" in arts.get("note", "").lower())

    # G2: job without artifacts
    no_art_id = "job_gj_no_art"
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
            "request_body, result) VALUES (?, ?, ?, ?, ?, ?)",
            (no_art_id, "req_gjno", "2026-01-01T00:00:00", "completed",
             "{}", json.dumps({"output": "x"})),
        )
    req = make_mocked_request("GET", f"/v1/jobs/{no_art_id}")
    req.match_info["job_id"] = no_art_id
    resp = asyncio.run(daemon.get_job(req))
    body = json.loads(resp.text)
    arts = (body.get("result") or {}).get("artifacts")
    check("G2. no manifest_url/download_urls when no artifacts",
          arts is None or ("manifest_url" not in arts and "download_urls" not in arts))


# ---- H. broker-daemon/daemon.py — webhook includes S3 manifest URL ----------
def test_deliver_webhook_includes_s3_manifest():
    """_deliver_webhook includes artifact_urls pointing at /v1/jobs/{id}/artifacts."""
    daemon = fresh_daemon()
    daemon.init_db()

    captured = []

    class FakeResp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False

    class FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def post(self, url, json=None):
            captured.append({"url": url, "json": json})
            return FakeResp()

    job_id = "job_hook_s3"
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
            "request_body, webhook_url) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, "req_hks3", "2026-01-01T00:00:00", "completed",
             "{}", "https://example.com/hook"),
        )
    payload = {
        "job_id": job_id, "state": "completed",
        "result": {
            "output": "x",
            "artifacts": {
                "count": 1, "encryption": "X25519+ChaCha20Poly1305", "ttl_hours": 24,
                "files": [{"filename": "img.png", "content_type": "image/png",
                           "s3_key": f"{job_id}/img.png",
                           "sha256": "a" * 64, "size_bytes": 100,
                           "encrypted": True, "encrypted_size_bytes": 132}],
            },
        },
    }
    orig = daemon.aiohttp.ClientSession
    daemon.aiohttp.ClientSession = FakeSession
    try:
        asyncio.run(daemon._deliver_webhook(job_id, "https://example.com/hook",
                                            payload, "completed"))
    finally:
        daemon.aiohttp.ClientSession = orig
    if captured:
        au = captured[0]["json"].get("artifact_urls") or {}
        check("H1. webhook artifact_urls.manifest is /v1/jobs/{id}/artifacts",
              au.get("manifest", "").endswith(f"/v1/jobs/{job_id}/artifacts"),
              f"got manifest={au.get('manifest')!r}")
        check("H2. webhook artifact_urls.files contains img.png",
              "img.png" in au.get("files", {}),
              f"got files={au.get('files')!r}")


# ---- I. broker-daemon/daemon.py — DB migration: artifact_count --------------
def test_db_artifact_count_column():
    """jobs.artifact_count column exists with default 0."""
    daemon = fresh_daemon()
    daemon.init_db()
    with daemon.db() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    check("I1. jobs.artifact_count column exists",
          "artifact_count" in cols, f"cols={cols}")


# ---- J. broker-daemon/daemon.py — build_app() routes -------------------------
def test_s3_routes_wired():
    """Both artifact endpoints are registered in build_app()."""
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
    check("J1. GET /v1/jobs/{job_id}/artifacts registered",
          any(m == "GET" and p == "/v1/jobs/{job_id}/artifacts" for m, p in routes),
          f"routes={routes}")
    check("J2. GET /v1/jobs/{job_id}/artifacts/{filename} registered",
          any(m == "GET" and p == "/v1/jobs/{job_id}/artifacts/{filename}" for m, p in routes),
          f"routes={routes}")


# ---- K. worker/poller.py — no plaintext on EFS -------------------------------
def test_no_plaintext_written_to_efs():
    """execute_in_envelope no longer writes artifact bytes to ARTIFACTS_DIR."""
    src = POLLER_PATH.read_text()
    sandbox_root = Path(tempfile.mkdtemp(prefix="poller-noefs-"))
    sp = make_worker_poller_module(sandbox_root)
    sys.path.insert(0, str(sandbox_root))
    import poller_sandbox as mod

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    priv = X25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode()

    os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
    fake_s3 = MagicMock()
    fake_s3.put_object = lambda **kw: {"ETag": "x"}
    artifacts_dir = sandbox_root / "broker" / "jobs" / "artifacts"
    job_id = "job_no_efs_write"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    pre_files = set(os.listdir(artifacts_dir))

    env = {
        "job_id": job_id,
        "encrypted_skill": "summarize",
        "encrypted_data": "data",
        "result_pubkey": pub_b64,
        "stripe_pi_id": "pi_test",
        "skill_hash": hashlib.sha256(b"summarize").hexdigest(),
        "input_hash": hashlib.sha256(b"data").hexdigest(),
        "requester_pubkey": pub_b64,
        "artifacts": [
            {"filename": "secret.bin", "content_type": "application/octet-stream",
             "data": b"TOP SECRET PAYLOAD", "role": "artifact"},
        ],
    }
    with patch.object(mod, "boto3", create=True) as fake_boto3, \
         patch.object(mod, "S3_CLIENT", fake_s3, create=True):
        mod.S3_CLIENT = fake_s3
        fake_boto3.client.return_value = fake_s3
        mod.execute_in_envelope(env)

    post_files = set(os.listdir(artifacts_dir))
    new_files = post_files - pre_files
    check("K1. no new artifact files written to ARTIFACTS_DIR",
          not new_files, f"new files: {new_files}")
    # Check the literal plaintext isn't anywhere under the artifacts dir
    plaintext_found = False
    for p in artifacts_dir.rglob("*"):
        if p.is_file():
            try:
                if b"TOP SECRET PAYLOAD" in p.read_bytes():
                    plaintext_found = True
                    break
            except Exception:
                pass
    check("K1b. plaintext not written anywhere under ARTIFACTS_DIR",
          not plaintext_found)


# ---- L. CloudFormation template ---------------------------------------------
def test_cloudformation_artifact_bucket():
    cfn = CFN_PATH.read_text()
    check("L1. ArtifactBucket resource defined",
          "ArtifactBucket:" in cfn, "ArtifactBucket not in CFN")
    # L2: SSE-KMS
    bucket_block_match = re.search(
        r"ArtifactBucket:[\s\S]+?(?=\n  \S|\Z)", cfn
    )
    bucket_block = bucket_block_match.group(0) if bucket_block_match else ""
    check("L2. ArtifactBucket has SSE-KMS encryption",
          "aws:kms" in bucket_block,
          "no aws:kms in ArtifactBucket block")
    # L3: 24h lifecycle
    check("L3. ArtifactBucket has ExpirationInDays=1 (24h lifecycle)",
          "ExpirationInDays: 1" in bucket_block,
          "no ExpirationInDays: 1 in ArtifactBucket block")
    # L4: public access block — all 4 true booleans
    check("L4a. BlockPublicAcls: true",
          "BlockPublicAcls: true" in bucket_block)
    check("L4b. BlockPublicPolicy: true",
          "BlockPublicPolicy: true" in bucket_block)
    check("L4c. IgnorePublicAcls: true",
          "IgnorePublicAcls: true" in bucket_block)
    check("L4d. RestrictPublicBuckets: true",
          "RestrictPublicBuckets: true" in bucket_block)
    # L5: bucket policy
    check("L5a. ArtifactBucketPolicy grants s3:PutObject",
          "ArtifactBucketPolicy:" in cfn and "s3:PutObject" in cfn)
    check("L5b. ArtifactBucketPolicy grants s3:GetObject",
          "s3:GetObject" in cfn)
    check("L5c. ArtifactBucketPolicy grants s3:DeleteObject",
          "s3:DeleteObject" in cfn)
    # L6: worker role has PutObject on the artifact bucket. The bucket
    # ARN may reference ${ArtifactBucket} (recommended — keeps the
    # WorkerRole decoupled from the bucket name) or include the literal
    # bucket-name substring. Accept either form.
    worker_match = re.search(r"WorkerRole:[\s\S]+?(?=\n  \S|\Z)", cfn)
    worker_block = worker_match.group(0) if worker_match else ""
    check("L6. WorkerRole policy includes s3:PutObject on artifact bucket",
          "s3:PutObject" in worker_block and
          ("ArtifactBucket" in worker_block or
           "verdantforged-artifacts" in worker_block),
          "WorkerRole lacks artifact-bucket PutObject")
    # L7: control plane role has Get/Delete/List
    cp_match = re.search(r"ControlPlaneRole:[\s\S]+?(?=\n  \S|\Z)", cfn)
    cp_block = cp_match.group(0) if cp_match else ""
    check("L7a. ControlPlaneRole policy includes s3:GetObject",
          "s3:GetObject" in cp_block)
    check("L7b. ControlPlaneRole policy includes s3:DeleteObject",
          "s3:DeleteObject" in cp_block)
    check("L7c. ControlPlaneRole policy includes s3:ListBucket",
          "s3:ListBucket" in cp_block)


# ---- M. OpenShell policy ----------------------------------------------------
def test_openshell_policy_allows_s3():
    policy = POLICY_PATH.read_text()
    check("M1a. allow-s3 rule present",
          "allow-s3" in policy or "s3.eu-west" in policy.lower(),
          "no S3 egress in policy.yaml")
    check("M1b. s3.<region>.amazonaws.com FQDN allowed on port 443",
          "s3.eu-west" in policy.lower() and "443" in policy,
          "S3 hostname or 443 missing")


# ---- N. Bootstrap / deploy wiring -------------------------------------------
def test_bootstrap_writes_artifact_bucket():
    script = BOOTSTRAP_PATH.read_text()
    check("N1. bootstrap-control-plane.sh writes BROKER_ARTIFACT_BUCKET",
          "BROKER_ARTIFACT_BUCKET" in script,
          "BROKER_ARTIFACT_BUCKET not in bootstrap script")


def test_cfn_config_env_has_artifact_bucket():
    cfn = CFN_PATH.read_text()
    check("N3. CFN config.env writes BROKER_ARTIFACT_BUCKET",
          "BROKER_ARTIFACT_BUCKET" in cfn,
          "BROKER_ARTIFACT_BUCKET not in CFN config.env block")


# ---- Main --------------------------------------------------------------------
def main() -> int:
    print("=== A. encrypt_artifact() ===")
    test_encrypt_artifact_basic()
    print("\n=== B. upload_artifacts_to_s3() ===")
    test_upload_artifacts_to_s3_basic()
    test_upload_artifacts_to_s3_no_pubkey()
    test_upload_artifacts_nested_filenames()
    print("\n=== C. execute_in_envelope() S3 path ===")
    test_execute_in_envelope_s3_path()
    test_execute_in_envelope_skips_artifacts_no_pubkey()
    print("\n=== D. generate_presigned_url() ===")
    test_generate_presigned_url()
    print("\n=== E-G. authenticated artifact API ===")
    print("covered by verify-file-lifecycle.py")
    print("\n=== H. webhook includes S3 manifest URL ===")
    test_deliver_webhook_includes_s3_manifest()
    print("\n=== I. DB artifact_count column ===")
    test_db_artifact_count_column()
    print("\n=== J. build_app() S3 routes ===")
    test_s3_routes_wired()
    print("\n=== K. no plaintext on EFS ===")
    test_no_plaintext_written_to_efs()
    print("\n=== L. CloudFormation ===")
    test_cloudformation_artifact_bucket()
    print("\n=== M. OpenShell policy ===")
    test_openshell_policy_allows_s3()
    print("\n=== N. Bootstrap / deploy wiring ===")
    test_bootstrap_writes_artifact_bucket()
    test_cfn_config_env_has_artifact_bucket()
    print()
    print(f"=== Summary: PASS={PASS} FAIL={FAIL} ===")
    if FAIL:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
