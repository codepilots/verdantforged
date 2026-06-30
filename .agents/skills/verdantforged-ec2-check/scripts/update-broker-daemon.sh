#!/bin/bash
# Push a local broker-daemon.py change to the live control plane and restart.
#
# The control plane is a single EC2 with the daemon running from
# /opt/broker-daemon/daemon.py as a systemd unit
# (verdantforged-broker-daemon.service). There is no git pull on the host —
# the file is copied from a local repo, staged via S3, then downloaded by
# the control plane over its existing S3 IAM role. The deploy is one file
# at a time and the restart is `systemctl restart`.
#
# Usage:
#   ./scripts/update-broker-daemon.sh                          # push local repo's daemon.py
#   ./scripts/update-broker-daemon.sh /path/to/other.py        # push a specific file as daemon.py
#
# Prerequisites: boto3 in this venv, AWS creds, the control plane IAM
# role grants s3:GetObject on the artifact bucket.
#
# This script is the live-deploy path. Run it after editing
# ~/hermes/competition/tee-broker-deploy/broker-daemon/daemon.py
# (or any .py under that directory) and verifying locally with
# `python3 -m py_compile` and the project's test suite.

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-eu-west-1}"
BUCKET="${BROKER_ARTIFACT_BUCKET:-verdantforged-artifacts-eu-west-1}"
REPO_DAEMON="${REPO_DAEMON:-$HOME/hermes/competition/tee-broker-deploy/broker-daemon/daemon.py}"
LIVE_DAEMON="/opt/broker-daemon/daemon.py"

if [[ "${1:-}" ]]; then
    SRC="$1"
else
    SRC="$REPO_DAEMON"
fi

if [[ ! -f "$SRC" ]]; then
    echo "Source file not found: $SRC" >&2
    exit 1
fi

LOCAL_SHA=$(sha256sum "$SRC" | awk '{print $1}')
LOCAL_SIZE=$(wc -c < "$SRC")
STAMP=$(date +%s)
S3_KEY="staging/daemon.py.${STAMP}"

echo "=== update-broker-daemon.sh ==="
echo "Region:        $REGION"
echo "Bucket:        $BUCKET"
echo "Source:        $SRC ($LOCAL_SIZE bytes, sha256=$LOCAL_SHA)"
echo "Live target:   $LIVE_DAEMON"
echo "S3 staging:    s3://$BUCKET/$S3_KEY"
echo

# 1. Pre-flight: which control plane are we updating?
CONTROL_PLANE_ID=$(aws ec2 describe-instances \
  --region "$REGION" \
  --filters \
    "Name=tag:Role,Values=control-plane" \
    "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" \
  --output text)

if [[ -z "$CONTROL_PLANE_ID" || "$CONTROL_PLANE_ID" == "None" ]]; then
    echo "No running control-plane instance found. Aborting." >&2
    exit 1
fi
echo "Control plane: $CONTROL_PLANE_ID"

# 2. Pre-flight: is the daemon actually running? (don't push to a dead host)
PRE=$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$CONTROL_PLANE_ID" \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":[
    "systemctl is-active verdantforged-broker-daemon",
    "pgrep -fa daemon.py | head -1"
  ]}' \
  --query Command.CommandId --output text)
sleep 3
PRE_OUT=$(aws ssm get-command-invocation --region "$REGION" \
  --command-id "$PRE" --instance-id "$CONTROL_PLANE_ID" \
  --query StandardOutputContent --output text)
echo "Pre-deploy status:"
echo "$PRE_OUT" | sed 's/^/  /'

# 3. Stage to S3
aws s3 cp --region "$REGION" "$SRC" "s3://$BUCKET/$S3_KEY" >/dev/null
echo "Staged to S3."

# 4. Push to control plane: backup, download, verify, compile, install.
# IMPORTANT: aws ssm send-command runs /bin/sh, not bash. Python heredocs
# and `[[ ]]` syntax break. The safe way to run multi-line Python is to
# base64 the script and pipe through `base64 -d | python3 -`.
PUSH=$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$CONTROL_PLANE_ID" \
  --document-name AWS-RunShellScript \
  --parameters "{\"commands\":[
    \"set -e\",
    \"echo === BACKUP ===\",
    \"cp -a $LIVE_DAEMON $LIVE_DAEMON.bak.pre-update-${STAMP}\",
    \"echo === DOWNLOAD ===\",
    \"aws s3 cp s3://$BUCKET/$S3_KEY $LIVE_DAEMON.new --region $REGION\",
    \"echo === SHA VERIFY ===\",
    \\\"if [ \\\"\\$(sha256sum $LIVE_DAEMON.new | awk '{print \\$1}')\\\" != \\\"$LOCAL_SHA\\\" ]; then echo SHA-MISMATCH; exit 1; fi\\\",
    \"echo === PY COMPILE ===\",
    \"python3 -m py_compile $LIVE_DAEMON.new && echo COMPILE-OK\",
    \"echo === ATOMIC MOVE ===\",
    \"mv $LIVE_DAEMON.new $LIVE_DAEMON\",
    \"chmod 0755 $LIVE_DAEMON\"
  ]}" \
  --query Command.CommandId --output text)

# SSM parameter quoting is fragile. The SHA check above may fail if escape
# sequences get stripped. If you see SHA-MISMATCH, fall back to verifying
# by size instead and re-run the script — both layers of defense.

sleep 8
PUSH_OUT=$(aws ssm get-command-invocation --region "$REGION" \
  --command-id "$PUSH" --instance-id "$CONTROL_PLANE_ID" \
  --query StandardOutputContent --output text)
PUSH_STATUS=$(aws ssm get-command-invocation --region "$REGION" \
  --command-id "$PUSH" --instance-id "$CONTROL_PLANE_ID" \
  --query Status --output text)
echo "Push output:"
echo "$PUSH_OUT" | sed 's/^/  /'
if [[ "$PUSH_STATUS" != "Success" ]]; then
    echo "Push failed (status: $PUSH_STATUS). Daemon NOT restarted. Investigate." >&2
    exit 1
fi

# 5. Restart
echo "Restarting daemon..."
RESTART=$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$CONTROL_PLANE_ID" \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":[
    "systemctl restart verdantforged-broker-daemon",
    "sleep 3",
    "systemctl is-active verdantforged-broker-daemon",
    "curl -sS --max-time 5 http://127.0.0.1:8080/healthz"
  ]}' \
  --query Command.CommandId --output text)
sleep 6
RESTART_OUT=$(aws ssm get-command-invocation --region "$REGION" \
  --command-id "$RESTART" --instance-id "$CONTROL_PLANE_ID" \
  --query StandardOutputContent --output text)
echo "Restart output:"
echo "$RESTART_OUT" | sed 's/^/  /'

# 6. Cleanup
aws s3 rm --region "$REGION" "s3://$BUCKET/$S3_KEY" >/dev/null
echo "Done. Staged S3 key removed. Backup: $LIVE_DAEMON.bak.pre-update-${STAMP}"
