#!/usr/bin/env bash
# demo-attestation-verifier.sh — judge-facing demo for the attestation-verifier
# showcase skill (Skill 1, NVIDIA/AMD SEV-SNP pillar — "Prove the TEE is real").
#
# Run against a live broker URL. Default: http://127.0.0.1:8000 (local dev).
# Override with BROKER_URL=... for staging/prod.
#
# The full judge run-through in SHOWCASE_SKILLS.md is 30 seconds:
#   1. Pull the broker's own attestation block from /v1/discover
#   2. POST it back to /v1/jobs under skill=attestation-verifier
#   3. Poll /v1/jobs/<id> for the signed verdict
#   4. Verify the Ed25519 signature over the canonical verdict JSON
#
# Requirements: curl, jq, base64. Broker must have attestation-verifier
# registered (run tests/verify-attestation-verifier.py::C1 first to confirm).
#
# Why "trust but verify": the broker doesn't just claim it runs in a TEE —
# anyone with this script can pull its attestation, send it back through the
# deterministic verifier, and check the Ed25519 signature with the broker's
# public key. The verdict is reproducible and non-repudiable.

set -euo pipefail

BROKER_URL="${BROKER_URL:-http://127.0.0.1:8000}"
DEMO_PI="${DEMO_PI:-pi_demo_verify}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

bold "== VerdantForged TEE Broker — attestation-verifier demo =="
dim  "Target: $BROKER_URL"
echo

# ---- 1. Pull the broker's attestation block --------------------------------
bold "1. Fetching the broker's attestation from /v1/discover…"
DISC=$(curl -sf "$BROKER_URL/v1/discover")
ATTEST=$(echo "$DISC" | jq -c .attestation)
TEE_TYPE=$(echo "$ATTEST" | jq -r '.tee_type // "unknown"')
MEAS=$(echo "$ATTEST" | jq -r '.measurement // "<missing>"')
CHIP=$(echo "$ATTEST" | jq -r '.chip_id // "<missing>"')
CERT_LEN=$(echo "$ATTEST" | jq -r '.cert_chain | length // 0')
bold "   TEE type:    $TEE_TYPE"
bold "   Chip ID:     $CHIP"
bold "   Measurement: ${MEAS:0:32}${MEAS:32:+...}"
bold "   Cert chain:  $CERT_LEN entr$( [ "$CERT_LEN" = "1" ] && echo y || echo ies)"
echo

# Sanity-check that attestation-verifier is advertised — without this the
# POST /v1/jobs step below would route the request to a stub or 404.
if ! echo "$DISC" | jq -e '.supported_skills | index("attestation-verifier")' >/dev/null; then
  dim "  /v1/discover does NOT list 'attestation-verifier'."
  dim "  The broker must have the skill registered (POST /v1/skills) before"
  dim "  this demo can run. See tests/verify-attestation-verifier.py::C5."
  exit 1
fi

# ---- 2. Submit the attestation block for verification -------------------------
bold "2. Submitting the attestation block to the verifier (skill=attestation-verifier)…"
VERIFY_BODY=$(jq -n --arg data "$ATTEST" --arg pi "$DEMO_PI" '{
  client_req_id: ("demo-attest-" + (now | tostring)),
  encrypted_skill: "attestation-verifier",
  encrypted_data: $data,
  requester_sig: "0x",
  result_pubkey: "0x",
  stripe_pi_id: $pi
}')
RESP=$(curl -sf -X POST "$BROKER_URL/v1/jobs" \
  -H 'Content-Type: application/json' \
  -d "$VERIFY_BODY")
JOB_ID=$(echo "$RESP" | jq -r .job_id)
bold "   Verify job_id: $JOB_ID"
echo

# ---- 3. Poll for the signed verdict ----------------------------------------
bold "3. Polling for the verdict…"
for i in 1 2 3 4 5 6 7 8 9 10; do
  RESP=$(curl -sf "$BROKER_URL/v1/jobs/$JOB_ID")
  STATE=$(echo "$RESP" | jq -r .state)
  if [ "$STATE" = "completed" ] || [ "$STATE" = "failed" ]; then
    break
  fi
  sleep 1
done

bold "== Verdict =="
echo "$RESP" | jq '.result.attestation_verdict // .result // .'
echo

# ---- 4. Verify the Ed25519 signature ---------------------------------------
bold "== Verifier (judge runs this to prove the signature) =="
VERDICT=$(echo "$RESP" | jq '.result.attestation_verdict')
if [ -z "$VERDICT" ] || [ "$VERDICT" = "null" ]; then
  dim "  No attestation_verdict in response — was the skill registered?"
  exit 1
fi

# Canonical payload the broker signed: every claim field except the three
# envelope-only fields (broker_signature, broker_pubkey, signed_payload).
# Ensure_ascii=False keeps unicode (→) literal so byte strings match.
CANONICAL=$(echo "$VERDICT" | jq -cS 'to_entries
  | map(select(.key | IN("broker_signature","broker_pubkey","signed_payload") | not))
  | from_entries')
bold "Canonical payload the broker signed:"
echo "$CANONICAL"
echo
bold "Sig+pubkey:"
echo "$VERDICT" | jq -r '"  pubkey:        \(.broker_pubkey)\n  sig (b64):     \(.broker_signature)"'

dim  "Verify with: python3 - <<PY"
dim  "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey"
dim  "import base64, json"
dim  "v = $VERDICT"
dim  "payload = json.dumps({k: v[k] for k in sorted(v.keys())"
dim  "                              if k not in ('broker_signature','broker_pubkey','signed_payload')},"
dim  "                     sort_keys=True, ensure_ascii=False).encode()"
dim  "pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(v['broker_pubkey']))"
dim  "pub.verify(base64.b64decode(v['broker_signature']), payload)"
dim  "print('verified')"
dim  "PY"
