# The Hardcoded Instance ID Pitfall

## Problem
Hardcoding EC2 instance IDs in scripts (like `i-0f3fcdb4c3561baf6`) creates brittle automation that breaks when:
- Instances are replaced (via CloudFormation updates, Auto Scaling, or manual replacement)
- Environments are cloned (dev/staging/prod)
- Disaster recovery scenarios occur
- Instances are stopped/started and get new IDs (though rare with EBS-backed)

## Symptoms
- "InvalidInstanceID.NotFound" errors when running diagnostic scripts
- Scripts that worked yesterday suddenly fail today
- Confusion when troubleshooting because the script checks the wrong instance
- False negatives in monitoring/alerting systems

## Root Cause
EC2 instance IDs are ephemeral identifiers tied to a specific virtual machine allocation. When that VM is terminated and a new one launched (even with identical configuration), it gets a new ID.

## Correct Approaches

### 1. Tag-Based Lookup (Recommended for most cases)
Use AWS resource tags to identify instances by purpose/role:

```bash
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=verdantforged-ec2" \
            "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" \
  --output text)
```

Advantages:
- Works across environment rebuilds
- Survives instance replacement
- Human-readable intent

Considerations:
- Requires consistent tagging strategy
- Returns arbitrary match if multiple instances share tags (add more filters to disambiguate)

### 2. CloudFormation Outputs (Best for infrastructure-as-code workflows)
If the instance was launched via CloudFormation, query the stack outputs:

```bash
INSTANCE_ID=$(aws cloudformation describe-stacks \
  --stack-name verdantforged-ec2 \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" \
  --output text)
```

Advantages:
- Directly tied to your IaC source of truth
- No ambiguity about which instance (if stack has 1:1 instance mapping)
- Works even if tags are missing or inconsistent

Considerations:
- Requires knowing the exact stack name
- Only works for resources managed by CloudFormation

### 3. Systems Manager Inventory (Advanced)
For large fleets, use SSM Inventory to query instances by application/components:

```bash
INSTANCE_ID=$(aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=*" \
  --query "InstanceInformationList[?Contains(AgentVersion, 'verdantforged')].InstanceId" \
  --output text)
```

## Verification Pattern
Always validate your lookup returned a sensible result:

```bash
if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "(none)" || "$INSTANCE_ID" == "None" ]]; then
  echo "Error: Could not find verdantforged-ec2 instance" >&2
  exit 1
fi

echo "Found instance: $INSTANCE_ID"
```

## Prevention
- Never hardcode resource IDs in scripts that should be reusable
- Use infrastructure-as-code tools (CloudFormation, Terraform) as source of truth
- Tag resources consistently with purpose/environment/owner
- Parameterize scripts to accept instance ID or stack name as arguments
- Add validation checks after lookups

## Related AWS Documentation
- [Tagging Your Amazon EC2 Resources](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Using_Tags.html)
- [Working with Stacks](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cs-describing-stacks.html)
- [Systems Manager Inventory](https://docs.aws.amazon.com/systems-manager/latest/userguide/system-inventory.html)