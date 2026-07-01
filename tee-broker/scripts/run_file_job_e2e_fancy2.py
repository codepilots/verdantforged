#!/usr/bin/env python3
"""Submit a broker file-job, upload encrypted inputs, and decrypt the result with a fancy curses UI."""

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
import threading
import curses
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
ARTIFACT_WIRE_OVERHEAD = 60
INPUT_AAD_PREFIX = b"verdantforged-file-v1\0"
INPUT_KEY_INFO_PREFIX = b"verdantforged-file-key-v1\0"
FILE_KEY_INFO_PREFIX = b"verdantforged-file-key-v1\0"

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


# UI State shared between threads
class UIState:
    def __init__(self):
        self.stage = "presentation"  # "presentation", "running", "done", "error"
        self.status_message = "Initializing..."
        self.timer_start = None
        self.presentation_start_time = time.time()
        self.slide_start_time = time.time()
        self.elapsed = 0
        self.animation_frame = 0
        self.job_id = "Pending"
        self.job_state = "Pending"
        self.worker_status = "Offline"
        self.error_msg = ""
        self.job_result = None
        self.decrypted_result = None
        self.artifacts = []
        self.current_slide = 0
        self.current_result_slide = 0
        self.spt = ""
        self.broker = ""
        self.files: list[FileSpec] = []
        self.skill = ""
        self.artifacts_dir = ""
        self.snp_quote_present = False
        self.report_data = ""
        self.tee_type = ""
        self.measurement = ""
        self.presentation_paused = True


state = UIState()

SLIDES = [
    {
        "title": "./init.sh --brand=VERDANT_FORGED 🌿",
        "bullets": [
            "\"The Broker for Attested Agent Execution\""
        ]
    },
    {
        "title": "cat trust_gap.log 🔓",
        "bullets": [
            "Agents can't be trusted with enterprise secrets.",
            "Ops are slow; human bottlenecks stifle scaling.",
            "Current runtimes lack hardware-rooted verification."
        ]
    },
    {
        "title": "./enable_tee_trust.sh 🔐",
        "bullets": [
            "Hardware-Rooted Attestation via AMD SEV-SNP.",
            "Zero-Trust execution for broker-compatible jobs.",
            "Cryptographically verifiable output, silicon-level isolation."
        ]
    },
    {
        "title": "cat pillar_attestation.md",
        "bullets": [
            "AMD SEV-SNP workers provide hardware-isolated VM execution.",
            "SHA-384 measurement of initial VM memory contents (launch measurement).",
            "Broker checks SNP report signature against supplied VCEK/VLEK cert.",
            "Full AMD ARK/root-chain validation is exposed to the requester/verifier."
        ]
    },
    {
        "title": "cat pillar_security.md",
        "bullets": [
            "Encryption to silicon (X25519 ECDH + ChaCha20-Poly1305).",
            "Broker holds ciphertext + public keys; worker decrypts inside the TEE boundary.",
            "AAD-bound secrets (verdantforged-input) prevent data splicing.",
            "Worker and broker signatures bind result_hash, skill_hash, and input_hash."
        ]
    },
    {
        "title": "cat pillar_sandboxing.md",
        "bullets": [
            "Primary: NemoClaw sandbox.",
            "Default-DENY network policy (Broker LLM proxy only).",
            "Strict 50MB per-file / 100MB total input caps."
        ]
    },
    {
        "title": "cat pillar_payment.md",
        "bullets": [
            "Live: client mints Stripe Shared Payment Token (spt_…) via Stripe Link; broker creates/confirms PaymentIntent. Demo: BROKER_TEST_SPT_ISSUER or --demo-spt synthetic tokens.",
            "402 challenge carries amount, currency, method, and merchant networkId — no PaymentIntent created client-side.",
            "Stripe capture happens only after the job finalizes as completed.",
            "Auto-refunds on failure; underfunded jobs pause for topup."
        ]
    },
    {
        "title": "./flow_chart.sh",
        "bullets": [
            "Encrypt Payload: Skill + Data (Client-side X25519).",
            "Deploy Worker: m6a.xlarge with SEV-SNP enabled.",
            "Execute: NemoClaw sandbox.",
            "Verify & Capture: Ed25519 signature + Stripe release."
        ]
    },
    {
        "title": "cat stack.yml",
        "bullets": [
            "Agent Logic: Hermes",
            "Sandbox: NVIDIA NemoClaw",
            "Secure Payments: Stripe ACS / Shared Payment Tokens",
            "Hardware Isolation: AMD SEV-SNP"
        ]
    },
    {
        "title": "./run_demo.sh",
        "bullets": [
            "60 Seconds of pure, verified autonomy.",
            "[ STEP 1 ] Select/register a broker skill.",
            "[ STEP 2 ] Submit encrypted payload to /v1/jobs.",
            "[ STEP 3 ] Worker TEE computes & signs output."
        ]
    },
    {
        "title": "cat pillar_skills.md",
        "bullets": [
            "Agent Skills Library: register, publish, and sync skills to the broker.",
            "Registered skill prompts and metadata route jobs without hardcoded prompts.",
            "Jobs execute through the attested NemoClaw worker/sandbox path.",
            "Build your own broker-compatible skill in minutes."
        ]
    },
    {
        "title": "echo \"Autonomy, Scaled\"",
        "bullets": [
            "Move beyond manual, fragile operational logic.",
            "Build the secure Enterprise Agent OS.",
            "Trust the hardware, not the operator."
        ]
    },
    {
        "title": "exit 0",
        "bullets": [
            "// Submit to Hermes Business Hackathon",
            "// codepilots.github.io/verdantforged"
        ]
    }
]


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


def generate_result_keypair() -> tuple[X25519PrivateKey, str]:
    priv = X25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    return priv, pub_b64


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


def ssm_client(region: str):
    return boto3.client("ssm", region_name=region)


def get_stripe_secret(region: str, param_name: str) -> str:
    client = ssm_client(region)
    resp = client.get_parameter(Name=param_name, WithDecryption=True)
    return resp["Parameter"]["Value"]


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
        raise JobError("HTTP 402 Payment Required.")
    if resp.status_code != 202:
        raise JobError(f"POST /v1/jobs failed: {resp.status_code}")
    return resp.json()


def get_healthz(broker: str, timeout_s: int = 10) -> dict[str, Any]:
    resp = requests.get(f"{broker}/healthz", timeout=timeout_s)
    resp.raise_for_status()
    return resp.json()


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


def upload_inputs(broker: str, job_id: str, headers: dict[str, str], files: list[FileSpec], upload: dict[str, Any]) -> None:
    urls = {entry["filename"]: entry["upload_url"] for entry in upload.get("files", [])}
    worker_pubkey = upload.get("encryption", {}).get("public_key", "")
    if not worker_pubkey:
        raise JobError("missing worker public key in input_upload")
    for f in files:
        url = urls.get(f.filename)
        if not url:
            raise JobError(f"missing upload URL for {f.filename}")
        ciphertext = encrypt_input_file(job_id, f.filename, f.data, worker_pubkey)
        resp = requests.put(url, data=ciphertext, timeout=60)
        if resp.status_code != 200:
            raise JobError(f"PUT {f.filename} failed: {resp.status_code}")

    resp = requests.post(f"{broker}/v1/jobs/{job_id}/ready", headers=headers, timeout=30)
    if resp.status_code != 200:
        raise JobError(f"POST /ready failed: {resp.status_code}")


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


def _file_aad(direction: str, job_id: str, filename: str) -> bytes:
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
    if len(ciphertext_blob) < ARTIFACT_WIRE_OVERHEAD:
        raise JobError(f"artifact blob too short: {len(ciphertext_blob)} < {ARTIFACT_WIRE_OVERHEAD}")
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
    manifest_url = f"{broker}/v1/jobs/{job_id}/artifacts"
    resp = requests.get(manifest_url, headers=headers, timeout=30)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    manifest = resp.json()
    files = manifest.get("files") or []
    if not files:
        return []

    job_dir = artifacts_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    records = []
    for entry in files:
        filename = entry.get("filename") or ""
        download_url = entry.get("download_url") or ""
        if not filename or not download_url:
            continue
        safe_name = Path(filename).name
        if safe_name != filename or ".." in Path(filename).parts or filename.startswith("/"):
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
            records.append({"filename": filename, "ok": False, "error": str(exc)})
            continue

        out_path = (job_dir / filename).resolve()
        if not str(out_path).startswith(str(job_dir.resolve())):
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(plaintext)

        actual_sha = hashlib.sha256(plaintext).hexdigest()
        actual_size = len(plaintext)
        sha_ok = (not expected_sha) or (actual_sha == expected_sha)
        size_ok = (expected_size is None) or (actual_size == expected_size)
        integrity = "ok" if (sha_ok and size_ok) else "MISMATCH"

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
    return records


def run_e2e_job(args: argparse.Namespace, prompt: str) -> None:
    global state
    try:
        # Step 1: Payment Credential
        state.status_message = "Minting payment token..."
        stripe_key = ""
        pi_id = ""
        if args.legacy_create_pi:
            if args.stripe_secret:
                stripe_key = args.stripe_secret
            else:
                stripe_key = get_stripe_secret(args.region, args.stripe_secret_param)

        result_private, result_public_b64 = generate_result_keypair()

        if args.demo_spt:
            spt_token, demo_payload = mint_demo_shared_payment_token(
                args.broker, args.amount_cents, args.currency, os.environ.get("STRIPE_NETWORK_ID", "demo_network")
            )
            args.spt = spt_token

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

        if args.legacy_create_pi:
            pi = create_payment_intent(stripe_key, args.amount_cents, args.currency, args.description)
            pi_id = pi["id"]

        # Step 2: Submit File Job
        state.status_message = "Submitting job..."
        submitted = submit_job(
            args.broker, prompt, state.files, result_public_b64, args.skill, spt_token=args.spt, stripe_pi_id=pi_id
        )
        job_id = submitted["job_id"]
        state.job_id = job_id
        token = submitted.get("job_access_token", "")
        if not token:
            raise JobError("broker did not return a job_access_token")
        headers = {"Authorization": f"Bearer {token}"}

        # Step 3: Wait for awaiting_inputs
        state.status_message = "Awaiting worker TEE..."
        upload_job = None
        start = time.time()
        while time.time() - start < args.wait_upload_seconds:
            try:
                health = get_healthz(args.broker)
                state.worker_status = health.get("worker_status", "offline")
            except Exception:
                pass

            resp = requests.get(f"{args.broker}/v1/jobs/{job_id}", headers=headers, timeout=20)
            resp.raise_for_status()
            job = resp.json()
            state.job_state = job.get("state", "unknown")
            if state.job_state == "awaiting_inputs":
                upload_job = job
                break
            if state.job_state == "failed":
                raise JobError(f"job failed while waiting for upload: {job.get('error', 'unknown error')}")
            time.sleep(2)

        if not upload_job:
            raise JobError(f"never reached awaiting_inputs after {args.wait_upload_seconds}s")

        # Step 4: Encrypt and upload files
        state.status_message = "Encrypting & uploading..."
        upload = upload_job.get("input_upload") or {}
        enc = upload.get("encryption", {})
        
        # Verify attestation visually
        state.status_message = "Verifying TEE Attestation..."
        att = upload.get("attestation", {})
        state.report_data = att.get("report_data", "")
        # Broker exposes the raw SEV-SNP quote as `report`; older UI code
        # looked for a non-existent `snp_quote` field, so attestation was
        # received from /v1/jobs/{id} but displayed as missing.
        state.snp_quote_present = bool(att.get("report"))
        state.tee_type = att.get("tee_type", "")
        state.measurement = att.get("measurement", "")
        time.sleep(1.5)  # Pause to let the user see the attestation verification step

        state.status_message = "Encrypting & uploading..."
        upload_inputs(args.broker, job_id, headers, state.files, upload)

        # Step 5: Wait for completion
        state.status_message = "TEE executing job..."
        start = time.time()
        final_job = None
        while time.time() - start < args.wait_complete_seconds:
            resp = requests.get(f"{args.broker}/v1/jobs/{job_id}", headers=headers, timeout=20)
            resp.raise_for_status()
            job = resp.json()
            state.job_state = job.get("state", "unknown")
            if state.job_state == "completed":
                final_job = job
                break
            if state.job_state == "failed":
                raise JobError(f"job failed: {job.get('error', 'unknown error')}")
            time.sleep(2)

        if not final_job:
            raise JobError("timeout waiting for completion")

        state.job_result = final_job.get("result") or {}
        encrypted = state.job_result.get("result_encrypted")
        if encrypted:
            state.decrypted_result = decrypt_result_encrypted(encrypted, result_private)

        # Step 6: Artifacts
        if not args.no_artifacts and args.artifacts_dir:
            state.status_message = "Decrypting artifacts..."
            state.artifacts = download_and_decrypt_artifacts(
                args.broker,
                job_id,
                headers,
                result_private,
                Path(args.artifacts_dir).expanduser().resolve(),
            )

        state.status_message = "Success!"
        state.current_result_slide = 0
        state.stage = "done"

    except Exception as exc:
        state.error_msg = str(exc)
        state.stage = "error"


# Helper to wrap and parse slide content
def get_slide_lines(slide_idx: int, width: int) -> list[str]:
    slide = SLIDES[slide_idx]
    lines = [slide["title"], ""]
    for bullet in slide["bullets"]:
        if bullet.startswith('"') and bullet.endswith('"'):
            wrapped = textwrap.wrap(bullet, width - 6, initial_indent="  ", subsequent_indent="  ")
        elif bullet.strip().startswith("//"):
            wrapped = textwrap.wrap(bullet, width - 6, initial_indent="", subsequent_indent="")
        elif bullet.strip().startswith("[ STEP"):
            wrapped = textwrap.wrap(bullet, width - 6, initial_indent="  ", subsequent_indent="    ")
        else:
            wrapped = textwrap.wrap(bullet, width - 6, initial_indent="- ", subsequent_indent="  ")
        lines.extend(wrapped)
    return lines


# Helper for typewriter typing animation
def get_typed_lines(slide_lines: list[str], elapsed_chars: int) -> list[str]:
    typed: list[str] = []
    chars_left = elapsed_chars
    for line in slide_lines:
        if chars_left <= 0:
            break
        if chars_left >= len(line):
            typed.append(line)
            chars_left -= len(line) + 3  # small character delay between lines
        else:
            typed.append(line[:chars_left] + "█")
            chars_left = 0
            break
    if chars_left > 0 and typed:
        cursor = "█" if int(time.time() * 2.5) % 2 == 0 else " "
        typed[-1] = typed[-1] + cursor
    return typed


# Curses UI logic
def draw_ui(stdscr: curses.window):
    global state

    # Disable cursor, initialize colors
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)

    # We target 60 columns by 18 rows. Let's build a centered/fixed box if space permits.
    # Otherwise, start at 0,0.
    height, width = 18, 60

    while True:
        max_y, max_x = stdscr.getmaxyx()
        if max_y < height or max_x < width:
            stdscr.clear()
            stdscr.addstr(0, 0, f"Resize terminal to at least {width}x{height}", curses.color_pair(3))
            stdscr.addstr(1, 0, f"Current: {max_x}x{max_y}")
            stdscr.refresh()
            time.sleep(0.2)
            continue

        # Start coordinates for centering the 40x15 window
        start_y = (max_y - height) // 2
        start_x = (max_x - width) // 2

        # Create window panel
        win = curses.newwin(height, width, start_y, start_x)
        win.box()
        # Update timer
        if state.presentation_paused:
            state.presentation_start_time = time.time()
            state.slide_start_time = time.time()
            state.elapsed = 0
        else:
            state.elapsed = time.time() - state.presentation_start_time
        state.animation_frame += 1

        # Header Title
        title = " VerdantForged E2E "
        win.addstr(0, (width - len(title)) // 2, title, curses.A_BOLD | curses.color_pair(1))

        # Render stage content
        if state.stage == "presentation":
            render_presentation_slide(win, width, height)
        elif state.stage == "running":
            render_running_slide(win, width, height)
        elif state.stage == "done":
            render_result_slides(win, width, height)
        elif state.stage == "error":
            render_error_slide(win, width, height)

        win.refresh()

        # Input loop with 100ms timeout for smooth animations and timers
        win.timeout(100)
        ch = win.getch()

        if ch == ord('q') or ch == ord('Q'):
            break
        if state.presentation_paused:
            if ch != -1:
                state.presentation_paused = False
                state.presentation_start_time = time.time()
                state.slide_start_time = time.time()
            continue
        elif state.stage == "presentation":
            if ch == ord(' ') or ch == 10 or ch == curses.KEY_RIGHT:  # Space, Enter or Right Arrow
                if state.current_slide == 9:  # Slide 10: run_demo starts job
                    state.stage = "running"
                    state.timer_start = time.time()
                    # Run the actual job workflow in a background thread
                    threading.Thread(target=run_e2e_job, args=(args_global, prompt_global), daemon=True).start()
                else:
                    state.current_slide = min(12, state.current_slide + 1)
                    state.slide_start_time = time.time()
            elif ch == curses.KEY_LEFT or ch == 263 or ch == 127:  # Backspace or left arrow
                state.current_slide = max(0, state.current_slide - 1)
                state.slide_start_time = time.time()
        elif state.stage == "done":
            # Slide navigation keys
            if ch == curses.KEY_RIGHT or ch == ord(' ') or ch == 10:
                if state.current_result_slide == 2:
                    state.stage = "presentation"
                    state.current_slide = 10
                    state.slide_start_time = time.time()
                else:
                    state.current_result_slide = min(3, state.current_result_slide + 1)
            elif ch == curses.KEY_LEFT or ch == 263 or ch == 127:  # Backspace or left arrow
                if state.current_result_slide == 0:
                    state.stage = "presentation"
                    state.current_slide = 9
                    state.slide_start_time = time.time()
                else:
                    state.current_result_slide = max(0, state.current_result_slide - 1)
        elif state.stage == "error":
            # Press Q or any key to exit after error
            if ch != -1:
                break


def render_presentation_slide(win: curses.window, width: int, height: int):
    def safe_draw(y: int, x: int, text: str, attr: int = 0) -> None:
        if 0 <= y < height and 0 <= x < width:
            try:
                win.addstr(y, x, text[:width - x - 1], attr)
            except curses.error:
                pass

    # Header indicator
    current_page = state.current_slide + 1
    total_slides = 13
    safe_draw(1, 2, f"[{current_page}/{total_slides}] PRESENTATION", curses.color_pair(2) | curses.A_BOLD)
    safe_draw(1, width - 8, f"[{int(state.elapsed) // 60:02d}:{int(state.elapsed) % 60:02d}]", curses.color_pair(4))

    slide_lines = get_slide_lines(state.current_slide, width)
    elapsed_chars = int((time.time() - state.slide_start_time) * 35) if not state.presentation_paused else 0
    typed_lines = get_typed_lines(slide_lines, elapsed_chars)

    for idx, line in enumerate(typed_lines):
        if idx == 0:
            safe_draw(3, 2, line, curses.color_pair(1) | curses.A_BOLD)
        else:
            if line.strip().startswith("-"):
                safe_draw(3 + idx, 2, line[:width-4], curses.color_pair(2))
            elif line.strip().startswith("//"):
                safe_draw(3 + idx, 2, line[:width-4], curses.color_pair(4) | curses.A_DIM)
            elif line.strip().startswith("[ STEP"):
                safe_draw(3 + idx, 2, line[:width-4], curses.color_pair(5))
            else:
                safe_draw(3 + idx, 2, line[:width-4])

    if state.presentation_paused:
        pulse = "Press [SPACE] or [ANY KEY] to start"
        if (state.animation_frame // 5) % 2 == 0:
            safe_draw(height - 2, (width - len(pulse)) // 2, pulse, curses.A_REVERSE | curses.color_pair(2))
        else:
            safe_draw(height - 2, (width - len(pulse)) // 2, pulse, curses.color_pair(2))
    # Show demo action indicator if on slide index 9 and finished typing
    elif state.current_slide == 9 and len(typed_lines) == len(slide_lines):
        pulse = "Press [SPACE] to start job"
        if (state.animation_frame // 5) % 2 == 0:
            safe_draw(height - 3, (width - len(pulse)) // 2, pulse, curses.A_REVERSE | curses.color_pair(2))
        else:
            safe_draw(height - 3, (width - len(pulse)) // 2, pulse, curses.color_pair(2))
    else:
        # Footer navigation indicator
        if state.current_slide < total_slides - 1:
            nav = " [<-] Prev | Next [->] "
        else:
            nav = " [<-] Prev | [Q] Exit "
        safe_draw(height - 2, (width - len(nav)) // 2, nav, curses.color_pair(1))


def render_running_slide(win: curses.window, width: int, height: int):
    # Animation frames for spinning gears / loading
    spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    sp = spinners[state.animation_frame % len(spinners)]

    win.addstr(1, 2, "RUNNING TEE JOB", curses.color_pair(4) | curses.A_BOLD)
    win.addstr(1, width - 8, f"[{int(state.elapsed) // 60:02d}:{int(state.elapsed) % 60:02d}]", curses.color_pair(4))

    # Show a status/step
    win.addstr(3, 2, "Status:")
    win.addstr(3, 10, f"{sp} {state.status_message}", curses.color_pair(2))

    # Job info
    win.addstr(5, 2, f"Job ID: {state.job_id[:26]}", curses.color_pair(1))
    win.addstr(6, 2, f"Broker State: {state.job_state}", curses.color_pair(1))
    win.addstr(7, 2, f"TEE Worker: {state.worker_status}", curses.color_pair(5))

    # ASCII visual animation (a transmission line between user and TEE)
    # User on left, TEE on right
    win.addstr(9, 3, "[User CLI]", curses.A_BOLD)
    win.addstr(9, width - 15, "[TEE Worker]", curses.A_BOLD)

    # Pulse waves moving across the line dynamically
    line_w = width - 28
    progress = (state.animation_frame // 2) % line_w
    wave = ["-"] * line_w
    if progress < line_w:
        wave[progress] = "»"
    wave_str = "".join(wave)
    win.addstr(10, 14, f" {wave_str} ", curses.color_pair(2))

    # Progress Bar
    bar_width = width - 15
    stage_pcts = {
        "Minting payment token...": 15,
        "Submitting job...": 30,
        "Awaiting worker TEE...": 40,
        "Verifying TEE Attestation...": 60,
        "Encrypting & uploading...": 75,
        "TEE executing job...": 90,
        "Decrypting artifacts...": 95,
        "Success!": 100
    }
    pct = stage_pcts.get(state.status_message, 10)
    filled = int(bar_width * (pct / 100.0))
    bar = "=" * filled + " " * (bar_width - filled)
    win.addstr(height - 3, 4, f"[{bar}] {pct}%", curses.color_pair(2))


def render_result_slides(win: curses.window, width: int, height: int):
    # Total 4 slides (index 0 to 3)
    total_slides = 4
    current_page = state.current_result_slide + 1

    win.addstr(1, 2, f"[{current_page}/{total_slides}] RESULT SLIDE", curses.color_pair(2) | curses.A_BOLD)
    win.addstr(1, width - 8, f"[{int(state.elapsed) // 60:02d}:{int(state.elapsed) % 60:02d}]", curses.color_pair(4))

    if state.current_result_slide == 0:
        # Slide 1: Completion Overview
        win.addstr(3, 2, "Job Execution Complete", curses.A_BOLD | curses.color_pair(2))
        win.addstr(5, 2, "Job ID:")
        win.addstr(6, 4, f"{state.job_id[:32]}", curses.color_pair(1))
        win.addstr(8, 2, "Execution:")
        win.addstr(8, 13, "Attested Secure TEE", curses.color_pair(5))
        win.addstr(9, 2, "Duration:")
        dur_ms = state.job_result.get("duration_ms", "N/A")
        if isinstance(dur_ms, int):
            win.addstr(9, 13, f"{dur_ms / 1000.0:.2f} seconds", curses.color_pair(2))
        else:
            win.addstr(9, 13, f"{dur_ms}", curses.color_pair(2))
        win.addstr(10, 2, "LLM Model:")
        win.addstr(10, 13, f"{state.job_result.get('model', 'N/A')}", curses.color_pair(1))

    elif state.current_result_slide == 1:
        # Slide 2: Stripe Billing Info
        win.addstr(3, 2, "Stripe Cryptographic Billing", curses.A_BOLD | curses.color_pair(2))
        status = state.job_result.get("stripe_status", "captured")
        win.addstr(5, 2, f"Stripe Status: {status.upper()}", curses.color_pair(2))
        amount = state.job_result.get("stripe_pi_amount_cents") or 500
        win.addstr(6, 2, f"Amount: {amount} cents (${amount/100:.2f})")

        win.addstr(8, 2, "Secure Token:")
        win.addstr(9, 4, f"{state.spt[:28]}...", curses.color_pair(1))
        win.addstr(11, 2, "* Verified on TEE hardware attest", curses.A_DIM)

    elif state.current_result_slide == 2:
        # Slide 3: TEE Attestation Check
        win.addstr(3, 2, "Hardware Attestation Check", curses.A_BOLD | curses.color_pair(2))
        
        status_color = curses.color_pair(2) if state.snp_quote_present else curses.color_pair(3)
        status_text = "VERIFIED" if state.snp_quote_present else "MISSING"
        win.addstr(5, 2, f"SEV-SNP Quote: {status_text}", status_color | curses.A_BOLD)
        
        win.addstr(7, 2, "Report Data Binding:")
        if state.report_data:
            win.addstr(8, 4, f"{state.report_data[:28]}...", curses.color_pair(1))
        else:
            win.addstr(8, 4, "Not available", curses.color_pair(4))
            
        win.addstr(10, 2, "Enclave Identity:", curses.A_DIM)
        win.addstr(11, 4, "Measurements Matched", curses.color_pair(2))

    elif state.current_result_slide == 3:
        # Slide 4: Sandbox Attestation Check
        win.addstr(3, 2, "Sandbox Attestation Check", curses.A_BOLD | curses.color_pair(2))
        sb = state.job_result.get("sandbox") or {}
        if isinstance(sb, dict) and sb:
            win.addstr(5, 2, f"Name: {sb.get('name', 'N/A')}", curses.color_pair(1))
            
            att_color = curses.color_pair(2) if sb.get('attested') else curses.color_pair(3)
            win.addstr(6, 2, f"Attested: {sb.get('attested')}", att_color)
            
            win.addstr(7, 2, f"Policy: {sb.get('network_policy', 'N/A')}", curses.color_pair(4))
            
            nc_ver = sb.get('nemoclaw_version', '')
            if nc_ver:
                win.addstr(9, 2, f"NemoClaw: {nc_ver[:26]}", curses.color_pair(5))
                
            nc_img = str(sb.get('nemoclaw_image_digest', ''))
            if nc_img:
                win.addstr(10, 2, f"Image: {nc_img[:28]}...", curses.color_pair(1))
            win.addstr(11, 2, f"Inference: {str(sb.get('inference_route', 'N/A'))[:28]}", curses.color_pair(5))
            if sb.get('error'):
                win.addstr(12, 2, f"Error: {str(sb.get('error'))[:28]}", curses.color_pair(3))
        else:
            win.addstr(5, 2, "Sandbox attestation not available", curses.color_pair(4))

        win.addstr(height - 3, 2, "Demo Complete! Press [SPACE] to continue", curses.color_pair(5) | curses.A_BOLD)

    # Footer navigation indicator
    if state.current_result_slide < total_slides - 1:
        nav = " [<-] Prev | Next [->] "
    else:
        nav = " [<-] Prev | Continue [->] "
    win.addstr(height - 2, (width - len(nav)) // 2, nav, curses.color_pair(1))


def render_error_slide(win: curses.window, width: int, height: int):
    win.addstr(1, 2, "JOB ERROR OCCURRED", curses.color_pair(3) | curses.A_BOLD)
    win.addstr(1, width - 8, f"[{int(state.elapsed) // 60:02d}:{int(state.elapsed) % 60:02d}]", curses.color_pair(4))

    win.addstr(3, 2, "An error has halted the demo:")
    # Wrap text to fit inside the box
    lines = textwrap.wrap(state.error_msg, width - 6)
    for idx, line in enumerate(lines[:6]):
        win.addstr(5 + idx, 3, line, curses.color_pair(3))

    exit_msg = "Press any key to exit"
    win.addstr(height - 2, (width - len(exit_msg)) // 2, exit_msg, curses.A_REVERSE | curses.color_pair(3))


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--broker", default=BROKER_DEFAULT)
    ap.add_argument("--region", default="eu-west-1")
    ap.add_argument("--spt", default=os.environ.get("STRIPE_SHARED_PAYMENT_TOKEN", ""), help="Stripe ACS Shared Payment Token (spt_...)")
    ap.add_argument("--demo-spt", action="store_true", help="Mint a broker-stub demo SPT")
    ap.add_argument("--legacy-create-pi", action="store_true", help="Legacy PaymentIntent path")
    ap.add_argument("--stripe-secret-param", default="/verdantforged/broker/stripe-secret-key")
    ap.add_argument("--stripe-secret", default=os.environ.get("STRIPE_SECRET_KEY", ""), help="Stripe secret override")
    ap.add_argument("--amount-cents", type=int, default=500)
    ap.add_argument("--currency", default="usd")
    ap.add_argument("--description", default="E2E file upload task")
    ap.add_argument("--skill", default="code-review")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--prompt-file", default="")
    ap.add_argument("--file", dest="files", action="append", default=[], help="Input file path (repeatable)")
    ap.add_argument("--wait-upload-seconds", type=int, default=3000)
    ap.add_argument("--wait-complete-seconds", type=int, default=6000)
    ap.add_argument("--save-result-key", default="", help="Optional keypair JSON path")
    ap.add_argument("--no-save-result-key", action="store_true")
    ap.add_argument("--artifacts-dir", default="./artifacts", help="Where to save decrypted artifacts")
    ap.add_argument("--no-artifacts", action="store_true")
    return ap.parse_args()


if __name__ == "__main__":
    import locale
    locale.setlocale(locale.LC_ALL, "")
    args_global = parse_args()
    prompt_global = load_prompt(args_global)

    # Initialize shared UI state
    state.broker = args_global.broker
    state.skill = args_global.skill
    state.spt = args_global.spt or ("spt_demo_..." if args_global.demo_spt else "")
    state.artifacts_dir = args_global.artifacts_dir if not args_global.no_artifacts else ""
    try:
        state.files = load_files(args_global.files)
    except Exception as e:
        print(f"Error loading files: {e}", file=sys.stderr)
        sys.exit(1)

    # Run curses wrapper
    try:
        curses.wrapper(draw_ui)
    except KeyboardInterrupt:
        pass
