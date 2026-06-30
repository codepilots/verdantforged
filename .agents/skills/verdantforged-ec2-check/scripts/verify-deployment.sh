#!/bin/bash
# VerdantForged EC2 Deployment Verification Script
# Usage: ./verify-deployment.sh [stack-name] [region]
# Defaults: stack-name=verdantforged-ec2, region=eu-west-2

set -euo pipefail

STACK_NAME="${1:-verdantforged-ec2}"
REGION="${2:-eu-west-2}"

export AWS_DEFAULT_REGION="$REGION"

echo "=== Verifying VerdantForged EC2 Deployment ==="
echo "Stack: $STACK_NAME"
echo "Region: $REGION"
echo ""

# 1. Check if stack exists
echo "1. Checking CloudFormation stack..."
STACK_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].StackStatus' \
  --output text 2>/dev/null) || {
    echo "❌ Stack '$STACK_NAME' not found in region $REGION"
    echo "   Available stacks:"
    aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
      --query 'StackSummaries[].StackName' --output text | tr '\t' '\n' | grep -v '^$'
    exit 1
}
echo "   Stack status: $STACK_STATUS"

# 2. Get instance ID from stack outputs
echo ""
echo "2. Retrieving instance ID from stack outputs..."
INSTANCE_ID=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue' \
  --output text)
if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
    echo "❌ Could not find InstanceId in stack outputs"
    exit 1
fi
echo "   Instance ID: $INSTANCE_ID"

# 3. Get public IP from stack outputs
echo ""
echo "3. Retrieving public IP from stack outputs..."
PUBLIC_IP=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey==`PublicIP`].OutputValue' \
  --output text)
if [[ -z "$PUBLIC_IP" || "$PUBLIC_IP" == "None" ]]; then
    echo "⚠️  Could not find PublicIP in stack outputs (may be using SSM only)"
else
    echo "   Public IP: $PUBLIC_IP"
fi

# 4. Check instance state
echo ""
echo "4. Checking EC2 instance state..."
INSTANCE_STATE=$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].State.Name' \
  --output text)
if [[ "$INSTANCE_STATE" != "running" ]]; then
    echo "❌ Instance is not running (state: $INSTANCE_STATE)"
    exit 1
fi
echo "   ✓ Instance is running"

# 5. Check SSM connectivity
echo ""
echo "5. Checking SSM connectivity..."
PING_STATUS=$(aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
  --query 'InstanceInformationList[0].PingStatus' \
  --output text)
if [[ "$PING_STATUS" != "Online" ]]; then
    echo "❌ SSM agent not online (status: $PING_STATUS)"
    exit 1
fi
echo "   ✓ SSM agent is online"

# 6. Run diagnostic commands via SSM
echo ""
echo "6. Running diagnostic commands via SSM..."
CMD_ID=$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":[
    "echo === NemoClaw Setup Log ===",
    "cat /var/log/nemoclaw-setup.log 2>/dev/null || echo NO_LOG",
    "echo ---",
    "echo === Cloud-Init Marker ===",
    "cat /var/log/cloud-init-verdantforged.marker 2>/dev/null || echo NO_MARKER",
    "echo ---",
    "echo === NemoHermes Process ===",
    "ps aux | grep -i nemo | grep -v grep || echo NO_NEMO",
    "echo ---",
    "echo === Docker Containers ===",
    "docker ps 2>/dev/null || echo NO_DOCKER",
    "echo ---",
    "echo === NemoHermes CLI ===",
    "which nemohermes 2>/dev/null || echo NO_NEMOHERMES",
    "echo ---",
    "echo === Sandbox Status ===",
    "nemohermes verdantforged status 2>/dev/null || echo SANDBOX_NOT_FOUND",
    "echo ---",
    "echo === Docker Images (if any) ===",
    "docker images 2>/dev/null | head -5 || echo NO_DOCKER
  "]}' \
  --query Command.CommandId \
  --output text)

echo "   Command ID: $CMD_ID"
echo "   Waiting for command to complete..."
sleep 10

OUTPUT=$(aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "$INSTANCE_ID" \
  --query "StandardOutputContent" \
  --output text)

STATUS=$(aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "$INSTANCE_ID" \
  --query "Status" \
  --output text)

echo ""
echo "=== Diagnostic Output ==="
echo "$OUTPUT"
echo "========================"

if [[ "$STATUS" != "Success" ]]; then
    echo "⚠️  SSM command did not complete successfully (status: $STATUS)"
else
    echo "✓ SSM command completed successfully"
fi

echo ""
echo "=== Verification Complete ==="
echo "To connect via SSM:"
echo "  aws ssm start-session --target $INSTANCE_ID --region $REGION"
if [[ -n "$PUBLIC_IP" && "$PUBLIC_IP" != "None" ]]; then
    echo "To connect via SSH (if key pair was provided):"
    echo "  ssh ubuntu@$PUBLIC_IP"
fi