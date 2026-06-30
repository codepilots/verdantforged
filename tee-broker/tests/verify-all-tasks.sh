#!/bin/bash
# Ad-hoc verification for tasks 1-6 (all daemon + worker changes).
# Runs against the LIVE broker at https://verdant.codepilots.co.uk.
# Each test is independent — failures don't cascade.

set -uo pipefail

BROKER="https://verdant.codepilots.co.uk"
PASS=0
FAIL=0
RESULTS=""

record() {
    local label="$1" status="$2" detail="${3:-}"
    if [ "$status" = "PASS" ]; then
        echo "[PASS] $label"
        PASS=$((PASS+1))
        RESULTS="${RESULTS}[PASS] $label\n"
    else
        echo "[FAIL] $label $detail"
        FAIL=$((FAIL+1))
        RESULTS="${RESULTS}[FAIL] $label $detail\n"
    fi
}

echo "=== Task 1: Worker user-data robustness (static checks) ==="
USER_DATA="/home/autumn/hermes/competition/tee-broker-deploy/worker/user-data.sh"
grep -q '^set -u' "$USER_DATA" && ! grep -q '^set -e' "$USER_DATA" \
    && record "1a. no set -e, uses set -u" PASS \
    || record "1a. no set -e, uses set -u" FAIL
grep -q 'for i in 1 2 3' "$USER_DATA" \
    && record "1b. apt retry logic" PASS \
    || record "1b. apt retry logic" FAIL
# In the slimmed user-data, the poller is copied from EFS instead of written
# inline. Check that mkdir /opt/worker happens before the cp.
grep -q 'mkdir -p /opt/worker' "$USER_DATA" \
    && MKDIR_LINE=$(grep -n 'mkdir -p /opt/worker' "$USER_DATA" | head -1 | cut -d: -f1) \
    && CP_LINE=$(grep -n 'cp /mnt/broker/logs/worker-poller.py' "$USER_DATA" | head -1 | cut -d: -f1) \
    && [ -n "$CP_LINE" ] && [ "$MKDIR_LINE" -lt "$CP_LINE" ] \
    && record "1c. mkdir before poller copy" PASS \
    || record "1c. mkdir before poller copy" FAIL

echo ""
echo "=== Task 2: SEV-SNP attestation in discover ==="
DISCOVER=$(curl -sS --max-time 10 "$BROKER/v1/discover" 2>&1)
if echo "$DISCOVER" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if d['attestation']['tee_type']=='amd-sev-snp' else 1)" 2>/dev/null; then
    record "2a. discover has tee_type=amd-sev-snp" PASS
else
    record "2a. discover has tee_type=amd-sev-snp" FAIL "$DISCOVER"
fi
if echo "$DISCOVER" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if 'worker_attested' in d['attestation'] else 1)" 2>/dev/null; then
    record "2b. discover has worker_attested field" PASS
else
    record "2b. discover has worker_attested field" FAIL
fi
# min_measurement should exist as a field (empty when no worker = valid)
if echo "$DISCOVER" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if 'min_measurement' in d['attestation'] else 1)" 2>/dev/null; then
    record "2c. min_measurement field present" PASS
else
    record "2c. min_measurement field present" FAIL
fi

echo ""
echo "=== Task 3: Stripe payment validation ==="
# Submit a job with a test Stripe PI id — should be accepted
RESP=$(curl -sS --max-time 10 -X POST "$BROKER/v1/jobs" \
    -H "Content-Type: application/json" \
    -d '{"client_req_id":"verify-stripe-'$(date +%s)'","encrypted_skill":"test","encrypted_data":"test","requester_sig":"sig","result_pubkey":"pk","stripe_pi_id":"pi_test_verify"}' 2>&1)
if echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if 'job_id' in d else 1)" 2>/dev/null; then
    record "3a. job with stripe_pi_id accepted" PASS
else
    record "3a. job with stripe_pi_id accepted" FAIL "$RESP"
fi
# Submit a job with empty stripe_pi_id — should be rejected
RESP2=$(curl -sS --max-time 10 -X POST "$BROKER/v1/jobs" \
    -H "Content-Type: application/json" \
    -d '{"client_req_id":"verify-stripe-empty-'$(date +%s)'","encrypted_skill":"test","encrypted_data":"test","requester_sig":"sig","result_pubkey":"pk","stripe_pi_id":""}' 2>&1)
HTTP_CODE=$(echo "$RESP2" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if d.get('error') else 1)" 2>/dev/null; echo $?)
if echo "$RESP2" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if 'error' in d else 1)" 2>/dev/null; then
    record "3b. empty stripe_pi_id rejected" PASS
else
    record "3b. empty stripe_pi_id rejected" FAIL "was accepted: $RESP2"
fi

echo ""
echo "=== Task 4: NemoClaw enclave stub in poller ==="
# Poller is now a standalone file (not embedded in user-data)
POLLER="/home/autumn/hermes/competition/tee-broker-deploy/worker/poller.py"
grep -q 'def execute_in_envelope' "$POLLER" \
    && record "4a. poller has execute_in_envelope" PASS \
    || record "4a. poller has execute_in_envelope" FAIL
grep -q 'attestation' "$POLLER" \
    && record "4b. poller result includes attestation" PASS \
    || record "4b. poller result includes attestation" FAIL

echo ""
echo "=== Task 5: started_at in job response ==="
# Submit a new job and check started_at appears after worker assignment
# (For now, just check the completed job has the field — even if null for queued)
JOB_ID=$(cat /tmp/e2e_job_id 2>/dev/null || echo "")
if [ -n "$JOB_ID" ]; then
    RESP=$(curl -sS --max-time 10 "$BROKER/v1/jobs/$JOB_ID" 2>&1)
    if echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if 'started_at' in d else 1)" 2>/dev/null; then
        record "5a. job response has started_at field" PASS
    else
        record "5a. job response has started_at field" FAIL
    fi
else
    record "5a. job response has started_at field" SKIP "no job id"
fi

# Check daemon source has started_at assignment
DAEMON="/home/autumn/hermes/competition/tee-broker-deploy/broker-daemon/daemon.py"
grep -q "started_at" "$DAEMON" \
    && record "5b. daemon source has started_at" PASS \
    || record "5b. daemon source has started_at" FAIL

echo ""
echo "=== Task 6: Three sponsors UI ==="
# Check for a static UI file served by the broker
UI_RESP=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "$BROKER/" 2>&1)
if [ "$UI_RESP" = "200" ] || [ "$UI_RESP" = "301" ] || [ "$UI_RESP" = "302" ]; then
    record "6a. broker serves a root page (HTTP $UI_RESP)" PASS
else
    record "6a. broker serves a root page" FAIL "got HTTP $UI_RESP"
fi
# Check if the UI file exists in the deploy repo
ls /home/autumn/hermes/competition/tee-broker-deploy/broker-daemon/caddy/*html 2>/dev/null || \
ls /home/autumn/hermes/competition/tee-broker-deploy/broker-daemon/static/*html 2>/dev/null || \
ls /home/autumn/hermes/competition/tee-broker-deploy/static/*html 2>/dev/null
if [ -f "/home/autumn/hermes/competition/tee-broker-deploy/broker-daemon/static/index.html" ]; then
    record "6b. static UI file exists" PASS
else
    record "6b. static UI file exists" FAIL
fi

echo ""
echo "=== Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo ""
echo "Ad-hoc verification (live broker API + static source checks)."
echo "Scope: tasks 1-6 (worker robustness, attestation, Stripe, enclave, started_at, UI)"

[ "$FAIL" = "0" ] && exit 0 || exit 1