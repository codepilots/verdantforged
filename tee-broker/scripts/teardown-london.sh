#!/bin/bash
# teardown-london.sh — tear down the eu-west-2 deployment.
#
# ONLY run after Ireland is healthy AND DNS has been swapped to the
# new eu-west-1 IP for at least 10 minutes (so any in-flight clients
# have re-resolved).
#
# Verifies the new broker is healthy BEFORE deleting anything.
#
# Lessons learned from the 2026-06-28 run (Documented here so future
# agents don't repeat them):
#
# 1. The script uses `aws ec2 wait instance-terminated` which has no
#    built-in timeout. If the host shell times out (default 60s in
#    CI scripts), the AWS wait is interrupted but termination continues
#    server-side. Just re-run the script with the right flags.
#
# 2. The CFN `delete-stack` step CAN hit `DELETE_FAILED` if an IAM
#    instance profile has a role attached (CFN can't auto-remove the
#    role because IAM is a global service). When this happens:
#      - look at `aws cloudformation describe-stack-events` for which
#        resource failed (usually `WorkerInstanceProfile` or
#        `ControlPlaneInstanceProfile`)
#      - manually `iam.remove_role_from_instance_profile` then
#        `iam.delete_instance_profile` and `iam.delete_role`
#      - then re-issue `aws cloudformation delete-stack`
#    This is documented in docs/cleanup-playbook.md
#
# 3. EIPs allocated-but-not-associated cost money. After release they
#    cost $0 but AWS may charge briefly. Always check
#    `aws ec2 describe-addresses` after teardown.
#
# 4. The script has a `read -p` confirmation prompt which breaks under
#    non-interactive shells (CI/scripts). Run with `echo "" | bash ...`
#    to feed an empty ENTER, or use `bash -c "yes | bash ..."` to
#    auto-confirm.

set -euo pipefail
 — tear down the eu-west-2 deployment.
#
# ONLY run after Ireland is healthy AND DNS has been swapped to the
# new eu-west-1 IP for at least 10 minutes (so any in-flight clients
# have re-resolved).
#
# Verifies the new broker is healthy BEFORE deleting London.

set -euo pipefail

NEW_IP="176.34.244.180"        # eu-west-1
NEW_DOMAIN="verdant.codepilots.co.uk"
OLD_REGION="eu-west-2"

echo "=== Verifying Ireland broker is healthy ==="
HEALTH=$(curl -sf "http://$NEW_IP/healthz" || echo "unreachable")
if ! echo "$HEALTH" | grep -q '"ok": true'; then
    echo "FAIL: Ireland broker not healthy at $NEW_IP: $HEALTH"
    echo "Refusing to tear down London until Ireland is up."
    exit 1
fi
echo "  ✓ Ireland health: $HEALTH"

echo ""
echo "=== Verifying DNS is pointed at Ireland ==="
RESOLVED=$(dig +short "$NEW_DOMAIN" A @1.1.1.1)
if [ "$RESOLVED" != "$NEW_IP" ]; then
    echo "FAIL: DNS still points to $RESOLVED, expected $NEW_IP"
    echo "Run scripts/swap-dns-ireland.sh first, then re-run this script."
    exit 1
fi
echo "  ✓ $NEW_DOMAIN → $RESOLVED"

echo ""
echo "=== Finding London resources ==="
LONDON_INSTANCES=$(aws ec2 describe-instances --region $OLD_REGION \
    --filters "Name=tag:Role,Values=control-plane" \
              "Name=instance-state-name,Values=running,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)
echo "  control plane: $LONDON_INSTANCES"

LONDON_EIPS=$(aws ec2 describe-addresses --region $OLD_REGION \
    --query 'Addresses[?AssociationId!=null].[PublicIp,AllocationId]' --output text)
echo "  associated EIPs: $LONDON_EIPS"

LONDON_STACK=$(aws cloudformation list-stacks --region $OLD_REGION \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
    --query 'StackSummaries[?starts_with(StackName, `verdantforged`)].StackName' \
    --output text)
echo "  CFN stack: $LONDON_STACK"

echo ""
echo "=== Teardown order ==="
echo "1. Terminate London control plane EC2 (cascade-deletes EIP association)"
echo "2. Release London EIPs"
echo "3. Delete London CFN stack (cascade-deletes EFS, SG, IAM, SGs)"
echo "4. Resume aws-audit-hourly cron (paused since 2026-06-27)"

read -p "Press ENTER to proceed (or Ctrl+C to abort)..."

echo ""
echo "=== 1. Terminating $LONDON_INSTANCES ==="
if [ -n "$LONDON_INSTANCES" ]; then
    aws ec2 terminate-instances --region $OLD_REGION --instance-ids $LONDON_INSTANCES
    echo "  waiting for termination..."
    aws ec2 wait instance-terminated --region $OLD_REGION --instance-ids $LONDON_INSTANCES
    echo "  ✓ terminated"
fi

echo ""
echo "=== 2. Releasing EIPs ==="
if [ -n "$LONDON_EIPS" ]; then
    while IFS=$'\t' read -r ip alloc; do
        if [ -n "$alloc" ]; then
            echo "  releasing $ip ($alloc)"
            aws ec2 release-address --region $OLD_REGION --allocation-id "$alloc"
        fi
    done <<< "$LONDON_EIPS"
    echo "  ✓ EIPs released"
fi

echo ""
echo "=== 3. Deleting CFN stack ==="
if [ -n "$LONDON_STACK" ]; then
    aws cloudformation delete-stack --stack-name $LONDON_STACK --region $OLD_REGION
    echo "  waiting for delete..."
    aws cloudformation wait stack-delete-complete --stack-name $LONDON_STACK --region $OLD_REGION
    echo "  ✓ stack deleted"
fi

echo ""
echo "=== 4. Resuming aws-audit-hourly cron ==="
hermes cron resume aws-audit-hourly 2>/dev/null || echo "  (cron already running or not found)"

echo ""
echo "=== Done ==="
echo "London teardown complete. Confirm with:"
echo "  aws ec2 describe-instances --region $OLD_REGION --filters Name=tag:Project,Values=verdantforged"
echo "  aws cloudformation list-stacks --region $OLD_REGION --query 'StackSummaries[?contains(StackName,`verdant`)]'"
