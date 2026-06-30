# VerdantForged TEE Broker — Code Review & Security Audit

**Date**: 2026-06-27  
**Reviewer**: GLM-5.2 (automated adversarial review)  
**Scope**: All source code in `tee-broker-deploy/`  
**Method**: Adversarial architecture review (Phase 0 + Round 1)

## Fix Status (as of 2026-06-28)

| ID | Severity | Status | Test |
|----|----------|--------|------|
| VULN-S1 | HIGH (demo-blocker) | FIXED (commit 9c1ee55) | verify-security-fixes.py D-series |
| VULN-S2 | HIGH (demo-blocker) | FIXED (commit 9c1ee55) | verify-security-fixes.py auth tests |
| VULN-S3 | HIGH (demo-blocker) | FIXED (commit 9c1ee55) | verify-crypto-e2e.py #15 |
| CQ-6   | HIGH (demo-blocker) | FIXED (commit cfb0d04) | verify-security-fixes.py D1-D3 |
| **VULN-S4** | MEDIUM | **FIXED (kanban t_6f3826b3, this batch)** | verify-medium-severity-fixes.py F1-F7 |
| **VULN-S5** | MEDIUM | **FIXED (kanban t_6f3826b3, this batch)** | verify-medium-severity-fixes.py E1-E9 |
| **VULN-S7** | MEDIUM | **FIXED (kanban t_6f3826b3, this batch)** | verify-medium-severity-fixes.py G1-G9 |
| **CQ-1**   | LOW    | **FIXED (kanban t_6f3826b3, this batch)** | verify-medium-severity-fixes.py I0-I10 |
| **CQ-4**   | MEDIUM | **FIXED (kanban t_6f3826b3, this batch)** | verify-medium-severity-fixes.py H1-H4 |
| **VULN-S6** | LOW    | **FIXED (kanban t_b13072b3, this batch)** | verify-low-priority-fixes.py J1-J7 |
| **VULN-S8** | LOW    | **FIXED (kanban t_b13072b3, this batch)** | verify-low-priority-fixes.py K1-K3 |
| **VULN-S10**| LOW    | **FIXED (kanban t_b13072b3, this batch)** | verify-low-priority-fixes.py L1-L6 |
| **VULN-S11**| LOW    | **FIXED (kanban t_b13072b3, this batch)** | verify-low-priority-fixes.py M1-M4 |
| **CQ-2**   | LOW    | **FIXED (kanban t_b13072b3, this batch)** | verify-low-priority-fixes.py N1-N7 |
| **CQ-3**   | LOW    | **FIXED (kanban t_b13072b3, this batch)** | verify-low-priority-fixes.py O1-O4 |
| VULN-S9 | MEDIUM | OPEN (post-hackathon: SG egress tightening) | — |
| CQ-5   | LOW | OPEN (post-hackathon: code cleanup) | — |

## Summary

The broker has a solid architectural foundation: per-job ephemeral LLM tokens, broker-side key isolation, EFS-based job queue, and cryptographic result envelopes. The crypto implementation (X25519 + ChaCha20Poly1305 + Ed25519) is correctly implemented with ephemeral-static ECDH for forward secrecy.

However, there are **11 security findings** (3 HIGH, 5 MEDIUM, 3 LOW) and **6 code quality issues** that should be addressed before the hackathon deadline.

---

## Security Findings

### VULN-S1: SSRF via webhook_url (HIGH)

**File**: `daemon.py:380, 926-935`  
**Attack**: An attacker submits a job with `webhook_url` set to `http://169.254.169.254/latest/meta-data/iam/security-credentials/ControlPlaneRole` or any internal AWS metadata endpoint. The daemon fetches the result and POSTs it to that URL, and while the daemon runs on the control plane (not the worker), it could be used to:
- Probe internal network topology (port scanning via webhook timing)
- Exfiltrate job results to attacker-controlled servers
- Hit the worker's private IP endpoint on port 8789

**Impact**: Internal network reconnaissance, potential IMDSv1 data exfiltration (daemon doesn't use IMDSv2 tokens for its own metadata calls), job result leakage to attacker.  
**Fix**: Validate webhook_url against a blocklist (deny 169.254.*, 10.*, 172.16-31.*, 192.168.*, ::1, fc00::/7) or require an authenticated callback token. At minimum, reject link-local and private IP ranges.

### VULN-S2: No authentication on POST /v1/skills registration (HIGH)

**File**: `daemon.py:763-832`  
**Attack**: Anyone with the broker URL can register arbitrary skills. A malicious skill registration could:
- Register a skill with a huge `prompt_template` (up to 32 KiB) to consume broker memory
- Register a `wasm_ref` pointing to an attacker-controlled URI — when WASM execution is wired up, the worker would fetch and execute arbitrary code from an attacker's server
- Shadow existing skill names by registering a new version with the same name

**Impact**: Arbitrary code execution on worker (when WASM is wired), resource exhaustion, skill name squatting.  
**Fix**: Require Ed25519 publisher signature on skill registration (the code comments even mention this: "production would require an Ed25519 publisher signature over the canonical manifest"). At minimum, add an API key or bearer token auth on POST /v1/skills.

### VULN-S3: Plaintext output retained alongside encrypted result (HIGH)

**File**: `poller.py:282-283`  
**Attack**: The poller encrypts the output to `result_pubkey` but ALSO keeps the plaintext `output` field in the result envelope. Anyone who can read the outbox on EFS (or query GET /v1/jobs/{id}) gets the plaintext without needing the private key. This defeats the purpose of result encryption entirely.

**Impact**: Result encryption is decorative — the plaintext is right there. Any party with EFS read access or API access to GET /v1/jobs/{id} bypasses encryption.  
**Fix**: When `result_encrypted` is present, set `output` to `"[encrypted]"` or remove it entirely. Add a config flag `BROKER_KEEP_PLAINTEXT_FOR_DEMO` that defaults to false.

### VULN-S4: Worker signing key not attestation-derived (MEDIUM)

**File**: `poller.py:21-34`  
**Attack**: The worker generates its own Ed25519 signing key on first boot and persists it to `/opt/worker/keys/worker_signing.priv`. This key is NOT derived from the SEV-SNP attestation report. An attacker who compromises the worker (but not the TEE) could:
- Replace the signing key with their own
- Sign fraudulent results that look like they came from the enclave
- The `broker_signature` field is actually the worker's signature, not the broker's — this naming is misleading

**Impact**: Signature proves the result came from the worker instance, but NOT that it ran inside the TEE. The signing key could be replaced.  
**Fix**: Derive the signing key from the SEV-SNP attestation report's `report_data` or `measurement` field. In the interim, document this as a known limitation and rename `broker_signature` to `worker_signature` for accuracy.

### VULN-S5: LLM proxy forwards full request body to upstream (MEDIUM)

**File**: `daemon.py:1019`  
**Attack**: `forward_body = {**body, "model": real_model}` copies the entire request body from the worker. If the worker sends additional fields (e.g., `temperature`, `top_p`, `stop`, `system`), these are forwarded verbatim to the upstream LLM API. A compromised worker could:
- Inject system prompts to extract information
- Set `stream: true` to attempt long-lived connections
- Set `max_tokens` to very large values to burn through the account's token cap

**Impact**: A compromised worker can abuse the LLM proxy beyond the intended single-shot chat completion.  
**Fix**: Construct a minimal forward body with only `model`, `messages`, `max_tokens`, and `stream: false`. Strip all other fields.

### VULN-S6: No rate limiting on job submission (MEDIUM)

**File**: `daemon.py:368-459`  
**Attack**: The job submission endpoint has no rate limiting. An attacker can flood POST /v1/jobs with thousands of requests, each of which:
- Writes to the SQLite DB
- Writes a JSON envelope to EFS
- Triggers a worker launch (if no worker is running)
- Issues an LLM token

**Impact**: DB bloating, EFS storage exhaustion, unnecessary worker launches (cost), LLM token table flooding.  
**Fix**: Add per-IP or per-account rate limiting (e.g., 10 jobs/minute). Use a simple in-memory token bucket or SQLite-based limiter.

### VULN-S7: Account key derivation is trivially spoofable (MEDIUM)

**File**: `daemon.py:388, 992`  
**Attack**: The account key is derived as `stripe_pi_id.split("_")[:2]` — e.g., `pi_test_123` → account `pi_test`. An attacker can submit jobs with `stripe_pi_id = "pivictim_1"` to use another "account's" token budget, or use `pi_attacker_1` through `pi_attacker_999` to create unlimited fresh accounts each with 50k tokens/day.

**Impact**: Demo cap bypass (create unlimited accounts), resource exhaustion.  
**Fix**: For the demo, document that account = Stripe customer ID in production. For now, rate-limit per-IP rather than per-account. Or hash the stripe_pi_id with a server secret to prevent account creation.

### VULN-S8: CORS allows all origins (MEDIUM)

**File**: `Caddyfile:26`  
**Attack**: `Access-Control-Allow-Origin: *` means any website can make cross-origin requests to the broker API. A malicious website could submit jobs using a visitor's browser, though they'd need to know the API shape.

**Impact**: Cross-origin API abuse from any web page.  
**Fix**: Restrict to `https://verdant.codepilots.co.uk` or a configurable allowlist. For a hackathon demo, `*` is acceptable but should be documented as a known issue.

### VULN-S9: Worker egress is unrestricted in CFN (MEDIUM)

**File**: `cloudformation-control-plane.yaml:158-161`  
**Attack**: The worker security group has `egress: all to 0.0.0.0/0`. The OpenShell policy file defines egress restrictions, but there's no evidence it's actually enforced at the network level. A compromised worker can exfiltrate data to any destination.

**Impact**: Data exfiltration from worker despite OpenShell policy claims.  
**Fix**: Add egress rules to the worker SG that only allow: TCP 443 to 0.0.0.0/0 (for LLM proxy + Stripe), TCP 2049 to VPC (for EFS), and deny everything else. Or run the OpenShell policy as an actual network filter, not just a hash attestation.

### VULN-S10: SQLite DB has no concurrent write protection (LOW)

**File**: `daemon.py:80-89`  
**Attack**: The daemon uses SQLite with WAL mode but opens a new connection per operation with `isolation_level=None` (autocommit). Under concurrent job submissions, two goroutines could both pass the idempotency check and insert the same `client_req_id`. The `UNIQUE (client_req_id)` constraint will catch this, but the error is unhandled — it'll throw a 500 instead of returning an idempotent replay.

**Impact**: Race condition on duplicate submissions — 500 error instead of idempotent replay.  
**Fix**: Wrap the idempotency check + insert in a transaction with `BEGIN IMMEDIATE` or catch `sqlite3.IntegrityError`.

### VULN-S11: LLM token expiry uses string comparison (LOW)

**File**: `daemon.py:978`  
**Attack**: `token_row["expires_at"] < datetime.now(timezone.utc).isoformat()` compares ISO timestamps as strings. This works correctly for UTC ISO 8601 strings with the same format, but if a token is created with a different timezone offset or format, the comparison could fail silently.

**Impact**: Potential token expiry bypass if timestamp formats diverge. Unlikely with current code since both use `datetime.now(timezone.utc).isoformat()`.  
**Fix**: Parse both timestamps with `datetime.fromisoformat()` and compare as datetime objects.

---

## Code Quality Issues

### CQ-1: Duplicate variable declaration in discover()

**File**: `daemon.py:505 and 532`  
The variable `attestation_path` is declared and used at line 505, then re-declared at line 532 with the same value. The first block (lines 505-514) reads the attestation and sets `live_measurement` and `worker_attested`, but the second block (lines 532-538) re-reads the same file into `full_attestation`. These should be merged.

### CQ-2: Dead code — worker_encryption_priv never used

**File**: `poller.py:37-50, 144`  
`_ensure_worker_encryption_key()` is called at line 144 but the returned `worker_encryption_priv` is never used. The actual encryption uses a fresh ephemeral key (lines 267-280). The static worker encryption key is generated and persisted but serves no purpose.

### CQ-3: `request_body` stored in DB as raw JSON (privacy)

**File**: `daemon.py:424`  
The full request body (including `encrypted_data`, `encrypted_skill`, `result_pubkey`, `requester_sig`) is stored in the `jobs.request_body` column as JSON text. This persists sensitive request metadata forever in the SQLite DB. In production, this should be encrypted or expired.

### CQ-4: Hardcoded fallback IP in poller.py

**File**: `poller.py:164`  
`os.environ.get("BROKER_CONTROL_PLANE_URL", "http://172.31.25.149:8080/v1/llm/chat/completions")` — the fallback IP is hardcoded to the current control plane's private IP. If the region changes (e.g., to eu-west-1), this fallback will silently break.

### CQ-5: DEMO_TOKEN_CAP redacted in bootstrap but not in CFN

**File**: `bootstrap-control-plane.sh:123` vs `cloudformation-control-plane.yaml:331`  
The bootstrap script has `DEMO_TOKEN_CAP=${DEMO_TOKEN_CAP:-***000}` (redacted in this doc) but the CFN template has `DEMO_TOKEN_CAP=***` with no default — they use different values. This was a known config drift bug.

### CQ-6: ~~Missing GET /v1/skills route in build_app()~~ (RESOLVED)

**File**: `daemon.py:1140-1142`  
~~The `register_skill`, `get_skill`, and `list_skills` handlers are defined but `build_app()` doesn't register the routes.~~  
**Correction**: Verified on 2026-06-27 — routes ARE wired at lines 1140-1142:
```python
app.router.add_post("/v1/skills", register_skill)
app.router.add_get("/v1/skills", list_skills)
app.router.add_get("/v1/skills/{ref}", get_skill)
```
This issue is resolved. No action needed.

---

## Priority Assessment

### Hackathon-critical (do before deadline EOD Jun 30):
1. **VULN-S3** (plaintext output) — one-line fix, kills the entire crypto story otherwise
2. ~~**CQ-6** (missing routes)~~ — RESOLVED, routes are already wired at daemon.py:1140-1142
3. **VULN-S2** (no auth on skills) — add at minimum a bearer token
4. **VULN-S1** (SSRF webhook) — blocklist private IPs

### Should-fix (important but not demo-blocking):
5. **VULN-S5** (LLM proxy body forwarding) — strip extra fields
6. **VULN-S9** (worker egress) — tighten SG egress rules
7. **VULN-S7** (account spoofing) — document limitation
8. **VULN-S4** (signing key not attestation-derived) — document limitation
9. **CQ-4** (hardcoded IP) — parameterize for region migration

### Nice-to-have (post-hackathon):
10. **VULN-S6** (rate limiting)
11. **VULN-S8** (CORS)
12. **VULN-S10** (SQLite race)
13. **VULN-S11** (timestamp comparison)
14. **CQ-1, CQ-2, CQ-3, CQ-5** (code cleanup)