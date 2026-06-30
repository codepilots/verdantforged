---
name: tee-broker-deploy-config
description: When configuring, redeploying, or debugging the VerdantForged TEE Broker, load this before editing deploy/bootstrap/CloudFormation paths. Covers config propagation, Stripe ACS/SPT demo modes, worker cold-start vs identity-gate stalls, live config drift, EC2 user-data limits, and marketing-copy ground-truth checks. Class-level umbrella — see references/ for session-specific deep dives.
tags: [tee-broker, deploy, secrets, cloudformation, aws, nemoclaw]
related_skills: [ad-hoc-verification-script, kanban-operations, tee-broker-protocol, tee-broker-pattern, aws-ephemeral-deploy, nemoclaw-hermes-sandbox-setup]
---

# tee-broker-deploy-config

Class-level umbrella for the VerdantForged TEE Broker deploy path. Load this
before touching `deploy.sh`, `scripts/bootstrap-control-plane.sh`, the
`cloudformation-*.yaml` templates, the `worker/` cloud-init, or the
`broker-daemon/` secrets/SSM plumbing.

## What you'll find here

- **Umbrella** (this file): triggers, top-level pitfalls, navigation map.
- **references/**: deep dives on specific failure modes and the canonical
  payload / schema for each attestation surface.

## Triggers (load this skill when you are about to ...)

- Edit `tee-broker-deploy/deploy.sh`, `scripts/bootstrap-control-plane.sh`, or
  any `cloudformation-*.yaml` in that repo.
- Debug a live broker that boots but `/healthz` shows the worker stuck
  (`offline`, `booting`, `idle` but no jobs, or `awaiting_worker`).
- Verify that Stripe ACS / SPT / MPP plumbing is correct (demo mode vs real
  capture).
- Debug why a worker isn't being launched (no `RunInstances` call, IAM
  failures, SG block, user-data too large).
- Verify Plan 2 attestation flow (NemoClaw Docker image digest signed by the
  worker's Ed25519 key).
- Confirm /v1/discover attestation block matches the marketing site's
  `/verify-attestation` page.

## Top-level pitfalls

- **AWS CLI SSM `send-command` is broken on aws-cli 2.31.35 + Python 3.14**
  with `badly formed help string`. Use boto3 directly. `deploy.sh` already
  ships the boto3 path; do not "simplify" it back to `aws ssm send-command`.
- **EC2 user-data is capped at ~16KB**. Cloud-init commands that pass
  `cloud-init status --long` and pull large files inline will fail. Use EFS
  for shared state and SSM `send-command` for post-boot writes.
- **Worker instance cold-start is ~5–10 min** (instance launch + cloud-init
  + poller boot + job dispatch). Live poll loops should be at least 5 min
  before declaring a worker stuck.
- **Poller stop ≠ worker stop.** `worker-poller.service` is `Type=oneshot`
  in the systemd unit and exits after each batch. If `/healthz` says
  `worker=booting` for >10 min, the service was stopped, not the instance.
  Restart with `systemctl start worker-poller` via SSM.
- **Stripe demo mode is the default.** `BROKER_PAYMENT_STUB_MODE=0` flips
  the broker into real Stripe capture mode and requires `STRIPE_SECRET_KEY`
  to be set; the broker will return `llm_upstream_not_configured` HTTP 503
  on first request if the key is missing. Demo mode is fine for
  Plan 2 attestation work.
- **Plan 2 metadata on already-running workers does not refresh from a\n  `deploy.sh` push alone.** Existing workers do not re-run user-data. See\n  `references/attestation-nemoclaw-image-digest.md` for the deploy step 4.5\n  fix that uses boto3 SSM to re-capture.\n  - **Worker Identity Cache**: The `worker-poller` caches the X25519 identity key in memory. If the deploy refresh script updates the on-disk key (`/opt/worker/keys/worker_input_x25519.priv`), the poller MUST be restarted via `systemctl restart worker-poller` to clear the cache and align its signing key with the identity advertised in `worker-keys.json`.\n  - **Binding Persistence**: The X25519 key MUST be persisted to disk *before* the TEE attestation report is generated; otherwise, the `report_data` binding will be empty or wrong, causing verifier Check 4 to fail.
- **EFS code vs running worker code divergence.** `deploy.sh` and the incremental S3 deploy pattern push `worker/poller.py` to EFS (`/mnt/broker/logs/worker-poller.py`), which is the source for **new** worker launches. But a **running** worker has its own copy at `/opt/worker/poller.py` that is never auto-refreshed. After any code change to worker-side files, copy EFS to `/opt/worker/` on each running worker and restart the poller. See `references/redeploy-runtime-copy-and-restart.md` for the SSM procedure. This caused a live `report_data[:64]` vs `[:128]` signature mismatch on 2026-06-30.
- **Manual poller restart loses `boot_stage` in heartbeat.** `user-data.sh` sets `boot_stage=ready` in `worker-heartbeat.json` at the end of bootstrap. But `pkill`+`nohup` restart of the poller can write a heartbeat without `boot_stage` if the file was absent. The daemon's `_worker_ready_for_jobs()` requires `boot_stage == "ready"` — without it, file jobs stall at `awaiting_worker` with `detail=ok` (identity passes, but ready check fails). The `heartbeat()` function now self-heals `boot_stage` if missing. See `references/worker-identity-gate.md` for diagnosis and fix.
- **NemoClaw "no-nemoclaw" fail-closed mode is intentional — it is not a network error.** The worker reports `no-nemoclaw` when `NEMOCLAW_SANDBOX_NAME` resolves to `stub` but `BROKER_NEMOCLAW_STUB_MODE=0` (the default) and `shim_present=True`. The poller refuses to fall back to a non-attested host-side LLM call. Fix options: (a) correct user-data/bootstrap so NemoClaw onboards to a real sandbox name, or (b) set `BROKER_NEMOCLAW_STUB_MODE=1` in the broker's `config.env` to enable demo fallback. On a fresh worker the offending variables are often wrong in `worker/user-data.sh` step 4 or `worker-bootstrap.sh`. If you see `NEMOCLAW_SANDBOX_NAME='stub'` and `stub_mode=False` in the worker logs, that is the exact trigger — do not chase the LLM provider first.
- **LLM provider exhaustion is signalled by HTTP 401 (invalid / revoked key) and HTTP 404 with "account balance is too low"** — both Ollama and Nous Portal surface these distinctly. The broker's `llm_error` field in job results reflects the upstream status; do not re-route to a different provider without first checking whether the existing key actually responds with a 2xx on a direct probe (the broker stores only one key per provider). If the stored Ollama key returns 401 and the user has topped up a *different* account, the key must be refreshed in the broker config (in `broker-daemon/daemon.py` config.env or the SSM-backed config source); a fresh top-up to the same account does not propagate automatically.
- **Worker attestation output lives in two places.** The attestation data is now attached to the *input upload* (`input_upload.attestation`) as a structured object (`tee_type`, `measurement`, `report`, `cert_chain`, `report_data`, `source`, `snp_quote`) AND surfaced again at the job result level under `result.sandbox` (with `name`, `attested`, `network_policy`, `inference_route`, `error`, `nemoclaw_version`, `nemoclaw_image`, `nemoclaw_image_digest`, `image_digest_sig`). Logs older than ~2026-06-30 may only show the legacy `input_upload.encryption.snp_quote` path. A robust client should read `input_upload.attestation` first and fall back to `encryption.snp_quote` for backward compatibility — the E2E script template in `templates/` was updated to match.
- **A terminated worker does not recover via poller restart.** If `describe_instances` shows `state=terminated`, the instance is gone and must be redeployed (`deploy.sh` or EC2 RunInstances). A stale `boot_stage=ready` heartbeat in EFS or SSM state can mislead the daemon into thinking a worker exists; clean it up before redeploying if you see ghost `awaiting_worker` hangs after termination.

## References

- `references/worker-identity-gate.md` — awaiting_worker stalls when
  `/healthz` says the worker is ready. Covers `worker-keys.json` missing
  `instance_id`/`policy_hash` (2026-06-30 live fix with self-heal code),
  `worker-heartbeat.json` missing `boot_stage` (2026-06-30 live fix with
  self-heal code), and the general identity-gate diagnostic pattern.
- `references/attestation-verifier-build-notes.md` — AMD KDS / cert-chain /
  worker_binding gotchas discovered while building the verifier.
- `references/attestation-nemoclaw-image-digest.md` — Plan 2 (NemoClaw
  Docker image digest signed by worker's Ed25519 key): canonical
  `report_data[:128]` payload, hardened metadata capture, deploy.sh step 4.5
  to refresh already-running workers, and how to tell a fail-closed empty
  `image_digest_sig` from a real one.
- `references/e2e-attestation-schema-compat.md` — schema migration from
  `input_upload.encryption.snp_quote` to `input_upload.attestation` /
  `result.sandbox`, LLM exhaustion signals (401/404), and the NemoClaw
  fail-closed / stub-mode trigger resolution paths observed on 2026-06-30.
- `references/tee-broker-deploy-config-context.md` — preserved historical
  context from the prior 100K-character SKILL.md (verbatim pitfalls, recipes,
  and ground-truth checks for Stripe ACS, SPT, MPP, user-data propagation,
  cold-start, and live verification). Read this when you need the full
  historical detail that was trimmed from the umbrella.

## When this skill is NOT the right fit

- TEE attestation *protocol* (HMAC, VLEK, ARK chain semantics) — that's
  `tee-broker-protocol` / `tee-broker-pattern`.
- Building the marketing site / Apple-style copy — that's
  `verdantforged-marketing-site`.
- Verifying a live broker as a reviewer / agent — that's
  `verdantforged-verify-attestation`.
