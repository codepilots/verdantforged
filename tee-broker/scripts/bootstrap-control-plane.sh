#!/bin/bash
# VerdantForged TEE Broker — control plane bootstrap.
# Run this AFTER cloudformation-control-plane.yaml finishes CREATE_COMPLETE.
# Replaces the cloud-init placeholders with the real daemon + Caddyfile, and
# writes the worker-bootstrap.sh that gets pushed to EFS for run-instances.
# Security note: this script NEVER writes the LLM API key to EFS — workers
# must use the broker LLM proxy with per-job tokens. Only the broker
# (running here) holds the upstream key.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "must be root" >&2
    exit 1
fi

REPO_DIR="${REPO_DIR:-/home/ubuntu/tee-broker-deploy}"
BR_DEPLOY="${BR_DEPLOY:-/opt/broker-daemon}"
BR_EFS="${BR_EFS:-/mnt/broker}"
LOG="$BR_EFS/logs/control-bootstrap.log"
mkdir -p "$BR_DEPLOY" "$BR_EFS/logs"
chmod 0755 "$BR_DEPLOY"
exec > >(tee -a "$LOG") 2>&1
echo "=== control-plane bootstrap $(date -u +%FT%TZ) ==="

REGION="${BROKER_REGION:-${AWS_REGION:-eu-west-1}}"
STACK_NAME="${BROKER_STACK_NAME:-verdantforged-broker-control}"

# ---- 1. Install Python deps into system Python ----
# CFN user-data usually installs python3-pip via apt, but fresh
# Ubuntu 24.04 AMIs don't ship pip by default. Install pip first
# if missing, then install aiohttp + boto3.
if ! python3 -c "import pip" 2>/dev/null; then
    echo "pip not installed — installing via apt"
    apt-get update -qq && apt-get install -y -qq python3-pip 2>&1 | tail -5
fi
if ! python3 -c "import aiohttp, boto3" 2>/dev/null; then
    echo "installing aiohttp + boto3 via pip"
    python3 -m pip install --quiet --break-system-packages \
        -r "$REPO_DIR/broker-daemon/requirements.txt"
fi

# ---- 1a. Install nfs-common (EFS helper) ----
# EFS mounts via NFSv4.1 — needs /sbin/mount.nfs + helpers in nfs-common.
# CFN user-data assumes this is installed but doesn't install it on
# fresh Ubuntu 24.04 AMIs.
if ! command -v mount.nfs >/dev/null 2>&1; then
    echo "nfs-common not installed — installing via apt"
    apt-get install -y -qq nfs-common 2>&1 | tail -3
fi

# ---- 1b. Install caddy (idempotent) ----
# CFN user-data assumes caddy is already installed but doesn't install
# it — fresh Ubuntu 24.04 doesn't ship caddy. Install from the
# official Cloudsmith repo on first run; skip if already installed.
if ! command -v caddy >/dev/null 2>&1; then
    echo "caddy not installed — installing from Cloudsmith repo"
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https 2>&1 | tail -3
    curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" 2>/dev/null \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
    curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" 2>/dev/null \
        | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq 2>&1 | tail -3
    apt-get install -y -qq caddy 2>&1 | tail -3
fi

# ---- 1c. Install awscli (idempotent) ----
# The bootstrap calls `aws cloudformation describe-stacks` to read
# CFN outputs (EFS DNS, SG IDs, etc). awscli isn't on the AMI by
# default — and the apt package is renamed/restructured in Ubuntu
# 24.04. Install from AWS's official zip bundle; skip if present.
if ! command -v aws >/dev/null 2>&1; then
    echo "awscli not installed — installing from AWS official zip"
    apt-get install -y -qq unzip curl 2>&1 | tail -3
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" \
        -o /tmp/awscliv2.zip
    unzip -q /tmp/awscliv2.zip -d /tmp/
    /tmp/aws/install
    rm -rf /tmp/awscliv2.zip /tmp/aws
fi

# ---- 2. Install daemon + Caddyfile + static UI ----
# Copy ALL python files (daemon.py, crypto.py, any modules) — not
# just daemon.py. The daemon imports sibling modules like `crypto`
# and `openshell` directly.
install -m 0755 "$REPO_DIR/broker-daemon/daemon.py" "$BR_DEPLOY/daemon.py"
install -m 0644 "$REPO_DIR/broker-daemon/crypto.py" "$BR_DEPLOY/crypto.py"
# Copy any subpackage directories (e.g. openshell/) wholesale
for py in "$REPO_DIR"/broker-daemon/*.py; do
    [ -f "$py" ] && install -m 0644 "$py" "$BR_DEPLOY/$(basename "$py")"
done
for d in "$REPO_DIR"/broker-daemon/*; do
    name="$(basename "$d")"
    case "$name" in
        __pycache__|static|caddy) continue ;;
    esac
    if [ -d "$d" ] && [ -f "$d/__init__.py" -o -n "$(ls "$d"/*.py 2>/dev/null)" ]; then
        rm -rf "$BR_DEPLOY/$name"
        cp -r "$d" "$BR_DEPLOY/"
    fi
done
install -m 0644 "$REPO_DIR/broker-daemon/caddy/Caddyfile" /etc/caddy/Caddyfile
mkdir -p "$BR_DEPLOY/static"
install -m 0644 "$REPO_DIR/broker-daemon/static/"*.html "$BR_DEPLOY/static/" 2>/dev/null || \
    install -m 0644 "$REPO_DIR/broker-daemon/static/index.html" "$BR_DEPLOY/static/index.html"
echo "daemon + caddyfile + static UI installed"

# ---- 3. Mount EFS (idempotent) ----
# CFN user-data writes to fstab, but if the deploy reused an instance where
# cloud-init didn't run (e.g., bootstrap on an already-running instance), we
# need to set up the mount ourselves. nfs-common is already installed by CFN.
mkdir -p "$BR_EFS"
if ! mount | grep -q " on $BR_EFS "; then
    # Get the EFS DNS from CFN
    EFS_DNS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query 'Stacks[0].Outputs[?OutputKey==`EfsDns`].OutputValue' --output text 2>/dev/null || echo "")
    if [ -z "$EFS_DNS" ]; then
        # Fallback: scan CFN resources for the EFS by ID
        EFS_ID=$(aws cloudformation describe-stack-resources --stack-name "$STACK_NAME" --region "$REGION" \
            --query 'StackResources[?ResourceType==`AWS::EFS::FileSystem`].PhysicalResourceId' --output text 2>/dev/null || echo "")
        if [ -n "$EFS_ID" ]; then
            EFS_DNS="${EFS_ID}.efs.${REGION}.amazonaws.com"
        fi
    fi
    if [ -n "$EFS_DNS" ]; then
        echo "mounting EFS at $BR_EFS from $EFS_DNS"
        # Add fstab entry (idempotent — check first)
        if ! grep -q "$EFS_DNS" /etc/fstab; then
            echo "$EFS_DNS:/ $BR_EFS nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport,_netdev 0 0" >> /etc/fstab
        fi
        mount "$BR_EFS" || mount -a
        echo "EFS mounted"
    else
        echo "WARNING: could not determine EFS DNS — daemon will write to local $BR_EFS (will not be visible to workers)"
    fi
else
    echo "EFS already mounted"
fi

mkdir -p "$BR_EFS/logs" "$BR_EFS/jobs/inbox" "$BR_EFS/jobs/outbox"

# ---- 4. Push worker-bootstrap.sh + worker-poller.py + worker-agent.py to EFS ----
# All three must live in $BR_EFS/logs/ — the daemon reads worker-bootstrap.sh at
# run-instances time, the worker copies poller.py from EFS in user-data, and
# user-data.sh copies worker-agent.py into the NemoClaw sandbox at onboard so
# the poller's `dispatch_to_sandbox` can call `python3 /sandbox/worker-agent.py`
# without trying to fetch the file inside the attested environment.
install -m 0755 "$REPO_DIR/worker/user-data.sh" "$BR_DEPLOY/worker-bootstrap.sh"
install -m 0755 "$REPO_DIR/worker/user-data.sh" "$BR_EFS/logs/worker-bootstrap.sh"
install -m 0755 "$REPO_DIR/worker/poller.py" "$BR_EFS/logs/worker-poller.py"
install -m 0755 "$REPO_DIR/worker/worker-agent.py" "$BR_EFS/logs/worker-agent.py"
install -m 0644 "$REPO_DIR/worker/sev_snp.py" "$BR_EFS/logs/worker-sev-snp.py"
install -m 0644 "$REPO_DIR/broker-daemon/openshell/policy.yaml" "$BR_EFS/logs/openshell-policy.yaml"
# NemoClaw skill catalog — pushed so user-data.sh step 4 can deploy
# them via `nemohermes <sbx> skill install <dir>`. t_ab320c7b.
if [ -d "$REPO_DIR/worker/skills" ]; then
    rm -rf "$BR_EFS/logs/skills"
    cp -r "$REPO_DIR/worker/skills" "$BR_EFS/logs/skills"
    echo "worker skills catalog pushed to EFS ($(ls "$BR_EFS/logs/skills" 2>/dev/null | wc -l) skills)"
fi
echo "worker-bootstrap.sh, worker-poller.py, worker-agent.py pushed to EFS"

# ---- 5. Ensure no llm-api-key on EFS (security) ----
# The LLM API key lives ONLY in the broker's config.env. Workers must use
# the broker LLM proxy with per-job tokens. If a stale key exists from
# an earlier deploy, remove it.
if [ -f "$BR_EFS/logs/llm-api-key" ]; then
    rm -f "$BR_EFS/logs/llm-api-key"
    echo "removed stale llm-api-key from EFS (security: workers must use broker proxy)"
fi
echo "no llm-api-key on EFS (broker holds the upstream key)"

# ---- 6. Generate config.env from CFN outputs ----
# Rebuild config.env cleanly so we don't get duplicate keys or mangled values
# (this was a bug in earlier deploys where heredoc + env var got joined).
echo "generating config.env from CFN outputs"
declare -A CFN_OUTPUTS
while IFS=$'\t' read -r key value; do
    [ -n "$key" ] && CFN_OUTPUTS["$key"]="$value"
done < <(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text 2>/dev/null)

CONTROL_IP=$(hostname -I | awk '{print $1}')

# Resolve STRIPE_SECRET_KEY before the heredoc so the writer
# below only sees a plain env var (no embedded `$(aws ...)` which
# would force command substitution on the heredoc body and trigger
# syntax errors from variable references on later lines). This
# block runs BEFORE the heredoc — fetch from SSM if env is empty.
if [ -z "${STRIPE_SECRET_KEY:-}" ]; then
    echo "step 5c: STRIPE_SECRET_KEY not in env — fetching from SSM Parameter Store"
    if STRIPE_SECRET_KEY=$(aws ssm get-parameter \
        --name /verdantforged/broker/stripe-secret-key \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text 2>/dev/null); then
        echo "step 5c: STRIPE_SECRET_KEY fetched from SSM (len=${#STRIPE_SECRET_KEY})"
    else
        echo "step 5c: SSM get-parameter failed — running in DEMO MODE"
        echo "step 5c: to enable live Stripe: deploy.sh writes it to SSM if STRIPE_SECRET_KEY env var is set,"
        echo "step 5c: or run manually: aws ssm put-parameter --name /verdantforged/broker/stripe-secret-key --type SecureString --value sk_test_..."
        STRIPE_SECRET_KEY=""
    fi
fi

if [ -z "${BROKER_LLM_API_KEY:-}" ]; then
    echo "step 5d: BROKER_LLM_API_KEY not in env — fetching from SSM Parameter Store"
    if BROKER_LLM_API_KEY=$(aws ssm get-parameter \
        --name /verdantforged/broker/llm-api-key \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text 2>/dev/null); then
        echo "step 5d: BROKER_LLM_API_KEY fetched from SSM (len=${#BROKER_LLM_API_KEY})"
    else
        echo "step 5d: SSM get-parameter failed — broker LLM proxy will return llm_upstream_not_configured"
        echo "step 5d: to enable live LLM: deploy.sh writes it to SSM if BROKER_LLM_API_KEY env var is set"
        BROKER_LLM_API_KEY=""
    fi
fi

if [ -z "${BROKER_ONBOARD_TOKEN:-}" ]; then
    echo "step 5e: BROKER_ONBOARD_TOKEN not in env — fetching from SSM Parameter Store"
    if BROKER_ONBOARD_TOKEN=$(aws ssm get-parameter \
        --name /verdantforged/broker/onboard-token \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text 2>/dev/null); then
        echo "step 5e: BROKER_ONBOARD_TOKEN fetched from SSM (len=${#BROKER_ONBOARD_TOKEN})"
    else
        echo "step 5e: SSM get-parameter failed — preserving existing config or generating a new token"
        BROKER_ONBOARD_TOKEN=""
    fi
fi

# Use a SINGLE-QUOTED heredoc delimiter ('ENVEOF') so bash does NOT
# expand $(), ${}, or backticks inside the body. The Stripe key and
# any other secrets that should not be expanded at write time go in
# via separate `echo ... >> config.env` calls AFTER the heredoc —
# those calls happen at script execution time, not at parse time.
# Preserve existing secrets on idempotent redeploys when deploy.sh does not
# explicitly provide replacements. This prevents a code-only redeploy from
# blanking the live LLM/skills keys in /opt/broker-daemon/config.env.
preserve_existing_config_var() {
    local name="$1" val=""
    [ -z "${!name:-}" ] || return 0
    [ -f "$BR_DEPLOY/config.env" ] || return 0
    val=$(env -i bash -c "set -a; . '$BR_DEPLOY/config.env' >/dev/null 2>&1; printf '%s' \"\${$name:-}\"" 2>/dev/null || true)
    [ -n "$val" ] && export "$name=$val"
}
preserve_existing_config_var BROKER_LLM_API_KEY
preserve_existing_config_var BROKER_SKILLS_API_KEY
preserve_existing_config_var BROKER_ONBOARD_TOKEN
if [ -z "${BROKER_ONBOARD_TOKEN:-}" ]; then
    BROKER_ONBOARD_TOKEN=$(openssl rand -base64 36 | tr -d '\n=' | cut -c1-48)
    echo "step 5e: generated new BROKER_ONBOARD_TOKEN for clean deploy"
fi
# Preserve a manually baked warm-worker AMI across code-only redeploys. CFN's
# WorkerAmiId remains the fallback, but once scripts/bake-worker-ami.py has
# validated and installed a cached NemoClaw image we must not silently revert
# to the cold Ubuntu AMI on the next bootstrap.
preserve_existing_config_var BROKER_WORKER_AMI
cat > "$BR_DEPLOY/config.env" <<ENVEOF
BROKER_REGION=$REGION
BROKER_STACK_NAME=$STACK_NAME
BROKER_EFS_MOUNT=$BR_EFS
# Artifact S3 bucket (t_96b86cff — encrypted result packs). Daemon
# presigns download URLs from this bucket; worker uploads to it. The
# bucket is created by CloudFormation with SSE-KMS + 24h lifecycle
# expiration; the value here is the same ArtifactBucketName CFN output.
BROKER_ARTIFACT_BUCKET=${CFN_OUTPUTS[ArtifactBucketName]:-verdantforged-artifacts-$REGION}
BROKER_DAEMON_PORT=8080
BROKER_DOMAIN=${BROKER_DOMAIN:-${CFN_OUTPUTS[DomainName]:-verdant.codepilots.co.uk}}
BROKER_IDLE_BUFFER_MINUTES=${BROKER_IDLE_BUFFER_MINUTES:-10}
BROKER_VPC_ID=${CFN_OUTPUTS[VpcId]:-}
BROKER_SUBNET_ID=${CFN_OUTPUTS[ControlPlaneSubnetId]:-${CFN_OUTPUTS[SubnetId]:-${CFN_OUTPUTS[WorkerSubnetIds]:-}}}
BROKER_WORKER_SG_ID=${CFN_OUTPUTS[WorkerSecurityGroupId]:-}
BROKER_CONTROL_SG_ID=${CFN_OUTPUTS[ControlPlaneSecurityGroupId]:-}
BROKER_WORKER_AMI=${BROKER_WORKER_AMI:-${CFN_OUTPUTS[WorkerAmiId]:-}}
BROKER_WORKER_INSTANCE_TYPE=${BROKER_WORKER_INSTANCE_TYPE:-m6a.xlarge}
BROKER_WORKER_IAM_ROLE=${CFN_OUTPUTS[WorkerInstanceProfileArn]:-${CFN_OUTPUTS[WorkerRoleArn]:-}}
BROKER_EFS_DNS=${CFN_OUTPUTS[EfsDns]:-}
BROKER_CONTROL_PLANE_URL=http://${CONTROL_IP}:8080
DEMO_TOKEN_CAP=${DEMO_TOKEN_CAP:-50000}
BROKER_PAYMENT_STUB_MODE=${BROKER_PAYMENT_STUB_MODE:-0}
BROKER_TEST_SPT_ISSUER=${BROKER_TEST_SPT_ISSUER:-1}
BROKER_ALLOW_STUB_WORKER_ATTESTATION=${BROKER_ALLOW_STUB_WORKER_ATTESTATION:-}
BROKER_LLM_API_KEY=${BROKER_LLM_API_KEY:-}
BROKER_LLM_BASE_URL=${BROKER_LLM_BASE_URL:-https://ollama.com/v1}
BROKER_LLM_MODEL=${BROKER_LLM_MODEL:-minimax-m3:cloud}
BROKER_ONBOARD_TOKEN=${BROKER_ONBOARD_TOKEN:-}
# Skills registration API key (VULN-S2). When unset, POST /v1/skills is refused.
# Generated on first bootstrap if missing. Rotate via SSM and redeploy.
BROKER_SKILLS_API_KEY=${BROKER_SKILLS_API_KEY:-$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | xxd -p -c 32)}
# Result encryption redaction (VULN-S3). Default ON for the hackathon demo so
# verify-crypto-e2e.py's plaintext-equivalence check still passes against the
# live broker. Set to 0 in production to redact the plaintext output from the
# result envelope.
BROKER_KEEP_PLAINTEXT_FOR_DEMO=${BROKER_KEEP_PLAINTEXT_FOR_DEMO:-1}
# Stripe PaymentIntent lifecycle (t_9fbec867). STRIPE_SECRET_KEY is
# resolved BEFORE the heredoc (see block above); the line below is
# the actual env var that's read by the daemon at boot. Empty value
# = DEMO MODE (format validation only, no live API calls).
# Never echo, never log, never include in summaries. The chmod 0600
# below is the only persistence path. Matches stripe-backend's PIN
# at tee-broker-site/stripe-backend/pyproject.toml.
STRIPE_SECRET_KEY=$STRIPE_SECRET_KEY
# Stripe Agentic Commerce Suite / Machine Payments Protocol merchant profile.
STRIPE_NETWORK_ID=${STRIPE_NETWORK_ID:-${STRIPE_MERCHANT_PROFILE_ID:-profile_test_61UxANkhMMDN58EdHA6UxANjDhE9lwNXZMFWQJI6y3Mu}}
STRIPE_ACS_VERSION=${STRIPE_ACS_VERSION:-2026-04-22.preview}
STRIPE_CURRENCY=${STRIPE_CURRENCY:-usd}
BROKER_PAYMENT_STUB_MODE=${BROKER_PAYMENT_STUB_MODE:-0}
BROKER_TEST_SPT_ISSUER=${BROKER_TEST_SPT_ISSUER:-1}
BROKER_ALLOW_STUB_WORKER_ATTESTATION=${BROKER_ALLOW_STUB_WORKER_ATTESTATION:-}
ENVEOF
chmod 0600 "$BR_DEPLOY/config.env"
echo "config.env written to $BR_DEPLOY/config.env"
cat "$BR_DEPLOY/config.env" | head -20

# ---- 7. Caddy drop-in for BROKER_DOMAIN env ----
mkdir -p /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/broker.conf <<'EOF'
[Service]
EnvironmentFile=/opt/broker-daemon/config.env
EOF

# ---- 8. Restart services ----
# Write our own systemd unit (CFN user-data writes a placeholder but
# this is the source of truth — keeps deploy idempotent whether the
# instance is fresh or being re-bootstrapped after a failed push).
cat > /etc/systemd/system/verdantforged-broker-daemon.service <<'SVCEOF'
[Unit]
Description=VerdantForged Broker Daemon
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
EnvironmentFile=/opt/broker-daemon/config.env
ExecStart=/usr/bin/python3 /opt/broker-daemon/daemon.py
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable --now caddy || echo "caddy failed to start — check journalctl -u caddy"
systemctl enable verdantforged-broker-daemon || systemctl enable broker-daemon
systemctl restart verdantforged-broker-daemon || systemctl restart broker-daemon
systemctl reload caddy || true

# Clear stale Let's Encrypt state if DNS has changed since last deploy
rm -rf /var/lib/caddy/.local/share/caddy/acme-v02.api.letsencrypt.org-directory 2>/dev/null || true
sleep 2
systemctl reload caddy || true

# ---- 9. Health check ----
sleep 3
if curl -sf http://127.0.0.1:8080/healthz >/dev/null; then
    echo "broker-daemon health: OK"
else
    echo "broker-daemon health: FAIL — check journalctl -u verdantforged-broker-daemon"
    journalctl -u verdantforged-broker-daemon --no-pager -n 20
    exit 1
fi

echo "=== control-plane bootstrap complete $(date -u +%FT%TZ) ==="
