#!/usr/bin/env bash
# demo-token-receipt.sh — judge-facing demo for the token-receipt showcase skill
# (Skill 2, Stripe pillar — Pay As Token Burns).
#
# Run against a live broker URL. Default: http://127.0.0.1:8000 (local dev).
# Override with BROKER_URL=... for staging/prod.
#
# The full judge run-through in SHOWCASE_SKILLS.md is 45 seconds:
#   1. Run a real job (the "summarize" built-in stub is fine for demo)
#   2. Submit a token-receipt job that references the prior job_id
#   3. Poll for the signed receipt
#
# Requirements: curl, jq. Broker must have token-receipt registered
# (it is, by default, in /v1/discover — see verify-token-receipt.py::D4).

set -euo pipefail

BROKER_URL="${BROKER_URL:-http://127.0.0.1:8000}"
# Demo-mode PI accepted by the broker when STRIPE_SECRET_KEY is unset.
DEMO_PI="${DEMO_PI:-pi_demo_billing}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

bold "== VerdantForged TEE Broker — token-receipt demo =="
dim  "Target: $BROKER_URL"
echo

# ---- 1. Run a real job so we have something to receipt -----------------------
bold "1. Submitting a real job (summarize) to generate usage data…"
DEMO_JOB_BODY=$(cat <<JSON
{
  "client_req_id": "demo-token-receipt-$(date +%s)",
  "encrypted_skill": "summarize",
  "encrypted_data": "The quick brown fox jumps over the lazy dog. The broker is a TEE-protected compute primitive — every job is signed, every receipt is non-repudiable.",
  "requester_sig": "0x",
  "result_pubkey": "0x",
  "stripe_pi_id": "$DEMO_PI"
}
JSON
)
RESP=$(curl -sf -X POST "$BROKER_URL/v1/jobs" \
  -H 'Content-Type: application/json' \
  -d "$DEMO_JOB_BODY")
PRIOR_JOB_ID=$(echo "$RESP" | jq -r .job_id)
bold "   Prior job_id: $PRIOR_JOB_ID"
echo

# Give the worker a moment to finish so the cost fields have real data.
dim "   waiting 4s for the worker to finish…"
sleep 4

# ---- 2. Submit a token-receipt job that references the prior job_id ---------
bold "2. Submitting a token-receipt job that references $PRIOR_JOB_ID…"
RECEIPT_BODY=$(cat <<JSON
{
  "client_req_id": "demo-receipt-$(date +%s)",
  "encrypted_skill": "token-receipt",
  "encrypted_data": "$PRIOR_JOB_ID",
  "requester_sig": "0x",
  "result_pubkey": "0x",
  "stripe_pi_id": "$DEMO_PI"
}
JSON
)
RESP=$(curl -sf -X POST "$BROKER_URL/v1/jobs" \
  -H 'Content-Type: application/json' \
  -d "$RECEIPT_BODY")
RECEIPT_JOB_ID=$(echo "$RESP" | jq -r .job_id)
bold "   Receipt job_id: $RECEIPT_JOB_ID"
echo

# ---- 3. Poll for the signed receipt -----------------------------------------
bold "3. Polling for the signed receipt…"
for i in 1 2 3 4 5 6 7 8; do
  RESP=$(curl -sf "$BROKER_URL/v1/jobs/$RECEIPT_JOB_ID")
  STATE=$(echo "$RESP" | jq -r .state)
  if [ "$STATE" = "completed" ] || [ "$STATE" = "failed" ]; then
    break
  fi
  sleep 1
done

bold "== Receipt =="
echo "$RESP" | jq '.result.receipt // .result // .'

echo
bold "== Verifier (judge runs this to prove the signature) =="
RECEIPT=$(echo "$RESP" | jq '.result.receipt')
# Reconstruct the canonical payload exactly as build_token_receipt() does.
# A judge with this exact recipe can verify the broker signed the receipt.
SIG_PAYLOAD=$(echo "$RECEIPT" | jq -c '{
  job_id,
  receipt_job_id,
  token_breakdown,
  cost_breakdown,
  stripe_pi_id,
  signed_at
}' | jq -cS .)
echo "Canonical payload the broker signed:"
echo "$SIG_PAYLOAD"

dim  "Verify with: python3 - <<PY"
dim  "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey"
dim  "import base64, json"
dim  "r = $RECEIPT"
dim  "pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(r['broker_pubkey']))"
dim  "payload = json.dumps({k: r[k] for k in ['job_id','receipt_job_id','token_breakdown','cost_breakdown','stripe_pi_id','signed_at']}, sort_keys=True).encode()"
dim  "pub.verify(base64.b64decode(r['broker_signature']), payload)"
dim  "print('verified')"
dim  "PY"