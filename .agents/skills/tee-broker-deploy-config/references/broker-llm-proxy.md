# Broker LLM proxy / upstream Authorization failures

Use this when file jobs upload successfully, reach the worker, but result metadata shows `execution_mode: broker-proxy-failed` or the worker says the broker proxy rejected the request.

Observed symptom
- File-job E2E reaches `awaiting_inputs` and uploads files.
- Worker starts execution and returns/completes an artifact, but the artifact says `broker-proxy-failed`.
- Broker journal shows an upstream error from `/v1/llm/chat/completions`, commonly HTTP 400 with `Missing or invalid Authorization header`.

Interpretation
- At this point the file-job lifecycle and worker identity gate are already working.
- The problem is broker-side upstream LLM configuration, not worker boot or SNP attestation.
- The broker proxy may be reachable while still forwarding an empty/invalid API key to the provider.

Debug sequence
1. Check the broker journal for the exact upstream response. Redact keys; keep status code and provider message.
2. Inspect only non-secret config facts on the control plane:
   - `BROKER_LLM_API_KEY` length/non-empty, never value
   - `BROKER_LLM_BASE_URL`
   - `BROKER_LLM_MODEL`
   - the running systemd process env, not just `/opt/broker-daemon/config.env`
3. If the key length is zero, fix config propagation before chasing worker code.
4. Verify the provider with a tiny local OpenAI-compatible request using the intended endpoint/model and an available key, redacting the key from output.
5. After patching config, restart `verdantforged-broker-daemon` and verify the new PID/env, then submit a fresh job. Old failed jobs generally will not recover.

Durable config pattern
- Keep upstream LLM keys broker-side only; never write them to EFS for workers.
- Persist the broker's upstream key in SSM Parameter Store, e.g. `/verdantforged/broker/llm-api-key` as `SecureString`.
- In `deploy.sh`, if `BROKER_LLM_API_KEY` is provided, write it to SSM, then unset/blank the shell env copy before sending bootstrap commands so secret material is not carried through SSM command history.
- In `bootstrap-control-plane.sh`, if `BROKER_LLM_API_KEY` is empty, fetch `/verdantforged/broker/llm-api-key` with decryption before generating `/opt/broker-daemon/config.env`.
- Preserve an existing non-empty key on code-only redeploys so a redeploy does not silently blank the live LLM proxy.
- Add a broker fail-fast guard: if `BROKER_LLM_API_KEY` is empty, return a clear 503 such as `llm_upstream_not_configured` instead of forwarding to the provider and surfacing a confusing upstream 400.

Provider default pitfall
- Do not assume the default provider has a configured key. In the observed case, Gemini defaults were present but no Gemini/Google key existed, so the upstream returned `Missing or invalid Authorization header`.
- If a known-good OpenAI-compatible provider/key exists elsewhere in the user's environment, validate it with a small request and switch defaults or live config explicitly.
- Capture the endpoint/model pair along with the key source; endpoint/model/key must match.

Tests and verification
- Add static deploy checks for:
  - SSM persistence of `BROKER_LLM_API_KEY`
  - bootstrap SSM fetch of `/verdantforged/broker/llm-api-key`
  - key remains broker-side only; no EFS `llm-api-key`
  - broker has a clear missing-key error path
- Run syntax checks for deploy/bootstrap scripts and py_compile for daemon changes.
- Smoke with a fresh file job after restart. Passing upload plus `broker-proxy-failed` means upload/attestation is fixed but LLM proxy is still misconfigured.

Related: if the symptom is `execution_mode: broker-llm-proxy` (post-fix: `no-nemoclaw-failclosed`) with NO `sandbox` block — the worker hit the legacy host-side urllib fallback because its NemoClaw install failed — see `references/worker-dispatch-fallback.md` (2026-06-30 incident). That path was the original cause of the upstream key being silently bypassed, because the worker called the proxy directly with the per-job token rather than routing through the broker's `/v1/llm` validation.