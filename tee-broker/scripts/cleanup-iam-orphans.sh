#!/bin/bash
# cleanup-iam-orphans.sh — clean up IAM instance profiles + roles
# left behind when a CFN delete fails on the WorkerInstanceProfile /
# ControlPlaneInstanceProfile resource.
#
# Pattern from docs/cleanup-playbook.md. Use when
# `aws cloudformation delete-stack` returns DELETE_FAILED with
# reason "must remove roles from instance profile first".

set -euo pipefail

REGION="${AWS_REGION:-eu-west-2}"
PROFILE_PATTERN="${PROFILE_PATTERN:-verdantforged}"

echo "=== Listing verdantforged instance profiles ==="
PROFILES=$(aws iam list-instance-profiles --output text \
    --query "InstanceProfiles[?contains(InstanceProfileName, \`$PROFILE_PATTERN\`)].InstanceProfileName")

for pname in $PROFILES; do
    echo ""
    echo "--- $pname ---"
    # List roles on this profile
    ROLES=$(aws iam get-instance-profile --instance-profile-name "$pname" \
        --query 'InstanceProfile.Roles[].RoleName' --output text)
    for rname in $ROLES; do
        echo "  removing role $rname"
        aws iam remove-role-from-instance-profile \
            --instance-profile-name "$pname" --role-name "$rname"
    done
    echo "  deleting instance profile $pname"
    aws iam delete-instance-profile --instance-profile-name "$pname"
done

echo ""
echo "=== Listing verdantforged roles ==="
ROLES=$(aws iam list-roles --output text \
    --query "Roles[?contains(RoleName, \`$PROFILE_PATTERN\`)].RoleName")

for rname in $ROLES; do
    echo ""
    echo "--- $rname ---"
    # Detach managed policies
    for arn in $(aws iam list-attached-role-policies --role-name "$rname" \
                --query 'AttachedPolicies[].PolicyArn' --output text); do
        echo "  detaching $arn"
        aws iam detach-role-policy --role-name "$rname" --policy-arn "$arn"
    done
    # Delete inline policies
    for pname in $(aws iam list-role-policies --role-name "$rname" \
                  --query 'PolicyNames' --output text); do
        echo "  deleting inline $pname"
        aws iam delete-role-policy --role-name "$rname" --policy-name "$pname"
    done
    echo "  deleting role $rname"
    aws iam delete-role --role-name "$rname"
done

echo ""
echo "=== Done. Now retry: aws cloudformation delete-stack --stack-name <name> --region $REGION ==="
