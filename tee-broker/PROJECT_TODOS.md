# tee-broker-deploy — Project TODOs

Open follow-ups that need human attention before they're actionable.
Tasks that are dispatched to workers live on the kanban board; this
file is for things workers can't drive themselves.

---

## Production go-live review (BLOCKING for prod, NON-BLOCKING for hackathon)

### Threat model — topup flow (3 HIGH severity)

Doc: `docs/security/threat-model-topup-flow.md`

User signed off the hackathon demo state 2026-06-28 ("3. sign off for
now"). Before any production deployment with a real `STRIPE_SECRET_KEY`,
the following must be re-reviewed:

- **B1 — Idempotent topup PaymentIntent capture** (open kanban: `t_b2ceaf21`) — **DONE 2026-06-28** (code + tests; 50/50 in verify-stripe-integration.py)
- **B2 — (covered in threat model doc; open as card before prod)**
- **B3 — Idempotent topup PI capture** — **DONE 2026-06-28** by t_b2ceaf21 (same task as B1; was a duplicate in the doc). Code: `broker-daemon/daemon.py` `topup_job()` lines 3476-3710. Tests: `tests/verify-stripe-integration.py` `test_b3_same_pi_concurrent_captures` + `test_b3_different_pi_concurrent_captures`. All three layers implemented: Stripe idempotency_key, BEGIN IMMEDIATE atomic state flip, cached-retry short-circuit.
- **B4 — Per-job webhook cap + async delivery** (open kanban: `t_30ca541f`) — **DONE 2026-06-28** (code + tests; 23/23 in verify-webhook-delivery.py; zero regression on verify-webhook-payload, verify-stripe-integration, verify-chargeback-ack, verify-insufficient-funds). Four mitigations: asyncio.Queue + dispatcher worker pool with bounded parallelism, per-job webhook_attempts cap, per-host rate limit, event_id UUID idempotency. Follow-up for prod: swap in-memory queue for SQLite-backed outbox so deliveries survive broker restart.
- **B5 — CFN NoEcho for STRIPE_SECRET_KEY** — DONE 2026-06-28 (commit 9cc217f, t_6b27237f)
- **B6 — Topup endpoint MUST verify topup PI customer matches original PI** (open kanban: `t_cc571e8f`)
- **B7 — (covered in threat model doc; open as card before prod)**

Action before production:
1. Reconcile B1 vs B3 in the threat model doc (likely duplicates)
2. Open kanban cards for B2 / B7 if not already represented
3. Run all B* tasks to GREEN
4. Have a security reviewer (not the original author) re-read the threat model with all mitigations in place

---

## Hackathon follow-ups still open

### Payment block audit (t_9fb71ad7 — approved 2026-06-28)
- [ ] `t_84d3e5ee` — webhook includes `payment` block (real bug, coder-deep)
- [ ] `t_8219dbb2` — document webhook payload shape in payment-flow.md (writer)
- [ ] `t_08618a12` — Stripe chargeback clause in project terms (writer)

### Live Stripe dashboard verification (left for interactive follow-up)
The audit couldn't verify the live Stripe dashboard state from inside
the worker sandbox (no browser, no live API access for the verifier).
To close this item interactively: visit
https://dashboard.stripe.com/test/payments and confirm the webhook
events for completed broker jobs land there with the expected shape.

---

## Carried-over / non-hackathon

- t_0cf1cc58 — Launch VerdantForged EC2 with NemoClaw (archived 2026-06-28; AWS free-tier can't resize to m6a.xlarge; needs AWS plan upgrade or fresh launch)
- t_0a2a7a95 — Decompose "zero claw" (archived 2026-06-28; stale protocol violation)
- t_c831bc1e — Document learning progress (left for now; missing source material from parent t_ca8e7511)
- t_762bc24e, t_8ea7a325 — Hermes Mobile Web App (archived 2026-06-28; web-dev profile doesn't exist, scope is post-hackathon)
- t_100e890f — API technical documentation (will pick up interactively; will split into smaller cards when picked up)