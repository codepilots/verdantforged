# LLM Proxy — Live Probe Recipe

How to smoke-test the broker's `/v1/llm/chat/completions` proxy against
the live control plane after a code change. The proxy requires a
worker-issued `llm_token` validated against the SQLite `llm_tokens`
table, so the only way to test from outside is to mint a token directly
into the DB.

## Endpoint

```
POST https://verdant.codepilots.co.uk/v1/llm/chat/completions
Authorization: Bearer <llm_token>
Content-Type: application/json

{
  "model":   "<ignored — broker uses BROKER_LLM_MODEL>",
  "messages":[{"role":"user","content":"..."}],
  "max_tokens": <int 1..100000, broker-capped at MAX_TOKENS_CAP>,
  "stream":  false
}
```

Response (200) is OpenAI-shaped, with a `_billing` block appended by
the broker that includes the hashed account, prompt_tokens,
completion_tokens, and demo_cap.

## Mint a token directly into the DB

The broker is the only thing that normally mints these tokens (at
`/v1/jobs` submit time). To probe the proxy without running a full job:

```bash
# 1. Find the control plane (use the SKILL.md filter; see Pitfall
#    below — the tag is `verdantforged-broker-control-control` on
#    the live deployment, not `verdantforged-broker-control`).
CONTROL_PLANE_ID=$(aws ec2 describe-instances \
  --region eu-west-1 \
  --filters "Name=tag:Role,Values=control-plane" \
           "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" --output text)

# 2. Mint via SSM. The DB lives at /mnt/broker/logs/broker.db.
#    Use a fresh pi_test_* stripe_pi_id so the daily cap is per-account
#    and you don't get rate-limited by your own prior probes.
TOKEN="llm_test_$(openssl rand -hex 12)"
JOB_ID="job_test_$(openssl rand -hex 6)"
PI_ID="pi_test_$(openssl rand -hex 4)"
NOW=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
EXP=$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%S+00:00)

SCRIPT=$(cat <<EOF
import sqlite3
c = sqlite3.connect("/mnt/broker/logs/broker.db")
c.execute(
    "INSERT OR REPLACE INTO llm_tokens (token, job_id, stripe_pi_id, created_at, expires_at, tokens_used, calls) VALUES (?, ?, ?, ?, ?, 0, 0)",
    ("$TOKEN", "$JOB_ID", "$PI_ID", "$NOW", "$EXP"),
)
c.commit()
print("MINTED:", c.execute("SELECT token, job_id, expires_at FROM llm_tokens WHERE token=?", ("$TOKEN",)).fetchone())
c.close()
EOF
)
B64=$(echo "$SCRIPT" | base64 -w0)
aws ssm send-command --region eu-west-1 \
  --instance-ids "$CONTROL_PLANE_ID" \
  --document-name AWS-RunShellScript \
  --parameters "{\"commands\":[\"echo $B64 | base64 -d | python3 -\"]}" \
  --query Command.CommandId --output text
```

**Why base64+pipe:** `aws ssm send-command` runs `/bin/sh`, not bash.
Quoting a Python heredoc with the multi-line INSERT inside JSON-string
parameters is brutal — every nested quote has to be escaped for both
sh and JSON. base64 sidesteps that.

## Send the actual probe

```bash
# From the control plane itself (so the bearer token is never on the
# public wire — but in practice verdant.codepilots.co.uk also accepts
# external requests with a valid token).
PROBE_SCRIPT=$(cat <<'EOF'
import urllib.request, json
url = "https://verdant.codepilots.co.uk/v1/llm/chat/completions"
body = json.dumps({
  "model":"minimax-m3:cloud",
  "messages":[{"role":"user","content":"<your prompt>"}],
  "max_tokens": 100000,
  "stream": False,
}).encode()
req = urllib.request.Request(url, data=body, headers={
  "Content-Type": "application/json",
  "Authorization": "Bearer <TOKEN>",
})
with urllib.request.urlopen(req, timeout=120) as r:
    obj = json.loads(r.read())
    msg = obj["choices"][0]["message"]
    print("content_len:", len(msg.get("content") or ""))
    print("reasoning_len:", len(msg.get("reasoning") or ""))
    print("usage:", obj.get("usage"))
    print("billing:", obj.get("_billing"))
EOF
)
B64=$(echo "$PROBE_SCRIPT" | base64 -w0)
aws ssm send-command --region eu-west-1 \
  --instance-ids "$CONTROL_PLANE_ID" \
  --document-name AWS-RunShellScript \
  --parameters "{\"commands\":[\"echo $B64 | base64 -d | python3 -\"]}" \
  --query Command.CommandId --output text
```

## Proving a max_tokens cap change took effect

The model is `minimax-m3:cloud`, a reasoning model that splits budget
between hidden `reasoning` and visible `content`. To prove a new cap
is in effect end-to-end, send a prompt that *requires* more than the
old cap to finish:

```
"Generate a numbered list of 200 items, one per line, formatted exactly as
 `<n>. <word>` where <n> goes from 1 to 200 and <word> is any common
 English noun. Begin with '1.' on the first line."
```

This produces ~2000–5000 completion tokens. If you see
`completion_tokens` in `usage` at >1500 and the response contains all
200 items, the cap is high enough. If the response truncates mid-list
at the old cap, you'll see `completion_tokens` ≈ old cap and the list
ends around item 60–80.

Cross-check the broker's own log to see what it forwarded:

```bash
grep "llm_proxy" /mnt/broker/logs/broker-daemon.log | tail -5
# Format: llm_proxy job=<id> prompt=<n> completion=<n> total=<n> account=<hash>
```

## Reading the response when the model is a reasoning model

The upstream `minimax-m3:cloud` returns a `reasoning` field separately
from `content`. If the response shows `content=""` but
`reasoning` is long, the cap is starving the visible answer, not the
model. The fix is to raise the cap (default 100k as of the VULN-S5
rework), not to chase the model.

## Common failures

- `401 {"error":"LLM token expired"}` — token's `expires_at` is in the
  past or before "now" at request time. Remint with `+1 hour` expiry.
- `429 {"error":"daily token cap exceeded"}` — that
  `stripe_pi_id` account already used 50k+ tokens today. Use a fresh
  `pi_test_<randhex>` to get a new account bucket.
- `502 {"error":"upstream LLM error: 4xx"}` — check the
  `BROKER_LLM_*` env vars in `/opt/broker-daemon/config.env` and that
  the upstream key is valid.
- Caddy 502 with healthy daemon on 8080 — usually Caddy reloading. Wait
  a few seconds and retry. The daemon is bound at `127.0.0.1:8080`
  and Caddy at `*:443`; they're independent processes.
