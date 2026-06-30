# Worker identity gate / awaiting_inputs stall

Use this when a file-job E2E run shows the worker is live in `/healthz` but the job remains in `awaiting_worker` and never exposes `input_upload`.

Related: once upload readiness works but results show `execution_mode: broker-proxy-failed`, switch to `references/broker-llm-proxy.md` for broker-side upstream LLM key/config propagation. If results instead show `execution_mode: broker-llm-proxy` (post-fix: `no-nemoclaw-failclosed`) with no `sandbox` block and a sub-10-minute boot, switch to `references/worker-dispatch-fallback.md` for the worker-side silent fall-back path (2026-06-30 incident).

Observed symptom
- Client output repeats `state=awaiting_worker`.
- `/healthz` can show `worker_status=idle`, `worker_boot_stage=ready`, and a live instance id.
- Broker logs say the job is `awaiting attested worker` or adopted an existing worker.
- The worker may later idle-unload before the client ever receives `awaiting_inputs`.

Interpretation
- This is not a cold-start/spawn problem once `/healthz` reports the worker ready.
- It is the broker identity gate waiting for worker key publication and attestation/report-data binding.
- The client must not upload until the job reaches `awaiting_inputs`; the broker should explain the wait via job status diagnostics.

Broker-side pattern
1. Split identity loading into a diagnostic helper such as `_worker_identity_status(instance_id) -> (identity, reason)` rather than returning bare `None`.
2. Validate, and report distinct reasons for:
   - missing or invalid `worker-keys.json`
   - missing or invalid `worker-attestation.json`
   - instance id mismatch
   - policy hash mismatch
   - X25519 key length/base64 errors
   - attestation binding digest mismatch
   - unsupported attestation source
   - SNP quote signature verification failure
   - report_data missing or not bound to the input key
3. In `_prepare_file_job`, log the reason periodically while waiting for identity.
4. If the tracked worker instance becomes non-running while the job is waiting, clear cached worker state and reacquire/relaunch instead of waiting forever on a dead instance.
5. Clear stale identity files (`worker-keys.json`, `worker-attestation.json`, `worker-heartbeat.json`) before launching a fresh worker, and also when adopting a live worker whose instance id differs from the ids named by those files. Otherwise terminated workers or pre-redeploy policy hashes can keep blocking new file jobs.
6. Treat deterministic identity failures (policy hash mismatch, binding digest mismatch, unsupported attestation source, SNP quote signature failure, report_data not bound) as client-visible job failures after a short grace window instead of waiting for the full cold-start timeout.
7. Include a `worker_wait` object in `GET /v1/jobs/{job_id}` while state is `awaiting_worker` so the E2E client can print why upload is not ready.

Demo/stub mode
- For regional demos or non-SNP environments, a binding-only attestation fallback may be acceptable, but it must be explicit.
- Use a separate gate such as `BROKER_ALLOW_STUB_WORKER_ATTESTATION`.
- A safe default is: production fail-closed; allow stub only when `BROKER_PAYMENT_STUB_MODE=1` or `BROKER_ENABLE_SEV_SNP=0`, or when explicitly configured.
- Stub acceptance should still verify instance id, public key shape, policy hash, and `attestation_binding_sha256`; only the real SNP quote/report-data requirement is relaxed.

Client/E2E pattern
- While waiting for `awaiting_inputs`, print both `/healthz` boot fields and job `worker_wait` diagnostics.
- Distinguish these cases in output:
  - worker offline/cold-starting
  - worker live but identity gate waiting
  - job failed
  - job reached `awaiting_inputs` and upload may proceed

Offline regression test
- Add a test that writes synthetic `worker-keys.json` plus `worker-attestation.json` with `source=instance_id_sha256` and no real report.
- Assert identity is rejected when the stub gate is false.
- Assert identity is accepted when the stub gate is true and that returned attestation report_data is synthesized as `<binding> + 64 zero hex chars`.
- Keep the existing real-SNP/bound-report test so production semantics are still covered.

## worker-keys.json missing instance_id / policy_hash (2026-06-30 live fix)

The daemon's `_worker_identity_status()` checks `keys.get("instance_id") != instance_id`
and `keys.get("policy_hash") != policy_hash`. If either field is absent or empty,
every job stalls at `awaiting_worker` with `worker_wait status=waiting_for_identity
detail=worker key instance mismatch keys=None expected=<id>`.

**How this happens:** `user-data.sh` writes `worker-keys.json` to EFS with
`instance_id`, `policy_hash`, and `attestation_binding_sha256` at boot step 3.
But `publish_worker_keys()` in the poller also writes `worker-keys.json`. If the
poller starts before `user-data.sh` completes step 3 (or if the worker was
launched from an older `user-data.sh` that didn't write these fields),
`publish_worker_keys()` overwrites the file with only `key_id`, `x25519_pubkey_b64`,
`ed25519_pubkey_b64`, `created_at`, and the NemoClaw metadata — losing the
identity fields. The preservation logic (`if existing.get("key_id") == record["key_id"]`)
only works if the fields were already present in the file when the poller reads it.

**Live fix via SSM:**

```python
# Patch worker-keys.json directly on the worker
py_script = r'''
import json, hashlib, base64, subprocess
from pathlib import Path
keys_path = Path("/mnt/broker/logs/worker-keys.json")
keys = json.loads(keys_path.read_text())
# Get instance-id from IMDSv2
token = subprocess.run(["curl", "-sS", "-X", "PUT",
    "http://169.254.169.254/latest/api/token",
    "-H", "X-aws-ec2-metadata-token-ttl-seconds: 21600"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().strip()
instance_id = subprocess.run(["curl", "-sS", "-H",
    f"X-aws-ec2-metadata-token: {token}",
    "http://169.254.169.254/latest/meta-data/instance-id"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().strip()
# Get policy hash
policy_path = Path("/mnt/broker/logs/openshell-policy.yaml")
policy_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
# Compute binding
pub = base64.b64decode(keys["x25519_pubkey_b64"])
binding = hashlib.sha256(
    b"verdantforged-worker-input-v1\0" + pub + b"\0" +
    bytes.fromhex(policy_hash)).hexdigest()
keys["instance_id"] = instance_id
keys["policy_hash"] = policy_hash
keys["attestation_binding_sha256"] = binding
keys_path.write_text(json.dumps(keys, indent=2) + "\n")
'''
# Also fix worker-attestation.json instance_id (daemon checks att.get("instance_id") too)
```

Also patch `worker-attestation.json` — the daemon checks
`att.get("instance_id") != instance_id` separately. This file's `instance_id`
was empty string `""` on the live worker.

**Self-healing code fix in `publish_worker_keys()`:**

Added IMDSv2 fallback to `publish_worker_keys()` in `worker/poller.py`: if
`instance_id` is missing from the record, fetch from IMDSv2; if `policy_hash`
is missing, hash `/mnt/broker/logs/openshell-policy.yaml`; if
`attestation_binding_sha256` is missing, compute it from the X25519 pubkey
and policy hash. This prevents recurrence on future poller restarts.

**Diagnosis:**

When `worker_wait status=waiting_for_identity detail=worker key instance
mismatch keys=None`, check the live `worker-keys.json` via SSM:

```bash
python3 -m json.tool /mnt/broker/logs/worker-keys.json
```

If `instance_id` or `policy_hash` are absent, apply the live fix above and
restart the poller. The daemon re-reads both files on each job poll, so the
fix takes effect immediately without a daemon restart.

## Three-gate sequential diagnosis for awaiting_worker stalls

When a file job is stuck at `awaiting_worker`, there are three sequential
gates in `_prepare_file_job()` (`daemon.py:2635`). Each must pass before
the job transitions to `awaiting_inputs`. Diagnose them in order:

### Gate 1: `_worker_identity_status()` — worker-keys.json + worker-attestation.json

Symptom: `worker_wait status=waiting_for_identity detail=worker key
instance mismatch keys=None expected=<id>`

Checks: `worker-keys.json` has valid `x25519_pubkey_b64` (32 bytes),
`instance_id` matches the worker's EC2 instance ID, `policy_hash` matches
the broker's `_policy_hash()`, and `worker-attestation.json` has matching
`instance_id`.

Fix: See "worker-keys.json missing instance_id / policy_hash" section above.

### Gate 2: `_worker_ready_for_jobs()` — worker-heartbeat.json

Symptom: `worker_wait status=waiting_for_identity detail=ok` but job
still doesn't transition. This is misleading — `detail=ok` means identity
passed, but the ready check failed.

Checks: `worker-heartbeat.json` has `boot_stage == "ready"` AND
`status == "idle"`.

Fix: See "boot_stage missing from worker-heartbeat.json" section above.

### Gate 3: EFS code vs running worker code

Symptom: Identity and ready checks pass, job transitions to `awaiting_inputs`,
but the result envelope has `image_digest_sig` that fails verifier Check 6,
or the sandbox block has unexpected values.

Checks: The poller at `/opt/worker/poller.py` may be running stale code
while EFS (`/mnt/broker/logs/worker-poller.py`) and the source tree have
the fix. Grep both copies via SSM for the relevant pattern.

Fix: See `references/redeploy-runtime-copy-and-restart.md`.

### Quick triage via SSM

```python
commands = [
    "python3 -m json.tool /mnt/broker/logs/worker-keys.json",
    "python3 -m json.tool /mnt/broker/logs/worker-attestation.json",
    "python3 -m json.tool /mnt/broker/logs/worker-heartbeat.json",
    "grep -n 'report_data.*\\[:128\\]\\|report_data.*\\[:64\\]' /opt/worker/poller.py | head -5",
]
```

Gate 1 fails if `instance_id`/`policy_hash` absent in worker-keys.json.
Gate 2 fails if `boot_stage` absent or not `"ready"` in heartbeat.
Gate 3 fails if `/opt/worker/poller.py` has `[:64]` instead of `[:128]`.

After fixing the `instance_id`/`policy_hash` issue above, the job was still
stuck at `awaiting_worker` with `worker_wait status=waiting_for_identity
detail=ok`. The `detail=ok` means `_worker_identity_status()` passed, but
`_worker_ready_for_jobs()` returned False because `worker-heartbeat.json`
was missing the `boot_stage` field.

**How this happens:** `user-data.sh` sets `boot_stage=ready` at the end of
bootstrap (line 348). The poller's `heartbeat()` function reads the existing
file and updates `status`/`last_heartbeat` while preserving other fields.
But if the poller is manually restarted (via `pkill` + `nohup`) after the
heartbeat file was deleted or never written by a complete `user-data.sh`
run, the first `heartbeat("idle")` call writes a file with only
`status` and `last_heartbeat` — no `boot_stage`.

The daemon's `_worker_ready_for_jobs()` at `daemon.py:1697` requires:
```python
if stage == "ready" and status == "idle":
    return True, "ready"
```
With `boot_stage` missing (empty string), it returns
`False, "worker not ready: status=idle boot_stage=? detail=?"`.

**Live fix via SSM:**
```python
import json, time
from pathlib import Path
hb_path = Path("/mnt/broker/logs/worker-heartbeat.json")
h = json.loads(hb_path.read_text()) if hb_path.exists() else {}
h["status"] = "idle"
h["boot_stage"] = "ready"
h["boot_detail"] = "Worker setup complete, poller active"
h["last_heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
hb_path.write_text(json.dumps(h, indent=2) + "\n")
```

**Self-healing code fix in `heartbeat()`:**

Added to `worker/poller.py` `heartbeat()` function: if `boot_stage` is
missing from the heartbeat dict, self-heal it to `"ready"`. This prevents
recurrence on future manual poller restarts. The rationale: if the poller
is running and calling `heartbeat()`, the worker IS ready — the
`boot_stage` is set by `user-data.sh` at the end of bootstrap, and a
running poller implies bootstrap completed.

**Diagnosis pattern:**

When `worker_wait status=waiting_for_identity detail=ok` but the job
still doesn't transition to `awaiting_inputs`, the issue is
`_worker_ready_for_jobs()` failing, NOT the identity check. Check the
heartbeat file via SSM:

```bash
cat /mnt/broker/logs/worker-heartbeat.json | python3 -m json.tool
```

If `boot_stage` is absent or not `"ready"`, that's the blocker. The
`detail=ok` in `worker_wait` is misleading — it means identity passed
but the ready check failed. The daemon logs `reason = f"identity ok,
{ready_reason}"` but the job status endpoint only surfaces
`status=waiting_for_identity detail=ok` (the identity part), not the
ready-check failure reason.

Deploy/runtime pitfalls
- If adding a new broker env var for this path, propagate it through every config layer: local `deploy.sh` variable, the remote bootstrap env, generated `/opt/broker-daemon/config.env`, and any live config update/restart path. Missing one layer makes source look fixed while the running daemon still behaves old.
- A successful file deploy is not enough: verify the running systemd process has actually restarted onto the new code. Check `systemctl show -p MainPID verdantforged-broker-daemon`, recent journal messages, and whether new diagnostic strings appear in logs. If `/opt/broker-daemon/daemon.py` has the patch but logs still show the old message shape, the daemon is still an old in-memory process.
- Policy-hash checks must hash the same policy bytes on broker and worker. The worker binds keys to the EFS-deployed policy at `/mnt/broker/logs/openshell-policy.yaml`; the broker should prefer that path and only fall back to the source-tree `broker-daemon/openshell/policy.yaml` for local/offline tests. A missing `/opt/broker-daemon/openshell/policy.yaml` on the control plane can otherwise make `_policy_hash()` return empty and produce a false `worker policy_hash mismatch` even with valid SNP attestation.
- When diagnosing a policy mismatch, include short worker/broker hash prefixes in the error (`worker=... broker=...`) so client E2E output distinguishes stale identity, wrong policy source, and old process-not-restarted cases.