#!/usr/bin/env python3
"""Submit a broker file-job, upload encrypted inputs, and decrypt the result.

This is a small end-to-end client for the VerdantForged broker:
1. Generate a fresh X25519 result key pair.
2. Submit with a Stripe ACS Shared Payment Token (spt_...), or mint a stubbed
   demo token from the broker when Stripe login is not available.
3. Submit a file-job with the supplied files. If `--demo-spt` is enabled, the
   helper mints a stubbed payment token from the broker first.
4. Wait for `awaiting_inputs`, but also poll `/healthz` so slow worker cold
   starts do not cause a false timeout. The helper only uploads once the job is
   ready and the worker is no longer reported as `offline`. If the job remains
   in `awaiting_worker` after the worker is live, the broker is still waiting
   for the attested worker key / report-data binding to publish.
5. Mark the job ready and poll until completion.
6. Decrypt `result_encrypted` locally and print the final artifact / billing data.
7. Fetch the artifact manifest from the broker, download each encrypted
   artifact blob, decrypt it with the result private key (X25519 + ChaCha20-
   Poly1305, AAD=verdantforged-artifact), write the plaintexts to
   --artifacts-dir, and print a content preview of each.

Usage:
  python3 scripts/run_file_job_e2e.py \
    --file auth_handler.py \
    --file deployment.md

Optional:
  --prompt-file prompt.txt
  --prompt "..."
  --skill code-review
  --spt spt_test_...
  --legacy-create-pi --amount-cents 500
  --save-result-key result_key.json
  --artifacts-dir ./artifacts
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

BROKER_DEFAULT = "https://verdant.codepilots.co.uk"
STRIPE_API_BASE = "https://api.stripe.com/v1"
RESULT_AAD = b"verdantforged-result"
# Artifact encryption uses the v1 file-payload scheme (matches
# worker/poller.py encrypt_file_payload / decrypt_file_payload):
#   AAD     = b"verdantforged-file-v1\0" + direction + b"\0" + job_id + b"\0" + filename
#   key     = HKDF-SHA256(shared, info=b"verdantforged-file-key-v1\0" + AAD)
#   cipher  = ChaCha20-Poly1305(key).encrypt(nonce, plaintext, AAD)
#   wire    = ephemeral_pubkey_32 || nonce_12 || ciphertext_with_16byte_tag
# (The older constant AAD b"verdantforged-artifact" is the path of the
# unused encrypt_artifact helper -- do NOT reintroduce it.)
ARTIFACT_WIRE_OVERHEAD = 60  # 32 ephemeral pub + 12 nonce + 16 poly1305 tag
ARTIFACT_PREVIEW_BYTES = 2000  # how much of each decrypted artifact to print
INPUT_AAD_PREFIX = b"verdantforged-file-v1\0"
INPUT_KEY_INFO_PREFIX = b"verdantforged-file-key-v1\0"
DEFAULT_PROMPT = textwrap.dedent(
    """
    Read the attached files and produce a concrete security review that proves you used the
    files, not generic reasoning.

    Requirements:
    1. Identify at least 5 findings.
    2. Every finding must include:
       - severity (1-5)
       - filename
       - exact line number(s)
       - a short quoted snippet from the file
       - why it matters
       - a recommended fix
    3. Include one section called 'Cross-file inconsistencies' that lists at least 3 mismatches
       between the files.
    4. Include one section called 'Extracted facts' with 10 exact values pulled from the files
       (constants, env vars, endpoints, limits, filenames, etc.).
    5. Return the report in markdown and also include a JSON block with:
       - findings[]
       - inconsistencies[]
       - extracted_facts[]
    6. Do not write 'summary only' — the output must contain file-specific evidence and concrete
       recommendations.

    Focus on correctness, specificity, and direct citations from the attached files.
    """
).strip()


@dataclass
class FileSpec:
    path: Path
    filename: str
    content_type: str
    data: bytes


class JobError(RuntimeError):
    pass


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def load_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return args.prompt.strip()
    if args.prompt_file:
        return Path(args.prompt_file).read_text().strip()
    return DEFAULT_PROMPT


def load_files(paths: list[str]) -> list[FileSpec]:
    if not paths:
        raise JobError("at least one --file is required")
    files: list[FileSpec] = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if not p.exists() or not p.is_file():
            raise JobError(f"file not found: {p}")
        data = p.read_bytes()
        content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        files.append(FileSpec(path=p, filename=p.name, content_type=content_type, data=data))
    return files


def ssm_client(region: str):
    return boto3.client("ssm", region_name=region)


def get_stripe_secret(region: str, param_name: str) -> str:
    client = ssm_client(region)
    resp = client.get_parameter(Name=param_name, WithDecryption=True)
    return resp["Parameter"]["Value"]


def mint_demo_shared_payment_token(broker: str, amount_cents: int, currency: str, network_id: str) -> tuple[str, dict[str, Any]]:
    resp = requests.post(
        f"{broker}/v1/demo/shared-payment-token",
        json={
            "amount_cents": amount_cents,
            "currency": currency,
            "networkId": network_id,
            "context": "Demo stub token for broker testing without a real Link login.",
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload.get("shared_payment_token") or payload.get("spt") or ""
    if not isinstance(token, str) or not token.startswith("spt_demo_"):
        raise JobError(f"demo token route returned an unexpected payload: {payload}")
    return token, payload


def create_payment_intent(stripe_key: str, amount_cents: int, currency: str, description: str) -> dict[str, Any]:
    resp = requests.post(
        f"{STRIPE_API_BASE}/payment_intents",
        data={
            "amount": str(amount_cents),
            "currency": currency,
            "description": description,
            "confirm": "true",
            "payment_method": "pm_card_visa",
            "automatic_payment_methods[enabled]": "true",
            "automatic_payment_methods[allow_redirects]": "never",
        },
        headers={"Authorization": f"Bearer {stripe_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def generate_result_keypair() -> tuple[X25519PrivateKey, str]:
    priv = X25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    return priv, pub_b64


def encrypt_input_file(job_id: str, filename: str, plaintext: bytes, worker_public_b64: str) -> bytes:
    worker_public = X25519PublicKey.from_public_bytes(base64.b64decode(worker_public_b64))
    ephemeral = X25519PrivateKey.generate()
    ephemeral_pub = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    aad = INPUT_AAD_PREFIX + b"input\0" + job_id.encode() + b"\0" + filename.encode()
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=INPUT_KEY_INFO_PREFIX + aad,
    ).derive(ephemeral.exchange(worker_public))
    nonce = os.urandom(12)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return ephemeral_pub + nonce + ciphertext


def decrypt_result_encrypted(result_encrypted_b64: str, result_private: X25519PrivateKey) -> dict[str, Any]:
    raw = base64.b64decode(result_encrypted_b64)
    if len(raw) < 60:
        raise JobError("result_encrypted too short to decode")
    eph_pub = X25519PublicKey.from_public_bytes(raw[:32])
    nonce = raw[32:44]
    ciphertext = raw[44:]
    shared = result_private.exchange(eph_pub)
    plaintext = ChaCha20Poly1305(shared).decrypt(nonce, ciphertext, RESULT_AAD)
    try:
        return json.loads(plaintext)
    except Exception:
        return {"output": plaintext.decode(errors="replace"), "_raw": plaintext.decode(errors="replace")}


FILE_KEY_INFO_PREFIX = b"verdantforged-file-key-v1\0"
FILE_AAD_PREFIX = b"verdantforged-file-v1\0"


def _file_aad(direction: str, job_id: str, filename: str) -> bytes:
    if direction not in ("input", "output"):
        raise ValueError("direction must be input or output")
    if not job_id or not filename or "\x00" in job_id or "\x00" in filename:
        raise ValueError("job_id and filename must be non-empty and NUL-free")
    return f"verdantforged-file-v1\x00{direction}\x00{job_id}\x00{filename}".encode("utf-8")


def _file_key(shared: bytes, aad: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=FILE_KEY_INFO_PREFIX + aad,
    ).derive(shared)


def decrypt_artifact(
    ciphertext_blob: bytes,
    result_private: X25519PrivateKey,
    *,
    job_id: str,
    filename: str,
    direction: str = "output",
) -> bytes:
    """Decrypt a single artifact blob.

    The worker uses `encrypt_file_payload` (worker/poller.py, line 183) for
    all artifact blobs uploaded to S3. That helper binds the ciphertext to
    the (direction, job_id, filename) tuple via an AAD of
        verdantforged-file-v1\\0<direction>\\0<job_id>\\0<filename>
    and derives the ChaCha20-Poly1305 key with HKDF-SHA256 over the raw
    X25519 shared secret using
        info = verdantforged-file-key-v1\\0 + AAD

    Wire format (must match the worker's encrypt_file_payload):
        ephemeral_pubkey_32 || nonce_12 || ciphertext_with_16byte_tag

    Earlier versions of this script used a constant AAD
    (b"verdantforged-artifact") and the raw shared secret as the key --
    that matched the unused `encrypt_artifact` helper but NOT the path
    the S3 uploader actually takes. Every artifact therefore decrypted
    with InvalidTag. The fix is to mirror encrypt_file_payload exactly
    and pass job_id + filename in.
    """
    if len(ciphertext_blob) < ARTIFACT_WIRE_OVERHEAD:
        raise JobError(
            f"artifact blob too short: {len(ciphertext_blob)} < {ARTIFACT_WIRE_OVERHEAD}"
        )
    if not job_id or not filename:
        raise JobError("decrypt_artifact requires job_id and filename to bind the AAD")
    eph_pub = X25519PublicKey.from_public_bytes(ciphertext_blob[:32])
    nonce = ciphertext_blob[32:44]
    ciphertext = ciphertext_blob[44:]
    aad = _file_aad(direction, job_id, filename)
    shared = result_private.exchange(eph_pub)
    key = _file_key(shared, aad)
    return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, aad)


def download_and_decrypt_artifacts(
    broker: str,
    job_id: str,
    headers: dict[str, str],
    result_private: X25519PrivateKey,
    artifacts_dir: Path,
) -> list[dict[str, Any]]:
    """Fetch the artifact manifest, download each file, decrypt, save, preview.

    Returns a list of records describing what was downloaded + where it
    landed. The manifest is fetched from
    ``GET /v1/jobs/{job_id}/artifacts`` (broker presigns per-file S3 GET
    URLs with a 15-min TTL, exactly mirroring ``generate_presigned_url`` on
    the broker side). We follow each presigned URL to retrieve the
    encrypted blob, decrypt it with the result private key, and write the
    plaintext under ``artifacts_dir/<job_id>/``.

    Path-traversal defence: filenames from the manifest are checked for
    path separators and '..' components before being joined into the
    output directory. The broker already enforces the same check on its
    end, but we mirror it here so a tampered manifest cannot write outside
    ``artifacts_dir``.
    """
    manifest_url = f"{broker}/v1/jobs/{job_id}/artifacts"
    resp = requests.get(manifest_url, headers=headers, timeout=30)
    if resp.status_code == 404:
        print("\n=== ARTIFACTS ===")
        print("  (no artifacts produced for this job)")
        return []
    resp.raise_for_status()
    manifest = resp.json()
    files = manifest.get("files") or []
    if not files:
        print("\n=== ARTIFACTS ===")
        print("  (manifest is empty)")
        return []

    job_dir = artifacts_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== ARTIFACTS ===")
    print(f"  manifest: {manifest_url}")
    print(f"  encryption: {manifest.get('encryption', 'N/A')}")
    print(f"  files: {len(files)}")
    print(f"  out_dir: {job_dir}")

    records: list[dict[str, Any]] = []
    for entry in files:
        filename = entry.get("filename") or ""
        download_url = entry.get("download_url") or ""
        if not filename or not download_url:
            print(f"  [skip] entry missing filename or download_url: {entry}")
            continue
        safe_name = Path(filename).name
        if safe_name != filename or ".." in Path(filename).parts or filename.startswith("/"):
            print(f"  [skip] unsafe filename in manifest: {filename!r}")
            continue
        expected_sha = (entry.get("sha256") or "").lower()
        expected_size = entry.get("size_bytes")

        blob_resp = requests.get(download_url, timeout=120)
        blob_resp.raise_for_status()
        ciphertext = blob_resp.content
        try:
            plaintext = decrypt_artifact(
                ciphertext, result_private, job_id=job_id, filename=filename,
            )
        except Exception as exc:
            print(f"  [fail] decrypt {filename}: {exc}")
            records.append({"filename": filename, "ok": False, "error": str(exc)})
            continue

        # Honour nested subdirs in the manifest filename (matches
        # worker/poller.write_artifacts A5) but constrain to job_dir.
        out_path = (job_dir / filename).resolve()
        if not str(out_path).startswith(str(job_dir.resolve())):
            print(f"  [skip] {filename} resolves outside artifacts dir")
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(plaintext)

        # Integrity check against manifest's sha256 + size if present.
        actual_sha = hashlib.sha256(plaintext).hexdigest()
        actual_size = len(plaintext)
        sha_ok = (not expected_sha) or (actual_sha == expected_sha)
        size_ok = (expected_size is None) or (actual_size == expected_size)
        integrity = "ok" if (sha_ok and size_ok) else "MISMATCH"
        if not sha_ok:
            print(f"  [warn] sha256 mismatch for {filename}: {actual_sha} != {expected_sha}")
        if not size_ok:
            print(f"  [warn] size mismatch for {filename}: {actual_size} != {expected_size}")

        records.append(
            {
                "filename": filename,
                "ok": True,
                "path": str(out_path),
                "size_bytes": actual_size,
                "sha256": actual_sha,
                "content_type": entry.get("content_type", "application/octet-stream"),
                "integrity": integrity,
            }
        )
        print(
            f"  downloaded+decrypted {filename} -> {out_path} "
            f"({actual_size} bytes, sha256={actual_sha[:16]}..., integrity={integrity})"
        )

        # Display a preview. Text-y types get a UTF-8 decode; binaries get
        # a hex excerpt so the operator can sanity-check the decryption
        # without dumping megabytes to a terminal.
        ct = entry.get("content_type", "")
        if ct.startswith("text/") or ct in (
            "application/json",
            "application/xml",
            "application/yaml",
            "application/x-yaml",
        ) or filename.endswith((".md", ".txt", ".py", ".json", ".yaml", ".yml", ".csv", ".log", ".sh")):
            try:
                preview = plaintext.decode("utf-8")
            except UnicodeDecodeError:
                preview = plaintext[:ARTIFACT_PREVIEW_BYTES].hex()
                ct = f"{ct}+binary-excerpt"
        else:
            preview = plaintext[:ARTIFACT_PREVIEW_BYTES].hex()
            ct = f"{ct}+binary-excerpt"

        print(f"\n--- {filename} ({ct}, {actual_size} bytes) ---")
        if len(preview) > ARTIFACT_PREVIEW_BYTES:
            print(preview[:ARTIFACT_PREVIEW_BYTES])
            print(f"... [truncated {len(preview) - ARTIFACT_PREVIEW_BYTES} more bytes]")
        else:
            print(preview)
        print(f"--- end {filename} ---")

    return records


def get_healthz(broker: str, timeout_s: int = 10) -> dict[str, Any]:
    resp = requests.get(f"{broker}/healthz", timeout=timeout_s)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise JobError(f"unexpected /healthz payload: {payload!r}")
    return payload


def format_healthz(health: dict[str, Any]) -> str:
    fields = [
        f"ok={health.get('ok')}",
        f"worker={health.get('worker')}",
        f"worker_status={health.get('worker_status')}",
    ]
    for key in (
        'worker_instance_id',
        'worker_boot_stage',
        'worker_boot_detail',
        'worker_boot_elapsed_seconds',
        'worker_boot_eta_seconds',
        'worker_uptime_seconds',
        'worker_idle_seconds',
    ):
        val = health.get(key)
        if val not in (None, '', 0):
            fields.append(f"{key}={val}")
    return ' '.join(fields)


def poll_job(broker: str, job_id: str, headers: dict[str, str], timeout_s: int, sleep_s: int = 5) -> dict[str, Any]:
    start = time.time()
    while True:
        if time.time() - start > timeout_s:
            raise JobError(f"timeout waiting for job {job_id} after {timeout_s}s")
        resp = requests.get(f"{broker}/v1/jobs/{job_id}", headers=headers, timeout=20)
        resp.raise_for_status()
        job = resp.json()
        state = job.get("state")
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:>3}s] state={state}")
        if state in {"awaiting_inputs", "running", "queued", "awaiting_worker"}:
            return job
        if state == "failed":
            raise JobError(f"job failed: {job.get('error', 'unknown error')}")
        if state == "completed":
            return job
        time.sleep(sleep_s)


def wait_for_completion(broker: str, job_id: str, headers: dict[str, str], timeout_s: int, sleep_s: int = 10) -> dict[str, Any]:
    start = time.time()
    while True:
        if time.time() - start > timeout_s:
            raise JobError(f"timeout waiting for completion of {job_id} after {timeout_s}s")
        resp = requests.get(f"{broker}/v1/jobs/{job_id}", headers=headers, timeout=20)
        resp.raise_for_status()
        job = resp.json()
        state = job.get("state")
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:>3}s] state={state}")
        if state == "completed":
            return job
        if state == "failed":
            raise JobError(f"job failed: {job.get('error', 'unknown error')}")
        time.sleep(sleep_s)


def submit_job(
    broker: str,
    prompt: str,
    files: list[FileSpec],
    result_pubkey_b64: str,
    skill: str,
    spt_token: str = "",
    stripe_pi_id: str = "",
) -> dict[str, Any]:
    payload = {
        "client_req_id": f"e2e-file-{int(time.time())}-{os.getpid()}",
        "encrypted_skill": skill,
        "encrypted_data": prompt,
        "requester_sig": "0x",
        "result_pubkey": result_pubkey_b64,
        "input_files": [
            {
                "filename": f.filename,
                "content_type": f.content_type,
                "size_bytes": len(f.data),
            }
            for f in files
        ],
    }
    if spt_token:
        payload["shared_payment_token"] = spt_token
    if stripe_pi_id:
        payload["stripe_pi_id"] = stripe_pi_id
    resp = requests.post(f"{broker}/v1/jobs", json=payload, timeout=30)
    if resp.status_code == 402:
        challenge = resp.headers.get("WWW-Authenticate", "")
        raise JobError(
            "broker returned HTTP 402 Payment Required. Mint an SPT for this "
            f"challenge and rerun with --spt. WWW-Authenticate: {challenge}"
        )
    if resp.status_code != 202:
        raise JobError(f"POST /v1/jobs failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def upload_inputs(broker: str, job_id: str, headers: dict[str, str], files: list[FileSpec], upload: dict[str, Any]) -> None:
    urls = {entry["filename"]: entry["upload_url"] for entry in upload.get("files", [])}
    worker_pubkey = upload.get("encryption", {}).get("public_key", "")
    if not worker_pubkey:
        raise JobError("missing worker public key in input_upload")
    print(f"  worker pubkey: {worker_pubkey[:24]}...")
    print(f"  upload expires: {upload.get('expires_at', 'N/A')}")
    for f in files:
        url = urls.get(f.filename)
        if not url:
            raise JobError(f"missing upload URL for {f.filename}")
        ciphertext = encrypt_input_file(job_id, f.filename, f.data, worker_pubkey)
        expected = len(f.data) + 60
        if len(ciphertext) != expected:
            raise JobError(f"ciphertext length mismatch for {f.filename}: {len(ciphertext)} != {expected}")
        resp = requests.put(url, data=ciphertext, timeout=60)
        if resp.status_code != 200:
            raise JobError(f"PUT {f.filename} failed: {resp.status_code} {resp.text[:300]}")
        print(f"  uploaded {f.filename}: {len(f.data)} -> {len(ciphertext)} bytes")

    resp = requests.post(f"{broker}/v1/jobs/{job_id}/ready", headers=headers, timeout=30)
    if resp.status_code != 200:
        raise JobError(f"POST /ready failed: {resp.status_code} {resp.text[:300]}")
    print(f"  ready response: {json.dumps(resp.json(), indent=2)[:400]}")


def print_result(job: dict[str, Any], result_private: X25519PrivateKey) -> None:
    result = job.get("result") or {}
    print("\n=== RESULT SUMMARY ===")
    print(f"job_id: {job.get('job_id')}")
    print(f"state: {job.get('state')}")
    print(f"skill: {result.get('skill', 'N/A')}")
    print(f"model: {result.get('model', 'N/A')}")
    print(f"execution_mode: {result.get('execution_mode', 'N/A')}")
    print(f"duration_ms: {result.get('duration_ms', 'N/A')}")
    usage = result.get("usage", {})
    if isinstance(usage, dict):
        print(
            f"usage: {usage.get('prompt_tokens', '?')} prompt + "
            f"{usage.get('completion_tokens', '?')} completion"
        )

    print(f"stripe_status: {job.get('stripe_status', result.get('stripe_status', 'N/A'))}")
    if job.get("stripe_capture_amount") is not None:
        print(f"stripe_capture_amount: {job['stripe_capture_amount']} cents")
    if job.get("stripe_pi_amount_cents") is not None:
        print(f"stripe_pi_amount_cents: {job['stripe_pi_amount_cents']} cents")

    # New sandbox attestation block (NemoClaw / NemoClaw stub).
    sb = result.get("sandbox") or {}
    if isinstance(sb, dict) and sb:
        print("\n=== SANDBOX ATTESTATION ===")
        print(f"  name:            {sb.get('name', 'N/A')}")
        print(f"  attested:        {sb.get('attested')}")
        print(f"  network_policy:  {sb.get('network_policy', 'N/A')}")
        print(f"  inference_route: {sb.get('inference_route', 'N/A')}")
        if sb.get("error"):
            print(f"  error:           {sb['error']}")
        for k in (
            "nemoclaw_version",
            "nemoclaw_image",
            "nemoclaw_image_digest",
            "image_digest_sig",
        ):
            if sb.get(k):
                v = str(sb[k])
                print(f"  {k}: {v[:80]}{'...' if len(v) > 80 else ''}")
        print("--- end sandbox attestation ---")

    artifacts = result.get("artifacts") or {}
    if isinstance(artifacts, dict):
        print(f"artifact_count: {artifacts.get('count', 0)}")
        print(f"artifact_encryption: {artifacts.get('encryption', 'N/A')}")
        files = artifacts.get("files", [])
        for item in files:
            print(
                f"artifact: {item.get('filename')} size={item.get('size_bytes')} "
                f"encrypted_size={item.get('encrypted_size_bytes')} sha256={item.get('sha256')}"
            )
    else:
        print("artifact_count: 0")

    encrypted = result.get("result_encrypted")
    if encrypted:
        try:
            plaintext = decrypt_result_encrypted(encrypted, result_private)
            print("\n=== DECRYPTED RESULT ===")
            print(json.dumps(plaintext, indent=2, ensure_ascii=False)[:8000])
        except Exception as exc:
            print(f"\n[warn] unable to decrypt result_encrypted: {exc}")
            print(f"result_encrypted (base64, first 120): {encrypted[:120]}...")
    else:
        print("\n(no result_encrypted field present)")

    print("\n=== RAW JOB RESULT ===")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:8000])


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--broker", default=BROKER_DEFAULT)
    ap.add_argument("--region", default="eu-west-1")
    ap.add_argument("--spt", default=os.environ.get("STRIPE_SHARED_PAYMENT_TOKEN", ""), help="Stripe ACS Shared Payment Token (spt_...) returned by the agent/Link flow")
    ap.add_argument("--demo-spt", action="store_true", help="Mint a broker-stub demo SPT from /v1/demo/shared-payment-token and use it")
    ap.add_argument("--legacy-create-pi", action="store_true", help="Legacy test path: create a PaymentIntent directly with a Stripe secret instead of ACS/SPT")
    ap.add_argument("--stripe-secret-param", default="/verdantforged/broker/stripe-secret-key")
    ap.add_argument("--stripe-secret", default=os.environ.get("STRIPE_SECRET_KEY", ""), help="Legacy override; otherwise fetched from SSM when --legacy-create-pi is set.")
    ap.add_argument("--amount-cents", type=int, default=500)
    ap.add_argument("--currency", default="usd")
    ap.add_argument("--description", default="E2E file upload task")
    ap.add_argument("--skill", default="code-review")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--prompt-file", default="")
    ap.add_argument("--file", dest="files", action="append", default=[], help="Input file path (repeatable)")
    ap.add_argument("--wait-upload-seconds", type=int, default=3000)
    ap.add_argument("--wait-complete-seconds", type=int, default=6000)
    ap.add_argument("--save-result-key", default="", help="Optional path to save the generated result private/public keypair JSON")
    ap.add_argument("--no-save-result-key", action="store_true", help="Do not write the result keypair to disk")
    ap.add_argument("--artifacts-dir", default="./artifacts", help="Where to write decrypted artifact files (default: ./artifacts). Set to '' to skip download+decrypt.")
    ap.add_argument("--no-artifacts", action="store_true", help="Skip the artifact download+decrypt step (equivalent to --artifacts-dir='')")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    prompt = load_prompt(args)
    files = load_files(args.files)

    stripe_key = ""
    pi_id = ""
    if args.legacy_create_pi:
        if args.stripe_secret:
            stripe_key = args.stripe_secret
        else:
            stripe_key = get_stripe_secret(args.region, args.stripe_secret_param)

    result_private, result_public_b64 = generate_result_keypair()

    if args.demo_spt:
        spt_token, demo_payload = mint_demo_shared_payment_token(args.broker, args.amount_cents, args.currency, os.environ.get("STRIPE_NETWORK_ID", "demo_network"))
        args.spt = spt_token
    else:
        demo_payload = {}

    if args.save_result_key and not args.no_save_result_key:
        key_path = Path(args.save_result_key).expanduser().resolve()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_payload = {
            "result_public_b64": result_public_b64,
            "result_private_b64": base64.b64encode(
                result_private.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            ).decode(),
        }
        key_path.write_text(json.dumps(key_payload, indent=2))
        print(f"saved result keypair to {key_path}")

    print("=== Step 1: Payment credential ===")
    if args.legacy_create_pi:
        pi = create_payment_intent(stripe_key, args.amount_cents, args.currency, args.description)
        pi_id = pi["id"]
        print(f"  Legacy PaymentIntent: {pi_id}")
        print(f"  Status: {pi.get('status')}")
        print(f"  Amount: ${pi.get('amount', args.amount_cents) / 100:.2f}")
    elif args.demo_spt:
        print(f"  Demo Shared Payment Token: {args.spt[:20]}...")
        if demo_payload:
            print(f"  Demo route: {demo_payload.get('source', 'route')} / {demo_payload.get('mode', 'demo')}")
            print(f"  Challenge: {demo_payload.get('challenge', '')}")
    elif args.spt:
        print(f"  Shared Payment Token: {args.spt[:16]}...")
    else:
        print("  No SPT supplied; expecting broker to return HTTP 402 challenge")

    print("\n=== Step 2: Result keypair ===")
    print(f"  Result pubkey: {result_public_b64[:24]}...")

    print("\n=== Step 3: Input files ===")
    for idx, f in enumerate(files, start=1):
        print(f"  File {idx}: {f.filename} ({len(f.data)} bytes, {f.content_type})")

    print("\n=== Step 4: Submit file job ===")
    submitted = submit_job(args.broker, prompt, files, result_public_b64, args.skill, spt_token=args.spt, stripe_pi_id=pi_id)
    job_id = submitted["job_id"]
    token = submitted.get("job_access_token", "")
    if not token:
        raise JobError("broker did not return a job_access_token")
    headers = {"Authorization": f"Bearer {token}"}
    print(f"  Job ID: {job_id}")
    print(f"  Initial state: {submitted.get('state')}")
    print(f"  Token: {token[:20]}...")

    print("\n=== Step 5: Wait for awaiting_inputs ===")
    upload_job = None
    start = time.time()
    last_reported_state = None
    while time.time() - start < args.wait_upload_seconds:
        elapsed = int(time.time() - start)
        try:
            health = get_healthz(args.broker)
            print(f"  [{elapsed:>3}s] healthz {format_healthz(health)}")
        except Exception as exc:
            print(f"  [{elapsed:>3}s] healthz error={exc}")
            time.sleep(5)
            continue

        resp = requests.get(f"{args.broker}/v1/jobs/{job_id}", headers=headers, timeout=20)
        resp.raise_for_status()
        job = resp.json()
        state = job.get("state")
        if state != last_reported_state:
            print(f"  [{elapsed:>3}s] state={state}")
            last_reported_state = state
        worker_wait = job.get("worker_wait") or {}
        if worker_wait:
            print(f"  [{elapsed:>3}s] worker_wait status={worker_wait.get('worker_status')} detail={worker_wait.get('detail')} instance={worker_wait.get('worker_instance_id', '')}")
        if state == "awaiting_worker" and health.get("worker_status") not in ("offline", None):
            print("  job is still awaiting worker key publication / attestation binding")
        if state == "awaiting_inputs":
            upload_job = job
            break
        if state == "failed":
            raise JobError(f"job failed while waiting for upload: {job.get('error', 'unknown error')}")
        time.sleep(5)
    if not upload_job:
        raise JobError(
            f"never reached awaiting_inputs with a live worker after {args.wait_upload_seconds}s; "
            f"last_state={last_reported_state or 'unknown'}"
        )

    upload = upload_job.get("input_upload") or {}
    enc = upload.get("encryption", {})
    att = upload.get("attestation", {})
    if enc.get("report_data"):
        print(f"  report_data: {enc.get('report_data')[:32]}...")
    if att.get("snp_quote") or enc.get("snp_quote"):
        print("  SNP quote: present")
    elif att.get("tee_type") == "amd-sev-snp":
        print(f"  attestation: tee_type={att.get('tee_type')} measurement={str(att.get('measurement',''))[:20]}... report={str(att.get('report',''))[:20]}...")
    elif att.get("tee_type"):
        print(f"  attestation: tee_type={att.get('tee_type')} source={att.get('source','')}")
    else:
        print("  attestation: not present")

    # NemoClaw sandbox attestation (new path) is surfaced in the final job
    # result under result.sandbox — we can only print it after completion,
    # but flag its presence here so the operator knows to look for it.
    print("  (sandbox attestation will appear in Step 7 result)")

    print("\n=== Step 6: Encrypt and upload files ===")
    upload_inputs(args.broker, job_id, headers, files, upload)

    print("\n=== Step 7: Wait for completion ===")
    final_job = wait_for_completion(args.broker, job_id, headers, args.wait_complete_seconds)

    print_result(final_job, result_private)

    if not args.no_artifacts and args.artifacts_dir:
        print("\n=== Step 8: Download and decrypt artifacts ===")
        try:
            records = download_and_decrypt_artifacts(
                args.broker,
                job_id,
                headers,
                result_private,
                Path(args.artifacts_dir).expanduser().resolve(),
            )
            ok = sum(1 for r in records if r.get("ok"))
            print(f"\nartifact summary: {ok}/{len(records)} ok")
        except Exception as exc:
            print(f"\n[warn] artifact download/decrypt failed: {exc}")
    else:
        print("\n=== Step 8: Skipped (--no-artifacts) ===")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JobError as exc:
        eprint(f"error: {exc}")
        raise SystemExit(1)
