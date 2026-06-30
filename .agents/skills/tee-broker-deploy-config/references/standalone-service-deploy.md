### 8.7 — Verification artifacts to capture

Save the captured outputs of every step above to `docs/<service>-live-YYYY-MM-DD.md` in the project repo. The smoke-test report (`docs/skill-library-smoketest-2026-06-29.md`) and the live status report (`docs/skill-library-live-2026-06-29.md`) are the worked-example artifacts for the skill-library service — copy that two-doc pattern for the next additive deploy.

## Step 9 — Caddy reverse-proxy integration (optional, for public access)

If the new service needs to be reachable from outside the control plane (external agents, public read endpoints, Swagger UI), merge a route block into `/etc/caddy/Caddyfile`. This step is class-level for any sibling service that needs HTTPS termination through the existing broker domain. Tested with Caddy v2.11.4 (the official apt build does NOT include the `rate_limit` module — drop that directive or use a custom build).

### 9.1 — Validate Caddyfile BEFORE reloading

Caddy reloads the new config and a syntax error will silently take down the live broker. Always pre-validate:

```python
# Upload new Caddyfile via S3 (no token patterns, safe from redaction)
s3.put_object(Bucket='verdantforged-artifacts-eu-west-1',
              Key='tmp/Caddyfile.new', Body=new_caddyfile.encode())

r = ssm.send_command(
    InstanceIds=[cp], DocumentName='AWS-RunShellScript',
    Parameters={'commands': [
        'bash -c "aws s3 cp s3://verdantforged-artifacts-eu-west-1/tmp/Caddyfile.new /tmp/Caddyfile.new"',
        'bash -c "sudo -n caddy adapt --config /tmp/Caddyfile.new --envfile /opt/broker-daemon/config.env > /tmp/caddy_adapted.json 2> /tmp/caddy_adapt_err.txt"',
        'bash -c "echo EXIT_CODE=$?"',
        'bash -c "cat /tmp/caddy_adapt_err.txt"',
    ]}, TimeoutSeconds=30
)
# Expected: EXIT_CODE=0, stderr empty. If non-zero, fix and retry.
```

The `caddy adapt` step is critical — Caddy's `systemctl reload` doesn't validate first; a bad directive takes the site down silently. The official apt `caddy` package does NOT include the `rate_limit` module (it's a community plugin), so add the directive via JSON config not the Caddyfile if you need rate-limiting.

### 9.2 — Reload, not restart

`systemctl reload caddy` sends a SIGHUP-style signal that re-reads the config and continues running with the SAME PID. A `restart` would drop in-flight connections and trigger a brief 502 window. Always reload:

```python
r = ssm.send_command(
    InstanceIds=[cp], DocumentName='AWS-RunShellScript',
    Parameters={'commands': [
        'bash -c "sudo -n cp /tmp/Caddyfile.new /etc/caddy/Caddyfile"',
        'bash -c "sudo -n systemctl reload caddy"',
        'bash -c "sleep 3; ps -eo pid,etime,cmd | grep caddy | grep -v grep"',  # PID unchanged
        'bash -c "ps -eo pid,etime,cmd | grep daemon.py | grep -v grep"',         # broker unchanged
    ]}, TimeoutSeconds=60
)
```

### 9.3 — The path-prefix routing pattern

For a service that wants to be reachable at `https://broker.domain/library/*` → `http://127.0.0.1:SERVICE_PORT/*` (i.e., the prefix is stripped at the proxy):

```caddy
{$BROKER_DOMAIN:placeholder.verdantforged.invalid} {
    encode zstd gzip
    # ... existing site config ...
    handle_path /library/* {
        reverse_proxy 127.0.0.1:8091
    }
    handle_path /library-docs/* {
        reverse_proxy 127.0.0.1:8091
    }
    # ... rest of site config ...
}
```

**Two Caddy gotchas that bit me:**

1. **`handle_path` strips the prefix before proxying** — `/library/v1/skills` becomes `/v1/skills` at the upstream. That's exactly what you want IF your service's routes don't start with `/library/...` again. If they do (like our library's `/v1/library/skills`), use plain `handle /library/*` instead and accept the duplicate prefix in the upstream URL.

2. **Insert `handle_path` blocks BEFORE `handle / {` (the file_server root).** Caddy's `handle` matching is order-sensitive — the first match wins, so put specific routes before the catch-all static-serve.

### 9.4 — Public-read + bearer-write security model

Most operator-facing services want anonymous GET but authenticated POST/DELETE. Caddy's `handle_path` doesn't have native method-based auth, but you can layer it with `basicauth` (NOT useful for tokens) or — the class pattern for any "public read / bearer write" API — handle it on the upstream side:

```python
# In your FastAPI route:
def get_skill(ref: str):  # public — no auth check
    ...

def register_skill(card: SkillCard, ...):  # requires bearer
    if not _check_bearer(request.headers.get("authorization")):
        raise HTTPException(401, "library_auth_required")
    ...
```

The bearer check lives in Python, Caddy just proxies everything. This keeps the auth logic versioned with the service and avoids the Caddyfile becoming an auth matrix.

**Optional light rate-limit** for unauthenticated writes (against random anonymous abuse):

```caddy
@library_write method POST DELETE PUT
rate_limit @library_write 60r/m  # ← needs custom Caddy build with rate_limit module
```

If `rate_limit` isn't available (default apt package), drop the block — the broker-side auth check already rejects the majority of write attempts. This is fine for hackathon-grade security; for production, add CloudFront/WAF in front.

### 9.5 — CORS defaults inherit from the broker

The broker's `{$BROKER_CORS_ORIGIN}` env var already controls CORS for the existing site. New `handle_path` blocks inside the same site block automatically inherit those headers because they're declared in the outer `header { ... defer }` block. Don't re-declare CORS in each `handle_path`.

### 9.6 — Verify public access from external IP

Local `curl` against `127.0.0.1:8091` only proves the service is up. Public exposure requires a curl from a non-VPC IP:

```python
import subprocess
r = subprocess.run(['curl', '-sS', 'https://verdant.codepilots.co.uk/library/healthz'],
                   capture_output=True, text=True)
print(r.stdout)  # expected: {"ok":true,"db":"ok","efs":"ok"}
```

If this returns 404 or 502, Caddy hasn't picked up the new route — check the Caddyfile location was right (BEFORE the `handle / {` block in the outer site, not after).

### 9.7 — Update agent skills + docs to point at the public URL

Once public, scripts in `~/.hermes/skills/<name>/scripts/` should default to the public URL so operators don't need to SSM-tunnel. Pattern:

```bash
# Before (internal-only):
URL="${SKILL_LIBRARY_URL:-http://127.0.0.1:8091}"

# After (public):
URL="${SKILL_LIBRARY_URL:-https://verdant.codepilots.co.uk/library}"
```

The `SKILL_LIBRARY_URL` env override stays — operators can still point to localhost for debugging.