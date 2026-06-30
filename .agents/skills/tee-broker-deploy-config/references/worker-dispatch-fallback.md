# Worker dispatch fall-back / silent execution_mode: broker-llm-proxy

Use this when a file-job E2E completes successfully but the result envelope shows `execution_mode: broker-llm-proxy` (or its post-fix successor `no-nemoclaw-failclosed`), no `sandbox` block is present, and the worker boot heartbeat reported `boot_stage=ready` in well under 16 minutes. This is the **worker-side** routing problem, distinct from the broker-side problems in `references/worker-identity-gate.md` (attestation gate) and `references/broker-llm-proxy.md` (upstream LLM key).

## Observed symptom (2026-06-30)

- File job completes, status `completed`, encrypted result downloads cleanly, Stripe capture attempt runs.
- Decrypted result: `output=""`, `usage={"prompt_tokens": 0, "completion_tokens": 200, "total_tokens": 200}`, `model="minimax-m3:cloud"`, **`execution_mode: "broker-llm-proxy"`**, **no `sandbox` block**.
- Worker heartbeat on EFS (`/mnt/broker/logs/worker-heartbeat.json`) shows `boot_stage: "ready"` and `boot_elapsed_seconds < 600`. Real NemoClaw install + sandbox creation takes ~16 minutes. Anything under 6 minutes means step 4 of the user-data bootstrap failed fast.
- `worker-keys.json` is present and valid; SEV-SNP attestation is valid. Identity gate is fine.

## Interpretation

The poller took the **legacy host-side `urllib` POST to `/v1/llm/chat/completions`** branch in `worker/poller.py` `execute_in_envelope`. That branch used to be the default else-case when `_active_sandbox_name()` returned empty. The decision tree in the poller was:

1. `use_tool_loop` opt-in → tool-calling-loop
2. `_active_sandbox_name()` truthy → NemoClaw sandbox path
3. else → **`broker-llm-proxy` (legacy host-side urllib call)** ← the bug

`_active_sandbox_name()` returns falsy when `NEMOCLAW_SANDBOX_NAME` is unset in the worker's env. That env var is set by a **systemd drop-in** at `/etc/systemd/system/worker-poller.service.d/nemoclaw.conf` that the bootstrap writes inside the `if [ "$SBX" != "0" ]` branch — i.e. **only after a successful NemoClaw install**. When NemoClaw install fails, the drop-in is never created, the env var is unset, and the poller falls through silently.

The user-data bootstrap's failure mode was log lines, not exceptions:
```
log "step 4: FATAL — NemoClaw install failed"
log "step 4: jobs will use broker-llm-proxy fallback"
```

The poller still came up "ready" with no error. The broker's `/healthz` reported `worker_status: ready, boot_stage: ready`. The job looked completed to the requester. No signal reached any of: the broker, the dashboard, Stripe (capture was attempted on a non-attested run), or the requester (no `sandbox` block, no error in the result envelope).

## Root cause (two stacked bugs)

**A. Bootstrap gating bug** — `worker/user-data.sh` step 4 (and the equivalent in the EFS-pushed `worker-bootstrap.sh` rendered by `broker-daemon/daemon.py:_render_worker_user_data`) writes the systemd drop-in only inside the `if [ "$SBX" != "0" ]` branch. When NemoClaw install fails, the drop-in is never written, the poller boots without `NEMOCLAW_SANDBOX_NAME`, and the poller's else-branch fires silently.

**B. Silent-fallback design** — the poller's else-branch (the legacy `urllib` POST) was the only place the failure could fall to. It returned a `completed` envelope with no `sandbox` block, indistinguishable from a properly-attested run except by an `execution_mode` string and the absence of the `sandbox` block — neither of which the demo client (`scripts/run_file_job_e2e.py`) surfaced.

## The fix (2026-06-30, commit `786a791`)

**A. Move the drop-in out of the success branch** — the drop-in that exports `NEMOCLAW_SANDBOX_NAME=...` to the poller is now written unconditionally at the top of step 4. This means the poller always has `NEMOCLAW_SANDBOX_NAME` set and the else-branch is unreachable on a worker that booted at all.

**B. Replace the legacy else-branch with fail-closed** — the `broker-llm-proxy` else-branch in `execute_in_envelope` is replaced with:

```python
execution_mode = "no-nemoclaw-failclosed"
llm_error = "no-nemoclaw: worker has no NemoClaw sandbox ... refusing to fall back to a non-attested host-side LLM call. Check user-data.sh step 4 / worker-bootstrap.sh and rerun the job. To enable demo fallback, set BROKER_NEMOCLAW_STUB_MODE=1 in the broker's config.env."
sandbox_attestation = {"name": sb_name, "attested": False, "network_policy": "n/a (no sandbox)", "inference_route": "n/a — refused", "error": llm_error}
```

The legacy `urllib` POST block is preserved as a comment for archaeological context, never to be uncommented.

**C. Add stub-mode opt-in for demos without payments** — `BROKER_NEMOCLAW_STUB_MODE=1` on the broker:
- The broker's `_render_worker_user_data` substitutes `__NEMOCLAW_STUB_MODE__=1` in the rendered user-data.
- The bootstrap's stub branch (gated on that variable) skips the real NemoClaw install loop entirely, writes a tiny bash shim at `/usr/local/bin/nemohermes` that emulates `nemohermes <sb> exec` by running `worker-agent.py` on the host, and writes a stub `.nemoclaw_metadata` file.
- The poller's stub helper (`_run_stub_sandbox_dispatch`) imports `worker-agent.py` in-process with the same env contract, records `execution_mode: "nemoclaw-sandbox-stub"` with a clearly-labelled `attested: false, stub: true` block.
- Real NemoClaw (if present) takes priority over the stub — the `elif _active_sandbox_name()` branch is reached first; the stub only fires in the else.

## Operator workflow when E2E shows `execution_mode: broker-llm-proxy` or `no-nemoclaw-failclosed`

1. Confirm the symptom. Read the EFS outbox JSON for the job on the control plane:
   ```
   sudo python3 -c "import json; print(json.dumps(json.load(open('/mnt/broker/jobs/outbox/<job_id>.json')), indent=2)[:3000])"
   ```
   Look for `execution_mode` and the presence/absence of `sandbox` block.
2. Read the worker heartbeat:
   ```
   sudo cat /mnt/broker/logs/worker-heartbeat.json
   ```
   `boot_elapsed_seconds < 600` plus `boot_stage: "ready"` means step 4 failed. The worker is on a doomed boot.
3. Check the NemoClaw install log on the (now-terminated) worker. The worker EC2 is gone (idle-terminated after 10 min) so the log is lost — re-launch a worker and tail the log live via SSM SendCommand to reproduce.
4. The most common cause of the install failure is the NemoClaw installer timing out downloading the sandbox image from `https://www.nvidia.com/nemoclaw.sh` (which 301-redirects to `https://raw.githubusercontent.com/NVIDIA/NemoClaw/refs/heads/main/install.sh`). The bootstrap wraps it in `timeout 120 bash -c '... | tail -20'` with 2 retries (15s sleep) — total ~4 minutes — which is too tight on flaky CDN.
5. For demos without payments, enable the stub mode:
   ```
   echo "BROKER_NEMOCLAW_STUB_MODE=1" | sudo tee -a /opt/broker-daemon/config.env
   sudo systemctl restart verdantforged-broker-daemon
   ```
   Next worker launch will skip the real NemoClaw install and use the shim. Result envelopes will record `execution_mode: nemoclaw-sandbox-stub` and a `stub: true` sandbox block. Pay attention: this is honest about not being attested, so do not use stub mode in production.
6. For real NemoClaw, the deeper fix is to make the NemoClaw installer tolerant of CDN timeouts (longer timeout, more retries, pre-warmed image in EFS). Out of scope for the 2026-06-30 fix; tracked separately.

## Diagnostic commands cheat sheet (control plane, EU-WEST-1)

```bash
# Read the live worker's outbound user-data (what was rendered with __NEMOCLAW_STUB_MODE__=?)
# Worker EC2 is gone after idle termination, so this needs a re-launch + SSM
aws ssm send-command --instance-ids <NEW_WORKER_ID> \
  --document-name AWS-RunShellScript \
  --parameters commands="sudo cat /var/log/user-data.log | grep -E 'step 4|stub|NEMOCLAW_STUB_MODE' | head -50"

# List recent outbox execution_modes across jobs
sudo python3 -c "
import json, os
out = '/mnt/broker/jobs/outbox'
for f in sorted(os.listdir(out)):
    p = os.path.join(out, f)
    try:
        d = json.load(open(p))
        r = d.get('result', d)
        em = r.get('execution_mode', '?')
        sb = 'YES' if 'sandbox' in r else 'NO'
        print(f'{f[:30]:30s} mode={em:25s} sandbox={sb}')
    except Exception:
        pass
"

# Check broker's stub-mode config (must be set BEFORE the worker launches, not after)
sudo grep -E 'BROKER_NEMOCLAW_STUB_MODE|BROKER_PAYMENT_STUB_MODE' /opt/broker-daemon/config.env

# Verify the running broker daemon has the new code (not the old in-memory process)
sudo systemctl show -p MainPID verdantforged-broker-daemon
sudo sha256sum /opt/broker-daemon/daemon.py
```

## Test pinning

- `tests/verify-sandbox-execution.py` S7 was rewritten to assert `execution_mode == "no-nemoclaw-failclosed"` and that `urllib.request.urlopen` is **never called** in the no-sandbox path. The test patches `urllib.request.urlopen` to raise `AssertionError` and asserts the poller refuses the job without contacting the broker proxy.
- `tests/verify-sandbox-execution.py` S10b pins the stub-mode path: `NEMOCLAW_STUB_MODE=1` + shim present → `execution_mode: "nemoclaw-sandbox-stub"` (or `sandbox-failed` if the inner worker-agent call errors), with a `sandbox` block always present.
- `tests/verify-sandbox-execution.py` S10c pins priority: real NemoClaw on PATH wins over stub.
- `tests/verify-tool-calling-loop.py` T12 was rewritten to assert fail-closed (was asserting the legacy single-call behavior; that test was the back-compat guard for the now-removed path).

## Related signals

- The systemd drop-in write being inside the success branch is the same anti-pattern as writing a config file only when a downstream step succeeds. Always write the drop-in (or env-file, or systemd override) at the top of the install path so a downstream failure leaves a known-default state behind.
- "Worker says it's ready" is not the same as "worker is correctly configured". Pair `boot_stage: ready` with the absence/presence of expected files (`/opt/worker/.nemoclaw_metadata`, `/etc/systemd/system/worker-poller.service.d/nemoclaw.conf`, `/usr/local/bin/nemohermes`) before trusting the heartbeat.
- "Execution completed with no error" is not the same as "execution was attested". For security-bearing execution paths, the result envelope must carry an explicit attestation/sandbox block whose absence is treated as a failure, not as "everything's fine, no block was needed".
