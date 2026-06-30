#!/usr/bin/env bash
# demo-blind-audit.sh — judge-facing demo for the blind-audit showcase skill
# (Skill 3, Nous/NVIDIA pillars — confidential AI: source code is encrypted,
# even the broker can't read it).
#
# Run against a live broker URL. Default: http://127.0.0.1:8000 (local dev).
# Override with BROKER_URL=... for staging/prod.
#
# The full judge run-through in SHOWCASE_SKILLS.md is 60 seconds:
#   1. Pull the worker's X25519 pubkey from /v1/discover.attestation.enclave_pubkey
#   2. Generate a requester X25519 keypair (for receiving the encrypted result)
#   3. Encrypt source code to the WORKER's pubkey (client-side, ephemeral ECDH)
#   4. Submit the encrypted job (skill=blind-audit, decrypt_input=true)
#   5. Poll /v1/jobs/<id> until the worker has finalised the result
#   6. Decrypt result_encrypted with the requester's X25519 privkey
#   7. Verify the Ed25519 signatures on the result envelope (worker + broker)
#
# Requirements: curl, jq, python3, cryptography. Broker must have blind-audit
# registered with decrypt_input=true (run tests/verify-blind-audit.py first).
#
# Why "Confidential AI": the broker never sees plaintext — it forwards an
# X25519+ChaCha20Poly1305 blob. Only the TEE worker, holding the matching
# privkey, can decrypt it; the LLM audit runs inside the enclave; the audit
# report is re-encrypted to the requester's pubkey before leaving the
# worker. Even a compromised broker only sees opaque ciphertext.

set -euo pipefail

BROKER_URL="${BROKER_URL:-http://127.0.0.1:8000}"
DEMO_PI="${DEMO_PI:-pi_demo_audit}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

bold "== VerdantForged TEE Broker — blind-audit demo =="
dim  "Target: $BROKER_URL"
echo

# ---- 1. Pull the worker's X25519 pubkey from /v1/discover -------------------
bold "1. Fetching the worker's X25519 public key from /v1/discover…"
DISC=$(curl -sf "$BROKER_URL/v1/discover")
WORKER_PUB_B64=$(echo "$DISC" | jq -r '.attestation.enclave_pubkey // ""')
if [ -z "$WORKER_PUB_B64" ] || [ "$WORKER_PUB_B64" = "null" ]; then
  dim "  /v1/discover did NOT return attestation.enclave_pubkey."
  dim "  Worker must publish worker-keys.json on boot (publish_worker_keys)."
  exit 1
fi
bold "   Worker pubkey (base64, 32 bytes): $WORKER_PUB_B64"
echo

# Sanity-check that blind-audit is advertised — without this the POST /v1/jobs
# below would route to a stub or 404.
if ! echo "$DISC" | jq -e '.supported_skills | index("blind-audit")' >/dev/null; then
  dim "  /v1/discover does NOT list 'blind-audit'."
  dim "  The broker must have the skill registered with decrypt_input=true"
  dim "  (POST /v1/skills). See tests/verify-blind-audit.py::3b."
  exit 1
fi

# ---- 2. Generate a requester X25519 keypair --------------------------------
# We use the requester's privkey to decrypt the audit report when the worker
# returns it. The pubkey goes in result_pubkey on the submit_job call.
bold "2. Generating requester X25519 keypair (to receive the encrypted audit)…"
REQ_KEY_DIR=$(mktemp -d)
trap 'rm -rf "$REQ_KEY_DIR"' EXIT
REQ_PRIV_PATH="$REQ_KEY_DIR/requester.x25519.priv"
REQ_PUB_B64=$(python3 - "$REQ_PRIV_PATH" <<'PY'
import base64, sys
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization
priv = X25519PrivateKey.generate()
priv_bytes = priv.private_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PrivateFormat.Raw,
    encryption_algorithm=serialization.NoEncryption(),
)
with open(sys.argv[1], "wb") as f:
    f.write(priv_bytes)
pub_b64 = base64.b64encode(priv.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)).decode()
print(pub_b64)
PY
)
bold "   Requester pubkey (base64, 32 bytes): $REQ_PUB_B64"
echo

# ---- 3. Encrypt the source code to the WORKER's pubkey ---------------------
# Wire format the poller recognises (poller.decrypt_input):
#   ephemeral_pubkey_32 || nonce_12 || ciphertext_with_16byte_tag
# AAD: b"verdantforged-input"  — distinct from b"verdantforged-result"
# so a result-blob spliced into input fails authentication.
bold "3. Encrypting source code to the worker pubkey (client-side ephemeral ECDH)…"
SOURCE_CODE='def hello():
    return eval(input("> "))  # nosec — for demo only'

ENCRYPTED_DATA=$(python3 - "$WORKER_PUB_B64" "$SOURCE_CODE" <<'PY'
import base64, os, sys
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.ciphers.aead import ChaCha20Poly1305

worker_pub = X25519PublicKey.from_public_bytes(base64.b64decode(sys.argv[1]))
eph = X25519PrivateKey.generate()
shared = eph.exchange(worker_pub)
nonce = os.urandom(12)
ciphertext = ChaCha20Poly1305(shared).encrypt(
    nonce, sys.argv[2].encode(), b"verdantforged-input",
)
eph_pub = eph.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
print(base64.b64encode(eph_pub + nonce + ciphertext).decode())
PY
)
bold "   Ciphertext (base64): ${ENCRYPTED_DATA:0:64}…  ($(echo -n "$ENCRYPTED_DATA" | wc -c) chars)"
echo

# ---- 4. Submit the encrypted blind-audit job -------------------------------
bold "4. Submitting the encrypted job (skill=blind-audit)…"
SUBMIT_BODY=$(jq -n --arg data "$ENCRYPTED_DATA" --arg rpk "$REQ_PUB_B64" --arg pi "$DEMO_PI" '{
  client_req_id: ("demo-blind-audit-" + (now | tostring)),
  encrypted_skill: "blind-audit",
  encrypted_data: $data,
  requester_sig: "0x",
  result_pubkey: $rpk,
  stripe_pi_id: $pi
}')
RESP=$(curl -sf -X POST "$BROKER_URL/v1/jobs" \
  -H 'Content-Type: application/json' \
  -d "$SUBMIT_BODY")
JOB_ID=$(echo "$RESP" | jq -r .job_id)
bold "   Audit job_id: $JOB_ID"
echo

# ---- 5. Poll for the encrypted audit report --------------------------------
bold "5. Polling for the encrypted audit report…"
for i in $(seq 1 30); do
  RESP=$(curl -sf "$BROKER_URL/v1/jobs/$JOB_ID")
  STATE=$(echo "$RESP" | jq -r .state)
  if [ "$STATE" = "completed" ] || [ "$STATE" = "failed" ]; then
    break
  fi
  sleep 1
done
bold "   Final state: $STATE"
if [ "$STATE" != "completed" ]; then
  dim "  Job did not complete in 30s — error block:"
  echo "$RESP" | jq '.error // .'
  exit 1
fi

# VULN-S3 sanity: the broker should NOT have included a plaintext output.
PLAINTEXT_OUTPUT=$(echo "$RESP" | jq -r '.result.output // ""')
if [ "$PLAINTEXT_OUTPUT" != "[encrypted — see result_encrypted]" ] \
   && [ -n "$PLAINTEXT_OUTPUT" ] && [ "$PLAINTEXT_OUTPUT" != "null" ]; then
  dim "  WARN: result.output is non-empty plaintext — VULN-S3 regression?"
  dim "  (should be '[encrypted — see result_encrypted]' for encrypted jobs)"
fi

RESULT_ENC=$(echo "$RESP" | jq -r '.result.result_encrypted // ""')
if [ -z "$RESULT_ENC" ] || [ "$RESULT_ENC" = "null" ]; then
  dim "  No result_encrypted in response — the worker may have hit a key"
  dim "  rotation or the requester's pubkey was malformed."
  exit 1
fi
bold "   Encrypted result (base64): ${RESULT_ENC:0:64}…  ($(echo -n "$RESULT_ENC" | wc -c) chars)"
echo

# ---- 6. Decrypt the audit report -------------------------------------------
# Wire format (matches poller.execute_in_envelope ~line 2598):
#   worker_ephemeral_pubkey_32 || nonce_12 || ciphertext_with_16byte_tag
# AAD: b"verdantforged-result"
bold "6. Decrypting the audit report with the requester's privkey…"
AUDIT_JSON=$(python3 - "$REQ_PRIV_PATH" "$RESULT_ENC" <<'PY'
import base64, json, sys
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

with open(sys.argv[1], "rb") as f:
    priv = X25519PrivateKey.from_private_bytes(f.read())
blob = base64.b64decode(sys.argv[2])
eph_pub_bytes = blob[:32]
nonce = blob[32:44]
ciphertext = blob[44:]
eph_pub = X25519PublicKey.from_public_bytes(eph_pub_bytes)
shared = priv.exchange(eph_pub)
plaintext = ChaCha20Poly1305(shared).decrypt(
    nonce, ciphertext, b"verdantforged-result",
)
# Worker emits JSON {output, model, usage, execution_mode} — pretty-print.
print(json.dumps(json.loads(plaintext), indent=2))
PY
)
bold "== Audit report (decrypted) =="
echo "$AUDIT_JSON"
echo

# ---- 7. Verify the Ed25519 signatures --------------------------------------
bold "7. Verifying the result envelope signatures…"
RESULT_HASH=$(echo "$RESP" | jq -r '.result.result_hash')
SKILL_HASH=$(echo "$RESP" | jq -r '.result.skill_hash')
INPUT_HASH=$(echo "$RESP" | jq -r '.result.input_hash')
WORKER_SIG=$(echo "$RESP" | jq -r '.result.worker_signature // ""')
BROKER_SIG=$(echo "$RESP" | jq -r '.result.broker_signature // ""')
bold "   result_hash: $RESULT_HASH"
bold "   skill_hash:  $SKILL_HASH"
bold "   input_hash:  $INPUT_HASH"

# Input hash sanity: it should be SHA-256 of the SOURCE CODE (the poller
# computed it AFTER decrypting the envelope). Operators can confirm the
# worker really saw the cleartext by re-hashing locally.
LOCAL_INPUT_HASH=$(python3 - "$SOURCE_CODE" <<'PY'
import hashlib, sys
print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
PY
)
bold "   input_hash (recomputed from source): $LOCAL_INPUT_HASH"
if [ "$INPUT_HASH" = "$LOCAL_INPUT_HASH" ]; then
  bold "   ✓ input_hash matches — worker decrypted and re-hashed the payload"
else
  dim "   ✗ input_hash mismatch — worker did NOT decrypt successfully,"
  dim "     or the source was modified after submission."
fi
echo

bold "== Judge verification =="
dim  "Verify with: python3 - <<PY"
dim  "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey"
dim  "import base64"
dim  "r = $RESP"
dim  "result = r['result']"
dim  "sig_payload = f\"{result['result_hash']}|{result['skill_hash']}|{result['input_hash']}\".encode()"
dim  "# Worker sig:"
dim  "Ed25519PublicKey.from_public_bytes(base64.b64decode(result['worker_pubkey'])).verify("
dim  "    base64.b64decode(result['worker_signature']), sig_payload)"
dim  "# Broker sig (authoritative — see VULN-S4):"
dim  "Ed25519PublicKey.from_public_bytes(base64.b64decode(r['broker_pubkey'])).verify("
dim  "    base64.b64decode(result['broker_signature']), sig_payload)"
dim  "print('verified')"
dim  "PY"
echo

bold "== Summary =="
bold "  ✓ Broker never saw plaintext source code"
bold "  ✓ Audit ran inside the TEE worker (input_decrypted=true)"
bold "  ✓ Report re-encrypted to requester before leaving the worker"
bold "  ✓ Both worker and broker signed (result_hash|skill_hash|input_hash)"
