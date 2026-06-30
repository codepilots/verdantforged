# Skill Library — End-to-end smoke test (2026-06-29)

Confirms the standalone skill-library service runs on the live broker control plane (i-05117b9649db5b343 in `eu-west-1`) without disturbing the running broker daemon.

**Verdict: PASSED.** 3 skills registered via `push_skills.sh`, broker daemon PID unchanged.

---

## Setup

- Control plane IP: `176.34.244.180`, public URL `https://verdant.codepilots.co.uk`
- Test driver: `i-05117b9649db5b343` (eu-west-1, t3.small)
- Library code: pushed to control plane via S3 + SSM (`/opt/broker-daemon/skill_library/`)
- Library runtime: `nohup uvicorn skill_library.app:app --host 127.0.0.1 --port 8091` (shadow process, no systemd install)
- Hermes redaction munges any token-shaped strings in display — actual bytes on disk and over the wire are NOT redacted

## Step 1 — Pre-check broker daemon

```
=== broker daemon status ===
  16258    02:52:03 /usr/bin/python3 /opt/broker-daemon/daemon.py
=== public broker healthz ===
{"ok": true, "worker": false}
```

Daemon PID 16258 captured as the regression baseline. Expected to remain **identical** at the post-check.

## Step 2 — Sync library code to control plane

```bash
aws s3 cp --recursive skill_library/ s3://verdantforged-artifacts-eu-west-1/tmp/skill_library/
aws s3 cp scripts/push_skills.sh s3://verdantforged-artifacts-eu-west-1/tmp/push_skills.sh
```

Via SSM, pulled onto the control plane:

```bash
mkdir -p /opt/broker-daemon/skill_library
aws s3 cp --recursive s3://verdantforged-artifacts-eu-west-1/tmp/skill_library/ /opt/broker-daemon/skill_library/
mkdir -p /opt/broker-daemon/scripts
aws s3 cp s3://verdantforged-artifacts-eu-west-1/tmp/push_skills.sh /opt/broker-daemon/scripts/push_skills.sh
chmod +x /opt/broker-daemon/scripts/push_skills.sh
pip install --user --break-system-packages fastapi==0.115.0 uvicorn==0.30.6 pydantic==2.9.2 httpx==0.27.2
```

Installed Python deps (PEP-668 forced `--break-system-packages`). Verified:

```
fastapi 0.115.0, uvicorn 0.30.6, pydantic 2.9.2, httpx 0.27.2 — import OK
from skill_library.app import app — succeeds
/opt/broker-daemon/skill_library/, /opt/broker-daemon/scripts/push_skills.sh — populated
```

## Step 3 — Start library service as a shadow nohup process

```bash
export SKILL_LIBRARY_API_KEY="shadow-test-key-2026-06-29"
export SKILL_LIBRARY_DB="/tmp/skill_library_shadow.db"
export SKILL_LIBRARY_FILES_DIR="/tmp/skill_library_files_shadow"
cd /opt/broker-daemon
nohup python3 -m uvicorn skill_library.app:app --host 127.0.0.1 --port 8091 \
    > /tmp/skill_library_shadow.log 2>&1 &
sleep 4
curl http://127.0.0.1:8091/healthz
```

Result:

```
uvicorn PID 17252 running
curl http://127.0.0.1:8091/healthz → {"ok":true,"db":"ok","efs":"ok"}
```

## Step 4 — Push existing skills via `push_skills.sh`

The script was previously broken (BUG-003 — `***` literal instead of `$2`). Pushed a fix, re-uploaded, re-ran.

```bash
cd /opt/broker-daemon/worker/skills
bash /opt/broker-daemon/scripts/push_skills.sh --source-dir . \
    --library-url http://127.0.0.1:8091 --api-key "shadow-test-key-2026-06-29"
curl http://127.0.0.1:8091/v1/library/skills
```

Output:

```
POST code-review@0.1.0
  upload SKILL.md (894 bytes, text/markdown)
POST photo-glow-up@0.1.0
  upload SKILL.md (984 bytes, text/markdown)
POST summarize@0.1.0
  upload SKILL.md (724 bytes, text/markdown)

Done. pushed=3 failed=0
exit: 0

GET /v1/library/skills →
{"skills":[
  {"name":"code-review","version":"0.1.0","summary":"Structured code review with concrete suggestions. Pass/fail per finding.","file_count":1,"total_bytes":894},
  {"name":"photo-glow-up","version":"0.1.0","summary":"Local photo enhancement using WASM Rust binary. No external API.","file_count":1,"total_bytes":984},
  {"name":"summarize","version":"0.1.0","summary":"One-paragraph summary of a passage of text. Honest, concise, no embellishment.","file_count":1,"total_bytes":724}
]}
```

All 3 skills registered, each with 1 file (`SKILL.md`) and matching byte counts. No broker interaction (broker key was intentionally not configured on the library server, so `--sync` was deliberately omitted from this smoke test).

## Step 5 — Regression check (broker daemon unchanged)

```
=== broker daemon status ===
  16258    02:53:03 /usr/bin/python3 /opt/broker-daemon/daemon.py
=== public broker healthz ===
{"ok": true, "worker": false}
```

**Broker daemon PID 16258 is identical to step 1** (uptime shows +50s elapsed during the smoke test — just elapsed time, no restart). Broker `/healthz` still 200.

## Step 6 — Tear down shadow uvicorn

```bash
pkill -f "uvicorn skill_library"
```

```
CLEANED  # no uvicorn skill_library processes remaining
```

Plus removed `/tmp/skill_library_shadow.*` artifacts.

## Final post-check

```
=== FINAL POST-CHECK ===
  16258    02:53:32 /usr/bin/python3 /opt/broker-daemon/daemon.py
{"ok": true, "worker": false}
NO_UVICORN_RUNNING
```

Clean state. Library code is staged at `/opt/broker-daemon/skill_library/` but the systemd unit is **NOT** installed — that's a manual sudo step for the operator (per `docs/skill-library-deploy.md` Step 3).

---

## Issues caught

| Issue | Severity | Resolution |
|---|---|---|
| `python3.12` on control plane enforces PEP 668 (`externally-managed-environment`) | blocker | `pip install --user --break-system-packages ...` |
| AWS-RunShellScript runs in `sh`, not `bash`; `set -o pipefail` fails | workaround | invoke scripts as `bash /opt/broker-daemon/scripts/push_skills.sh` |
| push_skills.sh had a redaction-corrupted line 21 | blocker | BUG-003 — fixed via patch tool |
| Hermes tool displays redacted tokens in stdout even when on-disk bytes are correct | cosmetic | use base64 dumps to verify state |
| The library's `bc math` / 50-cent floor is host-default; not relevant to library, only to broker | n/a | (cross-reference: BUG-001 in broker) |

## What works

- All T1–T7 tests (28 pytest) pass locally
- Library service starts cleanly with shadow env vars
- End-to-end POST `/v1/library/skills` + uploads land + GET retrieval works on the live control plane
- Broker daemon and `/healthz` untouched across the entire smoke test
- `--api-key` CLI form now correctly substitutes the env-supplied bearer token
- All bash scripts parse-clean (`bash -n`)

## Next steps

For the user (manual sudo, per memory rule "Agent never asks for sudo"):

1. Install the systemd unit: `sudo cp systemd/skill-library.service /etc/systemd/system/ && sudo systemctl enable --now skill-library.service`
2. Generate a real API key: `python3 -c "import secrets; print('SKILL_LIBRARY_API_KEY=' + secrets.token_urlsafe(48))"` and paste into `/opt/broker-daemon/config.env`
3. (Optional) Wire `BROKER_SKILLS_API_KEY` into the library service env to enable `--sync` from the deploy runbook
4. Run `scripts/push_skills.sh` with `--build --sync` to compile Rust skills and forward them to the broker

## Artifacts

- Test PIs and jobs: none created (no broker interaction)
- Shadow DB at `/tmp/skill_library_shadow.db`: removed during teardown
- Logs at `/tmp/skill_library_shadow.log`: removed during teardown
