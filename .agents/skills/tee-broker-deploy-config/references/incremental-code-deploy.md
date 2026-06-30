# Incremental code deploy to a running control plane

When you need to push updated daemon.py, worker files, or config changes
to a **running** control plane without a full CFN redeploy. The pattern
is: tarball locally → upload to the artifact S3 bucket → `aws s3 cp` on
the instance via SSM → extract → backup old files → install new →
restart daemon → verify.

This is the preferred pattern for any payload >97 KB (the SSM
`Parameters` limit). For payloads <97 KB, inline SSM `send_command`
with base64 is fine but still slower than S3.

## Why S3 intermediary beats SSM chunking

The skill's main pitfalls mention the SSM 97KB limit and the
`aws-ephemeral-deploy` skill documents the presigned URL pattern
(`references/s3-presigned-single-file-push.md`). For broker deploys,
**neither is needed** — the control plane's IAM role already has
`s3:GetObject` on the artifact bucket (`verdantforged-artifacts-<region>`).
Just upload and `aws s3 cp` down. No presigned URL, no chunking, no
base64 escaping issues.

| Payload | Pattern | Round-trips |
|---------|---------|-------------|
| <1 KB | SSM inline `cat <<EOF` | 1 |
| 1–90 KB | SSM base64 in single `send_command` | 1 |
| 90–97 KB | SSM base64, may hit limit — use S3 instead | 1+ |
| >97 KB | **S3 intermediary (this pattern)** | 3 |
| Multi-file | **S3 tarball (this pattern)** | 3 |

## The recipe

```python
import boto3, tarfile, io, os, time

region = "eu-west-1"
instance_id = "i-05117b9649db5b343"
bucket = "verdantforged-artifacts-eu-west-1"  # CFN ArtifactBucket output
repo_base = "/path/to/tee-broker-deploy"

# 1. Create tarball of files to push
files_to_push = [
    "broker-daemon/daemon.py",
    "broker-daemon/static/openapi.json",
    "worker/poller.py",
    "worker/user-data.sh",
    "worker/sev_snp.py",
    "worker/worker-agent.py",
]
tar_buffer = io.BytesIO()
with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
    for f in files_to_push:
        tar.add(os.path.join(repo_base, f), arcname=f)
tar_bytes = tar_buffer.getvalue()

# 2. Upload to S3 (artifact bucket, not a throwaway bucket)
s3 = boto3.client("s3", region_name=region)
deploy_key = "deploy/update.tar.gz"
s3.put_object(Bucket=bucket, Key=deploy_key, Body=tar_bytes)

# 3. Download + extract + install via SSM
ssm = boto3.client("ssm", region_name=region)
def run_ssm(commands, timeout=60):
    r = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
        TimeoutSeconds=timeout,
    )
    cmd_id = r["Command"]["CommandId"]
    for _ in range(20):
        time.sleep(3)
        inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
        if inv["Status"] in ["Success", "Failed", "TimedOut", "Cancelled"]:
            return inv
    return inv

# Download + extract
run_ssm([
    f"bash -c 'aws s3 cp s3://{bucket}/{deploy_key} /tmp/update.tar.gz'",
    "bash -c 'mkdir -p /tmp/update-extract && tar xzf /tmp/update.tar.gz -C /tmp/update-extract'",
], timeout=60)

# Backup old files, install new ones
run_ssm([
    # Backup
    "bash -c 'cp /opt/broker-daemon/daemon.py /opt/broker-daemon/daemon.py.bak.$(date +%s)'",
    # Install daemon files
    "bash -c 'cp /tmp/update-extract/broker-daemon/daemon.py /opt/broker-daemon/daemon.py'",
    "bash -c 'cp /tmp/update-extract/broker-daemon/static/openapi.json /opt/broker-daemon/static/openapi.json'",
    # Install worker files to EFS (daemon reads these for worker launches)
    "bash -c 'cp /tmp/update-extract/worker/poller.py /mnt/broker/logs/worker-poller.py'",
    "bash -c 'cp /tmp/update-extract/worker/user-data.sh /mnt/broker/logs/worker-bootstrap.sh'",
    "bash -c 'cp /tmp/update-extract/worker/worker-agent.py /mnt/broker/logs/worker-agent.py'",
    # Set permissions
    "bash -c 'chmod 0644 /opt/broker-daemon/daemon.py /opt/broker-daemon/static/openapi.json'",
    "bash -c 'chmod 0755 /mnt/broker/logs/worker-poller.py /mnt/broker/logs/worker-bootstrap.sh /mnt/broker/logs/worker-agent.py'",
], timeout=30)

# Restart daemon
run_ssm([
    "bash -c 'systemctl restart verdantforged-broker-daemon'",
    "bash -c 'sleep 3 && systemctl is-active verdantforged-broker-daemon'",
], timeout=30)

# 4. Verify
inv = run_ssm([
    "bash -c 'md5sum /opt/broker-daemon/daemon.py'",
    "bash -c 'grep -c \"input_files\" /opt/broker-daemon/daemon.py'",
], timeout=30)
print(inv.get("StandardOutputContent", ""))

# 5. Clean up S3
s3.delete_object(Bucket=bucket, Key=deploy_key)
```

## EFS file mapping

The daemon's `_render_worker_user_data()` reads worker files from EFS.
The mapping from git source to EFS path:

| Git source | EFS path | Purpose |
|------------|----------|---------|
| `worker/poller.py` | `/mnt/broker/logs/worker-poller.py` | Job poller, copied to worker on launch |
| `worker/user-data.sh` | `/mnt/broker/logs/worker-bootstrap.sh` | Worker cloud-init script |
| `worker/worker-agent.py` | `/mnt/broker/logs/worker-agent.py` | Agent helper |
| `worker/sev_snp.py` | `/mnt/broker/logs/worker-sev-snp.py` | SEV-SNP attestation fetcher |

The daemon only substitutes `__EFS_DNS__`, `__ARTIFACT_BUCKET__`,
`__ARTIFACT_REGION__`, and `${BROKER_ONBOARD_TOKEN}` in the bootstrap
script — all other content is copied verbatim. If the source has
corrupted placeholders (literal `***`, unclosed `${`), they propagate
to every launched worker. See Pitfall 18 in the main SKILL.md.

NOTE: When pushing updated `user-data.sh`, the boot_stage tracking
function (`update_boot_stage()`) writes to the heartbeat file via
a small Python inline script. This works under cloud-init's bash
but the Python snippet must be on a single line or use proper
heredoc escaping. The `update_boot_stage` calls are no-ops if
`/mnt/broker/logs/` doesn't exist yet (the `mkdir -p` guard handles
this). See the "Enhanced /healthz" section in the main SKILL.md.

## Post-deploy verification

1. **MD5 match**: compare `md5sum /opt/broker-daemon/daemon.py` on the
   instance with `md5sum` of the local file. They must match.

2. **Route registration**: grep the live daemon for new route patterns:
   ```bash
   grep -n "add_post.*ready\|add_get.*artifact" /opt/broker-daemon/daemon.py
   ```

3. **Public health check**: `curl https://verdant.codepilots.co.uk/healthz`
   should return 200. A 502 means Caddy is still recovering — wait 5s
   and retry. If still 502 after 10s, check `systemctl status` on the
   daemon.

4. **Config.env check**: verify the daemon has the env vars it needs.
   Most `BROKER_INPUT_*` vars have daemon defaults, so they don't need
   to be in config.env. `BROKER_ARTIFACT_BUCKET` must be set (written
   by bootstrap from the CFN `ArtifactBucketName` output).

5. **Worker file freshness**: the EFS copies are read by the daemon
   when launching new workers. Any running worker still has the OLD
   code. To pick up new worker code, terminate the running worker
   (the daemon will launch a fresh one with the updated EFS files) or
   wait for the idle timer to cycle it.

## When to use this vs. full deploy.sh

- **Incremental code deploy** (this pattern): use when only daemon.py
  and/or worker files changed, the CFN template is unchanged, and you
  want zero downtime. The daemon restarts in ~3 seconds.
- **Full deploy.sh**: use when the CFN template changed (new parameters,
  new outputs, new IAM policies), when the bootstrap script needs
  re-running, or when the instance itself needs to be replaced.
  deploy.sh pushes the full repo tarball and re-runs bootstrap.
- **Standalone service deploy** (`references/standalone-service-deploy.md`):
  use when adding a new sibling service (e.g. skill-library) alongside
  the broker. Different pattern — systemd unit + Caddy route + config.env
  append.

## Caddy 502 after daemon restart

The daemon restarts in ~3s but Caddy's reverse_proxy upstream health
check may not catch up immediately. A `curl` within 2-3s of the restart
can return 502. Wait 5s and retry. If still 502 after 10s:

```bash
# Check daemon is actually listening
ss -tlnp | grep python3
# Should show 127.0.0.1:8080

# Check Caddy can reach it
curl -sS http://127.0.0.1:8080/healthz
# Should return {"ok": true, ...}

# If both work, reload Caddy
sudo systemctl reload caddy
```