# Cryptographic hardening for split-control-plane TEE brokers

Concrete implementation patterns gathered while building the VerdantForged
broker (NVIDIA × Stripe × Nous hackathon, June 2026). Use as a checklist
when designing any TEE broker that has:

  - An **always-on control plane** (cheap, public, holds secrets)
  - **Ephemeral workers** (m6a.xlarge SEV-SNP, launched on demand)
  - A **trusted upstream** (LLM, model API, payment processor) that the
    control plane must talk to but the worker must not

The classic mistake is letting the upstream API key reach the worker
envelope, the worker env, or the EFS inbox. This reference shows how to
keep it on the control plane and still let workers do real work.

---

## 1. Broker-side LLM proxy (key isolation pattern)

**Problem:** Workers need LLM access to execute skills. If the upstream API
key is baked into the worker image, EFS, or envelope, it's exposed to:
  - The worker's `os.environ` (visible to any process in the enclave)
  - The systemd unit on disk (visible to anyone who can SSH in pre-boot)
  - `/mnt/broker/logs/llm-api-key` (visible to anyone with NFS access)
  - The stderr of any subprocess (leaks via `print()`)

**Pattern: per-job ephemeral tokens.**

```
Client                    Broker (control plane)              Worker (ephemeral)
  |                              |                                   |
  | POST /v1/jobs                |                                   |
  |  encrypted_skill             |                                   |
  |  encrypted_data              |                                   |
  |  result_pubkey (X25519)      |                                   |
  |  requester_sig (Ed25519)     |                                   |
  |  stripe_pi_id                |                                   |
  |----------------------------->|                                   |
  |                              | 1. INSERT job                     |
  |                              | 2. INSERT llm_tokens row:         |
  |                              |     llm_token = llm_<48hex>       |
  |                              |     expires_at = +10 min          |
  |                              | 3. Write envelope to EFS inbox    |
  |                              |    (envelope includes llm_token)  |
  |                              | 4. (asyncio.create_task)          |
  |                              |    _kick_worker_for_job()         |
  |                              |     → boto3 RunInstances          |
  |<-- 202 {job_id, llm_token}---|                                   |
  |                              |                                   |
  |                              |   <SSM handshake, EFS mount>      |
  |                              |---------------------------------->|
  |                              |                                   | poller reads envelope
  |                              |                                   |     envelope["llm_token"]
  |                              |                                   |
  |                              |<-- POST /v1/llm/chat/completions -|
  |                              |    Authorization: Bearer llm_<..>|
  |                              |    (with real LLM key inside)     |
  |                              |----> upstream LLM API ---------->|
  |                              |<--- completion -------------------|
  |                              | records usage in 3 tables:       |
  |                              |   llm_tokens.tokens_used         |
  |                              |   account_daily_tokens           |
  |                              |   jobs.llm_tokens_used           |
  |                              |--- {choices:[...], _billing:{}}->|
  |                              |                                   | poller writes to
  |                              |                                   |   /mnt/broker/jobs/outbox/
  |                              |<-- outbox-poller picks up --------|
  |                              | updates jobs.result, state        |
  |<-- GET /v1/jobs/{id} {state: completed, result:{...}} ----------|
```

**Token lifecycle:**
  - 48-char hex (`secrets.token_hex(24)`) → 96-bit entropy
  - 10-minute expiry (`datetime.now() + timedelta(minutes=10)`)
  - DB-tracked: `llm_tokens` table with `expires_at`, `tokens_used`, `calls`
  - Per-account daily cap enforced at submit + at every proxy call

**Verify upstream keys are scoped correctly:**

The Hermes `~/.hermes/auth.json` credential pool often has tokens with
narrow scopes. We discovered:
  - `ollama-cloud` entry is metadata only, key in `OLLAMA_API_KEY` env var
  - `custom:api.ollama.com` has 57-char API keys that DO work for chat
  - A Gemini OAuth token (`AQ.Ab8...`) works for `/v1/models` on Ollama
    but returns **401 on `/v1/chat/completions`** — it's just the wrong
    credential pool entry

Always test with `curl -sS -X POST https://ollama.com/v1/chat/completions
-H "Authorization: Bearer $KEY" -d '{...}'` before trusting a key for
real inference. If it returns 401 but `/v1/models` works, the key has
only the listing scope.

---

## 2. Per-job result encryption (X25519 + ChaCha20-Poly1305)

**Why:** The worker's output goes through EFS (visible to anyone with NFS
mount access to the EFS security group) and the broker daemon (which is
in the trust boundary you control but should not be trusted with plaintext
output). The requester wants to verify confidentiality.

**Pattern: ephemeral-static ECDH, ChaCha20-Poly1305 AEAD.**

```python
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives import serialization
import base64, os

# Requester provides result_pubkey (X25519) at submit time
requester_pub_b64 = body["result_pubkey"]  # base64-encoded 32 bytes
requester_pub = X25519PublicKey.from_public_bytes(base64.b64decode(requester_pub_b64))

# Worker generates a PER-JOB ephemeral keypair (forward secrecy)
eph = X25519PrivateKey.generate()
shared_secret = eph.exchange(requester_pub)

# Encrypt with explicit 12-byte nonce (cryptography >= 41 requires this)
nonce = os.urandom(12)
ciphertext = ChaCha20Poly1305(shared_secret).encrypt(
    nonce,
    payload_json_bytes,
    associated_data=b"verdantforged-result",  # domain separation
)

# Output: worker_eph_pubkey || nonce || ciphertext_with_tag
result_encrypted = base64.b64encode(
    eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    + nonce
    + ciphertext
).decode()
# eph.private_key is GC'd at end of function — never persisted, never reused
```

**Requester decryption:**
```python
eph_pub = X25519PublicKey.from_public_bytes(enc[:32])
shared = req_priv.exchange(eph_pub)
plaintext = ChaCha20Poly1305(shared).decrypt(enc[32:44], enc[44:], b"verdantforged-result")
```

**Forward secrecy** comes from discarding the ephemeral privkey after one
use. Compromise of the worker's long-term encryption key still cannot
decrypt past results (you'd need each per-job ephemeral privkey, which is
gone).

**Pitfall — `cryptography >= 41` requires explicit nonce:**
The signature changed. Old code:
```python
aead.encrypt(associated_data, data, None)  # ← 41.x raises "Nonce must be 12 bytes"
```
New code:
```python
aead.encrypt(nonce, data, associated_data)
```
If you copy from older tutorials, you'll get a runtime error at first
production job.

**Pitfall — wrong arg order for `decrypt`:**
Same trap: `aead.decrypt(nonce, ciphertext, aad)` — NOT `(aad, ciphertext)`.
The first positional is always the nonce.

**Demo simplification:** For the hackathon, the worker's `_ensure_worker_encryption_key`
returned its STATIC privkey (not a per-job ephemeral) — this still works
but loses forward secrecy. Production code must generate a fresh
`X25519PrivateKey.generate()` per job.

---

## 3. Broker signature (Ed25519)

**Why:** Even with the result encrypted to the requester, the requester
needs to verify "this came from the broker enclave I trust." The
signature covers a content-addressed hash, so any tampering breaks it.

**Pattern:**
```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import hashlib, base64

# Compute content-addressed hashes
skill_hash = hashlib.sha256(skill.encode()).hexdigest()
input_hash = hashlib.sha256(input_data.encode()).hexdigest()

canonical_payload = {
    "job_id": job_id, "skill_hash": skill_hash, "input_hash": input_hash,
    "output": output, "fuel_used": fuel_used, "duration_ms": duration_ms,
    "execution_mode": execution_mode, "measurement": measurement,
}
canonical_bytes = json.dumps(canonical_payload, sort_keys=True).encode()
result_hash = hashlib.sha256(canonical_bytes).hexdigest()

# Worker (or broker, depending on architecture) signs the chain
sig_payload = f"{result_hash}|{skill_hash}|{input_hash}".encode()
broker_signature = base64.b64encode(signer.sign(sig_payload)).decode()
# Ed25519 sig is always 64 bytes — verify len(sig_bytes) == 64
```

**Key persistence:**
```python
# Generate once, persist mode 0600, never write outside that path
key_path = "/opt/worker/keys/worker_signing.priv"
key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
if not Path(key_path).exists():
    priv = Ed25519PrivateKey.generate()
    key_path.write_bytes(priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
    key_path.chmod(0o600)
```

**Verification requires the public key** — for production, expose it as
part of the broker's attestation report (the SEV-SNP quote includes
fields you can bind a key to). For demo, hardcode or pass via config.

---

## 4. Requester signature verification (Ed25519, opt-in)

**Why:** Without this, anyone with the broker URL can submit jobs
claiming to be any `stripe_pi_id`. The `requester_sig` field is already
in the spec but currently accepted as a string. Verifying it adds ~3ms
and rejects obvious forgery.

**Pattern (opt-in to preserve backward compat with demo clients):**
```python
# In validate_submit():
if body.get("requester_pubkey") and body["requester_sig"] != "0x":
    canonical = f"{sha256(body['encrypted_skill'])}|{sha256(body['encrypted_data'])}|{body['result_pubkey']}|{body['stripe_pi_id']}|{body.get('timestamp','')}".encode()
    try:
        Ed25519PublicKey.from_public_bytes(b64decode(body["requester_pubkey"])).verify(
            b64decode(body["requester_sig"]), canonical)
    except Exception:
        return False, "invalid requester_sig (signature failed verification)"
```

**Demo fallback:** Old clients pass `"0x"` for `requester_sig`. The
check above skips verification if sig equals `"0x"` — keeps the demo
flowing. Production should set `BROKER_REQUIRE_REQUESTER_SIG=1` and
reject any `"0x"` submission.

---

## 5. IMDSv2 token fetch (EC2 worker metadata)

**Problem:** Modern EC2 instances (anything launched in the last few
years) default to IMDSv2. Curl/wget calls to `http://169.254.169.254/...`
return **HTTP 401 Unauthorized** unless you first `PUT` to
`/latest/api/token` to get a session token, then include it as
`X-aws-ec2-metadata-token: <token>` on subsequent GETs.

**Reproduction:**
```
$ curl -sS --max-time 2 http://169.254.169.254/latest/meta-data/instance-id
HTTP/1.1 401 Unauthorized
```

**Fix in shell (user-data.sh):**
```bash
IMDS_TOKEN=$(curl -sf --max-time 5 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null || echo "")
IMDS_HEADER=()
[ -n "$IMDS_TOKEN" ] && IMDS_HEADER=(-H "X-aws-ec2-metadata-token: $IMDS_TOKEN")
INSTANCE_ID=$(curl -sf --max-time 5 "http://169.254.169.254/latest/meta-data/instance-id" "${IMDS_HEADER[@]}")
```

**Fix in Python (poller.py):**
```python
import urllib.request
req = urllib.request.Request(
    "http://169.254.169.254/latest/api/token", method="PUT",
    headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"})
token = ""
try:
    with urllib.request.urlopen(req, timeout=2) as r:
        token = r.read().decode().strip()
except Exception:
    pass
meta_headers = {"X-aws-ec2-metadata-token": token} if token else {}
iid_req = urllib.request.Request(
    "http://169.254.169.254/latest/meta-data/instance-id", headers=meta_headers)
with urllib.request.urlopen(iid_req, timeout=2) as r:
    instance_id = r.read().decode().strip()
```

**Symptom to recognize:** Heartbeat files show `"instance_id": "unknown"`,
`"private_ip": "0.0.0.0"`, poller's SEV-SNP measurement is
`"stub-no-measurement"`. All three fix themselves once IMDSv2 is right.

---

## 6. Test pattern: live behavioral verification

For a TEE broker, you cannot unit-test most of the interesting behavior
because it spans control plane + worker + EC2 + EFS + upstream API. The
right pattern is **ad-hoc verification scripts** that hit the live
broker and assert on real responses.

**Template:** `tests/verify-crypto-e2e.py` (19 checks, exit 0 against
live broker).

```python
#!/usr/bin/env python3
"""Verify the cryptographic end-to-end security model."""
import urllib.request, json, time, sys, base64
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

BROKER = "https://verdant.codepilots.co.uk"
PASS = 0; FAIL = 0

def check(label, cond):
    global PASS, FAIL
    if cond: print(f"[PASS] {label}"); PASS += 1
    else:    print(f"[FAIL] {label}"); FAIL += 1

# Generate requester keypair
priv = X25519PrivateKey.generate()
pub_b64 = base64.b64encode(priv.public_key().public_bytes(...)).decode()

# Submit, poll, decrypt, verify
# ... (full test in tests/verify-crypto-e2e.py)

print(f"Passed: {PASS}, Failed: {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
```

**Conventions that work:**
  - Name: `hermes-verify-<scope>.sh` or `.py`
  - First arg is the broker URL (default to live)
  - Each check is `[PASS]` or `[FAIL]` with a one-line label
  - Final summary with exit 0 / 1
  - Save to `/tmp/hermes-verify-<scope>` AND `tests/verify-<scope>` so
    the test survives across sessions
  - Always end with `sys.exit(0 if FAIL == 0 else 1)` so CI/scripts can
    gate on it

**Pitfall:** don't make tests depend on mock servers. Tests should hit
the real broker to be useful — but use `--quick` flag (or low iteration
count) for fast iteration vs. full E2E.

---

## 7. Checklist for a hardened split-control-plane broker

Before going to production:

  - [ ] Real LLM API key is in `config.env` mode 0600 on control plane
  - [ ] `llm-api-key` does NOT exist on EFS (`rm -f /mnt/broker/logs/llm-api-key`)
  - [ ] No direct LLM fallback path in poller (single broker proxy only)
  - [ ] Per-job tokens are cryptographically random (48+ hex chars)
  - [ ] Per-job tokens have a TTL (10 min is fine for demo, 5 min for prod)
  - [ ] Per-account daily cap enforced at submit AND at proxy call
  - [ ] Model field is forced server-side (don't trust client's choice)
  - [ ] Worker requests X25519 result pubkey from envelope
  - [ ] Worker generates fresh per-job ephemeral X25519 keypair
  - [ ] Result is content-addressed (skill_hash, input_hash, result_hash)
  - [ ] Result is signed by the worker/broker enclave key
  - [ ] Worker uses IMDSv2 token for metadata fetches
  - [ ] All envelope data validated at receipt (skill name whitelist, etc.)
  - [ ] Outbox poller matches results to jobs by job_id (never by index)
  - [ ] Idle timer terminates workers after N minutes (cost control)
  - [ ] Webhook URL is validated (HTTPS, no localhost) before storing
  - [ ] Verification tests run against live broker and pass

## See also

  - `tee-broker-protocol` — canonical event kinds, tags, validation
  - `tee-broker-negotiate` — full request/response lifecycle
  - `tee-broker-task-design` — schemas and execution constraints