# Broker jobs must route to real NemoClaw/Hermes sandbox, not broker-LLM fallback

Use this when a VerdantForged broker file job completes with `execution_mode: broker-llm-proxy` or an empty artifact instead of running through the NemoClaw/Hermes sandbox.

## Symptom

A demo/fake-payment file job like:

```bash
python3 scripts/run_file_job_e2e.py --demo-spt --file BUGS.md --file deploy.sh
```

returns a completed job with:

```json
{
  "execution_mode": "broker-llm-proxy",
  "usage": {"prompt_tokens": 0, "completion_tokens": 200},
  "artifacts": {"primary": {"filename": "output.txt", "size_bytes": 0}}
}
```

This is a routing failure, not a successful sandbox run.

## Correct invariant

For demo/fake-payment flows, the broker must still use the real NemoClaw/Hermes sandbox. Fake payment only bypasses Stripe payment capture; it must not imply stub mode or direct LLM execution.

Accept only:

```json
"execution_mode": "nemoclaw-sandbox"
```

with a real `sandbox` block / attestation metadata and processed uploaded files.

Reject unless explicitly requested for emergency dev:

```json
"execution_mode": "broker-llm-proxy"
"execution_mode": "nemoclaw-sandbox-stub"
```

Stub mode, if enabled, must be explicit and honest: `attested=false`, `stub=true`, `execution_mode=nemoclaw-sandbox-stub`.

## Root-cause checklist

1. Worker heartbeat readiness
   - Do not treat worker identity/key publication as ready.
   - Gate file-job input exposure on worker heartbeat reporting something equivalent to `status=idle` and `boot_stage=ready`.
   - File jobs should sit in `awaiting_worker` until the sandbox is ready.

2. Idle termination during cold boot
   - Do not apply idle-kill timers to workers that have not reached ready heartbeat state.
   - NemoClaw install + onboard + Docker sandbox build can exceed a 10 minute idle buffer.
   - Idle termination should only consider workers that are already ready/idle.

3. Poller fallback behavior
   - If `NEMOCLAW_SANDBOX_NAME` or `nemohermes` is missing, fail closed instead of falling back to direct broker LLM proxy.
   - Fake/demo payment is not a reason to use stub mode.
   - Stub mode must require an explicit env var such as `BROKER_NEMOCLAW_STUB_MODE=1` or `NEMOCLAW_STUB_MODE=1`.

4. Worker bootstrap source of truth
   - Verify the daemon is rendering from the actual EFS bootstrap it uses, not only the repo or `/opt` copy.
   - In the VerdantForged deployment this was `/mnt/broker/logs/worker-bootstrap.sh`; stale EFS bootstrap caused fresh workers to keep receiving old mock endpoint config.

5. NemoClaw onboard endpoint
   - The worker bootstrap must onboard NemoClaw against the broker public URL, e.g. `https://<broker>/v1/llm`.
   - Remove temporary `127.0.0.1:11434` / mock Ollama endpoints and mock validation keys from production/fake-payment workers.
   - The broker needs a dedicated onboard token accepted by the LLM proxy for onboard validation; do not use the upstream LLM provider key.

6. OpenShell local inference policy
   - Add the `local-inference` policy after sandbox creation so Hermes inside the sandbox can call `inference.local`.

7. Sandbox env/input transport
   - Never shell-source uploaded file contents into an env file.
   - Package job metadata and file content as JSON, base64 it, and decode inside the sandbox with Python or another non-shell parser.
   - This prevents uploaded scripts such as `deploy.sh` from being interpreted during dispatch.

## Verification pattern

Run local syntax/regression checks before deployment:

```bash
python3 -m py_compile worker/poller.py broker-daemon/daemon.py tests/verify-sandbox-execution.py
python3 tests/verify-sandbox-execution.py
bash -n worker/user-data.sh
```

After AWS deployment, run the file e2e and require:

- `execution_mode=nemoclaw-sandbox`
- uploaded files are represented in the prompt/result path
- artifact is non-empty unless the task legitimately returns empty output
- no `broker-llm-proxy`
- no stub mode unless explicitly opted in

## Deployment notes

- Push large patched files via S3 + SSM rather than heredocing through SSM.
- Restart the broker daemon after changing daemon config/rendering.
- Terminate workers launched from stale bootstrap and let the daemon launch a fresh one.
- Probe rendered EC2 user-data on the live worker for the actual endpoint/token placeholder/policy, because repo changes alone do not prove deployed user-data changed.
