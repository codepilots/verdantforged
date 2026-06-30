# LLM / Compute runtime — what actually runs vs what the user-data.sh claims

**Updated 2026-06-29 (session 2):** the NemoClaw install path went through
three iterations in a single session. The CORRECT install is the official
NVIDIA installer at `https://www.nvidia.com/nemoclaw.sh` — which IS LIVE
(returns 200, ~6KB bash script). An earlier version of this reference
claimed the URL was 404; that was wrong — the 404 was from a local network
issue, not a dead URL. The installer clones the NemoClaw repo from GitHub,
installs Node.js + npm + Docker + OpenShell + the nemoclaw/nemohermes CLI
automatically.

The architecture now is:

- **Worker runs Hermes inside a NemoClaw/OpenShell sandbox** (Docker
  container on the worker host, installed via the official NVIDIA installer
  script, NOT pip, NOT raw npm).
- **LLM calls go through the broker proxy** with a per-job ephemeral
  token (broker holds the upstream key; sandbox sees only the token).
- **Skills (summarize / code-review / photo-glow-up) execute inside
  the sandbox** via `nemohermes <sandbox> exec ... python3
  /sandbox/worker-agent.py`. Skill catalogs are pulled from EFS at
  `worker/skills/{name}/SKILL.md` and deployed via `nemohermes <sandbox>
  skill install`.
- **Crypto (Ed25519 signing, X25519+ChaCha20 encryption, result
  envelope) stays on the host**. The sandbox only does the skill
  execution; the poller signs the result before the broker verifies.

## TL;DR — the live compute path (June 29, 2026+)

```
client → POST /v1/jobs (broker)
   ↓
control plane daemon:
  - validates stripe_pi_id (calls Stripe API)
  - generates llm_token (10-30 min TTL)
  - writes envelope to /mnt/broker/jobs/inbox/
  - stamps NEMOCLAW_SANDBOX_NAME into envelope
   ↓
worker poller picks up envelope:
  - reads llm_token from envelope
  - calls `nemohermes <sandbox> exec --no-tty --timeout 120 \
            --env <json> <sandbox> python3 /sandbox/worker-agent.py`
   ↓
NemoClaw exec runs `worker-agent.py` INSIDE the sandbox:
  - reads SKILL_PROMPT, INPUT_DATA, RESULT_PUBKEY from --env
  - calls inference.local (OpenShell-intercepted LLM)
  - gets routed by OpenShell policy to broker proxy at
    https://verdant.codepilots.co.uk/v1/llm with the per-job token
   ↓
broker daemon validates token, forwards to:
  BROKER_LLM_BASE_URL (currently https://ollama.com/v1)
  with BROKER_LLM_API_KEY (from config.env)
  forcing model = BROKER_LLM_MODEL (currently minimax-m3)
   ↓
Ollama Cloud returns response
   ↓
worker-agent.py prints JSON on stdout, nemohermes exec returns it
   ↓
worker poller (host) embeds response in result envelope + Ed25519 signs
   ↓
broker verifies signature, captures Stripe PI, signs result, persists
   result.execution_mode = "nemoclaw-sandbox"
   result.sandbox_attestation = {name, attested: true, network_policy: ...}
```

**The sandbox IS real.** SEV-SNP is on the underlying m6a.xlarge,
the OpenShell sandbox is what Hermes runs in, and `sandbox.attested: true`
in the result envelope is verifiable.

## The correct install path (verified 2026-06-29)

The official NVIDIA installer at `https://www.nvidia.com/nemoclaw.sh` is
**LIVE** (returns HTTP 200, 6356 bytes, `text/plain`). It is a bash script
that:

1. Clones the NemoClaw repo from GitHub (`git clone --depth 1`)
2. Runs `scripts/install.sh` from the clone
3. Installs Node.js 22+, npm, Docker, OpenShell, and the `nemoclaw` CLI
4. Auto-launches `nemoclaw onboard` if it can find the binary

For non-interactive (cloud-init) use:

```bash
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
export NEMOCLAW_PROVIDER=custom
export NEMOCLAW_ENDPOINT_URL="https://your-broker-domain/v1/llm"
export NEMOCLAW_MODEL="minimax-m3"
export COMPATIBLE_API_KEY="$BROKER_ONBOARD_TOKEN"
export NEMOCLAW_AGENT=hermes
export NEMOCLAW_SANDBOX_NAME="tee-worker"
export NEMOCLAW_NO_EXPRESS=1
export NEMOCLAW_NO_OLLAMA_AUTOSTART=1
export HOME=/root

curl -fsSL https://www.nvidia.com/nemoclaw.sh | \
    bash -s -- --non-interactive --yes-i-accept-third-party-software
```

After install, `hash -r` to refresh PATH, then verify:
```bash
hash -r 2>/dev/null || true
export PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"
[ -x /usr/bin/nemohermes ] && echo "nemohermes OK"
```

## Three things that blocked the install (all fixed in this session)

### 1. The URL was assumed dead (it wasn't)

An earlier version of this reference claimed `https://www.nvidia.com/nemoclaw.sh`
returns 404. This was wrong — the URL is live and returns the official
installer. The 404 was from a local network/DNS issue on the test machine,
not a dead URL. The URL is the canonical install path per NVIDIA's docs at
`https://docs.nvidia.com/nemoclaw/latest/get-started/quickstart.html`.

### 2. NemoClaw is a Node.js CLI, not a Python package

The `nemohermes` and `nemoclaw` binaries are Node.js scripts from the
`nemoclaw@0.1.0` npm package. `pip install hermes-agent` installs the
Hermes Python agent runtime (different CLIs: `hermes`, `hermes-acp`),
NOT NemoClaw. The official installer handles Node.js + npm + Docker
prerequisites automatically — don't try to `npm install -g nemoclaw`
directly (missing prereqs).

### 3. Onboard validation needs a real token

The NemoClaw onboard wizard validates the inference endpoint by making a
test `POST /v1/llm/chat/completions` call. The broker's LLM proxy only
accepts per-job tokens from its database. A placeholder `onboard-placeholder`
is rejected with 401. Fix: add `BROKER_ONBOARD_TOKEN` to the broker's
config.env — a dedicated long-lived token (`secrets.token_hex(24)`) that
the daemon accepts for non-job validation calls. Do NOT use the upstream
LLM API key for this.

### 4. Broker daemon listens on 127.0.0.1, not 0.0.0.0

The daemon's HTTP server binds to `127.0.0.1:8080`. Workers cannot reach
it via the private IP. Use the public URL (`https://your-domain/v1/llm`)
which goes through Caddy (TLS on 443 → proxy to localhost:8080). Also add
a SG rule for port 8080 from the worker SG as a fallback.

### 5. Idle timer too short for NemoClaw cold start

The default 10-min idle timer kills the worker before NemoClaw finishes
installing (~3-5 min) + onboarding (~2 min) + Docker image pull (~1 min).
Set `BROKER_IDLE_BUFFER_MINUTES=20` to give the worker time to complete
the full boot sequence.

## What to tell the operator when they ask "is NemoClaw wired up?"

1. **Check the actual code path** (grep user-data.sh for `nemoclaw.sh`
   and `nemohermes onboard`).
2. **Check the runtime** via SSM: `[ -x /usr/bin/nemohermes ] && echo OK`
   on a running worker.
3. **Check the test job's execution_mode**: `"nemoclaw-sandbox"` = working,
   `"sandbox-failed"` = wiring there but runtime bug, `"broker-llm-proxy"`
   = fallback running because NemoClaw isn't up.
4. **Cross-check against CHANGELOG.md** for which commits wired it up.

## Class-level pattern — "lock-in" vs "wire-up"

What "lock in" means in this codebase:
1. **Verification script** (`tests/verify-nemoclaw-onboard.py`) —
   11 static checks + `--live` flag for runtime checks.
2. **Live-state check** — SSH into a worker and assert `nemohermes`
   exists at `/usr/bin/nemohermes`.
3. **Idempotent bootstrap** — `--resume` flag, systemd drop-in,
   EFS-mirrored skill catalog.
4. **Architecture comment in source** — the `dispatch_to_sandbox()`
   docstring explains the threat model.

## What is dead / vestigial

| What | Status |
|------|--------|
| `mock-ollama.py` on :11434 | Still started in user-data step 4 but killed immediately. Vestigial — should be removed. |
| `pip install hermes-agent` | REMOVED. Was wrong package manager. |
| `npm install -g nemoclaw` direct | REMOVED. Missing prerequisites. Use the official installer. |
| `COMPATIBLE_API_KEY=onboard-placeholder` | REMOVED. Replaced with `BROKER_ONBOARD_TOKEN`. |

## Related

- `tests/verify-nemoclaw-onboard.py` — the gate that catches regressions.
- `worker/skills/{summarize,code-review,photo-glow-up}/SKILL.md` — skill catalog.
- `worker/worker-agent.py` — runs inside the sandbox per job.
- `worker/poller.py:808-822` — `dispatch_to_sandbox()` architecture + threat model.
- `references/sev-snp-tsm-configfs-attestation.md` — real SEV-SNP attestation.
- `~/.hermes/skills/devops/nemoclaw-hermes-sandbox-setup/SKILL.md` — canonical NemoClaw setup.