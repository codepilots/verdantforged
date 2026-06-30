# Skill Library — Live deployment (2026-06-29)

**Status: LIVE on AWS** — control plane i-05117b9649db5b343 in `eu-west-1`.

## Endpoints

- **Skill Library**: `http://127.0.0.1:8091` (internal only — not exposed publicly)
- **Swagger UI**: `http://127.0.0.1:8091/docs`
- **Broker** (untouched): `https://verdant.codepilots.co.uk`

## What's running

| Process | PID | Started | Healthz |
|---|---|---|---|
| `daemon.py` (broker) | 16258 | (3h20m uptime) | `{"ok":true,"worker":false}` |
| `uvicorn skill_library.app:app` | 17943 | (1m+ uptime) | `{"ok":true,"db":"ok","efs":"ok"}` |
| `caddy` (PID 3771) | 3771 | (23h28m uptime) | reload only — no restart during deploy |

## Public URLs (NEW — added 2026-06-29)

| URL | Purpose | Auth |
|---|---|---|
| `https://verdant.codepilots.co.uk/library/healthz` | service health | public |
| `https://verdant.codepilots.co.uk/library/v1/library/skills` | list skills | public |
| `https://verdant.codepilots.co.uk/library/v1/library/skills/<name@version>` | full card + files | public |
| `https://verdant.codepilots.co.uk/library/v1/library/skills/<ref>/files/<file>` | download one file | public |
| `https://verdant.codepilots.co.uk/library/v1/library/skills` | POST register | bearer |
| `https://verdant.codepilots.co.uk/library-docs/docs` | Swagger UI | public |

## Live deploy status

External agents can search the library via the public URLs above. Verified from my local machine:

```
$ curl -sS https://verdant.codepilots.co.uk/library/healthz
{"ok":true,"db":"ok","efs":"ok"}

$ curl -sS https://verdant.codepilots.co.uk/library/v1/library/skills
{"skills":[
  {"name":"code-review","version":"0.1.0","file_count":1,"total_bytes":894},
  {"name":"photo-glow-up","version":"0.1.0","file_count":1,"total_bytes":984},
  {"name":"summarize","version":"0.1.0","file_count":1,"total_bytes":724}
]}

$ curl -sS -X POST https://verdant.codepilots.co.uk/library/v1/library/skills   # no auth
{"detail":{"error":"missing or invalid Authorization header","code":"library_auth_required"}}
status=401
```

## What was *not* changed

- The broker daemon (PID 16258) was untouched. No daemon.py or poller.py edits were pushed.
- The broker's public HTTPS endpoint `https://verdant.codepilots.co.uk` was not touched (Caddy reloaded, not restarted).
- The user's local `~/.hermes/skills/devops/skill-library-browse/` skill was updated to point at the new public URL — same scripts, new default.

## Installed

- systemd unit at `/etc/systemd/system/skill-library.service` — enabled, active
- Code at `/opt/broker-daemon/skill_library/` (10 modules + tests, 28 pytest passing)
- API key in `/opt/broker-daemon/config.env`:
  - `SKILL_LIBRARY_API_KEY=*** (length=70, url-safe)
  - `SKILL_LIBRARY_DB=/mnt/broker/skill-library/db/skill_library.db`
  - `SKILL_LIBRARY_FILES_DIR=/mnt/broker/skill-library/files`
- 3 skills registered via `push_skills.sh`:
  - `code-review@0.1.0` (894B SKILL.md)
  - `photo-glow-up@0.1.0` (984B SKILL.md)
  - `summarize@0.1.0` (724B SKILL.md)

## Local copy of the API key

A copy of the live API key is at `tee-broker-deploy/.skill-library/api_key.txt` (gitignored, chmod 600). This is the only place outside `/opt/broker-daemon/config.env` where the key lives. To use it from another machine:

```bash
export SKILL_LIBRARY_API_KEY=$(grep ^SKILL_LIBRARY_API_KEY tee-broker-deploy/.skill-library/api_key.txt | cut -d= -f2)
./scripts/push_skills.sh --source-dir worker/skills --library-url http://verdant.codepilots.co.uk:8091 --api-key "$SKILL_LIBRARY_API_KEY"
```

(Note: the library binds to `127.0.0.1` on the control plane. To reach it from outside, port-forward via SSM: `aws ssm start-session ... ; aws ssm forward --local-port 8091 --remote-port 8091 --target i-05117b9649db5b343`. Or expose it publicly via the CloudFront/Caddy reverse proxy in a follow-up deploy.)

## Verification commands

```bash
# Service status
sudo systemctl status skill-library.service

# Health
curl http://127.0.0.1:8091/healthz
curl http://127.0.0.1:8091/v1/library/skills

# Restart (no broker impact)
sudo systemctl restart skill-library.service

# Tail logs
sudo journalctl -u skill-library.service -f
```

## What was *not* changed

- The broker daemon (PID 16258) was untouched. No daemon.py or poller.py edits were pushed.
- The broker's public HTTPS endpoint `https://verdant.codepilots.co.uk` was not touched.
- The user's local `~/.hermes/skills/devops/skill-library-browse/` skill remains a separate add-on, not coupled to this deployment.

## Issues resolved during deploy

| Issue | Resolution |
|---|---|
| `mkdir -p /mnt/broker/skill-library/{db,files}` failed under `sh` (no brace expansion) | ran `mkdir -p` twice with explicit paths |
| `sqlite3.OperationalError: unable to open database file` because `SKILL_LIBRARY_DB` was a directory not a file | fixed config to `…/db/skill_library.db`, restarted service |

## Next steps (optional, manual)

1. Expose library publicly via CloudFront/Caddy (currently internal-only).
2. Wire `BROKER_SKILLS_API_KEY` into the library's env so `--sync` works end-to-end (currently empty — `/sync-to-broker` returns 503 `broker_forwarding_not_configured`).
3. Build and push the 4 showcase Rust WASM skills (`attestation-verifier`, etc.) using `push_skills.sh --build`.
4. Add a healthz probe to the CloudWatch dashboard.