#!/bin/bash
# Ad-hoc verification for worker/user-data.sh robustness + standalone poller.
# Tests:
#   1. Script does NOT use `set -e` (the bug that killed cloud-init)
#   2. mkdir /opt/worker happens BEFORE cp /mnt/broker/logs/worker-poller.py
#   3. apt-get has retry logic
#   4. EFS mount failure is fatal (exits 1)
#   5. Each step logs to marker file
#   6. Standalone poller includes job_id in outbox
#   7. Poller has execute_in_envelope (not just process())
#   8. Poller has SEV-SNP measurement function
#   9. Poller produces valid outbox JSON with job_id when run against mock dirs

set -uo pipefail

WORK=$(mktemp -d -t hermes-verify-worker-XXXXXX)
trap "rm -rf '$WORK'" EXIT
echo "[verify] working dir: $WORK"

USER_DATA="/home/autumn/hermes/competition/tee-broker-deploy/worker/user-data.sh"
POLLER_SRC="/home/autumn/hermes/competition/tee-broker-deploy/worker/poller.py"
if [ ! -f "$USER_DATA" ] || [ ! -f "$POLLER_SRC" ]; then
    echo "[FAIL] source files not found"
    exit 1
fi

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

# 1. No `set -e` (the main bug)
check "1. no set -e at top level" "! grep -q '^set -e' '$USER_DATA'"
check "1b. no set -euo pipefail" "! grep -q 'set -euo pipefail' '$USER_DATA'"
check "1c. uses set -u" "grep -q 'set -u' '$USER_DATA'"

# 2. mkdir /opt/worker before cp poller
MKDIR_LINE=$(grep -n 'mkdir -p /opt/worker' "$USER_DATA" | head -1 | cut -d: -f1)
CP_LINE=$(grep -n 'cp /mnt/broker/logs/worker-poller.py' "$USER_DATA" | head -1 | cut -d: -f1)
check "2. mkdir /opt/worker before cp poller" "[ -n '$MKDIR_LINE' ] && [ -n '$CP_LINE' ] && [ '$MKDIR_LINE' -lt '$CP_LINE' ]"

# 3. apt retry logic
check "3. apt-get has retry loop" "grep -q 'for i in 1 2 3' '$USER_DATA'"

# 4. EFS mount failure is fatal
check "4. EFS mount failure exits 1" "grep -q 'EFS not mounted' '$USER_DATA' && grep -q 'exit 1' '$USER_DATA'"

# 5. Logging
check "5. has log() function" "grep -q 'log()' '$USER_DATA'"
check "5b. logs each step" "grep -c 'log \"step' '$USER_DATA' | grep -q '[5-9]'"

# 6-8. Poller checks (use standalone file, not embedded)
check "6. poller has job_id in outbox_payload" "grep -q 'outbox_payload.*job_id' '$POLLER_SRC'"
check "7. poller has execute_in_envelope" "grep -q 'def execute_in_envelope' '$POLLER_SRC'"
check "8. poller has get_sev_snp_measurement" "grep -q 'def get_sev_snp_measurement' '$POLLER_SRC'"

# 9. Functional test: copy poller to temp, run against mock dirs
cp "$POLLER_SRC" "$WORK/poller.py"
POLLER_PY="$WORK/poller.py"
check "9a. poller copied" "[ -s '$POLLER_PY' ]"

if [ -s "$POLLER_PY" ]; then
    MOCK_ROOT="$WORK/broker"
    INBOX="$MOCK_ROOT/jobs/inbox"
    OUTBOX="$MOCK_ROOT/jobs/outbox"
    LOGS="$MOCK_ROOT/logs"
    mkdir -p "$INBOX" "$OUTBOX" "$LOGS"

    # Patch paths
    sed -i "s|/mnt/broker/jobs/inbox|$INBOX|g" "$POLLER_PY"
    sed -i "s|/mnt/broker/jobs/outbox|$OUTBOX|g" "$POLLER_PY"
    sed -i "s|/mnt/broker/logs/worker-heartbeat.json|$LOGS/worker-heartbeat.json|g" "$POLLER_PY"
    sed -i "s|/mnt/broker/logs/llm-api-key|$LOGS/llm-api-key|g" "$POLLER_PY"

    # Pre-create heartbeat
    echo '{"instance_id":"i-test","status":"starting"}' > "$LOGS/worker-heartbeat.json"

    # Write test job
    JOB_ID="job_verify_robust_001"
    cat > "$INBOX/${JOB_ID}.json" <<EOF
{"job_id":"$JOB_ID","encrypted_skill":"summarize","encrypted_data":"hello world test"}
EOF

    # Run poller (no LLM key on EFS so it should fall back gracefully)
    timeout 3 python3 "$POLLER_PY" >/dev/null 2>&1 || true

    OUTBOX_FILE="$OUTBOX/${JOB_ID}.json"
    check "9b. outbox file created" "[ -f '$OUTBOX_FILE' ]"

    if [ -f "$OUTBOX_FILE" ]; then
        check "9c. outbox has job_id" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); exit(0 if d.get('job_id')=='$JOB_ID' else 1)\""
        check "9d. outbox has state" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); exit(0 if d.get('state') in ('completed','failed') else 1)\""
        check "9e. outbox has attestation" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); exit(0 if 'attestation' in d.get('result',{}) else 1)\""
        check "9f. attestation has measurement" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); a=d['result']['attestation']; exit(0 if 'measurement' in a and len(a['measurement'])>0 else 1)\""
        check "9g. attestation has tee_type" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); a=d['result']['attestation']; exit(0 if a.get('tee_type')=='amd-sev-snp' else 1)\""
    fi
fi

echo ""
echo "=== Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo "Ad-hoc verification only (static checks + mock poller run, no live AWS)."
echo "Scope: worker/user-data.sh robustness + standalone poller behavior"

[ "$FAIL" = "0" ] && exit 0 || exit 1