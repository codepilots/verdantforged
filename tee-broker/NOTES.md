# VerdantForged — Working Notes

Scratchpad of findings, drift, and known gaps accumulated during the
2026-06-29 working session. **Not a polished spec** — capture-then-iterate.
Cross-linked from `README.md`, `STATUS.md`, and `SESSION_LOG.md`.

Last updated: 2026-06-29

---

## 1. What's actually deployed (the source of truth)

### Broker — `verdant.codepilots.co.uk`
- **Instance**: i-05117b9649db5b343 (t3.small, eu-west-1)
- **Daemon**: PID 16258, `/opt/broker-daemon/daemon.py` (started 3h+ ago, not restarted during skill-library work)
- **Public broker URL**: `https://verdant.codepilots.co.uk`
- **Caddy**: PID 3771, uptime 23h+, reload-only during skill-library deploy (no restart)

### Skill Library (separate service) — `https://verdant.codepilots.co.uk/library/*`
- **Service**: `uvicorn skill_library.app:app` PID 17943, systemd-managed
- **Routes**: `/library/healthz`, `/library/v1/library/skills`, `/library/v1/library/skills/{ref}`, `/library/v1/library/skills/{ref}/files/{file}`
- **Swagger**: `/library-docs/docs`, `/library-docs/openapi.json`
- **Skills registered**: `code-review@0.1.0`, `photo-glow-up@0.1.0`, `summarize@0.1.0` (3 prompt-template stubs)
- **Auth**: GET = public; POST/DELETE = `Authorization: Bearer *** [see .skill-library/api_key.txt for the live key, gitignored]

### Site — `verdantforged.pages.dev`
- **Stack**: Astro static site, deployed via Cloudflare Pages
- **Broker client**: `src/lib/broker-mock.ts` — pure in-browser mock, **does not call the real broker**
- **Stripe backend**: `https://stripe.codepilots.co.uk` — separate Cloudflare Worker, real Stripe Connect demo
- **Status as of audit**: uses 7 synthetic skills, $8-41 each, session-lease pricing model

### Stripe backend — `stripe.codepilots.co.uk`
- **Stack**: Python `http.server` on Cloudflare Worker, port 8790 default
- **Endpoints**: `POST /create-payment-intent`, `POST /create-transfer`, `POST /refund`, `GET /health`
- **Code**: `tee-broker-site/stripe-backend/server.py`
- **Real Stripe**: test mode, `STRIPE_SECRET_KEY=*** `

---

## 2. Drift between design docs and deployed reality

### 2.1 The "manifest to request a job" gap

**Designed** (in `tee-broker-docs-archive-2026-06-29/discovery/DISCOVERY.md` §9.2):
A signed Nostr-style manifest with `manifest_hash`, `skill_hash`, `input_schema`,
`output_schema`, `price_usd`, `provider_pubkey`, `provider_signature`,
`attestation_policy_hash`, etc. Hosts at `https://provider.example.com/manifests/<skill>-v3.2.json`.

**Implemented**: Broker accepts flat JSON at `POST /v1/jobs`:
```json
{
  "client_req_id": "string",
  "stripe_pi_id": "pi_...",
  "encrypted_skill": "skill-name",
  "encrypted_data": "<base64 ciphertext>",
  "requester_sig": "<ed25519 sig>",
  "result_pubkey": "<x25519 pubkey b64>",
  "input_files": [...] // optional, file-jobs only
}
```
**No `manifest` field, no `skill_hash` reference, no Nostr.**

This is the largest spec-vs-impl drift in the project. Two options to close:
- (a) Drop the manifest narrative from the demo (already partly done — DEMO.md lives in archive)
- (b) Implement manifest submission on `POST /v1/jobs` (significant work)

### 2.2 The session-lease vs per-job pricing gap

**Designed**: Session-lease model — user opens a session for $X/15min,
plans multi-step workflows, pays per-step with 5% app fee to broker +
95% to skill provider via Stripe Connect.

**Implemented**: Per-job PaymentIntent — user submits a job, broker
validates the PI, captures it on completion, refunds any unused amount.
**No multi-step, no per-step transfers, no Stripe Connect transfers at all.**

The broker's only Stripe calls are:
- `stripe.PaymentIntent.retrieve(pi_id)` — validate at submit
- `stripe.PaymentIntent.capture(pi_id, ...)` — settle on completion
- `stripe.Refund.create(payment_intent=pi_id)` — refund

### 2.3 The site-vs-broker integration gap

See `tee-broker-site/SITE_VS_BROKER_AUDIT.md` for the full table. Summary:
the site's `BrokerMock` and the broker daemon share zero API surface. The
site talks to `stripe.codepilots.co.uk` (Stripe Connect); the broker talks
to Stripe directly for PaymentIntents. They are deliberately independent.

If you want them integrated for the demo: replace the site's
`BrokerMock` with a thin `POST /v1/jobs` client (~200 LOC, ~3 components).

### 2.4 The Nostr / Carbon / MPP / WebLLM gap

The archived docs describe an agent marketplace with:
- Nostr kind 31989 skill announcements + relays
- Carbon gCO2eq ratings per skill
- MPP (Magic Payment Protocol) transactions
- WebLLM browser-side LLM chat
- 7 portable Hermes skills (was 13 in v1)

**None of this shipped.** The deployed reality is:
- No Nostr anywhere (broker has `/v1/discover` instead)
- No carbon ratings
- No MPP (only Stripe PaymentIntents)
- No WebLLM (browser chat demo uses mock LLM)
- 3 prompt-template skills (code-review, photo-glow-up, summarize) — not the
  7 the catalog describes

---

## 3. Known bugs (cross-reference to `BUGS.md`)

### BUG-001 — Stripe capture fails on jobs < $0.50
- **Status**: OPEN
- **Impact**: 14/32 jobs in the live DB ended with `stripe_status: error`
- **Error strings**: `"This PaymentIntent could not be captured because it has already been captured."` and `"The remaining amount on this PaymentIntent could not be captured because the remainder of the authorized amount has been released."`
- **Cause**: broker double-captures; pads PI amount to $0.50 minimum after release
- **Fix**: Option A (drop padding) or Option B (authorize ≥$0.50 upfront at PI creation)
- **Workaround**: create PIs with `amount >= 50` (cents)

### BUG-002 — `push_skills.sh` path-strip bug (FIXED)
- **Fix commit**: `5b430a0`
- **Symptom**: files uploaded under nested paths like `worker/skills/summarize/SKILL.md` instead of `SKILL.md`
- **Cause**: `${file_path#$skill_dir/}` pattern — `$skill_dir` retains trailing slash from glob expansion
- **Fix**: `${file_path#${skill_dir%/}/}` — strip trailing slash first

### BUG-003 — `push_skills.sh` `--api-key` parsing broken (FIXED)
- **Fix commit**: `1a9c786`
- **Symptom**: `--api-key $VAR` form silently fails auth; CLI shows `pushed=3 failed=0` because 401 is treated as non-fatal 409
- **Cause**: Hermes tool-call redaction munged `${2}` to `***` on the first write
- **Fix**: use `${2}` form which survives redaction
- **Lesson**: pre-commit byte-level verification (base64 dump) catches what grep cannot

---

## 4. Things that work and are documented

- **End-to-end live test job** (`job_a859628e21b27f9b326fa906`): submit → broker dispatches worker → NemoClaw onboard → sandbox execution → LLM call via `inference.local` → broker LLM proxy → Ollama Cloud → real LLM output → result returned
- **Inference route**: `https://inference.local/v1` from inside the sandbox; broker daemon `/v1/llm/v1/chat/completions`; Caddy does not need to know about it
- **Worker bootstrap**: `user-data.sh` line 154 uses `${BROKER_ONBOARD_TOKEN:-onboard-placeholder}` correctly
- **OpenShell policy**: `local-inference` policy allows sandbox → `host.openshell.internal` egress
- **Skill library**: deployable via `sudo tee-broker-deploy/scripts/deploy_skill_library.sh`; idempotent; integrates Caddy routes

---

## 5. Things to do (next session)

### P0 (blockers for demo)
- [ ] **Decide site/broker integration direction**: keep them separate (current) OR replace BrokerMock with a real `POST /v1/jobs` client
- [ ] **Fix BUG-001** if any test jobs in the demo will be < $0.50
- [ ] **Decide which 3 prompt skills are actually used in the demo** vs the 7 in the site catalog

### P1 (cleanups)
- [ ] Sync the site's `skill-catalog.ts` with the broker's actual 3 skills (or fetch from `/v1/library/v1/library/skills`)
- [ ] Add a "live broker status" indicator to the site (poll `/healthz`)
- [ ] Document the `skill_library` Caddy route in a runbook update
- [ ] Add integration test that hits the live `POST /v1/jobs` endpoint from the site

### P2 (future)
- [ ] Build `attestation-verifier.wasm` Rust crate → push via `--build --sync`
- [ ] Build `blind-audit.wasm` (unblocks skill-discoverer)
- [ ] Implement B6 (topup PI customer match) — kanban `t_cc571e8f`
- [ ] Consolidate `demo.sh` (no demo.sh exists; only per-skill demos)

---

## 6. Key file pointers

| Topic | File |
|---|---|
| Broker daemon | `tee-broker-deploy/broker-daemon/daemon.py` |
| Worker bootstrap | `tee-broker-deploy/worker/user-data.sh` |
| Worker poller | `tee-broker-deploy/worker/poller.py` |
| Stripe payment flow | `tee-broker-deploy/docs/payment-flow.md` |
| File-jobs spec | `tee-broker-deploy/docs/file-jobs.md` |
| Skill library service | `tee-broker-deploy/skill_library/` |
| Skill library live status | `tee-broker-deploy/docs/skill-library-live-2026-06-29.md` |
| Skill library deploy script | `tee-broker-deploy/scripts/deploy_skill_library.sh` |
| Skill library runbook | `tee-broker-deploy/docs/skill-library-deploy.md` |
| Skill library API ref | `tee-broker-deploy/docs/skill-library-api.md` |
| Live Caddyfile (snapshot) | `tee-broker-deploy/infrastructure/caddy/Caddyfile.skill-library` (gitignored *.live) |
| Bug log | `tee-broker-deploy/BUGS.md` |
| Session log | `tee-broker-deploy/SESSION_LOG.md` |
| Status | `tee-broker-deploy/STATUS.md` |
| Kanban audit | `tee-broker-deploy/KANBAN_AUDIT.md` |
| Skill library plan (this session's TDD plan) | `tee-broker-deploy/.hermes/plans/2026-06-29_161941-skill-library-service.md` (gitignored) |
| Archived v1 docs | `tee-broker-docs-archive-2026-06-29/` |
| Site broker audit | `tee-broker-site/SITE_VS_BROKER_AUDIT.md` |

---

## 7. Quick-recovery commands

If you need to pick up where this session left off:

```bash
# Check what's live on AWS
ssh-key-scan i-05117b9649db5b343  # or use SSM
curl -sS https://verdant.codepilots.co.uk/healthz
curl -sS https://verdant.codepilots.co.uk/library/healthz

# Check skill library is running
ps -eo pid,etime,cmd | grep "uvicorn skill_library" | grep -v grep

# Push a skill to the library (one-off)
bash tee-broker-deploy/scripts/push_skills.sh \
    --source-dir tee-broker-deploy/worker/skills \
    --library-url https://verdant.codepilots.co.uk/library \
    --api-key "$(cat tee-broker-deploy/.skill-library/api_key.txt | cut -d= -f2)"

# Re-deploy everything (idempotent)
sudo tee-broker-deploy/scripts/deploy_skill_library.sh
```

The live API key is at `tee-broker-deploy/.skill-library/api_key.txt`
(gitignored, chmod 600). Rotating it requires updating that file AND
`/opt/broker-daemon/config.env` on the control plane (via SSM) AND
restarting `skill-library.service`.