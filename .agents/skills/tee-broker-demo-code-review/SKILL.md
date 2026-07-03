---
name: tee-broker-demo-code-review
version: 0.2.0
description: >
  Use when demonstrating the tee-broker-code-review skill to a human. Creates sample code,
  submits it to the VerdantForged broker in demo mode, polls for completion, retrieves
  artifacts, and presents results to the user.
category: tee-broker
tags: [tee-broker, demo, code-review, submit-job, artifacts]
metadata:
  hermes:
    related_skills: [tee-broker-submit-job, tee-broker-code-review]
    changelog:
      - 0.1.0 (2026-07-03) — initial release.
      - 0.2.0 (2026-07-03) — lock primary path to upstream e2e client; tighten file-job
        contract, artifact contract, polling contract, and pitfalls.
---

# TEE Broker Demo: Code Review

Demonstrate `tee-broker-code-review` end-to-end against the VerdantForged broker.
This skill uses **demo mode only**; no Stripe client keys are required.

## Inputs

Offer once, then reuse:

- `target_language`: `python`, `javascript`, or `typescript` (default `python`)
- `review_focus`: `security` or `both` (default `security`)
- `persona`: `junior`, `mid`, or `senior` (default `mid`)

If unsure, use `python`, `security`, `mid`.

## Step 1: Create flawed sample code

Write an intentionally flawed snippet to disk. Use the exact Python sample below;
it produced findings in the mid-2026 broker without triggering parse errors:

```python
from decimal import Decimal


def calculate_order_total(items, discount="0"):
    try:
        total = sum(Decimal(str(item['price'])) * Decimal(item['qty']) for item in items)
        return total - (total * Decimal(discount)).quantize(Decimal("0.01"))
    except:
        pass


pwd = "super secret"
api_key = "AKIAIO...MPLE"
cart = [
    {"price": "19.99", "qty": 2},
    {"price": "5.50", "qty": 3},
]
print(calculate_order_total(cart, "0.1"))
long_line = 'x' * 150
```

For JS/TS, use an equivalent calculator with loose equality and no input validation.

Completion criterion: file exists at a known path and the human can read it.

## Step 2: Submit the job in demo mode

### MUST use `run_file_job_e2e.py` first

The local upstream file-job e2e client at `/tmp/verdantforged/tee-broker/scripts/run_file_job_e2e.py`
already implements the broker's exact flow: mint SPT, gen X25519 keypair, POST `/v1/jobs`,
upload encrypted inputs, POST `/ready`, poll, fetch + decrypt artifacts.

Use it before any hand-rolled client. In a fresh environment, install deps first:

```bash
python3 -m pip install --quiet requests boto3
```

Recommended command:

```bash
python3 /tmp/verdantforged/tee-broker/scripts/run_file_job_e2e.py \
  --demo-spt \
  --skill code-review \
  --file <target.py> \
  --save-result-key <key_path.json> \
  --artifacts-dir <output_dir> \
  --wait-upload-seconds 3000 \
  --wait-complete-seconds 6000
```

Completion criterion: submit response is valid JSON with `job_id` and `job_access_token`.

### MUST NOT substitute a thin wrapper instead of the upstream client

Only write a fallback client if the upstream script genuinely cannot run in the
current environment. If you do write a fallback, replicate every requirement below
exactly.

## File-job contract

When submitting a `code-review` file job, the after-mint POST `/v1/jobs` body must
contain these exact fields:

- `client_req_id`: machine-readable unique string
- `encrypted_skill`: `"code-review"`
- `encrypted_data`: a concrete review prompt that references the file
- `requester_sig`: `"0x"`
- `result_pubkey`: base64-encoded raw X25519 public key from a freshly generated keypair
- `input_files`: list of at least one entry with `filename`, `content_type`, `size_bytes`
- `shared_payment_token`: the demo SPT from `/v1/demo/shared-payment-token`

The broker will return an `input_upload` block containing per-file `upload_url`s and
an `encryption.public_key` that agents MUST use for input encryption.

## Input encryption contract

Before uploading, encrypt each input file as follows:

- generate a fresh X25519 ephemeral keypair per file
- derive shared secret: `ephemeral_private.exchange(worker_public_bytes_32)`
- AAD: `b"verdantforged-file-v1\x00input\x00<job_id>\x00<filename>"`
- key: `HKDF-SHA256(shared, salt=None, info=b"verdantforged-file-key-v1\x00" + AAD)`
- nonce: 12 random bytes
- ciphertext: `ChaCha20Poly1305(key).encrypt(nonce, plaintext, AAD)`
- wire blob: `ephemeral_pub_32 || nonce_12 || ciphertext_with_16byte_tag`
- expected ciphertext length: `len(plaintext) + 60`

CRITICAL: reuse the **same nonce** for the wire blob and the ChaCha20-Poly1305 call.
Using two different random nonces causes worker-side decryption failure and stalls
the job at `awaiting_inputs`.

## State machine and polling contract

After submission, expect one of these paths:

1. `awaiting_worker` → `awaiting_inputs` → `queued` → `running` → `completed`
2. `awaiting_worker` → `awaiting_inputs` → `running` → `failed`
3. `awaiting_worker` → awaiting_inputs stall if worker is offline

Concrete polling rules:

- poll `GET /v1/jobs/{job_id}` with `Authorization: Bearer {job_access_token}`
- do **not** poll more than once per 8s
- back off up to 30s between attempts
- retry up to 120 times; do not treat early completion as deterministic
- if the job remains in non-terminal state for more than 120s, surface a user-visible warning
- on `completed`, capture the full `result` block
- on `failed`, capture `error` and `llm_error`
- if status returns HTTP 401 with `job_unauthorized`, the token has expired

State handling:

- `awaiting_worker`: worker is launching. Be patient.
- `awaiting_inputs`: present; upload inputs now, then call `POST /ready`
- `queued` / `running`: normal; keep polling
- `completed`: success
- `failed`: stop and report

## Artifact retrieval contract

Only fetch artifacts after a terminal `completed` state. From the job result:

- `artifacts.manifest_url`
- `artifacts.download_urls[filename]`

For each artifact blob:

- expected wire format: `ephemeral_pub_32 || nonce_12 || ciphertext_with_16byte_tag`
- derive shared secret from result keypair + ephemeral pub
- AAD: `b"verdantforged-file-v1\x00output\x00<job_id>\x00<filename>"`
- key: `HKDF-SHA256(shared, salt=None, info=b"verdantforged-file-key-v1\x00" + AAD)`
- decrypt ciphertext
- verify `sha256` from the manifest after decryption
- save to disk under `<artifacts_dir>/<job_id>/<filename>`
- print a short text preview for text-y content types

If `/artifacts` returns 404, `no artifacts` is valid; do not fabricate content.

## Result presentation

Always show:

- `job_id`, `state`, `duration_ms`, `fuel_used`, `model`, `execution_mode`
- attestation summary: `sandbox.name`, `sandbox.attested`, `sandbox.inference_route`,
  `sandbox.nemoclaw_version`, `sandbox.nemoclaw_image_digest`
- payment summary: `payment.mode`, `payment.status`, `payment.amount_cents`
- review output: the decoded `result.output`
- artifact manifest or saved artifact path

Then offer follow-ups:

- “Show the reviewed file side-by-side with findings”
- “Resubmit with more flaws”
- “Save the review report to disk”
- “Switch language and rerun demo”

## Common pitfalls

1. **Wrong input nonce**: using two different random nonces for the same encrypted file
   causes worker-side decryption failure and stalls the job.
2. **Skipping upstream `run_file_job_e2e.py`**: hand-rolled clients often omit
   required fields or use the wrong encryption AAD. Use the canonical client.
3. **Cold worker / attestation binding**: workers may take 30–120s to transition from
   `awaiting_worker` to `awaiting_inputs`. Patience and backoff are required.
4. **Token expiration**: job access tokens are short-lived. If polling returns HTTP 401,
   stop and inform the human rather than blind-retrying.
5. **Sandbox failure is environmental**: `execution_mode: sandbox-failed` or
   `sandbox 'worker' is not ready` means the broker/worker path is unavailable,
   not that the request was malformed. Surface this plainly and do not synthesize
   review output.
6. **Artifacts may be absent**: `no artifacts` is valid. Record verbatim; do not
   fabricate file contents or substitute local analysis.

## Worker behavior notes (current broker)

- successful request + `completed` + `sandbox-failed` is possible
- artifacts are unavailable when `execution_mode: sandbox-failed`
- the legacy unauthenticated host proxy path is intentionally removed
- if submissions repeatedly fail with `sandbox-failed`, stop identical retries and
  report worker state honestly

## Verification checklist

- [ ] Flawed sample code written and visible
- [ ] Demo SPT minted successfully
- [ ] Job submitted with `job_id` and `job_access_token`
- [ ] Inputs encrypted with correct AAD and same nonce for wire + ciphertext
- [ ] Final job state captured in full
- [ ] Artifacts fetched and decrypted, or broker response recorded verbatim
- [ ] Human-readable result summary shown to the user