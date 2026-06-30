# Warm-pool architecture + provider quirks

Session notes from the VerdantForged eu-west-1 production bring-up
(2026-06-28). Captures two patterns that didn't fit cleanly in the
SKILL.md pitfall list: (1) the warm-pool design that keeps workers
alive across jobs, and (2) the LLM-backend provider quirks we hit.

## Warm-pool — full design

### The problem

- Worker cold-start is ~16 min (NemoClaw sandbox download from
  NVIDIA's CDN times out, retries with backoff, eventually gives up).
- Broker's idle-timer terminates workers after 10 min of inactivity
  (`BROKER_IDLE_BUFFER_MINUTES=10`).
- Burst traffic: job 1 takes 16 min, job 2 takes 16 min, job 3 takes
  16 min. Net throughput = 1 job / 16 min. Unusable for demos.

### The solution (two-part)

**Broker side** — flip two env vars on the control plane:

```bash
# /opt/broker-daemon/config.env
BROKER_DISABLE_IDLE_TERMINATION=1   # skip _idle_terminate_loop entirely
BROKER_LLM_TOKEN_TTL_MIN=30         # raise from default 10 min
```

`BROKER_DISABLE_IDLE_TERMINATION=1` causes `_idle_terminate_loop` to
return immediately after logging "idle timer DISABLED". The broker
still does `_find_existing_worker()` on startup and adopts any
running tagged worker.

`BROKER_LLM_TOKEN_TTL_MIN=30` covers the 16-min cold start with
margin. The original 10-min value was set when broker workers
cold-started in ~3 min (no NemoClaw). Now that cold start is
~16 min, the token expires mid-bootstrap.

**Out-of-band lifecycle** — `scripts/warm-worker-manager.sh`:

Standalone bash loop, runs every 5 min via cron OR systemd. Steps:

1. Find running workers via `aws ec2 describe-instances` filtered by
   `tag:Project=verdantforged + tag:Role=tee-worker + state=running`.
2. If none, launch one with `aws ec2 run-instances` using the same
   AMI/SG/subnet/IAM profile as the broker. Tags: `ManagedBy=
   warm-worker-manager` (vs `broker-daemon` for broker-launched).
3. If at least one is running, verify health via SSM
   `LastPingDateTime` — if older than 5 min, terminate it and let
   the loop relaunch.
4. Sleep 5 min, repeat.

Designed to NEVER launch a duplicate (the tag filter is the
gate), but in practice the broker's `_find_existing_worker` uses
`ManagedBy=broker-daemon`, so warm-manager-tagged workers are
adopted the same way. If you run both the broker's launch and the
warm-manager, you'll get two workers (one tagged each way). Disable
warm-manager when broker-side launch is sufficient.

### Verified end-to-end (2026-06-28)

Worker `i-02c2c57cef9f2f975` launched at 18:18 UTC. With
`BROKER_DISABLE_IDLE_TERMINATION=1` on the broker:

- 18:18:49 — broker adopted existing worker (`adopted existing worker
  i-02c2c57cef9f2f975` in daemon log)
- 18:42:08 — last job completed (broker-llm-proxy, 200 completion tokens)
- 18:50 — still running, 28+ min uptime, broker idle 10+ min, no
  termination attempt

Log line confirming flag is honored:
```
INFO broker-daemon idle timer DISABLED (BROKER_DISABLE_IDLE_TERMINATION=1);
warm-pool worker will not be terminated by the broker.
Out-of-band lifecycle (warm-worker-manager.sh) owns termination.
```

### Cost

- `m6a.xlarge` on-demand: ~$0.23/hr in eu-west-1
- Persistent warm worker: ~$168/month
- Alternative: spot instance, ~$0.06/hr = ~$45/month
- The `BROKER_ENABLE_SEV_SNP=1` flag (CFN) gates whether the broker
  even asks for SEV-SNP — disable for dev/ci to use t3.medium instead
  (~$30/month) and skip the warm-pool dance entirely.

### When NOT to use this pattern

- Per-job latency tolerance is high (10+ min is acceptable) → just
  let workers cold-start per job, save the warm-pool cost
- Worker cold-start is fast (< 5 min) → keep `BROKER_IDLE_BUFFER_MINUTES`
  low, let workers come and go
- SEV-SNP isn't required → drop to `BROKER_ENABLE_SEV_SNP=0`, use
  Nitro isolation only, switch to cheaper instance types

## LLM provider quirks

### Ollama Cloud

**Endpoint**: `https://ollama.com/v1` — NOT `https://api.ollama.com/v1`.

The `api.` host 301-redirects to `ollama.com`. `urllib` follows
redirects as GET (RFC 7231 default), which makes the POST'd
`/v1/chat/completions` request arrive at the destination as a GET,
returning 405 Method Not Allowed. The fix is to use `ollama.com`
directly and skip the redirect hop.

**Model names**: NO `:cloud` suffix. The model is `minimax-m3`,
NOT `minimax-m3:cloud`. The suffix is a convention from
self-hosted Ollama (where `:8b` / `:70b` etc. distinguish sizes).
On Ollama Cloud every model is "cloud" by definition, so the
suffix is dropped. Use `GET /v1/models` with the auth key to see
the current model list — 35 models as of 2026-06-28 including
`minimax-m3`, `glm-5.2`, `gemini-3-flash-preview`, etc.

**Auth key location**: `~/.hermes/auth.json` →
`credential_pool["custom:api.ollama.com"][0].access_token` on the
operator's local machine. Push to broker via SSM with the
redact-friendly pattern (write to config.env directly, never echo
the key, chmod 0600 the file). See `references/bootstrap-idempotency-checklist.md`
for the SSM env push pattern.

**Test recipe** before pushing the key to the broker:

```python
import json, os, urllib.request
with open(os.path.expanduser('~/.hermes/auth.json')) as f:
    auth = json.load(f)
key = auth['credential_pool']['custom:api.ollama.com'][0]['access_token']
req = urllib.request.Request(
    "https://ollama.com/v1/chat/completions",
    data=json.dumps({
        "model": "minimax-m3",
        "messages": [{"role": "user", "content": "Reply with just 'pong'."}],
        "max_tokens": 20,
    }).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as r:
    print(json.loads(r.read())["choices"][0]["message"]["content"])
# Expected: "pong" (or similar short reply)
```

If you get 405, you're hitting `api.ollama.com`. If you get 404,
wrong model name. If you get 401, expired key.

### Broker LLM proxy failure modes

When a job's `execution_mode` is `broker-proxy-failed`, the result
envelope still includes the SEV-SNP attestation (good — the worker
ran, the broker received a result, but the LLM call failed). Common
causes:

1. **"LLM token expired"** — token TTL too short for the cold start.
   Fix: raise `BROKER_LLM_TOKEN_TTL_MIN` above the cold-start time.
2. **"upstream LLM error: 400"** — Ollama rejected the request format
   (rare; usually means model doesn't support the call).
3. **"upstream LLM error: 429"** — Ollama free-tier rate limit.
   Upgrade to paid tier or back off.
4. **No model name in result** (`model: ""`) — proxy never reached
   upstream. Check daemon log for the exact failure.

For demos that don't actually need a real LLM call, the broker
still signs the result envelope with valid Ed25519 signatures — the
attestation block in the result IS real SEV-SNP hardware evidence,
even when the LLM call fails. Don't be fooled into thinking a
`broker-proxy-failed` means the broker is broken.
