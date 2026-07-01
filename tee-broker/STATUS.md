> **2026-07-01 LLM auth update**: the persistent sandbox `LLM HTTP 401: invalid or expired LLM token` was fixed live by adding a JSON body sideband fallback (`verdant_llm_token`, `verdant_job_id`) for the worker → `inference.local` → broker proxy path. Live E2E `job_b93fe6a410fb72d6fb1fbbea` completed in `nemoclaw-sandbox` with `attested=True`, model `minimax-m3:cloud`, and artifact `output.txt` (2248 bytes). See `SESSION_LOG.md` for hashes and deployment notes.

# VerdantForged TEE Broker — Status Writeup (2026-06-28)

> **2026-06-30 gold worker AMI**: baked from live worker `i-0cd8b60358d6d5509` after NemoClaw/OpenShell completed the expensive sandbox build and after queue/key scrub. AMI: `ami-099e2272620073023` (`verdantforged-nemoclaw-gold-worker-20260630T163844Z-i-0cd8b60358d6d5509`), state observed `available` at `2026-06-30T16:51:28Z`. Source worker had Docker layers `openshell/sandbox-from:1782835611` and `ghcr.io/nvidia/nemoclaw/hermes-sandbox-base:v0.0.55` (both 4.36GB), with live queue cleaned to 0 before imaging.

> **2026-06-30 demo update**: added a broker-side stub payment route for
> regional-lockout cases. Set `BROKER_PAYMENT_STUB_MODE=1`, mint a synthetic
> `spt_demo_...` token via `/v1/demo/shared-payment-token`, or use
> `scripts/run_file_job_e2e.py --demo-spt`.

> **2026-06-29 docs update**: README now explains the full request path
> (ACS 402 challenge → SPT mint/retry → server-side PaymentIntent → on-demand
> worker launch → NemoClaw sandbox → broker LLM proxy). `docs/file-jobs.md`
> now calls out the payment preflight before file uploads, and the old
> client-side `stripe_pi_id` example was removed.

Author: Hermes Agent session 2026-06-28
Scope: Where we are, what's done, what's blocked, what's coming up next.

> **2026-06-29 update**: File-upload E2E is now verified on the live control plane.
> The public `/healthz` endpoint exposes worker boot progress and ETA, the
> attestation gate now accepts the deployed empty-policy runtime, and the
> presigned upload path works with direct `requests.put()` after switching the
> artifact bucket to AES256 default encryption. The successful E2E job was
> `job_95a0e3d472ba583750439c96` using the `code-review` skill with two input
> files, and it completed in the NemoClaw sandbox with a downloadable artifact.
>
> See [`SESSION_LOG.md`](SESSION_LOG.md) for the full writeup and the bug trail.
> [`CHANGELOG.md`](CHANGELOG.md) remains the release-oriented history.

> **2026-06-29 ACS update**: Live payment intake has been pivoted to Stripe ACS/SPT.
> The broker now returns HTTP 402 with the configured `networkId` when no SPT is
> supplied, then creates/confirms the PaymentIntent server-side from
> `payment_method_data[shared_payment_granted_token]`. Worker user-data is
> currently **15150 bytes**, below EC2's 16384-byte launch limit.

## TL;DR

The Stripe PaymentIntent lifecycle (kanban t_9fbec867) is now code-complete, fully tested (32/32 PASS), and the demo-mode helpers now return the same response shape as live mode so callers don't have to branch on demo vs live. The task is BLOCKED only on a human review that this writeup is intended to satisfy.

Once unblocked, the chain promotes:
- t_0ef31767 (S3 input attachments, P13) — promotes from todo to ready
- t_73c10bc4 (consolidated demo.sh, P8) — same
- t_4740dce6 (NemoClaw sandbox execution, P14) — promotes; gates on t_0ef31767, t_06245189, t_96b86cff (all done)
- All 4 showcase skills (attestation-verifier, token-receipt, blind-audit, skill-discoverer) — chain unblocks through t_0ef31767

## What's shipped (commit history)

| Commit | What |
|--------|------|
| 6d080f6 | Mark VULN-S6/S8/S10/S11 + CQ-2/CQ-3 as FIXED in SECURITY_REVIEW.md |
| bc646a1 | Fix 6 low-priority code-quality + security issues |
| 0ab89e1 | Stripe lifecycle integration tests (32/32 PASS) |
| 5147725 | Fix 5 medium-severity issues (VULN-S4/S5/S7, CQ-1, CQ-4) |
| 2d6a571 | Salvage untracked test suites from concurrent workers |
| 932ce30 | Salvage concurrent worker WIP — Stripe deps + VULN-S4 sig rename |
| 4ecd232 | Encrypted S3 result-pack storage (24h lifecycle) |
| aead5c9 | Document eu-west-1 region migration |
| 9c1ee55 | Fix 4 hackathon-critical security issues (VULN-S1/S2/S3 + CQ-6) |
| cfb0d04 | Easy wins — ephemeral-static ECDH, OpenShell policy, SEV-SNP framework |

Live broker: eu-west-1, real SEV-SNP measurement 56fb747a1f...

## What's in this commit (pending)

This commit lands:

1. **Cost ledger (gitignored)** — `COST_LEDGER.jsonl` is now auto-populated by every demo-mode finalize via the new `_log_demo_lifecycle()` helper in `broker-daemon/daemon.py`. Append-only JSONL, best-effort writes, never blocks finalize. Path is overridable via `BROKER_COST_LEDGER` env var.

2. **Full lifecycle in demo mode** — `capture_payment()` and `refund_payment()` now return the SAME shape as live mode (`captured: True`, `status: succeeded`, `id: <pi_id>`, etc.) when `STRIPE_SECRET_KEY` is unset. The `demo: True` flag is the only signal that the response is synthetic. Rationale: callers (front-end, webhooks, downstream automation) shouldn't have to branch on demo vs live.

3. **Demo-mode prefix on `stripe_status` column** — rows tagged with `demo_` prefix so dashboards can filter real vs demo with one SQL query (`WHERE stripe_status LIKE 'demo_%'`). The client-facing `payment.status` field STrips the prefix so clients see `succeeded` either way. The `payment.demo` flag carries the demo/live signal in the JSON.

4. **New gitignored ledger file + review-path docs directory** — `.gitignore` entries for `COST_LEDGER.jsonl` and `docs/cost-review/`.

5. **Skill: tee-broker-deploy-config** — `~/.hermes/skills/tee-broker-deploy-config/SKILL.md`. Documents the CFN → bootstrap → deploy.sh → daemon.py parameter flow with a checklist for adding new (especially secret) config knobs. Cross-references the B5 NoEcho regression.

6. **Updated tests** — `tests/verify-stripe-integration.py` adjusted (D2/D3/E4/E4a/E5/E6) to match the new full-lifecycle demo-mode shape. All 32 assertions PASS.

7. **Sentinel-state for unfinalized demo jobs** — the `payment.status: "demo_pending"` placeholder for un-finalized demo jobs is now `payment.status: "pending"` (cleaner) plus `payment.demo: True` flag.

## New kanban tasks created (waiting in the queue)

| ID | Title | Assignee | Priority | Status |
|----|-------|----------|----------|--------|
| t_9a705578 | Handle insufficient-funds (reauthorise on completion before releasing result) | coder-deep | 9 | todo |
| t_84384c7a | Document payment flow + terms on project website | default | 5 | todo |
| t_1015a43e | Review insufficient-funds topup flow for exploitation vectors | default | 4 | todo |
| t_29b31ecb | Review cost-calculation accuracy | default | 8 | ready (no parents) |
| t_9fb71ad7 | Verify payment block shape in webhook + front-end renderer | default | 7 | ready |
| t_100e890f | Write API technical docs + add to project website | default | 6 | ready |
| t_86ce871c | Run B5 CFN NoEcho test against live deployment + document + regression | default | 5 | ready |

The first (t_9a705578) blocks on t_9fbec867 — same chain as the existing unblocked work.

## Demo mode contract (what judges will see)

```
POST /v1/jobs      → 202 {job_id, state, llm_token, ...}
GET  /v1/jobs/{id} → 200 {
  state: "queued" | "running" | "completed" | "failed" | "timeout",
  result: {...},
  payment: {
    status: "pending" | "succeeded",
    amount_cents: 20..N,
    stripe_id: "pi_..." | "re_demo_pi_...",
    held_amount_cents: 0..N,
    mode: "demo" | "live",
    demo: true | false
  }
}
```

In demo mode (`STRIPE_SECRET_KEY` unset), `payment.demo == true` is the discriminator. The `stripe_id` field echoes the original `pi_id` for captures and prefixes it with `re_demo_` for refunds.

## What this commit does NOT do (deferred)

- Stripe Connect transfers to skill providers (Phase 3 of t_9fbec867) — not in scope of this review.
- Insufficient-funds topup flow — new card t_9a705578.
- Live CFN B5 regression test — new card t_86ce871c (requires AWS creds at deploy time).
- API documentation on the website — new card t_100e890f.

## Verification

```
$ cd ~/hermes/competition/tee-broker-deploy && python3 tests/verify-stripe-integration.py
=== Summary ===
Passed: 32
Failed: 0

$ python3 tests/verify-security-fixes.py
=== Summary: PASS=29 FAIL=0 ===
```

No regressions. The only test that previously asserted on `payment.status == "demo_capture"` was updated to assert on the new full-lifecycle shape; all assertions are more rigorous now (they check `demo: True`, `stripe_id` prefix, etc., not just status string).

## Open review questions for Autumn

1. **Cost formula** — is $0.20/15min + $0.001/1K tokens the right number for the demo? t_29b31ecb will revisit this with real ledger data.
2. **Pricing transparency** — does the demo need a "demo mode active" banner on the front-end? Currently `payment.demo: True` exposes it cleanly; the front-end can render this as a hint.
3. **Topup flow approval** — t_9a705578 describes the proposed reauthorisation-on-shortfall pattern. Before it lands, t_1015a43e will sanity-check for exploitation vectors (the 7-attack-vector threat model is enumerated in that card).

## Files changed in this commit

```
.gitignore                                 | +6 -1
broker-daemon/daemon.py                    | +60 -15  (full-lifecycle demo mode + _log_demo_lifecycle + prefix logic)
tests/verify-stripe-integration.py         | +25 -12  (updated D2/D3/E4/E4a/E5/E6 expectations)
~/.hermes/skills/tee-broker-deploy-config/ | NEW      (skill documenting config flow)
COST_LEDGER.jsonl                          | NEW, gitignored
docs/cost-review/                          | NEW, gitignored
STATUS.md                                  | NEW      (this file)
```

## Acceptance criteria for t_9fbec867 (from the original task body)

- [x] stripe library added to requirements.txt (was already done in 932ce30)
- [x] PaymentIntent verified at submit time (rejected if invalid)
- [x] Payment captured on job completion (actual amount, not full hold)
- [x] Refund issued on job failure
- [x] Cost calculation: $0.20/15min + $0.001/1K tokens
- [x] Payment status included in job result (GET /v1/jobs/{id})
- [x] Demo mode works without STRIPE_SECRET_KEY (now: returns same shape as live)
- [x] Tests in tests/verify-stripe-integration.py (32/32 PASS)

Task is ready for human sign-off. Recommend marking done and unblocking the chain.

---

## Skill Library — Standalone service (added 2026-06-29)

A standalone FastAPI service on port `:8091` catalogs broker-compatible skills
independently of `daemon.py`. It lives alongside the broker — additive, no
broker restart required.

- Source: `skill_library/` (Python FastAPI app, ~600 lines, 28 pytest tests)
- API docs: `docs/skill-library-api.md` and live Swagger UI at `/docs`
- Deploy script: `scripts/push_skills.sh` walks a skill directory and POSTs
  each card + file to the library (with optional `--build` for Rust crates
  and `--sync` to forward to the live broker).
- Service unit: `systemd/skill-library.service`
- Runbook: `docs/skill-library-deploy.md`
- Companion Hermes skill: `~/.hermes/skills/devops/skill-library-browse/`
  (3 scripts: `skill_library_list.sh`, `skill_library_install.sh <name@version>`,
  `skill_library_download.sh <name@version> <file> [dest]`)

`install` posts to the library's `/v1/library/skills/{ref}/sync-to-broker`
endpoint using `$SKILL_LIBRARY_API_KEY`. See `docs/skill-library-api.md` for
the full API surface.

### Capability summary

| Surface | Method | Auth | Purpose |
|---|---|---|---|
| `/healthz` | GET | none | liveness + DB + EFS probe |
| `/docs` | GET | none | interactive Swagger UI |
| `/v1/library/skills` | GET | none | list all (name, version) pairs |
| `/v1/library/skills/{name}@{version}` | GET | none | full card + file list |
| `/v1/library/skills/{name}@{version}/files/{filename}` | GET | none | download raw bytes |
| `/v1/library/skills` | POST | Bearer | register a new card |
| `/v1/library/skills/{ref}/files/{filename}` | POST | Bearer | upload one file (X-File-Sha256 verified) |
| `/v1/library/skills/{ref}/sync-to-broker` | POST | Bearer | forward to broker (`POST /v1/skills` + WASM upload) |
| `/v1/library/skills/{ref}` | DELETE | Bearer | remove card + all blobs |