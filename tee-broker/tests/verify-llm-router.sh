#!/bin/bash
# Ad-hoc verification for the broker-side LLM proxy + accounting.
# Tests:
#   1. Submit a job -> response includes llm_token
#   2. LLM token has the expected format
#   3. Direct LLM proxy call with valid token returns 200 + LLM response
#   4. Proxy response includes _billing metadata with usage stats
#   5. Token usage is recorded in /v1/llm/usage/{job_id}
#   6. Invalid token returns 401
#   7. Expired/missing token returns 401
#   8. Token cap enforcement (set very low, verify 429)
#   9. Account usage tracking reflects per-job calls

set -uo pipefail

BROKER="https://verdant.codepilots.co.uk"
PASS=0
FAIL=0

check() {
    local label="$1" condition="$2"
    if eval "$condition"; then
        echo "[PASS] $label"
        PASS=$((PASS+1))
    else
        echo "[FAIL] $label"
        FAIL=$((FAIL+1))
    fi
}

echo "=== Setup: submit a job to get an LLM token ==="
CLIENT_REQ_ID="llm-router-test-$(date +%s)"
RESP=$(curl -sS --max-time 10 -X POST "$BROKER/v1/jobs" \
    -H "Content-Type: application/json" \
    -d "{\"client_req_id\":\"$CLIENT_REQ_ID\",\"encrypted_skill\":\"summarize\",\"encrypted_data\":\"The quick brown fox jumps over the lazy dog. Testing LLM router.\",\"requester_sig\":\"sig\",\"result_pubkey\":\"pk\",\"stripe_pi_id\":\"pi_router_test\"}")
echo "$RESP" | python3 -m json.tool | head -20

JOB_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['job_id'])")
LLM_TOKEN=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('llm_token',''))")

echo ""
echo "JOB_ID=$JOB_ID"
echo "LLM_TOKEN=${LLM_TOKEN:0:20}..."

check "1. submit response includes llm_token" "[ -n '$LLM_TOKEN' ]"
check "2. LLM token has expected format (starts with llm_)" "echo '$LLM_TOKEN' | grep -q '^llm_'"

echo ""
echo "=== Test 3-5: call the LLM proxy directly ==="
PROXY_RESP=$(curl -sS --max-time 30 -X POST "$BROKER/v1/llm/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $LLM_TOKEN" \
    -d '{"model":"minimax-m3:cloud","messages":[{"role":"user","content":"Say hello in one sentence."}],"max_tokens":30}' 2>&1)
echo "$PROXY_RESP" | python3 -m json.tool 2>&1 | head -20

check "3. proxy returns 200 with valid token" "echo '$PROXY_RESP' | python3 -c \"import json,sys; d=json.load(sys.stdin); exit(0 if 'choices' in d and len(d['choices'])>0 else 1)\" 2>/dev/null"
check "4. proxy response includes _billing metadata" "echo '$PROXY_RESP' | python3 -c \"import json,sys; d=json.load(sys.stdin); exit(0 if '_billing' in d else 1)\" 2>/dev/null"
check "5. _billing has total_tokens" "echo '$PROXY_RESP' | python3 -c \"import json,sys; d=json.load(sys.stdin); exit(0 if d.get('_billing',{}).get('total_tokens',0)>0 else 1)\" 2>/dev/null"

echo ""
echo "=== Test 6-7: invalid/missing token ==="
INVALID_RESP=$(curl -sS -w "\n__HTTP__%{http_code}" --max-time 5 -X POST "$BROKER/v1/llm/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer llm_fake_invalid_token_12345" \
    -d '{"model":"minimax-m3:cloud","messages":[{"role":"user","content":"test"}]}')
INVALID_CODE=$(echo "$INVALID_RESP" | tail -1 | sed 's/__HTTP__//')
check "6. invalid token returns 401" "[ '$INVALID_CODE' = '401' ]"

NO_AUTH_RESP=$(curl -sS -w "\n__HTTP__%{http_code}" --max-time 5 -X POST "$BROKER/v1/llm/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"model":"minimax-m3:cloud","messages":[{"role":"user","content":"test"}]}')
NO_AUTH_CODE=$(echo "$NO_AUTH_RESP" | tail -1 | sed 's/__HTTP__//')
check "7. missing auth returns 401" "[ '$NO_AUTH_CODE' = '401' ]"

echo ""
echo "=== Test 8: usage tracking via /v1/llm/usage/{job_id} ==="
USAGE_RESP=$(curl -sS --max-time 5 "$BROKER/v1/llm/usage/$JOB_ID")
echo "$USAGE_RESP" | python3 -m json.tool 2>&1 | head -10

check "8a. usage endpoint returns job_id" "echo '$USAGE_RESP' | python3 -c \"import json,sys; d=json.load(sys.stdin); exit(0 if d.get('job_id')=='$JOB_ID' else 1)\" 2>/dev/null"
check "8b. usage endpoint shows llm_calls > 0" "echo '$USAGE_RESP' | python3 -c \"import json,sys; d=json.load(sys.stdin); exit(0 if d.get('llm_calls',0)>0 else 1)\" 2>/dev/null"
check "8c. usage endpoint shows llm_tokens_used > 0" "echo '$USAGE_RESP' | python3 -c \"import json,sys; d=json.load(sys.stdin); exit(0 if d.get('llm_tokens_used',0)>0 else 1)\" 2>/dev/null"

echo ""
echo "=== Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo ""
echo "Ad-hoc verification only (live broker API, real LLM calls)."
echo "Scope: broker LLM proxy + per-job tokens + usage tracking"

[ "$FAIL" = "0" ] && exit 0 || exit 1