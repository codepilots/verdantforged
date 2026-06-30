# VerdantForged Cleanup Playbook

When a CFN stack fails mid-deploy, several AWS resources don't get cleaned
up by CFN's automatic rollback. This playbook documents the manual cleanup
needed before retrying the deploy.

## Background

CFN rollback has known gaps: IAM roles + instance profiles, and sometimes
S3 buckets + EFS, persist after the stack is deleted. CFN will fail to
recreate these with the same name because the resources "already exist".

Symptoms in deploy.sh output:
  * `Waiter ChangeSetCreateComplete failed: ... ResourceExistenceCheck`
  * Stack events show `CREATE_FAILED — Resource already exists`

## Cleanup steps (eu-west-1 example — adapt the region)

### 1. Delete any leftover CFN stacks
```bash
aws cloudformation delete-stack --stack-name <name> --region eu-west-1
```

### 2. Wait for stack deletion to complete
```bash
aws cloudformation describe-stacks --stack-name <name> --region eu-west-1 \
    --query 'Stacks[0].StackStatus'
```

### 3. Manually empty + delete S3 bucket (CFN doesn't always delete the
   bucket because it might still hold objects from a successful partial
   create earlier)
```python
import boto3
s3 = boto3.client('s3', region_name='eu-west-1')
bucket = 'verdantforged-artifacts-eu-west-1'
try:
    s3.head_bucket(Bucket=bucket)
    paginator = s3.get_paginator('list_object_versions')
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get('Versions', []):
            s3.delete_object(Bucket=bucket, Key=obj['Key'], VersionId=obj['VersionId'])
        for obj in page.get('DeleteMarkers', []):
            s3.delete_object(Bucket=bucket, Key=obj['Key'], VersionId=obj['VersionId'])
    s3.delete_bucket(Bucket=bucket)
except s3.exceptions.ClientError as e:
    if e.response['Error']['Code'] not in ('NoSuchBucket', '404'):
        raise
```

### 4. Delete orphan IAM instance profiles + roles
CFN creates IAM resources with stable suffixes (e.g.
`verdantforged-broker-control-ControlPlaneInstanceProfile-G36mIziv2IxZ`).
When the stack fails and CFN can't reach them during rollback, they
persist forever.

```python
import boto3
iam = boto3.client('iam')

# Get current orphan names first
profiles, roles = [], []
paginator = iam.get_paginator('list_instance_profiles')
for page in paginator.paginate(PathPrefix='/'):
    for p in page['InstanceProfiles']:
        if 'verdant' in p['InstanceProfileName'].lower():
            profiles.append(p['InstanceProfileName'])
paginator = iam.get_paginator('list_roles')
for page in paginator.paginate(PathPrefix='/'):
    for r in page['Roles']:
        if 'verdant' in r['RoleName'].lower():
            roles.append(r['RoleName'])

# Detach all policies + delete inline policies
for r in roles:
    for p in iam.get_paginator('list_attached_role_policies').paginate(RoleName=r):
        for ap in p['AttachedPolicies']:
            iam.detach_role_policy(RoleName=r, PolicyArn=ap['PolicyArn'])
    for p in iam.get_paginator('list_role_policies').paginate(RoleName=r):
        for ip in p['PolicyNames']:
            iam.delete_role_policy(RoleName=r, PolicyName=ip)

# Remove roles from profiles, then delete profiles
for p in profiles:
    prof = iam.get_instance_profile(InstanceProfileName=p)
    for r in prof['InstanceProfile'].get('Roles', []):
        iam.remove_role_from_instance_profile(InstanceProfileName=p, RoleName=r['RoleName'])
    iam.delete_instance_profile(InstanceProfileName=p)

# Delete roles
for r in roles:
    iam.delete_role(RoleName=r)
```

### 5. EFS — usually cleaned by CFN cascade, but verify:
```bash
aws efs describe-file-systems --region eu-west-1 \
    --query 'FileSystems[].FileSystemId'
```

### 6. EIPs — released by CFN, but verify:
```bash
aws ec2 describe-addresses --region eu-west-1 \
    --query 'Addresses[?AssociationId==null].PublicIp'
```

## Prevention

The cleanest way to avoid this is to deploy with `--disable-rollback`
(see `aws cloudformation create-stack`) so failures leave the stack
visible and the orphans are clearly identified by name. Add this to
`deploy.sh`'s `aws cloudformation deploy` → use `create-stack` +
`execute-change-set` instead so we get a real stack ID and detailed
events.

## Recorded cleanup runs

### 2026-06-28 — First eu-west-1 redeploy attempt

Triggered by London → Ireland migration (eu-west-2 doesn't support AMD
SEV-SNP). Sequence of failures and cleanups:

1. `cfn-lint` E3001 on `STRIPE_SECRET_KEY` parameter — moved into
   `Parameters:` block, updated Ref syntax. (Fixed in this session.)
2. `cfn-lint` exit code 4 from W3037 (HeadObject false positive) —
   deploy.sh treated any non-zero exit as fatal. Replaced
   `if ! cfn-lint` with the same `grep -q "^E[0-9]"` pattern used for
   the worker template. (Fixed in this session.)
3. `ResourceExistenceCheck` from orphan stack
   `verdantforged-broker-control-001` (UPDATE_COMPLETE) holding the
   S3 bucket `verdantforged-artifacts-eu-west-1`. Deleted stack +
   emptied + deleted bucket.
4. Same `ResourceExistenceCheck` from orphan IAM instance profile
   `verdantforged-broker-control-worker-instance-profile` and three
   other roles/profiles with CFN stable suffixes. Detached policies,
   removed roles from profiles, deleted profiles, deleted roles.

After cleanup, retry deploy.sh — should succeed.
