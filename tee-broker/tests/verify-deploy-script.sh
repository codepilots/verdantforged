#!/bin/bash
# Verify the deploy.sh and bootstrap-control-plane.sh fixes.
# Checks:
#   1. deploy.sh syntax is valid
#   2. bootstrap-control-plane.sh syntax is valid
#   3. CFN has WorkerInstanceProfileArn output
#   4. CFN user-data has DEMO_TOKEN_CAP and LLM config
#   5. bootstrap pushes poller.py to EFS and keeps LLM key broker-side
#   6. bootstrap mounts EFS if not mounted
#   7. bootstrap uses CFN output for config.env
#   8. deploy.sh uses boto3 instead of aws ssm send-command
#   9. deploy.sh chunks tarball for large payloads
#  10. daemon.py reads both BROKER_WORKER_SG and BROKER_WORKER_SG_ID

set -uo pipefail

PASS=0
FAIL=0
check() {
    local label="$1" condition="$2"
    if eval "$condition" 2>/dev/null; then
        echo "[PASS] $label"
        PASS=$((PASS+1))
    else
        echo "[FAIL] $label"
        FAIL=$((FAIL+1))
    fi
}

REPO=$(cd "$(dirname "$0")/.." && pwd)

echo "=== Static checks ==="
check "1. deploy.sh has valid bash syntax" "bash -n '$REPO/deploy.sh'"
check "2. bootstrap-control-plane.sh has valid bash syntax" "bash -n '$REPO/scripts/bootstrap-control-plane.sh'"
check "3. CFN exports WorkerInstanceProfileArn" "grep -q 'WorkerInstanceProfileArn' '$REPO/cloudformation-control-plane.yaml'"
check "4. CFN user-data has DEMO_TOKEN_CAP" "grep -q 'DEMO_TOKEN_CAP' '$REPO/cloudformation-control-plane.yaml'"
check "5. CFN user-data has BROKER_LLM_BASE_URL" "grep -q 'BROKER_LLM_BASE_URL' '$REPO/cloudformation-control-plane.yaml'"
check "6. bootstrap pushes worker-poller.py to EFS" "grep -q 'poller.py.*BR_EFS/logs' '$REPO/scripts/bootstrap-control-plane.sh' || grep -q 'install.*poller.py.*BR_EFS/logs' '$REPO/scripts/bootstrap-control-plane.sh'"
check "7. bootstrap keeps upstream LLM key broker-side" "grep -q 'BROKER_LLM_API_KEY=' '$REPO/scripts/bootstrap-control-plane.sh' && grep -q 'preserve_existing_config_var BROKER_LLM_API_KEY' '$REPO/scripts/bootstrap-control-plane.sh' && grep -q 'no llm-api-key on EFS' '$REPO/scripts/bootstrap-control-plane.sh'"
check "7b. bootstrap fetches LLM key from SSM when env is empty" "grep -q '/verdantforged/broker/llm-api-key' '$REPO/scripts/bootstrap-control-plane.sh' && grep -q 'BROKER_LLM_API_KEY fetched from SSM' '$REPO/scripts/bootstrap-control-plane.sh'"
check "7c. deploy persists LLM key to SSM and clears bootstrap env copy" "grep -q '/verdantforged/broker/llm-api-key' '$REPO/deploy.sh' && grep -q 'unset BROKER_LLM_API_KEY' '$REPO/deploy.sh'"
check "7d. daemon reports missing LLM key before upstream call" "grep -q 'llm_upstream_not_configured' '$REPO/broker-daemon/daemon.py'"
check "8. bootstrap mounts EFS if not already mounted" "grep -q 'mount | grep.*BR_EFS' '$REPO/scripts/bootstrap-control-plane.sh'"
check "9. bootstrap generates config.env from CFN outputs" "test -n \"\$(grep -F 'CFN_OUTPUTS[WorkerInstanceProfileArn]' '$REPO/scripts/bootstrap-control-plane.sh')\""
check "10. deploy.sh uses boto3 for SSM" "grep -q 'import boto3' /tmp/_verdantforged_push.py 2>/dev/null || grep -q 'boto3.client.ssm' '$REPO/deploy.sh' || grep -q '_verdantforged_push.py' '$REPO/deploy.sh'"
check "11. deploy.sh chunks tarball for SSM payload limit" "grep -q 'CHUNK' '$REPO/deploy.sh'"
check "12. daemon.py reads both BROKER_WORKER_SG and BROKER_WORKER_SG_ID" "grep -q 'BROKER_WORKER_SG_ID.*BROKER_WORKER_SG' '$REPO/broker-daemon/daemon.py'"
check "13. Caddyfile has :80 fallback for direct EIP access" "grep -q '^:80' '$REPO/broker-daemon/caddy/Caddyfile'"
check "14. poller.py uses private IP for LLM proxy URL" "grep -q 'BROKER_CONTROL_PLANE_URL' '$REPO/worker/poller.py'"
check "15. SG opens ports 80 and 443 in CFN" "grep -q 'FromPort: 80' '$REPO/cloudformation-control-plane.yaml' && grep -q 'FromPort: 443' '$REPO/cloudformation-control-plane.yaml'"
check "16. deploy and bootstrap preserve NemoClaw onboard token" "grep -q '/verdantforged/broker/onboard-token' '$REPO/deploy.sh' && grep -q '/verdantforged/broker/onboard-token' '$REPO/scripts/bootstrap-control-plane.sh' && grep -q 'BROKER_ONBOARD_TOKEN=' '$REPO/scripts/bootstrap-control-plane.sh'"
check "17. deploy defaults to gold worker AMI and pushes it into config" "grep -q 'ami-099e2272620073023' '$REPO/deploy.sh' && grep -q 'WorkerAmiId=\$WORKER_AMI_ID' '$REPO/deploy.sh' && grep -q 'export BROKER_WORKER_AMI' '$REPO/deploy.sh'"
check "18. CFN lets control plane read broker SecureString config" "grep -q '/verdantforged/broker/stripe-secret-key' '$REPO/cloudformation-control-plane.yaml' && grep -q '/verdantforged/broker/llm-api-key' '$REPO/cloudformation-control-plane.yaml' && grep -q '/verdantforged/broker/onboard-token' '$REPO/cloudformation-control-plane.yaml'"
check "19. deploy refreshes metadata on already-running workers" "grep -q 'REFRESH_EXISTING_WORKERS' '$REPO/deploy.sh' && grep -q 'openshell.ai/sandbox-name' '$REPO/deploy.sh' && grep -q '.nemoclaw_metadata' '$REPO/deploy.sh'"

echo ""
echo "=== Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo "Ad-hoc verification only (static checks against source files)."
echo "Scope: deploy.sh + bootstrap-control-plane.sh bug fixes"

[ "$FAIL" = "0" ] && exit 0 || exit 1