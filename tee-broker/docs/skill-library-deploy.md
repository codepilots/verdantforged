# Skill Library — deploy runbook

End-to-end deploy of the standalone skill-library service onto the existing
broker control plane (i-05117b9649db5b343 in eu-west-1).

**TL;DR**: run `sudo scripts/deploy_skill_library.sh` from anywhere with
AWS CLI access. The script handles every step idempotently.

---

## Public endpoints (after deploy)

| URL | Purpose |
|---|---|
| `https://verdant.codepilots.co.uk/library/healthz` | service health (public) |
| `https://verdant.codepilots.co.uk/library/v1/library/skills` | list skills (public) |
| `https://verdant.codepilots.co.uk/library/v1/library/skills/<name@version>` | full card + files (public) |
| `https://verdant.codepilots.co.uk/library/v1/library/skills/<ref>/files/<file>` | download one file (public) |
| `https://verdant.codepilots.co.uk/library/v1/library/skills` | POST register (bearer) |
| `https://verdant.codepilots.co.uk/library/v1/library/skills/<ref>/files/<file>` | POST upload (bearer) |
| `https://verdant.codepilots.co.uk/library/v1/library/skills/<ref>/sync-to-broker` | POST forward to broker (bearer) |
| `https://verdant.codepilots.co.uk/library-docs/docs` | interactive Swagger UI (public) |
| `https://verdant.codepilots.co.uk/library-docs/openapi.json` | OpenAPI spec (public) |

Bearer token lives at `/opt/broker-daemon/config.env` as `SKILL_LIBRARY_API_KEY`.

---

## 0. Prerequisites

- Existing broker already running on the same instance.
- EFS mount present at `/mnt/broker/`.
- `aws` CLI on the operator machine (for the SSM-driven path) OR direct
  shell access to the control plane as root.
- `cargo` + `wasm32-wasip1` rustup target installed if you intend to
  rebuild any Rust skills (`--build`).

---

## 1. Automated deploy (preferred)

Run the consolidated deploy script. It pulls code, installs Python deps,
generates an API key if needed, installs the systemd unit, merges
Caddy routes, and pushes the existing 3 prompt-template skills.

### 1a. From a remote operator machine (via SSM)

```bash
cd /home/autumn/hermes/competition-wt-nemoclaw/tee-broker-deploy

# Push the script to S3 so the control plane can pull it
aws s3 cp scripts/deploy_skill_library.sh s3://verdantforged-artifacts-eu-west-1/deploy_skill_library.sh

# Run on the control plane via SSM (use boto3, not the aws CLI — see BUG-003-style
# quoting gotchas around Bearer tokens and braces)
python3 - <<'PY'
import boto3, time
ssm = boto3.client('ssm', region_name='eu-west-1')
r = ssm.send_command(
    InstanceIds=['i-05117b9649db5b343'],
    DocumentName='AWS-RunShellScript',
    Parameters={'commands': [
        'aws s3 cp s3://verdantforged-artifacts-eu-west-1/deploy_skill_library.sh /tmp/deploy_skill_library.sh',
        'chmod +x /tmp/deploy_skill_library.sh',
        'sudo bash /tmp/deploy_skill_library.sh',
    ]},
    TimeoutSeconds=600,
)
inv = ssm.get_command_invocation(CommandId=r['Command']['CommandId'], InstanceId='i-05117b9649db5b343')
# poll until done
while inv['Status'] not in ('Success','Failed','Cancelled','TimedOut'):
    time.sleep(5)
    inv = ssm.get_command_invocation(CommandId=r['Command']['CommandId'], InstanceId='i-05117b9649db5b343')
print('status:', inv['Status'])
print('STDOUT:', inv['StandardOutputContent'])
if inv.get('StandardErrorContent'):
    print('STDERR:', inv['StandardErrorContent'][:1000])
PY
```

### 1b. From a shell on the control plane

```bash
sudo /home/ubuntu/tee-broker-deploy/scripts/deploy_skill_library.sh
# Or, if the script isn't on the instance yet:
sudo bash <(curl -s https://verdant.codepilots.co.uk/...)   # not yet hosted; prefer SSM path
```

The script is **idempotent** — re-running it after a partial deploy resumes
from wherever it stopped without breaking the live service.

---

## 2. Manual steps (only if you want fine control)

### 2a. Pull code from S3

```bash
cd /home/autumn/hermes/competition-wt-nemoclaw/tee-broker-deploy
S3=verdantforged-artifacts-eu-west-1
aws s3 cp --recursive skill-library/ s3://$S3/tmp/skill_library/
aws s3 cp scripts/push_skills.sh s3://$S3/tmp/push_skills.sh
```

Then on the control plane (via SSM):

```python
import boto3
ssm = boto3.client('ssm', region_name='eu-west-1')
ssm.send_command(
    InstanceIds=['i-05117b9649db5b343'],
    DocumentName='AWS-RunShellScript',
    Parameters={'commands': [
        'set -e',
        'mkdir -p /opt/broker-daemon/skill_library',
        'aws s3 cp --recursive s3://verdantforged-artifacts-eu-west-1/tmp/skill_library/ /opt/broker-daemon/skill_library/',
        'mkdir -p /opt/broker-daemon/scripts',
        'aws s3 cp s3://verdantforged-artifacts-eu-west-1/tmp/push_skills.sh /opt/broker-daemon/scripts/push_skills.sh',
        'chmod +x /opt/broker-daemon/scripts/push_skills.sh',
    ]},
    TimeoutSeconds=120,
)
```

### 2b. Install Python deps

The control plane runs Python 3.12 with PEP 668 enforced; use
`--break-system-packages`:

```python
ssm.send_command(
    InstanceIds=['i-05117b9649db5b343'],
    DocumentName='AWS-RunShellScript',
    Parameters={'commands': [
        'pip3 install --break-system-packages fastapi==0.115.0 uvicorn==0.30.6 pydantic==2.9.2 httpx==0.27.2',
    ]},
)
```

### 2c. Generate + install API key

The deploy script generates the key automatically. To do it manually:

```bash
python3 -c "import secrets; print('SKILL_LIBRARY_API_KEY=' + secrets.token_urlsafe(48))"
# Append to /opt/broker-daemon/config.env (chmod 600)
# Also append:
#   SKILL_LIBRARY_DB=/mnt/broker/skill-library/db/skill_library.db
#   SKILL_LIBRARY_FILES_DIR=/mnt/broker/skill-library/files
```

### 2d. Install systemd unit + start

```bash
sudo cp systemd/skill-library.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now skill-library.service
sudo systemctl status skill-library.service
curl -s http://127.0.0.1:8091/healthz
```

Expected: `{"ok":true,"db":"ok","efs":"ok"}`.

### 2e. Merge library routes into Caddyfile

The deploy script does this automatically. To do it manually, append to
`/etc/caddy/Caddyfile` BEFORE the `handle /v1/* {` block in BOTH the
`{$BROKER_DOMAIN...}` site and the `:80` site:

```caddy
    # Skill Library — public reads + bearer-authenticated writes.
    handle_path /library/* {
        reverse_proxy 127.0.0.1:8091
    }
    handle_path /library-docs/* {
        reverse_proxy 127.0.0.1:8091
    }
```

Validate + reload (no broker restart):

```bash
sudo caddy adapt --config /etc/caddy/Caddyfile --envfile /opt/broker-daemon/config.env > /tmp/caddy_adapted.json
sudo systemctl reload caddy
```

---

## 3. Push existing skills

The deploy script pushes the 3 prompt-template skills already in
`tee-broker-deploy/worker/skills/`. To push the Rust WASM skills
separately (requires `cargo` + `wasm32-wasip1`):

```bash
cd /home/autumn/hermes/competition-wt-nemoclaw/tee-broker-deploy
./scripts/push_skills.sh \
    --source-dir ../tee-broker-pattern/tee-broker-skills/code-review \
    --library-url https://verdant.codepilots.co.uk/library \
    --api-key "$SKILL_LIBRARY_API_KEY" \
    --build
```

---

## 4. Verify public access

```bash
# Public reads (no auth)
curl -sS https://verdant.codepilots.co.uk/library/healthz
curl -sS https://verdant.codepilots.co.uk/library/v1/library/skills
curl -sS https://verdant.codepilots.co.uk/library/v1/library/skills/summarize@0.1.0/files/SKILL.md | head -5

# Interactive Swagger
xdg-open https://verdant.codepilots.co.uk/library-docs/docs

# Authenticated writes
curl -sS -X POST https://verdant.codepilots.co.uk/library/v1/library/skills \
    -H "Authorization: Bearer $SKILL_LIBRARY_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"name":"my-skill","version":"1.0.0","description":"hello"}'
```

---

## 5. (Optional) Sync to broker

The library can forward each card into the live broker via
`POST /v1/library/skills/{ref}/sync-to-broker` (which calls the broker's
`POST /v1/skills` and `POST /v1/skills/{n}/wasm` endpoints). Requires
`BROKER_SKILLS_API_KEY` to be set in the library's env (currently empty
— see status report).

```bash
./scripts/push_skills.sh --source-dir worker/skills --sync ...
```

---

## 6. Rollback

```bash
sudo systemctl stop skill-library.service
sudo systemctl disable skill-library.service
sudo rm /etc/systemd/system/skill-library.service
# Caddy: revert /etc/caddy/Caddyfile to .bak and reload
# Library DB + files left in place — re-enable to resume from same state.
```

The library is **additive** — no broker daemon restart, no API surface
changes on the broker.

**Related**: the broker itself supports a separate "file jobs" feature
(two-phase worker-first upload + encrypted attachment lifecycle). See
[`docs/file-jobs.md`](file-jobs.md) — that's broker-side code, not part
of the library service.

---

## 7. Files this deploy touches

| Local repo | Live on control plane |
|---|---|
| `tee-broker-deploy/skill_library/` | `/opt/broker-daemon/skill_library/` |
| `tee-broker-deploy/scripts/push_skills.sh` | `/opt/broker-daemon/scripts/push_skills.sh` |
| `tee-broker-deploy/scripts/deploy_skill_library.sh` | (run from anywhere, idempotent) |
| `tee-broker-deploy/systemd/skill-library.service` | `/etc/systemd/system/skill-library.service` |
| (Caddyfile block) | `/etc/caddy/Caddyfile` (handles /library/* and /library-docs/*) |
| (config.env entries) | `/opt/broker-daemon/config.env` (SKILL_LIBRARY_*) |
| `~/.hermes/skills/devops/skill-library-browse/scripts/*.sh` | (agent skill, defaults to public URL) |

**Untouched** across this deploy:
- `/opt/broker-daemon/daemon.py` (broker daemon, PID 16258)
- `/opt/broker-daemon/poller.py` (worker poller)
- `/opt/broker-daemon/user-data.sh` (worker bootstrap)
- `/mnt/broker/jobs/`, `/mnt/broker/results/`, `/mnt/broker/logs/broker.db`
- All other EFS-mounted artifacts