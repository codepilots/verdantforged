# Redeploy runtime copy and restart pitfalls

When changing `broker-daemon/daemon.py` or sibling modules, verify the bootstrap copies root-level Python files into `/opt/broker-daemon/`, not only subdirectories. The systemd unit executes `/usr/bin/python3 /opt/broker-daemon/daemon.py`, so a tarball push alone is insufficient.

Checklist:

1. `scripts/bootstrap-control-plane.sh` should install all root Python files:

```bash
for py in "$REPO_DIR"/broker-daemon/*.py; do
    [ -f "$py" ] && install -m 0644 "$py" "$BR_DEPLOY/$(basename "$py")"
done
```

2. After writing the unit, use `systemctl restart verdantforged-broker-daemon`; `systemctl enable --now` does not restart an already-running service, so the live daemon may keep old code.

3. Quote generated Python heredocs in `deploy.sh` (`<<'PYEOF'`) when comments contain backticks. Unquoted heredocs execute command substitutions in comments, producing errors such as `.: filename argument required`.

4. Verify remotely after deploy:

```bash
python3 -m py_compile /opt/broker-daemon/daemon.py
grep -n 'required =' /opt/broker-daemon/daemon.py | head
systemctl status verdantforged-broker-daemon --no-pager -l
```

## Worker poller code propagation gap (2026-06-30)

The incremental deploy recipe pushes `worker/poller.py` to EFS
(`/mnt/broker/logs/worker-poller.py`) and the daemon reads that EFS copy
when launching **new** workers. But a **running** worker has its own copy
at `/opt/worker/poller.py` that was written at bootstrap time and is never
refreshed by `deploy.sh` or the incremental S3 deploy pattern.

This caused a live signature mismatch: the EFS/control-plane copy had the
`report_data[:128]` fix, the verifier had `[:128]`, but the running worker
still signed with `report_data[:64]`. Local tests passed (they import the
source tree), the EFS copy was correct, but live jobs produced signatures
that failed verification.

### Fix procedure for a running worker

```bash
# Via boto3 SSM on the worker instance:
cp /mnt/broker/logs/worker-poller.py /opt/worker/poller.py
chmod +x /opt/worker/poller.py
pkill -f 'python3 /opt/worker/poller.py'
sleep 2
nohup python3 /opt/worker/poller.py > /mnt/broker/logs/poller-restart.log 2>&1 &
```

### Diagnosis technique

When a live signature mismatch is suspected, grep the deployed code on
both sides via SSM:

```python
# Compare EFS copy vs running worker copy
ssm_commands = [
    "grep -n 'report_data.*\\[:128\\]\\|report_data.*\\[:64\\]' /mnt/broker/logs/worker-poller.py",
    "grep -n 'report_data.*\\[:128\\]\\|report_data.*\\[:64\\]' /opt/worker/poller.py",
]
```

If the EFS copy has `[:128]` but `/opt/worker/poller.py` has `[:64]`,
the worker is running stale code. The fix is the copy+restart procedure
above.

### General rule

**EFS is the source of truth for new worker launches, but `/opt/worker/`
is the source of truth for running workers.** Any code change to
`worker/poller.py` (or any worker-side file) must be propagated to
`/opt/worker/` on each running worker and the poller restarted. The
`deploy.sh` step 4.5 refresh handles metadata, not code.

### Pushing updated poller.py to EFS (>97KB)

`poller.py` is ~166KB — exceeds the SSM `send_command` 97KB parameter
limit. Use S3 as an intermediary:

```python
import boto3, base64
s3 = boto3.client('s3', region_name='eu-west-1')
sts = boto3.client('sts', region_name='eu-west-1')
account = sts.get_caller_identity()['Account']
bucket = f"verdantforged-deploy-{region}-{account}"
# Create bucket if it doesn't exist
try: s3.head_bucket(Bucket=bucket)
except: s3.create_bucket(Bucket=bucket,
    CreateBucketConfiguration={'LocationConstraint': region})

key = f"worker-poller-{int(time.time())}.py"
with open('worker/poller.py', 'rb') as f:
    s3.put_object(Bucket=bucket, Key=key, Body=f.read())

# SSM to control plane: download from S3 → EFS
ssm.send_command(InstanceIds=[control_id],
    DocumentName='AWS-RunShellScript',
    Parameters={'commands': [
        f"aws s3 cp s3://{bucket}/{key} /mnt/broker/logs/worker-poller.py --region {region}",
        "chmod +x /mnt/broker/logs/worker-poller.py",
    ]}, TimeoutSeconds=120)

# Then SSM to worker: copy EFS → /opt/worker/ + restart (as above)

# Cleanup
s3.delete_object(Bucket=bucket, Key=key)
```

The bucket `verdantforged-deploy-<region>-<account>` is a throwaway —
the worker/control-plane IAM roles have `s3:GetObject` via the
`AmazonSSMManagedInstanceCore` policy, so no additional bucket policy
is needed.

### Verifying the fix end-to-end

After copying the fixed poller and restarting, submit a test job and
verify the signature:

1. `GET /v1/discover` — extract `enclave_pubkey`, `worker_ed25519_pubkey`, `report_data`
2. `POST /v1/demo/shared-payment-token` (empty body) — get SPT
3. `POST /v1/jobs` with `Authorization: Bearer <SPT>` header — get `job_id` + `job_access_token`
4. `GET /v1/jobs/<job_id>` with `Authorization: Bearer <job_access_token>` — poll until `completed`
5. Reconstruct payload: `f"{version}|{digest}|{name}|{enclave_pub}|{report_data[:128]}".encode()`
6. Verify: `Ed25519PublicKey.from_public_bytes(b64decode(ed25519_pub)).verify(bytes.fromhex(sig), payload)`

Live-verified 2026-06-30: signature VERIFIES after EFS→worker copy + restart.

### Worker key stability on restart

`publish_worker_keys()` is idempotent — if `/mnt/broker/logs/worker-keys.json`
exists, it returns the existing record without rotating keys. So restarting
the poller does NOT rotate the X25519 or Ed25519 keys, and `/v1/discover`
continues to advertise the same pubkeys. Verified live 2026-06-30: same
`key_id`, same `x25519_pubkey_b64`, same `ed25519_pubkey_b64` before and
after restart.

### worker-keys.json identity fields can be lost on restart

If `user-data.sh` wrote `instance_id`/`policy_hash`/`attestation_binding_sha256`
to `worker-keys.json` at boot, but `publish_worker_keys()` later overwrites
the file (e.g. after a poller restart with a new in-memory X25519 key that
generates a different `key_id`), the preservation logic
(`if existing.get("key_id") == record["key_id"]`) fails and the identity
fields are lost. The daemon then rejects all jobs with
`worker key instance mismatch keys=None`.

Fix: `publish_worker_keys()` now self-heals — if `instance_id` is missing,
fetches from IMDSv2; if `policy_hash` is missing, hashes the policy file;
if `attestation_binding_sha256` is missing, computes it. See
`references/worker-identity-gate.md` for the full diagnostic and fix
procedure.
