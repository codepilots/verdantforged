# NemoClaw worker gold AMI bake notes

Use this reference when the VerdantForged worker has reached a working NemoClaw/OpenShell state and the user wants a reusable AMI for faster cold starts.

## Session-derived pattern

1. Treat the local repo copy as canonical before baking. If live EFS/control-plane bootstrap files drift, back up EFS first, then sync local `worker/user-data.sh`, `worker/poller.py`, `worker/worker-agent.py`, `worker/sev_snp.py`, and OpenShell policy files to the control plane/EFS copies.
2. Back up EFS and job queues before destructive cleanup. In the 2026-06-30 run, useful backup shapes were:
   - full EFS logs backup: `s3://<artifact-bucket>/backups/efs/<timestamp>/broker-logs.tar.gz`
   - job queue + broker DB backup: `s3://<artifact-bucket>/backups/jobs/<timestamp>/broker-jobs.tar.gz`
3. Clear stale job JSONs before validation or bake. Old `inbox`/`outbox` entries can cause confusing `Incorrect padding`/payload decode errors after a worker fix, because the poller may reprocess stale payloads.
4. Verify the expensive cache exists before imaging:
   - `nemohermes --version`
   - bootstrap marker shows NemoClaw install complete and skill install complete
   - `/opt/worker/.nemoclaw_metadata` and `.nemoclaw_sandbox_name` exist
   - `docker images` includes the NemoClaw/OpenShell sandbox layers (for example `ghcr.io/nvidia/nemoclaw/hermes-sandbox-base:v0.0.55` and `openshell/sandbox-from:*`, both ~4.36GB in the observed run)
5. Do not rely only on `nemohermes list --json` as the bake gate. In the observed worker, bootstrap logs reported `NemoClaw sandbox OK (1 sandbox(es))` and worker-agent loaded, but `nemohermes list --json` still returned `sandboxes: []`. Cross-check marker logs, metadata files, Docker layers, and a quick sandbox/worker-agent probe.
6. Scrub per-instance/live state before `create_image`, while preserving installed runtimes and Docker layers:
   - stop `worker-poller.service`
   - remove `/mnt/broker/jobs/inbox/*.json` and `/mnt/broker/jobs/outbox/*.json`
   - remove `/opt/worker/keys`
   - remove live shared worker keys/heartbeat/attestation JSONs from `/mnt/broker/logs/`
   - remove NemoClaw session/registry/credential-ish state such as `/root/.nemoclaw/onboard-session.json`, `/root/.nemoclaw/sandboxes.json`, `/root/.nemoclaw/state`, and files matching `*credential*`, `*token*`, `*secret*`, `*key*` under the NemoClaw state tree
   - run `cloud-init clean --logs`
   - `sync`
7. Bake the AMI with tags that record source instance and purpose. A no-reboot bake is faster but less conservative; use only when the worker has been scrubbed and synced.
8. Watch both AMI state and root snapshot progress. A 50GB encrypted root snapshot can sit in `pending` for several minutes; `describe_snapshots` progress is more informative than repeated `describe_images` alone.
9. Log the resulting AMI in `STATUS.md` and `SESSION_LOG.md`, including AMI ID, name, source worker, base AMI, region, state/time observed, preserved Docker layers, scrubbed state, and backup S3 paths.
10. After the AMI becomes `available`, set `BROKER_WORKER_AMI=<ami-id>` on the control plane, restart the daemon, trigger a cold-start job, and verify the worker launches from the new AMI and completes an e2e job.

## Pitfalls

- Deployed EFS bootstrap can drift from local repo. User correction in the session was explicit: treat local versions as most recent, back up EFS, then make files consistent.
- Short NemoClaw bootstrap timeouts are not enough for the 47-step Docker build. Keep the long local timeouts (observed canonical values: installer 1800s, explicit onboard 1200s, worker-agent sandbox copy 60s), or move the long onboard/build into a systemd service that survives cloud-init timeout behavior.
- Poller logs from stale jobs can look like a new worker failure. Clear old queues after backing them up before judging the fixed worker path.
- Do not bake secrets or per-instance identity. Preserve caches/layers; scrub live keys, tokens, heartbeat/attestation, job payloads, and cloud-init logs.
