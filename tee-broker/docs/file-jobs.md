# Encrypted file jobs

File jobs use a payment-gated, worker-first, two-phase upload. The broker first
issues an ACS 402 challenge if the request is unpaid, then never accepts file
bytes directly and never returns the worker's LLM token to the requester.

## Payment preflight

Before any file upload flow begins, clients must either:

1. submit `POST /v1/jobs` and receive a `402 Payment Required` challenge, then
   mint a Stripe Shared Payment Token (`spt_...`) and retry, or
2. reuse a valid request that already includes `shared_payment_token`, `spt`,
   or `Payment: spt_...`.

The broker creates/confirms the PaymentIntent server-side; clients do not
create or expose a Stripe secret.

## Limits and lifecycle

- 10 input and 10 output files maximum.
- 50 MiB maximum per file and 100 MiB aggregate in either direction.
- Upload and download URLs last 15 minutes; S3 objects expire after 24 hours.
- Inputs must be encrypted to the job's attested worker key. Outputs are always
  encrypted to the requester's `result_pubkey`.
- Keep the `job_access_token` returned by the initial submission. It is shown
  once and is required as a Bearer token for status, ready, top-up,
  acknowledgement, manifest, and download operations.

## API sequence

1. Generate an X25519 result key pair.
2. `POST /v1/jobs` with `input_files`, plaintext sizes and the base64 result
   public key. If payment is already satisfied, the response is
   `202 awaiting_worker` and includes the one-time job access token.
4. Poll `GET /v1/jobs/{job_id}` with the token. When the state becomes
   `awaiting_inputs`, `input_upload` contains the worker public key, SNP quote,
   report-data binding, expiry, and one PUT URL per file. If the job stays in
   `awaiting_worker` even though `/healthz` says the worker is live, the broker
   is still waiting for the attested worker key / report-data binding to
   publish; the status response includes `worker_wait.detail` with the current
   reason. Deterministic identity failures such as `worker policy_hash mismatch`
   transition the job to `failed` so clients can stop waiting and surface the
   `error` field.

   On every new worker launch the broker removes stale `worker-keys.json`,
   `worker-attestation.json`, and `worker-heartbeat.json` from EFS before the
   worker publishes its fresh identity. This prevents a terminated worker's old
   policy/key binding from blocking a new file job.

   `SHA256("verdantforged-worker-input-v1\\0" || worker_pubkey || "\\0" ||
   policy_hash_bytes)`.
5. Encrypt each file, PUT the resulting bytes to its URL, then call
   `POST /v1/jobs/{job_id}/ready` with the Bearer token and an empty body.
6. Poll status. On completion, fetch the authenticated artifact manifest or an
   individual artifact route, follow its S3 redirect, and decrypt locally.

If the worker changes before `/ready`, the broker returns
`worker_key_changed`; discard the ciphertext and submit a new file job. An
expired upload becomes `abandoned` and uploaded inputs are deleted.

## Encryption format

The scheme identifier is `x25519-hkdf-sha256-chacha20poly1305-v1`.

```text
wire = ephemeral_x25519_public_key[32] || nonce[12] || ciphertext_and_tag
shared = X25519(ephemeral_private, recipient_public)
aad = UTF8("verdantforged-file-v1\0" + direction + "\0" + job_id + "\0" + filename)
key = HKDF-SHA256(shared, salt=None, info="verdantforged-file-key-v1\0" || aad, length=32)
ciphertext_and_tag = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
```

`direction` is `input` for uploads and `output` for returned artifacts. The
encrypted size is always the plaintext size plus 60 bytes.

## Minimal Python client

```python
import base64, json, os, requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

BROKER = "https://verdant.codepilots.co.uk"
result_private = X25519PrivateKey.generate()
result_public = base64.b64encode(result_private.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
source = open("document.pdf", "rb").read()

submitted = requests.post(f"{BROKER}/v1/jobs", json={
    "client_req_id": "file-example-001",
    "encrypted_skill": "summarize",
    "encrypted_data": "Summarize the attached document",
    "requester_sig": "0x",
    "result_pubkey": result_public,
    "input_files": [{"filename": "document.pdf",
                     "content_type": "application/pdf",
                     "size_bytes": len(source)}],
}).json()
job_id, token = submitted["job_id"], submitted["job_access_token"]
headers = {"Authorization": f"Bearer {token}"}

while True:
    job = requests.get(f"{BROKER}/v1/jobs/{job_id}", headers=headers).json()
    if job["state"] == "awaiting_inputs":
        break
    if job["state"] == "failed":
        raise RuntimeError(job["error"])

upload = job["input_upload"]
worker_public = X25519PublicKey.from_public_bytes(
    base64.b64decode(upload["encryption"]["public_key"]))
ephemeral = X25519PrivateKey.generate()
ephemeral_public = ephemeral.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)
aad = f"verdantforged-file-v1\0input\0{job_id}\0document.pdf".encode()
key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
           info=b"verdantforged-file-key-v1\0" + aad).derive(
               ephemeral.exchange(worker_public))
nonce = os.urandom(12)
ciphertext = ephemeral_public + nonce + ChaCha20Poly1305(key).encrypt(
    nonce, source, aad)
requests.put(upload["files"][0]["upload_url"], data=ciphertext).raise_for_status()
requests.post(f"{BROKER}/v1/jobs/{job_id}/ready", headers=headers).raise_for_status()
```

Output decryption uses the same code with `direction="output"`, the artifact
filename, and `result_private` as the recipient private key. Verify the
decrypted byte length and SHA-256 against the artifact manifest.

## Failure codes

| Code | Meaning |
| --- | --- |
| `job_unauthorized` | Missing or incorrect job access token. |
| `file_encryption_required` | File job omitted a valid result X25519 key. |
| `inputs_pending` | One or more declared S3 objects are absent. |
| `input_size_mismatch` | Uploaded ciphertext length is not plaintext size + 60. |
| `worker_key_changed` | The worker/key binding no longer matches the job. |
| `input_upload_expired` | The 15-minute upload window elapsed. |

