---
name: verdantforged-ec2-check
description: Standardized procedure to verify the status of, and push code to, a VerdantForged EC2 deployment (NemoClaw + Hermes) launched via CloudFormation. Covers status checks, the S3-staging push workflow for live broker-daemon updates, and SSM shell-vs-Python patterns.
category: devops
metadata:
  hermes:
    changelog:
      - 1.1.0 (2026-06-29): Updated to reflect ACTIVE deployment is tee-broker-deploy/ (not ec2-deploy/). Active region is eu-west-1 (not eu-west-2). Added worker bootstrap placeholder corruption check. Added SSM `describe-instance-information` verification. Added cloud-init log inspection steps.
      - 1.3.0 (2026-06-30): Added scripts/update-broker-daemon-boto3.py — the boto3 equivalent of the shell wrapper that sidesteps the `aws ssm send-command` "badly formed help string" CLI bug (aws-cli 2.31.x + Python 3.14) and uses the public healthz URL instead of 127.0.0.1:8080. Promoted it to the primary wrapper. Documented the two-worktree divergence (~/hermes/competition/ on feature/s3-encrypted-artifacts vs. ~/hermes/competition-wt-nemoclaw/ on main) with the default-path risk. Added the BROKER_TEST_SPT_ISSUER hybrid-mode pattern as a class-level pitfall — when adding a hybrid-mode flag, grep for the OLD predicate and update every site in the payment path.
---

# VerdantForged EC2 Deployment Check Skill

## Overview
This skill provides a standardized way to verify the status of a VerdantForged EC2 deployment (NemoClaw + Hermes) launched via CloudFormation, AND to push broker-daemon code changes to the live control plane. **The active deployment is in `~/hermes/competition/tee-broker-deploy/` (not `ec2-deploy/`).** The active stack `verdantforged-broker-control` runs in **eu-west-1** (Ireland), not eu-west-2 (London).

## Key Files
- `references/hardcoded-instance-id-pitfall.md`: Explanation of why hardcoded instance IDs are problematic and how to fix them
- `references/llm-proxy-live-probe.md`: How to mint an `llm_token` directly into the broker DB and probe `/v1/llm/chat/completions` live (the only way to test the proxy from outside, since workers are the normal callers)
- `references/worker-bootstrap-placeholder-corruption.md` (in tee-broker-deploy-config): How a broken `COMPATIBLE_API_KEY` placeholder in worker user-data causes NemoClaw onboard HTTP 401 and hung jobs
- `scripts/verify-deployment.sh`: Read-only status verification script (legacy eu-west-2 path; tee-broker-deploy uses the boto3 patterns below instead)
- `scripts/update-broker-daemon.sh`: Push a local `daemon.py` change to the live control plane and restart the daemon in one shot (shell; hits the "badly formed help string" CLI bug on aws-cli 2.31.x + Python 3.14 — use the boto3 variant below if it dies)
- `scripts/update-broker-daemon-boto3.py`: Same workflow via boto3 — preferred. Unaffected by the SSM CLI bug, also hits the public `https://verdant.codepilots.co.uk/healthz` for the post-restart check (the daemon isn't bound to 127.0.0.1:8080 in this deployment — Caddy proxies 443 → daemon internally)

## Prerequisites
- AWS CLI v2 configured with appropriate permissions (ec2:Describe*, ssm:Describe*, ssm:GetCommand*, ssm:SendCommand, s3:PutObject/GetObject/Delete on the artifact bucket)
- Default region set to `eu-west-1` or AWS_DEFAULT_REGION environment variable defined
- The EC2 instance tagged with `Role=control-plane` (current live tag name) and `Project=verdantforged` (workers)

## Active Deployment Architecture (tee-broker-deploy/)

```
Control plane (broker daemon)
  EC2 i-0a537c94d3a3f37af in eu-west-1  (was i-05117b9649db5b343; check by tag)
  Public IP: 176.34.244.180
  Caddy on 443 → broker-daemon on 127.0.0.1:8080
  EFS mounted at /mnt/broker
  CloudFormation: verdantforged-broker-control
  Daemon systemd unit: verdantforged-broker-daemon.service
  Live code path:     /opt/broker-daemon/daemon.py  (NOT a git checkout;
                       pushes go via S3 staging — see Pitfall below)

Worker (NemoClaw + poller)
  EC2 launched on-demand per job
  AMI: Ubuntu 24.04 LTS
  User-data rendered from /mnt/broker/logs/worker-bootstrap.sh (EFS)
  Sandbox: tee-worker (NemoClaw onboard)
  Poller: worker-poller systemd service
```

The legacy `ec2-deploy/` directory contains a single-EC2 CloudFormation template that is NOT the active deployment. Do NOT use it for live checks.

## Steps

### 1. Set AWS Region to eu-west-1
```bash
export AWS_DEFAULT_REGION=eu-west-1
```

### 2. Find the Control Plane Instance

**Use the `Role=control-plane` tag, not `Name=verdantforged-broker-control`.** The CloudFormation
`Name` tag is `verdantforged-broker-control-control` (the stack name has `-control` already, then
the `Name` tag adds another `-control` suffix). The `Role` tag is the stable lookup:

```bash
CONTROL_PLANE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Role,Values=control-plane" "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" \
  --output text)
echo "Control Plane: $CONTROL_PLANE_ID"
```

If you want the instance by `Name` (e.g. for cross-referencing with CloudWatch), filter on
`Name=tag:Name,Values=verdantforged-broker-control-control` — the exact value, no truncation.

### 3. Find Active Worker Instances
```bash
WORKER_IDS=$(aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=verdantforged" "Name=tag:Role,Values=tee-worker" "Name=instance-state-name,Values=running" \
  --query "Reservations[*].Instances[*].InstanceId" \
  --output text)
echo "Workers: $WORKER_IDS"
```

### 4. Check Broker Health Endpoint
```bash
curl -sS https://verdant.codepilots.co.uk/healthz | jq .
# Expected: {"ok": true, "worker": <boolean>}
# "worker": false means no active worker (broker will launch one on first job)
```

### 5. Verify SSM Connectivity

**IMPORTANT: `aws ssm send-command` CLI may fail with "badly formed help string" on
aws-cli 2.31.x with Python 3.14 (see `aws-ephemeral-deploy` Pitfall #20). If this
happens, use `boto3` in `execute_code` instead — the bug only affects the CLI
binary, not boto3.**

```bash
aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=$CONTROL_PLANE_ID" \
  --query "InstanceInformationList[0].{PingStatus:PingStatus, PlatformType:PlatformType, AgentVersion:AgentVersion}" \
  --output table
```
Should return `Online`. If `LostConnection` or no output, check SSM agent and network.

**boto3 alternative (when CLI fails):**
```python
import boto3, time
ssm = boto3.client('ssm', region_name='eu-west-1')
# describe-instance-information
resp = ssm.describe_instance_information(
    Filters=[{'Key': 'InstanceIds', 'Values': ['i-0a537c94d3a3f37af']}]
)
print(resp['InstanceInformationList'][0]['PingStatus'])

# send-command + get-command-invocation polling pattern
resp = ssm.send_command(
    InstanceIds=['i-0a537c94d3a3f37af'],
    DocumentName='AWS-RunShellScript',
    Parameters={'commands': ['uname -a', 'echo ---', 'systemctl is-active verdantforged-broker-daemon']},
)
cmd_id = resp['Command']['CommandId']
time.sleep(5)
result = ssm.get_command_invocation(CommandId=cmd_id, InstanceId='i-0a537c94d3a3f37af')
print(result['Status'])
print(result.get('StandardOutputContent', ''))
```

> **Live instance id is `i-0a537c94d3a3f37af` as of 2026-06-30.** The earlier
> `i-05117b9649db5b343` is from a previous incarnation; the instance has
> since been replaced by CloudFormation. Always resolve via the
> `Role=control-plane` tag (step 2) before sending commands — don't bake
> the id into scripts.

### 6. Inspect Control Plane Services
```bash
CMD_ID=$(aws ssm send-command \
  --instance-ids "$CONTROL_PLANE_ID" \
  --document-name AWS-RunShellScript \
  --parameters '{\"commands\":[
    \"echo === Broker Daemon ===\",
    \"systemctl is-active verdantforged-broker-daemon || echo daemon-inactive\",
    \"systemctl status verdantforged-broker-daemon --no-pager -n 5 || true\",
    \"echo ---\",
    \"echo === Caddy ===\",
    \"systemctl is-active caddy || echo caddy-inactive\",
    \"curl -sS http://127.0.0.1:8080/healthz | jq . || echo daemon-port-failed\",
    \"echo ---\",
    \"echo === EFS Mount ===\",
    \"mount | grep efs || echo efs-not-mounted\",
    \"ls -la /mnt/broker/ || echo no-efs\",
    \"echo ---\",
    \"echo === Worker Bootstrap ===\",
    \"grep -n '"'"'COMPATIBLE_API_KEY'"'"' /mnt/broker/logs/worker-bootstrap.sh | head -3 || echo no-bootstrap-file\"
  ]}' \
  --query Command.CommandId \
  --output text)
```
Wait a few seconds, then retrieve output:
```bash
aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "$CONTROL_PLANE_ID" \
  --query "StandardOutputContent" \
  --output text
```

### 7. Inspect Worker (if any active)
If workers exist:
```bash
for WORKER_ID in $WORKER_IDS; do
  echo "=== Worker $WORKER_ID ==="
  CMD_ID=$(aws ssm send-command \
    --instance-ids "$WORKER_ID" \
    --document-name AWS-RunShellScript \
    --parameters '{\"commands\":[
      \"echo === Cloud-Init ===\",
      \"tail -n 20 /var/log/cloud-init-output.log || echo no-cloud-init-log\",
      \"echo ---\",
      \"echo === NemoClaw Sandbox ===\",
      \"nemohermes list --json 2>/dev/null | jq -r .sandboxes[0].name || echo no-sandboxes\",
      \"echo ---\",
      \"echo === Poller Service ===\",
      \"systemctl status worker-poller --no-pager -n 5 2>/dev/null || echo no-poller-service\",
      \"echo ---\",
      \"echo === EFS Jobs ===\",
      \"ls -la /mnt/broker/jobs/inbox/ 2>/dev/null | head -5 || echo no-inbox\",
      \"ls -la /mnt/broker/jobs/outbox/ 2>/dev/null | head -5 || echo no-outbox\"
    ]}' \
    --query Command.CommandId \
    --output text)
  sleep 5
  aws ssm get-command-invocation \
    --command-id "$CMD_ID" \
    --instance-id "$WORKER_ID" \
    --query "StandardOutputContent" \
    --output text
done
```

### 8. Check for Worker Bootstrap Corruption
The #1 cause of hung jobs is a corrupted `COMPATIBLE_API_KEY` placeholder in the worker bootstrap. If the worker is running but has no sandbox and no poller:

```bash
# Verify the bootstrap file on the control plane
grep -n 'COMPATIBLE_API_KEY' /mnt/broker/logs/worker-bootstrap.sh
# BAD: export COMPATIBLE_API_KEY=***   (literal stars, truncated, unclosed ${...})
# GOOD: export COMPATIBLE_API_KEY=*** Fix: see tee-broker-deploy-config/references/worker-bootstrap-placeholder-corruption.md
```

## Pushing a broker-daemon code change

The control plane runs the daemon from `/opt/broker-daemon/daemon.py` — there is **no
`git pull` on the host**. The deployment path is: local repo → S3 staging → SSM
download → `systemctl restart`. For a one-shot push use
`scripts/update-broker-daemon-boto3.py` (preferred — the boto3 path dodges the
SSM CLI "badly formed help string" bug on aws-cli 2.31.x + Python 3.14). The
shell wrapper `scripts/update-broker-daemon.sh` has the same semantics but
will die mid-deploy on that CLI version.

1. Stage the local file to the artifact bucket:
   ```bash
   aws s3 cp ~/hermes/competition/tee-broker-deploy/broker-daemon/daemon.py \
     s3://verdantforged-artifacts-eu-west-1/staging/daemon.py.$(date +%s)
   ```
2. On the control plane, backup + atomic move:
   ```bash
   aws ssm send-command --instance-ids "$CONTROL_PLANE_ID" \
     --document-name AWS-RunShellScript \
     --parameters '{"commands":[
       "cp -a /opt/broker-daemon/daemon.py /opt/broker-daemon/daemon.py.bak.pre-update",
       "aws s3 cp s3://verdantforged-artifacts-eu-west-1/staging/daemon.py.<stamp> /opt/broker-daemon/daemon.py.new --region eu-west-1",
       "python3 -m py_compile /opt/broker-daemon/daemon.py.new && mv /opt/broker-daemon/daemon.py.new /opt/broker-daemon/daemon.py",
       "chmod 0755 /opt/broker-daemon/daemon.py"
     ]}' \
     --query Command.CommandId --output text
   ```
3. Restart:
   ```bash
   aws ssm send-command --instance-ids "$CONTROL_PLANE_ID" \
     --document-name AWS-RunShellScript \
     --parameters '{"commands":[
       "systemctl restart verdantforged-broker-daemon",
       "sleep 3",
       "systemctl is-active verdantforged-broker-daemon",
       "curl -sS --max-time 5 http://127.0.0.1:8080/healthz"
     ]}' \
     --query Command.CommandId --output text
   ```
4. Verify with a live LLM call (see `references/llm-proxy-live-probe.md`).

The `scripts/update-broker-daemon-boto3.py` wrapper handles pre-flight checks, the
SHA-256 round-trip verify, the py_compile gate, restart, health check, and
S3 cleanup in one go. Use `scripts/update-broker-daemon.sh` only if you
specifically need the shell form.

## Common Issues and Fixes
- **Instance not found**: The active deployment is `tee-broker-deploy/` in eu-west-1. `ec2-deploy/` in eu-west-2 is legacy. Verify `AWS_DEFAULT_REGION=eu-west-1` AND use `Role=control-plane` (not `Name=verdantforged-broker-control`).
- **SSM ping failed**: Verify the IAM role includes `AmazonSSMManagedInstanceCore` and the SSM agent is running. Reboot if necessary.
- **NemoHermes not found**: The NemoClaw sandbox may not have been onboarded. Check `/var/log/cloud-init-output.log` for HTTP 401 errors (placeholder corruption) or install failures.
- **Docker not running**: Ensure the user is in the `docker` group (`sudo usermod -aG docker ubuntu && newgrp docker`).
- **Sandbox status errors**: Check logs with `nemohermes tee-worker logs` inside the instance (via SSM or SSH).
- **Job hangs in `running`**: Likely worker bootstrap corruption. Verify COMPATIBLE_API_KEY line, terminate the worker, let the daemon relaunch.
- **Broker /v1/skills returns `[]`**: Normal on a fresh worker — skills are populated after the first job. Not an error.
- **LLM responses come back with empty `content` but long `reasoning`**: The broker's per-call `max_tokens` cap is starving the visible answer. Raise `MAX_TOKENS_CAP` in `daemon.py` (default 100000) and redeploy. See `references/llm-proxy-live-probe.md` for how to prove the new cap took effect.

## Pitfalls
- **Hardcoded instance IDs**: Scripts that embed specific instance IDs (like `i-0f3fcdb4c3561baf6` from `ec2-deploy/check_setup.sh`, or the now-stale `i-05117b9649db5b343` from earlier versions of this skill) become stale when instances are replaced. Always lookup via tags or CloudFormation outputs.
- **Region mismatch**: The legacy `ec2-deploy/` stack was in eu-west-2. The active `tee-broker-deploy/` stack is in eu-west-1. Always verify `AWS_DEFAULT_REGION`.
- **`Name` tag filter returns nothing**: The CloudFormation `Name` tag is `verdantforged-broker-control-control` (stack name + logical-id suffix), not the bare stack name. Use `Role=control-plane` for the stable lookup.
- **Assuming SSH access**: If no key pair was provided, SSM is the only access method. Adjust diagnostics accordingly.
- **Worker bootstrap placeholder corruption**: A broken `${...}` or literal `***` in `COMPATIBLE_API_KEY` causes NemoClaw onboard to fail with HTTP 401, leaving the worker with no sandbox and no poller. Jobs hang in `running` state. See tee-broker-deploy-config/references/worker-bootstrap-placeholder-corruption.md.
- **SSM command shell is `/bin/sh`, not bash**: `aws ssm send-command` defaults to dash, not bash. Python heredocs fail with `Syntax error: "(" unexpected`, and `[[ ]]` is bash-only. For any non-trivial Python on the control plane, base64-encode the script and pipe through `echo $B64 | base64 -d | python3 -` — that sidesteps shell-vs-JSON quote escaping entirely.
- **Broker daemon is not on `git pull`**: The control plane's `/opt/broker-daemon/daemon.py` is a hand-pushed file. Edits to the local repo do NOT propagate automatically. Use `scripts/update-broker-daemon-boto3.py` (or the manual S3-stage-then-SSM-download recipe in **Pushing a broker-daemon code change**) to push a code change and restart.
- **Two worktrees with divergent `daemon.py`**: As of 2026-06-30 the project lives in two git worktrees: `~/hermes/competition/` (on `feature/s3-encrypted-artifacts` — older) and `~/hermes/competition-wt-nemoclaw/` (on `main` — the deployment source of truth). They have different `daemon.py` files. The deploy script's `REPO_DAEMON` default points at `competition/`, which is the WRONG one for pushing main-branch fixes — pass the file path explicitly, or `export REPO_DAEMON=~/hermes/competition-wt-nemoclaw/tee-broker-deploy/broker-daemon/daemon.py`, or just use the boto3 wrapper with an explicit positional arg. Verify with `git -C <worktree> log -1 -- broker-daemon/daemon.py` before pushing.
- **Caddy proxies 443 → daemon, not 8080**: The shell script's post-restart `curl 127.0.0.1:8080/healthz` returns connection-refused on this deployment because the daemon is not bound to 8080 directly (Caddy is). Trust `systemctl is-active` + the public `curl https://verdant.codepilots.co.uk/healthz` instead. The boto3 wrapper already does this.
- **Hybrid mode flags need updates in every branch, not just the one you're fixing**: The `BROKER_TEST_SPT_ISSUER` flag was added to gate demo SPT routing. The first commit (8ce2806) updated 3 sites: the token extractor, the demo mint endpoint, and the `elif` stub branch in `submit_job`. It MISSED the first `if STRIPE_SECRET_KEY and not payment_stub_mode:` charge gate in `submit_job` AND the three sibling functions `capture_payment` / `refund_payment` / `verify_payment_intent` (which all gated on `not STRIPE_SECRET_KEY` alone). The bug surfaced as 402 `No such shared_payment_token` from real Stripe on every demo job. When adding any hybrid-mode flag to this codebase, grep for the OLD predicate (e.g. `not STRIPE_SECRET_KEY`) and update every site — the payment path has at least 4.

## Verification
After confirming the control plane is running and SSM is online, you can proceed to interact with the broker:
1. Submit a test job: `curl -sS -X POST https://verdant.codepilots.co.uk/v1/jobs ...`
2. Check job status: `curl -sS https://verdant.codepilots.co.uk/v1/jobs/<id>`
3. Verify completion: outbox file appears in `/mnt/broker/jobs/outbox/<id>.json`
4. Probe the LLM proxy directly: see `references/llm-proxy-live-probe.md` (mint a token, POST, check `content`)

## References
- Active CloudFormation template: `tee-broker-deploy/cloudformation-control-plane.yaml`
- Worker user-data source: `tee-broker-deploy/worker/user-data.sh`
- Broker daemon: `tee-broker-deploy/broker-daemon/daemon.py`
- Worker bootstrap placeholder corruption: `tee-broker-deploy-config/references/worker-bootstrap-placeholder-corruption.md`
- LLM proxy live probe recipe: `references/llm-proxy-live-probe.md`
- Live broker-daemon push script: `scripts/update-broker-daemon.sh`
- Legacy (inactive): `ec2-deploy/cloudformation.yaml`
