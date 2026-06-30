#!/usr/bin/env python3
"""Contract tests for the authenticated encrypted file-job lifecycle.

These tests deliberately exercise the seams missed by the older helper-level
S3 suites: client-token authorization, complete phase-two envelopes, explicit
file encryption, atomic input cleanup, and workspace output collection.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID


ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="file-lifecycle-"))
os.environ.update({
    "BROKER_EFS_MOUNT": str(TMP / "broker"),
    "BROKER_REGION": "eu-west-1",
    "BROKER_ARTIFACT_BUCKET": "test-file-bucket",
    "BROKER_VPC_ID": "",
    "BROKER_SUBNET_ID": "",
    "BROKER_WORKER_SG": "",
    "BROKER_WORKER_AMI": "",
    "BROKER_WORKER_IAM_ROLE": "",
    "BROKER_DAILY_JOB_CAP": "1000",
})


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


import sys
sys.path.insert(0, str(ROOT / "broker-daemon"))
daemon = load("file_lifecycle_daemon", ROOT / "broker-daemon" / "daemon.py")
poller = load("file_lifecycle_poller", ROOT / "worker" / "poller.py")


def request(*, job_id: str = "", token: str = "", body=None):
    req = MagicMock()
    req.match_info = {"job_id": job_id}
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
    req.remote = "127.0.0.1"

    async def get_json():
        return body or {}
    req.json = get_json
    return req


def valid_pubkey():
    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, base64.b64encode(pub).decode()


def test_access_token():
    daemon.init_db()
    token = daemon.create_job_access_token()
    digest = daemon.hash_job_access_token(token)
    assert token.startswith("jobtok_")
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert daemon.verify_job_access_token_value(token, digest)
    assert not daemon.verify_job_access_token_value(token + "x", digest)


def test_file_encryption_contract():
    priv, pub = valid_pubkey()
    plaintext = os.urandom(4096)  # catches the old >43-byte plaintext bug
    blob = poller.encrypt_file_payload(
        plaintext, pub, direction="input", job_id="job_abc",
        filename="photo.bin")
    assert len(blob) == len(plaintext) + 60
    assert poller.decrypt_file_payload(
        blob, priv, direction="input", job_id="job_abc",
        filename="photo.bin") == plaintext
    try:
        poller.decrypt_file_payload(
            blob, priv, direction="input", job_id="job_other",
            filename="photo.bin")
    except Exception:
        pass
    else:
        raise AssertionError("job-bound AAD was not enforced")


def test_shared_envelope_contains_file_and_llm_fields():
    body = {
        "encrypted_skill": "summarize",
        "encrypted_data": "process attachment",
        "requester_sig": "0x",
        "result_pubkey": valid_pubkey()[1],
        "stripe_pi_id": "pi_demo",
    }
    env = daemon.build_job_envelope(
        job_id="job_abc", created_at="now", body=body,
        llm_token="llm_secret", skill_hash="a" * 64,
        input_files=[{
            "filename": "input.bin", "content_type": "application/octet-stream",
            "size_bytes": 10, "encrypted_size_bytes": 70,
            "s3_key": "inputs/job_abc/input.bin",
        }],
    )
    assert env["llm_token"] == "llm_secret"
    assert env["llm_proxy_url"].endswith("/v1/llm/chat/completions")
    assert env["input_files"][0]["encrypted_size_bytes"] == 70
    assert env["result_pubkey"] == body["result_pubkey"]


def test_job_status_requires_access_token():
    daemon.init_db()
    token = daemon.create_job_access_token()
    now = "2026-06-29T00:00:00+00:00"
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id,client_req_id,created_at,state,request_body,"
            "job_access_token_hash) VALUES (?,?,?,?,?,?)",
            ("job_auth", "req_auth", now, "queued", "{}",
             daemon.hash_job_access_token(token)))
    unauth = asyncio.run(daemon.get_job(request(job_id="job_auth")))
    assert unauth.status == 401
    auth = asyncio.run(daemon.get_job(request(job_id="job_auth", token=token)))
    assert auth.status == 200


def test_submit_worker_upload_ready_envelope():
    daemon.init_db()
    worker_priv, worker_pub = valid_pubkey()
    _, result_pub = valid_pubkey()
    instance_id = "i-file-test"
    policy_hash = daemon._policy_hash()
    pub_raw = base64.b64decode(worker_pub)
    binding = hashlib.sha256(
        b"verdantforged-worker-input-v1\0" + pub_raw + b"\0" +
        bytes.fromhex(policy_hash)).hexdigest()
    report = bytearray(1184)
    report[80:144] = bytes.fromhex(binding) + b"\0" * 32
    quote_key = ec.generate_private_key(ec.SECP384R1())
    der_signature = quote_key.sign(bytes(report[:672]), ec.ECDSA(hashes.SHA384()))
    sig_r, sig_s = decode_dss_signature(der_signature)
    report[672:744] = sig_r.to_bytes(72, "little")
    report[744:816] = sig_s.to_bytes(72, "little")
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test VLEK")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(quote_key.public_key()).serial_number(1)
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .sign(quote_key, hashes.SHA384()))
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    daemon.LOGS.mkdir(parents=True, exist_ok=True)
    (daemon.LOGS / "worker-keys.json").write_text(json.dumps({
        "instance_id": instance_id, "key_id": "wk_test",
        "x25519_pubkey_b64": worker_pub, "policy_hash": policy_hash,
        "attestation_binding_sha256": binding,
    }))
    (daemon.LOGS / "worker-attestation.json").write_text(json.dumps({
        "instance_id": instance_id, "tee_type": "amd-sev-snp",
        "source": "tsm_configfs", "measurement": "ab" * 48,
        "report": base64.b64encode(report).decode(),
        "cert_chain": [base64.b64encode(cert_der).decode()],
        "report_data": binding + "0" * 64,
    }))

    class Manager:
        async def ensure_worker(self):
            return daemon.WorkerState(instance_id, "10.0.0.5", 0)
    daemon.worker_mgr = Manager()
    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.side_effect = (
        lambda method, Params, ExpiresIn: f"https://s3.test/{Params['Key']}")
    fake_s3.head_object.return_value = {"ContentLength": 65}
    body = {
        "client_req_id": "file-e2e-contract", "encrypted_skill": "summarize",
        "encrypted_data": "summarize file", "requester_sig": "0x",
        "result_pubkey": result_pub, "shared_payment_token": "spt_demo_file_lifecycle",
        "input_files": [{"filename": "a.txt", "content_type": "text/plain",
                         "size_bytes": 5}],
    }

    async def flow():
        with patch.object(daemon, "_get_s3_client", return_value=fake_s3), \
             patch.object(daemon, "BROKER_PAYMENT_STUB_MODE", True):
            submitted = await daemon.submit_job(request(body=body))
            submitted_body = json.loads(submitted.text)
            await asyncio.sleep(0.01)
            token = submitted_body["job_access_token"]
            status = await daemon.get_job(request(
                job_id=submitted_body["job_id"], token=token))
            status_body = json.loads(status.text)
            ready = await daemon.mark_job_ready(request(
                job_id=submitted_body["job_id"], token=token))
            return submitted, submitted_body, status, status_body, ready

    submitted, submitted_body, status, status_body, ready = asyncio.run(flow())
    assert submitted.status == 202 and submitted_body["state"] == "awaiting_worker"
    assert status.status == 200 and status_body["state"] == "awaiting_inputs"
    assert status_body["input_upload"]["encryption"]["public_key"] == worker_pub
    assert ready.status == 200
    envelope = json.loads((daemon.INBOX / f"{submitted_body['job_id']}.json").read_text())
    assert envelope["llm_token"].startswith("llm_")
    assert envelope["llm_proxy_url"].endswith("/v1/llm/chat/completions")
    assert envelope["worker_key_id"] == "wk_test"


def test_stub_worker_identity_unblocks_demo_file_uploads_offline():
    daemon.init_db()
    _worker_priv, worker_pub = valid_pubkey()
    instance_id = "i-stub-offline"
    policy_hash = daemon._policy_hash()
    pub_raw = base64.b64decode(worker_pub)
    binding = hashlib.sha256(
        b"verdantforged-worker-input-v1\0" + pub_raw + b"\0" +
        bytes.fromhex(policy_hash)).hexdigest()
    daemon.LOGS.mkdir(parents=True, exist_ok=True)
    (daemon.LOGS / "worker-keys.json").write_text(json.dumps({
        "instance_id": instance_id, "key_id": "wk_stub",
        "x25519_pubkey_b64": worker_pub, "policy_hash": policy_hash,
        "attestation_binding_sha256": binding,
    }))
    (daemon.LOGS / "worker-attestation.json").write_text(json.dumps({
        "instance_id": instance_id, "tee_type": "demo-stub",
        "source": "instance_id_sha256", "measurement": "cd" * 32,
        "report": "", "cert_chain": [], "report_data": "",
    }))

    with patch.object(daemon, "BROKER_ALLOW_STUB_WORKER_ATTESTATION", False):
        identity, reason = daemon._worker_identity_status(instance_id)
        assert identity is None
        assert "report_data" in reason
    with patch.object(daemon, "BROKER_ALLOW_STUB_WORKER_ATTESTATION", True):
        identity, reason = daemon._worker_identity_status(instance_id)
        assert reason == "ok-stub-attestation"
        assert identity["key_id"] == "wk_stub"
        assert identity["attestation"]["report_data"] == binding + "0" * 64


def test_policy_hash_prefers_deployed_efs_policy():
    daemon.LOGS.mkdir(parents=True, exist_ok=True)
    efs_policy = daemon.LOGS / "openshell-policy.yaml"
    efs_policy.write_text("efs-policy\n")
    expected = hashlib.sha256(b"efs-policy\n").hexdigest()
    assert daemon._policy_hash() == expected


def test_new_worker_launch_clears_stale_identity_files_offline():
    daemon.LOGS.mkdir(parents=True, exist_ok=True)
    for name in daemon.WORKER_IDENTITY_FILES:
        (daemon.LOGS / name).write_text(json.dumps({"instance_id": "i-old"}))
    assert daemon._published_worker_identity_instances() == {"i-old"}
    daemon._clear_worker_identity_files("test")
    for name in daemon.WORKER_IDENTITY_FILES:
        assert not (daemon.LOGS / name).exists()


def test_worker_identity_failure_reaches_file_job_client_offline():
    daemon.init_db()
    _worker_priv, worker_pub = valid_pubkey()
    _, result_pub = valid_pubkey()
    instance_id = "i-policy-mismatch"
    bad_policy_hash = "00" * 32
    binding = hashlib.sha256(
        b"verdantforged-worker-input-v1\0" + base64.b64decode(worker_pub) +
        b"\0" + bytes.fromhex(bad_policy_hash)).hexdigest()
    daemon.LOGS.mkdir(parents=True, exist_ok=True)
    (daemon.LOGS / "worker-keys.json").write_text(json.dumps({
        "instance_id": instance_id, "key_id": "wk_bad_policy",
        "x25519_pubkey_b64": worker_pub, "policy_hash": bad_policy_hash,
        "attestation_binding_sha256": binding,
    }))
    (daemon.LOGS / "worker-attestation.json").write_text(json.dumps({
        "instance_id": instance_id, "tee_type": "amd-sev-snp",
        "source": "tsm_configfs", "measurement": "ef" * 48,
        "report": "", "cert_chain": [], "report_data": binding + "0" * 64,
    }))
    token = daemon.create_job_access_token()
    now = datetime.now(timezone.utc).isoformat()
    body = {"input_files": [{"filename": "a.txt", "size_bytes": 1}],
            "result_pubkey": result_pub}
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id,client_req_id,created_at,state,request_body,"
            "job_access_token_hash) VALUES (?,?,?,?,?,?)",
            ("job_policy_mismatch", "req_policy_mismatch", now, "awaiting_worker",
             json.dumps(body), daemon.hash_job_access_token(token)))

    class Manager:
        async def ensure_worker(self):
            return daemon.WorkerState(instance_id, "10.0.0.6", 0)
        def get_state(self):
            return daemon.WorkerState(instance_id, "10.0.0.6", 0)

    old_mgr = daemon.worker_mgr
    old_wait = os.environ.get("BROKER_WORKER_IDENTITY_ERROR_SECONDS")
    try:
        setattr(daemon, "worker_mgr", Manager())
        os.environ["BROKER_WORKER_IDENTITY_ERROR_SECONDS"] = "0"
        asyncio.run(daemon._prepare_file_job("job_policy_mismatch"))
        status = asyncio.run(daemon.get_job(request(
            job_id="job_policy_mismatch", token=token)))
    finally:
        setattr(daemon, "worker_mgr", old_mgr)
        if old_wait is None:
            os.environ.pop("BROKER_WORKER_IDENTITY_ERROR_SECONDS", None)
        else:
            os.environ["BROKER_WORKER_IDENTITY_ERROR_SECONDS"] = old_wait
    status_body = json.loads(status.text)
    assert status.status == 200
    assert status_body["state"] == "failed"
    assert "worker policy_hash mismatch" in status_body["error"]


def test_atomic_input_cleanup_and_workspace_outputs():
    priv, pub = valid_pubkey()
    files = []
    fake_s3 = MagicMock()
    bodies = {}
    for name, data in (("a.bin", os.urandom(100)), ("b.bin", os.urandom(200))):
        blob = poller.encrypt_file_payload(
            data, pub, direction="input", job_id="job_files", filename=name)
        key = f"inputs/job_files/{name}"
        files.append({"filename": name, "s3_key": key,
                      "content_type": "application/octet-stream",
                      "size_bytes": len(data), "encrypted_size_bytes": len(blob)})
        bodies[key] = blob

    def get_object(*, Bucket, Key):
        body = MagicMock()
        body.read.return_value = bodies[Key]
        return {"Body": body, "ContentLength": len(bodies[Key])}
    fake_s3.get_object.side_effect = get_object

    workspace = TMP / "workspace"
    staged = poller.stage_encrypted_inputs(
        "job_files", files, priv, workspace, s3_client=fake_s3)
    assert sorted(staged) == ["a.bin", "b.bin"]
    assert fake_s3.delete_object.call_count == 2

    output_dir = workspace / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "output.txt").write_text("done")
    (output_dir / "processed.bin").write_bytes(b"processed")
    outputs = poller.collect_workspace_outputs(output_dir)
    assert [f["filename"] for f in outputs] == ["output.txt", "processed.bin"]
    assert outputs[1]["data"] == b"processed"
    uploaded = {}
    output_s3 = MagicMock()
    output_s3.put_object.side_effect = lambda **kw: uploaded.setdefault(
        kw["Key"], kw["Body"]) or {"ETag": "x"}
    manifest = poller.upload_artifacts_to_s3(
        "job_files", outputs, pub, s3_client=output_s3)
    assert manifest["encryption"] == poller.FILE_ENCRYPTION
    for item in manifest["artifacts"]:
        assert item["s3_key"].startswith("outputs/job_files/")
        assert poller.decrypt_file_payload(
            uploaded[item["s3_key"]], priv, direction="output",
            job_id="job_files", filename=item["filename"]
        ) == next(f["data"] for f in outputs
                  if f["filename"] == item["filename"])


def test_api_docs_and_infrastructure_contract():
    spec = json.loads((ROOT / "broker-daemon/static/openapi.json").read_text())
    assert "/v1/jobs/{job_id}/ready" in spec["paths"]
    assert "JobAccessToken" in spec["components"]["securitySchemes"]
    response_props = spec["components"]["schemas"]["JobSubmitResponse"]["properties"]
    assert "job_access_token" in response_props and "llm_token" not in response_props
    docs = (ROOT / "docs/file-jobs.md").read_text()
    assert "x25519-hkdf-sha256-chacha20poly1305-v1" in docs
    cfn = (ROOT / "cloudformation-control-plane.yaml").read_text()
    assert "CorsConfiguration:" in cfn
    assert "- s3:HeadObject" not in cfn
    assert "BROKER_ARTIFACT_REGION=__ARTIFACT_REGION__" in (
        ROOT / "worker/user-data.sh").read_text()


def test_worker_execute_upload_download_round_trip():
    worker_private, worker_public = valid_pubkey()
    result_private, result_public = valid_pubkey()
    job_id = "job_worker_roundtrip"
    plaintext = b"This is a long encrypted attachment. " * 20
    ciphertext = poller.encrypt_file_payload(
        plaintext, worker_public, direction="input", job_id=job_id,
        filename="source.txt")
    fake_s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = ciphertext
    fake_s3.get_object.return_value = {"Body": body}
    uploaded = {}
    fake_s3.put_object.side_effect = lambda **kw: uploaded.setdefault(
        kw["Key"], kw["Body"])
    poller.S3_CLIENT = fake_s3
    poller._WORKER_X25519_KEYPAIR = worker_private

    llm_response = json.dumps({
        "model": "test-model",
        "choices": [{"message": {"content": "processed successfully"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2,
                  "total_tokens": 4},
    }).encode()

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return llm_response

    env = {
        "job_id": job_id, "encrypted_skill": "summarize",
        "skill_hash": hashlib.sha256(b"summarize").hexdigest(),
        "encrypted_data": "process the attachment",
        "result_pubkey": result_public, "stripe_pi_id": "pi_demo",
        "llm_token": "llm_test", "llm_proxy_url": "https://broker.test/v1/llm/chat/completions",
        "input_files": [{
            "filename": "source.txt", "content_type": "text/plain",
            "size_bytes": len(plaintext),
            "encrypted_size_bytes": len(ciphertext),
            "s3_key": f"inputs/{job_id}/source.txt",
        }],
    }
    with patch.object(poller, "_active_sandbox_name", return_value=""), \
         patch.object(poller, "KEY_DIR", TMP / "roundtrip-keys"), \
         patch.object(poller, "WORKSPACE_ROOT", TMP / "roundtrip-workspaces"), \
         patch.object(urllib.request, "urlopen", return_value=Response()):
        completed = poller.execute_in_envelope(env)
    assert completed["state"] == "completed"
    artifacts = completed["result"]["artifacts"]
    assert artifacts["count"] == 1
    output = artifacts["files"][0]
    assert output["filename"] == "output.txt"
    assert poller.decrypt_file_payload(
        uploaded[output["s3_key"]], result_private, direction="output",
        job_id=job_id, filename="output.txt") == b"processed successfully"
    assert fake_s3.delete_object.call_count == 1


def test_legacy_database_migration_preserves_file_columns():
    legacy_db = TMP / "legacy" / "broker.db"
    legacy_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(legacy_db)
    conn.execute(
        "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, client_req_id TEXT NOT NULL "
        "UNIQUE, created_at TEXT NOT NULL, state TEXT NOT NULL, "
        "request_body TEXT NOT NULL, job_access_token_hash TEXT)")
    conn.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?)",
        ("job_old", "req_old", "now", "completed", "{}", "digest"))
    conn.commit()
    conn.close()
    with patch.object(daemon, "DB_PATH", legacy_db):
        daemon.init_db()
        with daemon.db() as migrated:
            columns = {row[1]: row for row in migrated.execute(
                "PRAGMA table_info(jobs)").fetchall()}
            row = migrated.execute(
                "SELECT job_access_token_hash FROM jobs WHERE job_id='job_old'"
            ).fetchone()
    assert columns["request_body"][3] == 0
    assert "worker_key_id" in columns
    assert row[0] == "digest"


def test_expired_upload_is_abandoned_and_deleted():
    daemon.init_db()
    job_id = "job_expired_upload"
    with daemon.db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id,client_req_id,created_at,state,request_body,"
            "input_file_count,input_status,input_upload_expires_at,"
            "job_access_token_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, "req_expired", "2020-01-01T00:00:00+00:00",
             "awaiting_inputs", json.dumps({"input_files": [{
                 "filename": "old.bin", "content_type": "application/octet-stream",
                 "size_bytes": 1}]}), 1, "awaiting_inputs",
             "2020-01-01T00:00:00+00:00", "digest"))
    fake_s3 = MagicMock()
    with patch.object(daemon, "_get_s3_client", return_value=fake_s3):
        assert daemon.sweep_expired_input_uploads() == 1
    with daemon.db() as conn:
        state = conn.execute(
            "SELECT state,input_status FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
    assert tuple(state) == ("abandoned", "expired")
    fake_s3.delete_object.assert_called_once_with(
        Bucket=daemon.ARTIFACT_BUCKET, Key=f"inputs/{job_id}/old.bin")


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print(f"\n{len(tests)} file lifecycle tests passed")


if __name__ == "__main__":
    main()
