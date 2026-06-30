#!/bin/bash
# VerdantForged TEE Broker — one-shot deploy.
# Order of operations:
#   1. Validate prerequisites (AWS CLI, jq, cfn-lint optional).
#   2. Deploy control-plane CloudFormation stack (creates t3.small + EFS + EIP + IAM + SG).
#   3. Wait for stack CREATE_COMPLETE.
#   4. Push repo tarball to control-plane instance via SSM (boto3 — aws-cli 2.31.35
#      has a "badly formed help string" bug on ssm send-command).
#   5. Run bootstrap-control-plane.sh on the instance via SSM (also boto3).
#   6. Smoke-test public endpoint.
#
# Workers in production are NOT created via this script — they're launched
# by the broker-daemon via boto3 run-instances when the first job arrives.
#
# Region: eu-west-1 (Ireland) is the default because it supports AMD SEV-SNP
# on m6a.*/c6a.* instance types, enabling real hardware attestation in /v1/discover.
# Use REGION=eu-west-2 (or any other) to deploy elsewhere; SEV-SNP will be auto-
# skipped if the region doesn't support it (BROKER_ENABLE_SEV_SNP=0).
set -euo pipefail

REGION="${REGION:-eu-west-1}"
STACK_NAME="${STACK_NAME:-verdantforged-broker-control}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.small}"
WORKER_INSTANCE_TYPE="${WORKER_INSTANCE_TYPE:-m6a.xlarge}"
GOLD_WORKER_AMI="ami-099e2272620073023"  # eu-west-1 NemoClaw/OpenShell cached worker, baked 2026-06-30
WORKER_AMI_ID="${WORKER_AMI_ID:-${BROKER_WORKER_AMI:-$GOLD_WORKER_AMI}}"
DOMAIN_NAME="${DOMAIN_NAME:-verdant.codepilots.co.uk}"
ADMIN_IP="${ADMIN_IP:-$(curl -s -4 ifconfig.me)/32}"
KEY_NAME="${KEY_NAME:-}"
VPC_ID="${VPC_ID:-}"
SUBNET_ID="${SUBNET_ID:-}"
IDLE_BUFFER_MINUTES="${IDLE_BUFFER_MINUTES:-10}"
# Skills registration API key (VULN-S2). Empty = let bootstrap generate one.
SKILLS_API_KEY="${SKILLS_API_KEY:-}"
# Plaintext-output redaction (VULN-S3). Default 1 for hackathon demo.
KEEP_PLAINTEXT_FOR_DEMO="${KEEP_PLAINTEXT_FOR_DEMO:-1}"

# LLM defaults — Ollama OpenAI-compatible endpoint. Override via env to
# switch providers (Gemini, OpenAI, NVIDIA endpoints, etc.).
BROKER_LLM_API_KEY="${BROKER_LLM_API_KEY:-}"
BROKER_LLM_BASE_URL="${BROKER_LLM_BASE_URL:-https://ollama.com/v1}"
BROKER_LLM_MODEL="${BROKER_LLM_MODEL:-minimax-m3:cloud}"
DEMO_TOKEN_CAP="${DEMO_TOKEN_CAP:-50000}"
# Dedicated non-job token used only for NemoClaw onboarding validation against
# the broker LLM proxy. If set, deploy.sh persists it to SSM so future clean
# control-plane bootstraps can rebuild config.env without manual secret entry.
# If unset on a clean deploy, bootstrap-control-plane.sh generates one locally
# on the control plane. Never logged or written to EFS.
BROKER_ONBOARD_TOKEN="${BROKER_ONBOARD_TOKEN:-}"
# Stripe PaymentIntent lifecycle (t_9fbec867). Leave empty to run in
# DEMO MODE (format validation only). Set to a sk_test_... key on the
# deploy host to wire real capture/refund. Never logged, never echoed
# in summaries.
#
# Persistence: when this env var is non-empty, deploy.sh writes it to
# SSM Parameter Store at /verdantforged/broker/stripe-secret-key
# (SecureString, KMS-encrypted) so future bootstrap runs (instance
# rebuild, broker restart, multi-region) pick it up without re-specifying.
# The IAM role on the control plane has ssm:GetParameter scoped to that
STRIPE_SECRET_KEY="${STRIPE_SECRET_KEY:-}"
# Stripe ACS merchant profile/network id for HTTP 402 MPP challenges.
STRIPE_NETWORK_ID="${STRIPE_NETWORK_ID:-profile_test_61UxANkhMMDN58EdHA6UxANjDhE9lwNXZMFWQJI6y3Mu}"
BROKER_PAYMENT_STUB_MODE="${BROKER_PAYMENT_STUB_MODE:-0}"
BROKER_ALLOW_STUB_WORKER_ATTESTATION="${BROKER_ALLOW_STUB_WORKER_ATTESTATION:-}"
# Deploy normally only updates the control plane + EFS bootstrap source. Existing
# live worker instances do not rerun user-data, so metadata captured by older
# worker-bootstrap.sh versions (notably NemoClaw image digest) can stay stale.
# Refresh their local /opt/worker/.nemoclaw_metadata and EFS worker-keys.json
# after pushing a new bootstrap unless explicitly disabled.
REFRESH_EXISTING_WORKERS="${REFRESH_EXISTING_WORKERS:-1}"

python3 -c "import boto3" 2>/dev/null || {
    echo "[deploy] boto3 not installed locally — falling back to venv at /tmp/ssmvenv"
    [ -x /tmp/ssmvenv/bin/python ] || {
        python3 -m venv /tmp/ssmvenv
        /tmp/ssmvenv/bin/pip install --quiet boto3
    }
    PYTHON="/tmp/ssmvenv/bin/python"
}
[ -z "${PYTHON:-}" ] && PYTHON="$(command -v python3)"

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

log() { printf "\033[1;34m[deploy]\033[0m %s\n" "$*"; }
die() { printf "\033[1;31m[deploy]\033[0m %s\n" "$*" >&2; exit 1; }

# ---- 1. Prerequisites ----
command -v aws >/dev/null || die "aws CLI not installed"
command -v jq >/dev/null || die "jq not installed (brew install jq / apt install jq)"
aws sts get-caller-identity >/dev/null || die "AWS credentials not configured"

if [ -z "$VPC_ID" ]; then
    VPC_ID=$(aws ec2 describe-vpcs --region "$REGION" --query "Vpcs[?IsDefault==\`true\`].VpcId" --output text)
    [ -n "$VPC_ID" ] || die "could not find default VPC in $REGION"
    log "using default VPC: $VPC_ID"
fi

if [ -z "$SUBNET_ID" ]; then
    SUBNET_ID=$(aws ec2 describe-subnets --region "$REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
        --query "Subnets[0].SubnetId" --output text)
    [ -n "$SUBNET_ID" ] || die "no subnets in VPC $VPC_ID"
    log "using first subnet: $SUBNET_ID"
fi

AMI_ID=$(aws ec2 describe-images --owners 099720109477 --region "$REGION" \
    --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
              "Name=state,Values=available" \
    --query "Images | sort_by(@, &CreationDate) | [-1].ImageId" --output text)
[ -n "$AMI_ID" ] || die "could not find Ubuntu 24.04 AMI in $REGION"
log "Ubuntu 24.04 AMI: $AMI_ID"

log "linting templates"
command -v cfn-lint >/dev/null && {
    CP_LINT=$(cfn-lint "$REPO_DIR/cloudformation-control-plane.yaml" 2>&1 || true)
    if echo "$CP_LINT" | grep -q "^E[0-9]"; then
        echo "$CP_LINT"
        die "control-plane CFN has errors"
    fi
    [ -n "$CP_LINT" ] && echo "$CP_LINT" | sed 's/^/[deploy]   lint: /'
    WORKER_LINT=$(cfn-lint "$REPO_DIR/cloudformation-worker.yaml" 2>&1 || true)
    if echo "$WORKER_LINT" | grep -q "^E[0-9]"; then
        echo "$WORKER_LINT"
        die "worker CFN has errors (warnings OK)"
    fi
    [ -n "$WORKER_LINT" ] && echo "$WORKER_LINT" | sed 's/^/[deploy]   lint: /'
} || log "cfn-lint not installed — skipping"

# ---- 2. Deploy control plane stack ----
log "deploying control plane (stack=$STACK_NAME, region=$REGION)"
aws cloudformation deploy \
    --template-file "$REPO_DIR/cloudformation-control-plane.yaml" \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --parameter-overrides \
        "DomainName=$DOMAIN_NAME" \
        "AdminIP=$ADMIN_IP" \
        "InstanceType=$INSTANCE_TYPE" \
        "VpcId=$VPC_ID" \
        "SubnetId=$SUBNET_ID" \
        "KeyName=$KEY_NAME" \
        "AmiId=$AMI_ID" \
        "WorkerInstanceType=$WORKER_INSTANCE_TYPE" \
        "WorkerAmiId=$WORKER_AMI_ID" \
        "IdleBufferMinutes=$IDLE_BUFFER_MINUTES" \
        "SkillsApiKey=$SKILLS_API_KEY" \
        "KeepPlaintextForDemo=$KEEP_PLAINTEXT_FOR_DEMO" \
        "StripeSecretKey=$STRIPE_SECRET_KEY" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset

log "waiting for stack completion (this can take 3-5 min for the control plane)"
for i in $(seq 1 60); do
    STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "")
    log "  [$(date +%H:%M:%S)] $STATUS"
    case "$STATUS" in
        CREATE_COMPLETE|UPDATE_COMPLETE) break ;;
        CREATE_FAILED|ROLLBACK_*) die "control plane stack failed: $STATUS" ;;
    esac
    sleep 10
done

PUBLIC_IP=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey==\`ControlPlanePublicIP\`].OutputValue" --output text)
INSTANCE_ID=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey==\`ControlPlaneInstanceId\`].OutputValue" --output text)
EFS_ID=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey==\`EfsFileSystemId\`].OutputValue" --output text)
WORKER_SG=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey==\`WorkerSecurityGroupId\`].OutputValue" --output text)
WORKER_ROLE=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey==\`WorkerInstanceProfileArn\`].OutputValue" --output text)

log "control plane up: $INSTANCE_ID @ $PUBLIC_IP (worker instance profile: $WORKER_ROLE)"

# ---- 3. Push repo to the instance via SSM (boto3) ----
# aws-cli 2.31.35 + python 3.14 has a bug that makes `aws ssm send-command`
# fail with "badly formed help string". Bypass by calling boto3 directly.
# Chose exclude=__pycache__/ over a manual rm because the repo picks up new
# __pycache__ entries on every .py import (broker-daemon/daemon.py,
# worker/poller.py, worker/sev_snp.py) — and the SSM SendCommand document size
# limit is 97KB including the base64 payload. 115KB tarball trips MaxDocumentSizeExceeded.
TMP_TAR=$(mktemp -t verdantforged-deploy-XXXXXX.tar.gz)
tar -C "$REPO_DIR" -czf "$TMP_TAR" \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
    broker-daemon/ worker/ scripts/ deploy.sh cloudformation-worker.yaml
SIZE=$(wc -c < "$TMP_TAR")
log "tarball size: $SIZE bytes"

cat > /tmp/_verdantforged_push.py <<PYEOF
import boto3, base64, sys, time
ssm = boto3.client('ssm', region_name=sys.argv[1])
instance_id = sys.argv[2]
b64_path = sys.argv[3]
with open(b64_path) as f:
    b64 = f.read().strip()
CHUNK = 60000  # bytes per chunk — base64 chars per shell command line
chunks = [b64[i:i+CHUNK] for i in range(0, len(b64), CHUNK)]
print(f"total_b64_size={len(b64)} chunks={len(chunks)}")

# Strategy: send ONE SSM command per chunk (so each SendCommand
# request is well under the 97KB Parameters limit). First chunk
# creates /tmp/repo.b64, subsequent chunks append.
for i, c in enumerate(chunks):
    op = '>' if i == 0 else '>>'
    cmds = [f'echo -n "{c}" {op} /tmp/repo.b64']
    r = ssm.send_command(InstanceIds=[instance_id], DocumentName='AWS-RunShellScript',
                         Comment=f'Push chunk {i+1}/{len(chunks)}', Parameters={'commands': cmds})
    cid = r['Command']['CommandId']
    for _ in range(30):
        time.sleep(2)
        try:
            inv = ssm.get_command_invocation(CommandId=cid, InstanceId=instance_id)
            if inv['Status'] != 'InProgress':
                if inv['Status'] != 'Success':
                    print(f"chunk {i+1} failed: {inv['Status']} stderr={inv.get('StandardErrorContent', '')[:200]}")
                    sys.exit(1)
                break
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(2)

# Final command: decode the file + extract tarball
final_cmds = [
    'mkdir -p /home/ubuntu/tee-broker-deploy',
    'base64 -d /tmp/repo.b64 | tar -xz -C /home/ubuntu/tee-broker-deploy/',
    'rm /tmp/repo.b64',
    'ls /home/ubuntu/tee-broker-deploy/',
    'echo push_done',
]
r = ssm.send_command(InstanceIds=[instance_id], DocumentName='AWS-RunShellScript',
                     Comment='Push final: decode + extract', Parameters={'commands': final_cmds})
cid = r['Command']['CommandId']
print(f"final_command={cid}")
for _ in range(60):
    time.sleep(5)
    try:
        inv = ssm.get_command_invocation(CommandId=cid, InstanceId=instance_id)
        if inv['Status'] != 'InProgress':
            print(f"final_status={inv['Status']}")
            print(inv.get('StandardOutputContent', '')[-500:])
            if inv.get('StandardErrorContent'):
                print(f"final_stderr={inv['StandardErrorContent'][-500:]}")
            sys.exit(0 if inv['Status'] == 'Success' else 1)
    except ssm.exceptions.InvocationDoesNotExist:
        time.sleep(2)
sys.exit(1)
PYEOF

base64 -w0 < "$TMP_TAR" > /tmp/_verdantforged_tar.b64
log "pushing tarball to $INSTANCE_ID via boto3"
"$PYTHON" /tmp/_verdantforged_push.py "$REGION" "$INSTANCE_ID" /tmp/_verdantforged_tar.b64 || die "repo push failed"
rm -f /tmp/_verdantforged_tar.b64 /tmp/_verdantforged_push.py "$TMP_TAR"

# ---- 3.5. Persist secrets to SSM Parameter Store (if set) ----
# When the deployer has STRIPE_SECRET_KEY or BROKER_LLM_API_KEY in env,
# write them to SecureString parameters so bootstrap can fetch them on every
# instance rebuild without re-specifying. Do not pass secret material in the
# bootstrap env after persistence.
if [ -n "$STRIPE_SECRET_KEY" ]; then
    log "persisting STRIPE_SECRET_KEY to SSM Parameter Store"
    "$PYTHON" - "$REGION" "$STRIPE_SECRET_KEY" <<'PYEOF' || die "failed to persist Stripe secret to SSM"
import sys, boto3
region = sys.argv[1]
secret = sys.argv[2]
assert secret.startswith("sk_"), "STRIPE_SECRET_KEY must start with sk_"
ssm = boto3.client("ssm", region_name=region)
try:
    ssm.put_parameter(
        Name="/verdantforged/broker/stripe-secret-key",
        Value=secret,
        Type="SecureString",
        Description="VerdantForged broker Stripe secret key (kanban t_9fbec867)",
        Overwrite=True,
    )
    print("[deploy] Stripe secret persisted to /verdantforged/broker/stripe-secret-key")
except Exception as e:
    print(f"[deploy] SSM put_parameter failed: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
    unset STRIPE_SECRET_KEY
    STRIPE_SECRET_KEY=""
fi

if [ -n "$BROKER_LLM_API_KEY" ]; then
    log "persisting BROKER_LLM_API_KEY to SSM Parameter Store"
    "$PYTHON" - "$REGION" "$BROKER_LLM_API_KEY" <<'PYEOF' || die "failed to persist LLM API key to SSM"
import sys, boto3
region = sys.argv[1]
secret = sys.argv[2]
assert secret.strip(), "BROKER_LLM_API_KEY must be non-empty"
ssm = boto3.client("ssm", region_name=region)
try:
    ssm.put_parameter(
        Name="/verdantforged/broker/llm-api-key",
        Value=secret,
        Type="SecureString",
        Description="VerdantForged broker upstream LLM API key",
        Overwrite=True,
    )
    print("[deploy] LLM API key persisted to /verdantforged/broker/llm-api-key")
except Exception as e:
    print(f"[deploy] SSM put_parameter failed: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
    unset BROKER_LLM_API_KEY
    BROKER_LLM_API_KEY=""
fi


if [ -n "$BROKER_ONBOARD_TOKEN" ]; then
    log "persisting BROKER_ONBOARD_TOKEN to SSM Parameter Store"
    "$PYTHON" - "$REGION" "$BROKER_ONBOARD_TOKEN" <<'PYEOF' || die "failed to persist onboard token to SSM"
import sys, boto3
region = sys.argv[1]
secret = sys.argv[2]
assert len(secret.strip()) >= 24, "BROKER_ONBOARD_TOKEN must be at least 24 characters"
ssm = boto3.client("ssm", region_name=region)
try:
    ssm.put_parameter(
        Name="/verdantforged/broker/onboard-token",
        Value=secret,
        Type="SecureString",
        Description="VerdantForged NemoClaw onboarding validation token",
        Overwrite=True,
    )
    print("[deploy] onboard token persisted to /verdantforged/broker/onboard-token")
except Exception as e:
    print(f"[deploy] SSM put_parameter failed: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
    unset BROKER_ONBOARD_TOKEN
    BROKER_ONBOARD_TOKEN=""
fi

# ---- 4. Run bootstrap on the instance via SSM (boto3) ----
log "running control-plane bootstrap"
# Build env file content — written to /tmp/_bootstrap_env.sh on instance, sourced there
BOOT_ENV=$(cat <<ENVEOF
export REPO_DIR=/home/ubuntu/tee-broker-deploy
export BR_DEPLOY=/opt/broker-daemon
export BR_EFS=/mnt/broker
export BROKER_REGION=$REGION
export BROKER_STACK_NAME=$STACK_NAME
export BROKER_WORKER_AMI='$WORKER_AMI_ID'
export BROKER_DOMAIN=$DOMAIN_NAME
export BROKER_IDLE_BUFFER_MINUTES=$IDLE_BUFFER_MINUTES
export BROKER_LLM_API_KEY='$BROKER_LLM_API_KEY'
export BROKER_LLM_BASE_URL='$BROKER_LLM_BASE_URL'
export BROKER_LLM_MODEL='$BROKER_LLM_MODEL'
export BROKER_ONBOARD_TOKEN='$BROKER_ONBOARD_TOKEN'
export DEMO_TOKEN_CAP='$DEMO_TOKEN_CAP'
export STRIPE_SECRET_KEY='$STRIPE_SECRET_KEY'
export STRIPE_NETWORK_ID='$STRIPE_NETWORK_ID'
export BROKER_PAYMENT_STUB_MODE='$BROKER_PAYMENT_STUB_MODE'
export BROKER_ALLOW_STUB_WORKER_ATTESTATION='$BROKER_ALLOW_STUB_WORKER_ATTESTATION'
export STRIPE_ACS_VERSION='2026-04-22.preview'
export STRIPE_CURRENCY='usd'
ENVEOF
)

cat > /tmp/_verdantforged_bootstrap.py <<'PYEOF'
import boto3, sys, time
ssm = boto3.client('ssm', region_name=sys.argv[1])
instance_id = sys.argv[2]
env_b64 = sys.argv[3]
cmds = [
    f'echo "{env_b64}" | base64 -d > /tmp/_bootstrap_env.sh',
    # SSM AWS-RunShellScript uses /bin/sh — `.` is POSIX-portable,
    # `source` is bash-only. Use `.` so the env loads in either shell.
    '. /tmp/_bootstrap_env.sh',
    'cd /home/ubuntu/tee-broker-deploy',
    'sudo -E bash scripts/bootstrap-control-plane.sh',
    'echo bootstrap_done',
]
r = ssm.send_command(InstanceIds=[instance_id], DocumentName='AWS-RunShellScript',
                     Comment='Bootstrap broker-daemon', Parameters={'commands': cmds})
cid = r['Command']['CommandId']
print(f"boot_command={cid}")
for _ in range(120):
    time.sleep(5)
    try:
        inv = ssm.get_command_invocation(CommandId=cid, InstanceId=instance_id)
        if inv['Status'] != 'InProgress':
            print(f"boot_status={inv['Status']}")
            out = inv.get('StandardOutputContent', '')
            err = inv.get('StandardErrorContent', '')
            print(out[-2000:] if out else '')
            if err:
                print(f"boot_stderr={err[-1500:]}")
            sys.exit(0 if inv['Status'] == 'Success' else 1)
    except ssm.exceptions.InvocationDoesNotExist:
        time.sleep(2)
sys.exit(1)
PYEOF

BASE64_ENV=$(base64 -w0 < <(echo "$BOOT_ENV"))
"$PYTHON" /tmp/_verdantforged_bootstrap.py "$REGION" "$INSTANCE_ID" "$BASE64_ENV" || die "bootstrap failed"
rm -f /tmp/_verdantforged_bootstrap.py

# ---- 4.5. Refresh metadata on already-running workers ----
# Existing workers do not rerun cloud-init/user-data when deploy.sh pushes a new
# worker-bootstrap.sh to EFS. If the bootstrap metadata capture changed, the
# live worker can keep stale /opt/worker/.nemoclaw_metadata and keep publishing
# "unknown" image/digest fields. Refresh in place; this preserves existing
# worker public keys and only updates the NemoClaw metadata fields.
if [ "$REFRESH_EXISTING_WORKERS" = "1" ]; then
    log "refreshing NemoClaw metadata on existing workers (if any)"
    "$PYTHON" - "$REGION" <<'PYEOF' || die "worker metadata refresh failed"
import boto3, json, sys, time

region = sys.argv[1]
ec2 = boto3.client("ec2", region_name=region)
ssm = boto3.client("ssm", region_name=region)

resp = ec2.describe_instances(Filters=[
    {"Name": "instance-state-name", "Values": ["running"]},
    {"Name": "tag:Project", "Values": ["verdantforged"]},
    {"Name": "tag:ManagedBy", "Values": ["broker-daemon"]},
    {"Name": "tag:Role", "Values": ["tee-worker"]},
])
workers = [i["InstanceId"] for r in resp.get("Reservations", [])
           for i in r.get("Instances", [])]
if not workers:
    print("[deploy] no running broker-managed workers found")
    sys.exit(0)

info = ssm.describe_instance_information(
    Filters=[{"Key": "InstanceIds", "Values": workers}])
online = {i["InstanceId"] for i in info.get("InstanceInformationList", [])
          if i.get("PingStatus") == "Online"}
targets = [iid for iid in workers if iid in online]
skipped = [iid for iid in workers if iid not in online]
if skipped:
    print(f"[deploy] metadata refresh skipped SSM-offline workers: {', '.join(skipped)}")
if not targets:
    print("[deploy] no SSM-online workers to refresh")
    sys.exit(0)

refresh_py = r'''
import datetime, hashlib, json, pathlib, subprocess
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

meta_path = pathlib.Path('/opt/worker/.nemoclaw_metadata')
keys_path = pathlib.Path('/mnt/broker/logs/worker-keys.json')
att_path = pathlib.Path('/mnt/broker/logs/worker-attestation.json')
local_att_path = pathlib.Path('/opt/worker/worker-attestation.json')
sandbox_path = pathlib.Path('/opt/worker/.nemoclaw_sandbox_name')
priv_path = pathlib.Path('/opt/worker/keys/worker_input_x25519.priv')
policy_path = pathlib.Path('/mnt/broker/logs/openshell-policy.yaml')
sandbox = (sandbox_path.read_text().strip() if sandbox_path.exists() else 'worker') or 'worker'

def run(args):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL).stdout

# Make sure the X25519 worker-input privkey is on disk — without it the
# SEV-SNP report_data binding has nothing to bind to and the verifier
# Check 4 fails. The original user-data.sh path creates this; the
# deploy refresh recreates it if missing so a worker that booted from
# an old gold AMI still produces a verifiable report.
priv_path.parent.mkdir(parents=True, exist_ok=True)
if priv_path.exists():
    priv = X25519PrivateKey.from_private_bytes(priv_path.read_bytes())
else:
    priv = X25519PrivateKey.generate()
    priv_path.write_bytes(priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()))
    priv_path.chmod(0o600)
public = priv.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)
policy_hash = (hashlib.sha256(policy_path.read_bytes()).hexdigest()
               if policy_path.exists() else '')
binding = hashlib.sha256(
    b'verdantforged-worker-input-v1\0' + public + b'\0' +
    (bytes.fromhex(policy_hash) if policy_hash else b'')).hexdigest()

version_lines = run(['nemohermes', '--version']).splitlines()
version = version_lines[0] if version_lines else 'unknown'
image_lines = run(['docker', 'ps', '-a',
                   '--filter', f'label=openshell.ai/sandbox-name={sandbox}',
                   '--format', '{{.Image}}']).splitlines()
image = image_lines[0] if image_lines else 'unknown'
digest = 'unknown'
if image != 'unknown':
    for line in run(['docker', 'image', 'inspect', image,
                     '--format', '{{range .RepoDigests}}{{println .}}{{end}}']).splitlines():
        if '@' in line:
            digest = line.split('@', 1)[1]
            break
    if digest == 'unknown':
        digest = run(['docker', 'image', 'inspect', image,
                      '--format', '{{.Id}}']).strip() or 'unknown'
meta = {
    'nemoclaw_version': version,
    'nemoclaw_image': image,
    'nemoclaw_image_digest': digest,
    'captured_at': datetime.datetime.now(datetime.UTC).replace(
        microsecond=0).isoformat().replace('+00:00', 'Z'),
}
meta_path.write_text(json.dumps(meta, indent=2) + '\n')
keys = {}
if keys_path.exists():
    try:
        keys = json.loads(keys_path.read_text())
    except Exception:
        keys = {}
keys.update({k: meta[k] for k in (
    'nemoclaw_version', 'nemoclaw_image', 'nemoclaw_image_digest')})
keys.update({
    'x25519_pubkey_b64': __import__('base64').b64encode(public).decode(),
    'policy_hash': policy_hash,
    'attestation_binding_sha256': binding,
})
if keys:
    keys_path.write_text(json.dumps(keys, indent=2) + '\n')

# Refresh the SEV-SNP attestation record on EFS. Pass the binding as
# the inblob to the kernel's TSM configfs so the report embeds it.
sev = {}
sev_script = pathlib.Path('/opt/worker/sev_snp.py')
if sev_script.exists():
    binding_bytes = bytes.fromhex(binding) + b'\0' * 32
    env = dict(__import__('os').environ)
    env['BROKER_ATTESTATION_REPORT_DATA_HEX'] = binding_bytes.hex()
    proc = subprocess.run(
        ['python3', str(sev_script)],
        env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sev_raw = proc.stdout or ''
    try:
        sev = json.loads(sev_raw) if sev_raw.strip() else {}
    except Exception:
        sev = {}
att_record = {
    'instance_id': keys.get('instance_id', ''),
    'tee_type': 'amd-sev-snp',
    'measurement': sev.get('measurement', ''),
    'source': sev.get('source', 'stub'),
    'report': sev.get('report', ''),
    'report_data': sev.get('report_data', ''),
    'cert_chain': sev.get('cert_chain', []),
    'chip_id': sev.get('chip_id', ''),
    'family_id': sev.get('family_id', ''),
    'measured_at': datetime.datetime.now(datetime.UTC).replace(
        microsecond=0).isoformat().replace('+00:00', 'Z'),
}
att_path.write_text(json.dumps(att_record, indent=2) + '\n')
local_att_path.write_text(json.dumps(att_record, indent=2) + '\n')

print(json.dumps({
    'sandbox': sandbox,
    'metadata': meta,
    'attestation_source': att_record['source'],
    'measurement_prefix': (att_record['measurement'] or '')[:20],
    'report_data_prefix': (att_record['report_data'] or '')[:32],
    'binding_prefix': binding[:32],
    'updated_worker_keys': bool(keys),
    'wrote_attestation': att_path.exists(),
}, sort_keys=True))
'''
commands = ["python3 - <<'PY'\n" + refresh_py + "\nPY"]
cmd = ssm.send_command(InstanceIds=targets, DocumentName="AWS-RunShellScript",
                       Comment="Refresh VerdantForged worker NemoClaw metadata",
                       Parameters={"commands": commands})["Command"]["CommandId"]
print(f"[deploy] metadata refresh command={cmd} targets={','.join(targets)}")
failed = []
for iid in targets:
    inv = None
    for _ in range(40):
        time.sleep(3)
        inv = ssm.get_command_invocation(CommandId=cmd, InstanceId=iid)
        if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
    status = inv["Status"] if inv else "Unknown"
    out = (inv.get("StandardOutputContent", "") if inv else "").strip()
    err = (inv.get("StandardErrorContent", "") if inv else "").strip()
    print(f"[deploy] metadata refresh {iid}: {status} {out[-500:]}")
    if err:
        print(f"[deploy] metadata refresh stderr {iid}: {err[-500:]}")
    if status != "Success":
        failed.append(iid)
if failed:
    print(f"[deploy] metadata refresh failed for: {', '.join(failed)}", file=sys.stderr)
    sys.exit(1)
PYEOF
else
    log "skipping existing-worker metadata refresh (REFRESH_EXISTING_WORKERS=$REFRESH_EXISTING_WORKERS)"
fi

# ---- 5. Smoke test ----
log "smoke-testing public endpoint"
sleep 5
HEALTH=$(curl -sf "http://$PUBLIC_IP/healthz" 2>&1 || echo "unreachable")
if echo "$HEALTH" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
    log "health check passed: $HEALTH"
else
    log "health check unreachable (Caddy not yet provisioned?) — try again in a minute"
    log "  curl http://$PUBLIC_IP/healthz"
fi

# ---- 6. Open SG ports for ACME challenge (Caddy needs :80 from Let's Encrypt) ----
# If a DomainName was provided, the CFN already opens 80/443. But for direct-EIP
# testing without a domain, also open them here so Caddy can serve the UI.
if [ -n "$DOMAIN_NAME" ]; then
    log "DNS should point $DOMAIN_NAME at $PUBLIC_IP — Caddy will issue Let's Encrypt cert automatically"
fi

# ---- Summary ----
cat <<EOF

============================================================
  VerdantForged broker — control plane deployed
============================================================
  Region:        $REGION
  Public IP:     $PUBLIC_IP
  Domain:        ${DOMAIN_NAME:-(none — HTTP only until DNS is pointed at the EIP)}
  EFS:           $EFS_ID
  Worker SG:     $WORKER_SG
  Worker role:   $WORKER_ROLE
  Worker AMI:    $WORKER_AMI_ID
  Idle buffer:   $IDLE_BUFFER_MINUTES minutes
  LLM model:     $BROKER_LLM_MODEL
============================================================

  Next steps:
  1. Point $DOMAIN_NAME's A record at $PUBLIC_IP (if not already).
  2. Wait ~30s for Let's Encrypt to issue the cert.
  3. Agent payment smoke test (expect HTTP 402 + WWW-Authenticate challenge):
       curl -i -X POST https://$DOMAIN_NAME/v1/jobs \\\\
            -H 'Content-Type: application/json' \\\\
            -d '{"client_req_id":"test-1","encrypted_skill":"summarize","encrypted_data":"hello","requester_sig":"0x","result_pubkey":"0x"}'
  4. Full file E2E after minting an ACS Shared Payment Token:
       python3 scripts/run_file_job_e2e.py --spt spt_test_... --file path/to/input.txt

  Teardown:
    aws cloudformation delete-stack --stack-name $STACK_NAME --region $REGION
EOF