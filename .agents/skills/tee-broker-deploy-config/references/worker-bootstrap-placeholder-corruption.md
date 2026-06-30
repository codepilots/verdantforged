# Worker Bootstrap Placeholder Corruption

## What happened (2026-06-29)

The worker user-data script (`tee-broker-deploy/worker/user-data.sh`) contained a broken placeholder on line 154:

```bash
export COMPATIBLE_API_KEY=***   # truncated, missing closing brace and value
```

This was copied verbatim to the EFS-rendered bootstrap (`/mnt/broker/logs/worker-bootstrap.sh`) because the daemon's `_render_worker_user_data()` only substitutes `__EFS_DNS__` and leaves all other placeholders untouched. Every newly launched worker received the literal `***` as its API key, causing NemoClaw onboard to fail with HTTP 401.

## Root cause chain

1. Git source `tee-broker-deploy/worker/user-data.sh` has malformed line
2. Control-plane bootstrap copies git source to EFS `/mnt/broker/logs/worker-bootstrap.sh`
3. Daemon `_render_worker_user_data()` reads EFS file, replaces `__EFS_DNS__` only
4. Corrupted `COMPATIBLE_API_KEY` line is propagated to worker user-data
5. Worker cloud-init runs `nemohermes onboard`; wizard validates endpoint with `COMPATIBLE_API_KEY` as Bearer token
6. Broker proxy rejects `***` with HTTP 401
7. Worker has no sandbox, no poller, job hangs in `running` state

## Fix

**Step 1 — Fix the git source:**

In `tee-broker-deploy/worker/user-data.sh` line 154, replace the broken placeholder with a proper shell variable reference:

```bash
export COMPATIBLE_API_KEY="${BROKER_ONBOARD_TOKEN:-}"
```

Or, if the daemon should inject the token at render time, use a placeholder the daemon knows how to substitute:

```bash
export COMPATIBLE_API_KEY="__BROKER_ONBOARD_TOKEN__"
```

And update the daemon:

```python
# In broker-daemon/daemon.py:_render_worker_user_data()
content = content.replace("__EFS_DNS__", efs_dns)
content = content.replace("__BROKER_ONBOARD_TOKEN__", os.environ.get("BROKER_ONBOARD_TOKEN", ""))
```

**Step 2 — Update the EFS copy on the control plane:**

```bash
# On control plane (i-05117b9649db5b343)
sudo cp /home/ubuntu/tee-broker-deploy/worker/user-data.sh /mnt/broker/logs/worker-bootstrap.sh
```

Or, if using the daemon's render function, restart the daemon to re-render:

```bash
sudo systemctl restart verdantforged-broker-daemon
```

**Step 3 — Verify the rendered bootstrap has no literal `***`:**

```bash
grep -n 'COMPATIBLE_API_KEY' /mnt/broker/logs/worker-bootstrap.sh
# Expected: export COMPATIBLE_API_KEY="..." (a real value or a proper placeholder)
# BAD:     export COMPATIBLE_API_KEY=***   (literal stars, truncated, or unclosed ${...})
```

**Step 4 — Terminate the broken worker and let the daemon launch a fresh one:**

```bash
aws ec2 terminate-instances --instance-ids i-014d17d3895dcd0d6
```

The daemon will launch a new worker with the corrected user-data.

## Prevention

- After editing `worker/user-data.sh`, always verify the EFS copy matches
- Add a verification assertion to `tests/verify-stripe-integration.py` (or a new `tests/verify-worker-bootstrap.py`) that greps for `COMPATIBLE_API_KEY=.*\*` and fails if literal asterisks are found
- Consider making the daemon's render function validate that no `***` or unclosed `${` remains before returning the user-data string
