# Changelog

All notable changes to the VerdantForged TEE Broker, newest first.

## 2026-06-28 — Lock in NemoClaw as the compute substrate (t_ab320c7b)

The worker previously tried to install NemoClaw via
`curl https://www.nvidia.com/nemoclaw.sh | bash` — that URL does
not exist (404 in the wild). The `pip install hermes-agent` +
`nemohermes onboard --resume` path is the canonical NemoClaw
install per the NVIDIA NemoClaw repo at `/home/autumn/NemoClaw/`.
Onboarding is now idempotent for warm-pool restarts (--resume
skips re-onboard if a sandbox already exists). The sandbox name
persists in `/etc/systemd/system/worker-poller.service.d/nemoclaw.conf`
so `dispatch_to_sandbox()` in `worker/poller.py:878` finds the
sandbox without re-querying.

- `worker/user-data.sh` step 4: replaced dead `curl nemoclaw.sh`
  with `pip install 'hermes-agent>=0.16,<1'` + `nemohermes onboard
  --non-interactive --resume --name ${NEMOCLAW_SANDBOX_NAME:-tee-worker}
  --agent hermes --no-ollama-autostart`. The `--non-interactive`
  path was chosen so cold starts can complete unattended.
- NemoClaw's inference endpoint is now wired to the broker proxy
  (`NEMOCLAW_ENDPOINT_URL=http://<broker>:8080/v1/llm`); the per-job
  ephemeral token is passed via `--env` at `nemohermes exec` time,
  never on the command line.
- `worker/worker-agent.py` rewritten as a Python script (was empty/
  stub). Runs inside the NemoClaw sandbox per job, calls `inference.local`
  (the OpenShell-intercepted LLM), prints structured JSON on stdout
  for the host poller to parse.
- `worker/skills/{summarize,code-review,photo-glow-up}/SKILL.md` —
  initial NemoClaw skill catalog deployed via `nemohermes <sbx>
  skill install` during onboard.
- `tests/verify-nemoclaw-onboard.py` — hard gates that the wiring
  is in place: dead `curl nemoclaw.sh` URL removed, `pip install
  hermes-agent` present, `dispatch_to_sandbox()` called from
  `execute_in_envelope` (not dead code), sandbox path preferred
  over broker-llm-proxy fallback, sandbox failures fail LOUD.
- EFS now serves the new `user-data.sh` (md5 2e087c73...) and
  `worker-agent.py` (md5 87ae248f1f8c...).

## 2026-06-28 — Fix CFN template: STRIPE_SECRET_KEY parameter misplaced

`deploy.sh` failed cfn-lint with E3001 — the `STRIPE_SECRET_KEY`
parameter was declared at the top level of the template (under
`Resources:`), not inside `Parameters:`. CFN rejected it; AWS would
have rejected the stack at deploy time.

- Moved `StripeSecretKey` into the `Parameters:` block alongside
  `KeepPlaintextForDemo` and the other params.
- Updated user-data.sh's `STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY}` to
  `STRIPE_SECRET_KEY=${StripeSecretKey}` (CFN Ref syntax).
- `deploy.sh` already exports `STRIPE_SECRET_KEY` to the bootstrap via
  SSM env, so the broker-side name was always correct — no change there.
- cfn-lint now reports zero E-level errors. W3037 (`s3:HeadObject` not
  in cfn-lint's action enum) is a known false positive — `HeadObject`
  is a real IAM action and AWS accepts it.

This was blocking the eu-west-1 redeploy (London doesn't support
AMD SEV-SNP, so we need Ireland for the attestation-verifier skill's
live E2E).

## 2026-06-28 — Fix deploy.sh: cfn-lint warnings were fatal

`deploy.sh` bailed with "control-plane CFN has errors" on the
W3037 (`s3:HeadObject`) warning — the same warning that was a
false positive in cfn-lint's action enum. The lint gate used
`if ! cfn-lint ...`, which trips on any non-zero exit (warnings
included). The worker CFN already had the correct pattern
(`grep -q "^E[0-9]"`). Applied the same pattern to control plane.

Now W3037 logs as `[deploy]   lint: W3037 ...` and deploy proceeds.

## 2026-06-28 — Fix deploy.sh SSM push: chunked into multiple SendCommand calls

`deploy.sh` failed with `MaxDocumentSizeExceeded: ... exceeds the 97KB
limit` when the tarball grew beyond 115KB compressed (current is 138KB
after `broker-daemon/daemon.py` and `worker/poller.py` grew). The old
code chunked the b64 payload into one SendCommand with many `echo`
commands, but `Parameters` is a single 97KB blob — chunking commands
within it doesn't help.

Replaced with: one SendCommand **per** chunk, then one final command
to base64 -d + extract. Each request is well under 97KB.

Push now uses N+1 SSM calls (N chunks + 1 extract) instead of 1 call.
Total time grows linearly with N (N=3 currently → ~30s overhead vs 0s
for the single-call version).

## 2026-06-28 — Documentation: Webhook payload and Stripe dispute handling

`task: t_8219dbb2`. Closed documentation gaps identified in the payment block audit:
- Added `docs/payment-flow.md` detailing the webhook payload shape for `job.completed` events, including the `payment` block and `artifact_urls`.
- Updated the Terms of Service (`terms.astro`) to explicitly state the broker's behavior on Stripe chargebacks (`charge.dispute.created`), specifically the suspension of results.
- Added `tests/verify-docs.py` to ensure these critical documentation strings persist.

## 2026-06-28 — Photo-glow-up WASM skill E2E (real photo processing)

`task: t_c27c1d8d`. The complex photo-glow-up skill
(`tee-broker-pattern/tee-broker-skills/photo-glow-up/`, 863-line Rust
source → 182 KiB WASM) is now wired into the live broker end-to-end:

  - `POST /v1/skills`                — register manifest with sha256 hash
  - `POST /v1/skills/{name}/wasm`    — upload the binary to EFS
  - `POST /v1/jobs`                  — submit a photo (input_data)
  - `worker.execute_in_envelope()`   — fetch wasm, run via wasmtime,
                                       return base64 BMP

The worker now distinguishes a "WASM skill" from an LLM/prompt/sandbox
job via the broker-injected `wasm_uri` and runs the WASM in-process via
the wasmtime Python binding (no NemoClaw, no broker proxy call). Three
new properties are pinned by `tests/verify-wasm-skill-e2e.py`:

  - execution_mode == "wasm-skill"  (deterministic, no LLM)
  - output_image_b64 decodes to a BMP whose width/height match the
    SkillOutput JSON
  - resource_limits.max_fuel / max_duration_ms from the manifest are
    forwarded into the envelope and enforced by wasmtime's consume_fuel
    + epoch_interruption

### Bug fixes in this slice

Three issues that the previous run (run #80, timed out) had introduced:

  - **Init block clobbered the WASM result.** `execute_in_envelope`
    initialised `llm_output=""`, `execution_mode="unknown"`, etc.
    AFTER the WASM block had set them. The WASM block fell through to
    the unified result-building block and the result envelope came out
    with empty output / "unknown" mode. Fixed by hoisting the init
    defaults to the top of `execute_in_envelope` (before the WASM
    check) so they always run exactly once.
  - **LLM/loop/sandbox ladder overwrote a valid WASM result.** Once
    the init defaults were unconditional, the no-llm-token guard then
    the broker-llm-proxy `else` branch both reassigned execution_mode
    and llm_output — silently erasing the WASM result. Fixed by
    gating the entire ladder on `not wasm_already_ran` (signalled by
    `_wasm_attestation_for_result is not None`).
  - **Resource caps never reached the worker.** `max_fuel` /
    `max_duration_ms` were stored in the `skills` table at
    registration time but never propagated into the envelope, so the
    worker's `env.get(\"max_fuel\", 100_000_000)` always fell back to the
    default. Added `resolve_skill_resource_limits()` and inject
    `max_fuel` + `max_duration_ms` into the envelope at both submit
    paths (single-phase and two-phase).

Verified: `tests/verify-wasm-skill-e2e.py` 20/20, no regression in
`tests/verify-wasm-skill-upload.py` (15/15), `verify-skill-registration.py`
(39/39), `verify-wasm-manifest-verification.py` (17/17),
`verify-tool-calling-loop.py` (16/16), `verify-sandbox-execution.py` (9/9).

## 2026-06-28 — Tool-calling loop + per-job execution context

`task: t_5e7f89fa`. Skills can now express multi-step reasoning or
per-job state instead of being one-shot prompts. The worker gained:

  - `JobContext`     — per-job scratch state (messages, budgets)
  - `SkillTool`      — one locally-callable tool the loop may invoke
  - `call_broker_llm_proxy()` — lifted-out single-turn proxy call
  - `run_tool_calling_loop()` — multi-turn loop, capped by turns+fuel

The loop stays within the broker proxy's VULN-S5 forwarding whitelist
(`model, messages, max_tokens, stream`) — local Python callables only,
never OpenAI-style `tools=[...]` forwarded upstream. Crypto envelope,
IMDSv2 fix, and broker-proxy-only LLM path all preserved.

Verified with `tests/verify-tool-calling-loop.py` (16/16) — built test
first, watched it fail, wrote minimal implementation to pass.

## 2026-06-28 — NemoClaw sandbox execution (NVIDIA pillar made real)

`task: t_4740dce6`. Jobs now execute INSIDE the NemoClaw sandbox, not
as a plain host process. The "trusted execution environment" claim
goes from a hollow on-paper assertion to a verifiable, attested
artifact in the result envelope.

### Architecture change

Before:
```
EFS inbox → poller.py (host) → broker LLM proxy → LLM → poller writes outbox
NemoClaw sandbox: dormant, unused
```

After:
```
EFS inbox → poller.py (host)
  → nemohermes exec sandbox: python3 /sandbox/worker-agent.py
    → agent calls inference.local → OpenShell intercepts on host
    → forwards to http://<broker-ip>:8080/v1/llm
  → nemohermes exec returns result JSON
→ poller reads result, applies crypto envelope, writes outbox
NemoClaw sandbox: active, attested, network-enforced
```

### Key files

- `worker/poller.py`
  - `dispatch_to_sandbox(job_id, skill_prompt, input_data, llm_token,
    result_pubkey, broker_ip, sandbox_name)` — new function that runs
    `nemohermes exec --no-tty --timeout <n> --env <json> <job_id>`
    python3 /sandbox/worker-agent.py` with the per-job LLM token as
    `COMPATIBLE_API_KEY` (set via `--env` so it never appears on the
    command line). Parses the agent's JSON output. Raises
    RuntimeError on non-zero exit with first 500 chars of stderr.
  - `_active_sandbox_name()` — resolves the sandbox name from
    env-supplied override → live `NEMOCLAW_SANDBOX_NAME` env var →
    module-level `DEFAULT_SANDBOX_NAME` (captured at import time).
    Re-reading the env on every call lets a worker pick up theLsandbox
    without a process restart.
  - `_broker_ip_from_proxy_url()` — extracts the IP from the
    envelope's `llm_proxy_url`.
  - `execute_in_envelope()` — new branch: when the active sandbox
    name is non-empty AND `llm_token` + `llm_proxy_url` are present,
    dispatch to the sandbox. Falls back to the legacy host-side
    `broker-llm-proxy` call when the sandbox is not configured.
  - Result envelope now carries `execution_mode: \"nemoclaw-sandbox\"`
    on success, `\"sandbox-failed\"` on dispatch error (with the
    failure reason in `llm_error`), and a `sandbox` attestation block
    naming the sandbox, declaring it attested, and documenting the
    inference routing (`inference.local → OpenShell → broker proxy`).

- `worker/worker-agent.py` — NEW. Stdlib-only Python script that runs
  inside the sandbox. Reads `JOB_ID`, `SKILL_PROMPT`, `INPUT_DATA`,
  `COMPATIBLE_API_KEY` from env (set by `nemohermes exec --env`),
  POSTs to `http://inference.local/chat/completions` with
  `Authorization: Bearer <COMPA...Y>`, prints the result as a
  single JSON object on stdout. Stdlib urllib only — no extra pip
  dependencies (the sandbox image is immutable; we don't modify it).

- `worker/user-data.sh` — step 4b: after `nemohermes onboard`, pre-load
  `/opt/worker/worker-agent.py` into the sandbox at
  `/sandbox/worker-agent.py` via `nemohermes cp`. Step 4c: kill the
  local mock LLM now that onboard is complete — runtime inference
  flows through the sandbox. Step 5 also fetches `worker-agent.py`
  from EFS into `/opt/worker/` so step 4b can find it.

- `scripts/bootstrap-control-plane.sh` — also push `worker-agent.py`
  to EFS alongside `poller.py`.

### Acceptance criteria

- [x] Mock LLM kept for onboard validation only; stopped once onboard
- [x] NemoClaw configured with `NEMOCLAW_PROVIDER=custom` so the
  sandbox accepts bearer-token auth
- [x] `worker-agent.py` pre-loaded into sandbox during user-data.sh
- [x] Poller dispatches jobs to sandbox via `nemohermes exec`
- [x] Sandbox agent calls `inference.local` which OpenShell routesS to
  the broker proxy
- [x] Per-job LLM token passed to sandbox as `COMPATIBLE_API_KEY`
  (via `--env`, never on the command line)
- [x] Result envelope shows `execution_mode: \"nemoclaw-sandbox\"`
- [x] Result envelope includes `sandbox.attested: true` plus the
  inference routing
- [x] Fallback: if sandbox exec fails, poller records
  `execution_mode: \"sandbox-failed\"` with the error in `llm_error`
- [x] `tests/verify-sandbox-execution.py` — 9/9 passing (S1-S9)
