# Skill Library - live deploy walkthrough (2026-06-29)

This is the worked example that exercises every step of the standalone
service deploy reference end-to-end against the live control plane
i-05117b9649db5b343 in eu-west-1. Two live runs:

1. Shadow uvicorn smoke test - non-destructive, no systemd touched.
   Verified all routes end-to-end on port 8091.
2. Production systemd deploy - persistent service, public via Caddy.

## What was deployed

A standalone FastAPI service that catalogues and serves broker-compatible
skills (name, version, files, optional sha256 manifest hash, optional
WASM blob). Bound to 127.0.0.1:8091, public via Caddy at
https://verdant.codepilots.co.uk/library/*.

Service code: tee-broker-deploy/skill_library/ (10 Python modules + 4
test files, 28 tests passing locally).
CLI: tee-broker-deploy/scripts/push_skills.sh.
systemd unit: tee-broker-deploy/systemd/skill-library.service.

## Step-by-step what actually ran

### Step 0 - shadow uvicorn smoke test

Started a temporary uvicorn on port 8091 (no systemd) to validate the
service end-to-end before making it persistent. The systemd unit was
NOT touched. After verifying, killed the shadow uvicorn and cleaned
up temp files.

### Step 1 - generate API key

Generated a strong API key with secrets.token_urlsafe(48) prefixed
with sklib_. Stored the value in a local file .skill-library/api_key.txt
under tee-broker-deploy/ (gitignored, chmod 600). Length: 70 chars.

### Step 2 - append env vars to /opt/broker-daemon/config.env

Three new env vars appended to the existing config.env (using tee -a
so existing entries are preserved):
- SKILL_LIBRARY_API_KEY=<generated>
- SKILL_LIBRARY_DB=/mnt/broker/skill-library/db/skill_library.db
- SKILL_LIBRARY_FILES_DIR=/mnt/broker/skill-library/files

NOTE: SKILL_LIBRARY_DB must point to a FILE path, not a directory.
The FastAPI app calls os.makedirs(os.path.dirname(cfg.db_path)) and
then sqlite3.connect(cfg.db_path). If you point it at a directory,
sqlite3 fails with "unable to open database file". The original
mistake was setting DB to the parent directory; fix was to set it
to the .db file inside that directory.

### Step 3 - create the data directories on EFS

sudo mkdir -p /mnt/broker/skill-library/{db,files}
sudo chown -R ubuntu:ubuntu /mnt/broker/skill-library

NOTE: SSM RunShellScript runs under /bin/sh (dash), NOT bash.
Brace expansion {db,files} does NOT work in sh - it creates a
literal directory named {db,files}. Either use explicit paths
or wrap in bash -c. See Pitfall 20 in the SKILL.md main file.

### Step 4 - install systemd unit

sudo cp /opt/broker-daemon/systemd/skill-library.service \
       /etc/systemd/system/skill-library.service
sudo systemctl daemon-reload
sudo systemctl enable --now skill-library.service

The service unit uses EnvironmentFile=/opt/broker-daemon/config.env
so it picks up SKILL_LIBRARY_API_KEY, SKILL_LIBRARY_DB, and
SKILL_LIBRARY_FILES_DIR automatically.

### Step 5 - first start failure (PID 17943 died immediately)

service status showed active (failed) with status=3/exit. Journal
showed sqlite3.OperationalError: unable to open database file.

Root cause: SKILL_LIBRARY_DB was set to /mnt/broker/skill-library/db
(directory), not /mnt/broker/skill-library/db/skill_library.db (file).
The directory existed (created in step 3) but the file inside it did
not. The app calls os.makedirs(os.path.dirname(db_path)) which only
creates the parent dir, not the db file itself.

Fix: updated config.env to point DB at the file path. Restarted
service. Status flipped to active (running), PID 17943.

### Step 6 - verify health

curl http://127.0.0.1:8091/healthz
Response: {"ok":true,"db":"ok","efs":"ok"}

### Step 7 - broker daemon regression check

Critical: confirm the broker daemon PID was NOT touched.
ps -eo pid,etime,cmd | grep daemon.py
Expected: PID 16258, etime ~3h+ (unchanged from before the deploy).
Result: PID 16258, etime 03:12:34 - no change.

### Step 8 - 60-second persistence check

Sleep 60. Re-check:
- systemctl status skill-library.service: still active, same PID.
- ps -eo pid,etime for skill-library: still PID 17943, etime 01:17.
- curl healthz: still 200.
- ps for daemon.py: still PID 16258, etime 03:13:34 (+60s as expected).

All processes persistent. Broker untouched.

### Step 9 - register existing skills

The deploy_skill_library.sh script bundles steps 1-9 above plus the
push of the 3 existing skills. Pushed via:
  scripts/push_skills.sh --url http://127.0.0.1:8091 --api-key <key> \
    <skill-dir-1> <skill-dir-2> <skill-dir-3>

After push:
  curl http://127.0.0.1:8091/v1/library/skills
  Returns 3 skills: code-review, photo-glow-up, summarize.

## Step 10 - Caddy public exposure

Merged two handle_path blocks into /etc/caddy/Caddyfile BEFORE the
handle / { root block:

  handle_path /library/* { reverse_proxy 127.0.0.1:8091 }
  handle_path /library-docs/* { reverse_proxy 127.0.0.1:8091 }

CRITICAL pre-validation step (do this BEFORE reload):
  sudo -n caddy adapt --config /tmp/Caddyfile.new \
    --envfile /opt/broker-daemon/config.env

Expected: EXIT_CODE=0, stderr empty. If non-zero, do NOT reload.

Reload (NOT restart - reload preserves PID and in-flight connections):
  sudo systemctl reload caddy

Caddy version: 2.11.4 (the official apt package, NOT a custom build).
This package does NOT include the rate_limit module - drop that
directive if you try to use it.

## Verification matrix

After the full deploy:
| Check | Endpoint / command | Result |
| Public healthz | curl https://verdant.codepilots.co.uk/library/healthz | 200 |
| Public list | curl https://verdant.codepilots.co.uk/library/v1/library/skills | 3 skills |
| Public docs | curl https://verdant.codepilots.co.uk/library-docs/openapi.json | valid JSON |
| Broker untouched | curl https://verdant.codepilots.co.uk/healthz | 200 |
| Library uptime | systemctl status skill-library.service | active 1m+ |
| Broker uptime | ps -eo etime for daemon.py | unchanged +60s |
| Caddy uptime | ps -eo etime for caddy | unchanged (reload only) |
| Auth gate | POST without Bearer | 401 library_auth_required |

All green. End-to-end deploy from local code to public URL working.

## What was NOT done (deliberately)

- The skill library code does NOT call the broker daemon. It is
  fully standalone. The push_skills.sh --sync flag exists but is
  not exercised - it would POST to broker /v1/skills and
  /v1/skills/{name}/wasm if you want the library to also register
  with the broker. Not needed for the live demo.
- No rate limiting on public GETs. The official Caddy apt package
  does not include the rate_limit module. Add a CloudFront/WAF
  layer for production hardening.
- No TLS client cert verification. Public HTTPS via Caddy's automatic
  Let's Encrypt is sufficient for the demo.

## Reusable deploy script

The full 9-step recipe is bundled as
tee-broker-deploy/scripts/deploy_skill_library.sh. Run with sudo
on the control plane. Idempotent - safe to re-run after partial
deploys.

The script uses S3 as an intermediary for the large files (systemd
unit, Caddyfile, push_skills.sh, requirements.txt) so they don't
hit the SSM 97KB command-parameter ceiling. See Pitfall in the
main SKILL.md about SSM file-size limits.

## Files written this run

In the repo (committed):
- tee-broker-deploy/scripts/deploy_skill_library.sh (NEW)
- tee-broker-deploy/docs/skill-library-deploy.md (REWRITTEN to
  point at the deploy script)
- tee-broker-deploy/docs/skill-library-live-2026-06-29.md (NEW,
  status report)
- tee-broker-deploy/docs/file-jobs.md was merged in from a
  sibling feature branch (file-jobs).
- ~/.hermes/skills/devops/skill-library-browse/{SKILL.md, list,
  install, download} (NEW skill for browsing the library)

On the server:
- /etc/systemd/system/skill-library.service
- /opt/broker-daemon/config.env (3 new lines appended)
- /mnt/broker/skill-library/db/skill_library.db
- /mnt/broker/skill-library/files/<sha256-blob>
- /etc/caddy/Caddyfile (2 handle_path blocks merged)

NOT in the repo (gitignored):
- tee-broker-deploy/.skill-library/api_key.txt (chmod 600,
  contains the generated API key)
- tee-broker-deploy/infrastructure/caddy/Caddyfile.skill-library.live
  (live snapshot, regenerated by deploy)
- tee-broker-deploy/systemd/skill-library.service.live (live snapshot,
  same)

## Commits this run

- 878d9dd: docs(skill-library) deploy script + runbook
- 380401d: docs(skill-library) live status update with public URLs
- 931aaa3: docs(skill-library) cross-link file-jobs.md

Plus the 17 prior skill-library commits from the original T1-T13
build (config.py, db.py, storage.py, models.py, routes, app.py,
push_skills.sh, systemd unit, docs, Hermes skill).