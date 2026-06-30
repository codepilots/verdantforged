# Threat Model — Insufficient-Funds Topup Flow

- **Reviewer:** kanban t_1015a43e (this run)
- **Date:** 2026-06-28
- **Spec under review:** New payment pattern from kanban t_9a705578
  ("Handle insufficient-funds: contact client on completion, reauthorise
  for full amount, then release result"). Implementation in flight —
  this review runs against the *design* and the current `daemon.py`
  surface it will hook into.
- **Implementation status:** `t_9a705578` is `running`. The current
  Stripe lifecycle in `broker-daemon/daemon.py` (commits `4ecd232`,
  `0ab89e1`, `5147725`, `6d080f6`, `bc646a1`, `f9f7f3c`) captures on
  `completed` and refunds on `failed`/`timeout` — there is **no
  `awaiting_topup` state yet**. This threat model assumes the design
  described in `t_9a705578`'s title (a NEW PI covering the shortfall is
  required before the result is released); the implementation owner
  should reconcile the actual final shape against this doc.
- **Companion docs:** `SECURITY_REVIEW.md` (top-level review),
  `docs/cost-review/2026-06-28-accuracy.md` (cost ledger accuracy, has
  overlap on the "exploitable cost floor" attack vector), `STATUS.md`
  §"Open decisions" item 3 (topup flow approval).

---

## 1. Verdict

**The topup flow as currently described is NOT SAFE for production.**
Two of seven attack vectors are HIGH severity and currently
NOT MITIGATED in the daemon; one is HIGH severity with a partial
mitigation; the remaining four are MEDIUM/LOW and have acceptable
defenses available but require explicit implementation.

| # | Attack vector | Severity | Current state |
|---|---|---|---|
| 1 | Free-work attack (underfund + abandon) | **HIGH** | Partial — no `awaiting_topup` TTL defined; no per-account cost ceiling |
| 2 | Result hostage (deliberate underfund → asymmetric negotiation) | MEDIUM | Design-dependent — result-not-released mitigates; counter-offer path unclear |
| 3 | Webhook spam / DoS (amplify shortfall events) | **HIGH** | NOT MITIGATED — no per-job webhook cap, no `awaiting_topup` rate limit |
| 4 | Chargeback abuse (topup → result → chargeback) | **HIGH** | NOT MITIGATED — 24h result retention window > Stripe chargeback window (≤120d) |
| 5 | Topup PI spoofing (someone else's PI as topup) | MEDIUM | NOT MITIGATED — `submit_job` does not bind `client_req_id` to PI customer; topup endpoint will inherit gap |
| 6 | Race condition (two simultaneous topup PIs) | MEDIUM | NOT MITIGATED — no `awaiting_topup` state machine yet |
| 7 | TTL manipulation (TOCTOU on cron refund vs late topup) | LOW | NOT MITIGATED — no cron / TTL sweeper exists for `awaiting_topup` jobs |

**Blocking items before production sign-off:**

- B1. Define the `awaiting_topup` state in `JOB_STATES` (daemon.py:903)
  and the transition path out of it (topup accepted → capture →
  `completed`; topup TTL elapsed → refund → `timeout` or new
  `abandoned` state).
- B2. Implement a hard per-job cost ceiling at submit-time equal to
  the held-PI amount **plus a small buffer** (proposed: held + 25%).
  Reject submissions where `tokens_used + projected_remaining >
  ceiling` and route the job to a cheaper skill / fail fast instead
  of silently allowing overrun.
- B3. Implement idempotent topup capture keyed on `(job_id,
  topup_pi_id)`. Same `BEGIN IMMEDIATE` pattern as the
  `client_req_id` idempotency race fix (daemon.py:1304-1313).
- B4. Add a per-job webhook cap (1 shortfall notification + 1
  timeout notification; intermediate retries are broker-side
  redelivery, not new fire-and-forget POSTs).
- B5. Reduce the 24h result retention window to ≤72h, **OR** keep 24h
  but add an explicit "results released under topup carry a 14-day
  chargeback clawback clause" term on the customer-facing page.
- B6. Topup endpoint MUST verify the topup PI's Stripe customer.id
  matches the original PI's customer.id (or, in the demo's hashed
  account form, the same `account_key_for(pi_id)`). Reject cross-customer
  PIs with HTTP 403 + `topup_customer_mismatch`.
- B7. Decide and document: when an `awaiting_topup` job's TTL elapses
  and we issue a refund on the ORIGINAL PI, do we also refund any
  prior partial topup captured? (Stripe's PaymentIntent lifecycle
  treats a second `pi_xxx` independently — there is no automatic
  linkage — so the answer needs to be explicit code.)

**Acceptance for demo:** all of B1–B7 may be tracked as
follow-up kanban cards during the hackathon **IF** the demo runs
exclusively under `payment.demo == true` (demo-mode PIs are exempt
from the topup path per the constraints in t_1015a43e's body). Any
`STRIPE_SECRET_KEY`-backed run MUST have B1–B7 in place before going
live.

---

## 2. Attack Vector 1 — Free-work attack (HIGH)

### 2.1 Threat

Attacker submits a job with a $0.01 Stripe PI. The job enters the
queue, the worker launches, the LLM proxy serves tokens up to (say)
$2.00 of real compute. `_finalize_job` discovers the held PI is
insufficient, transitions the job to `awaiting_topup`, and waits for
the customer to authorise a topup PI. Attacker never tops up. After
TTL, broker issues a refund on the original $0.01 PI (which captures
the full $0.01 to Stripe then refunds — net zero to attacker) and
abandons the job. Attacker walks away with $2.00 of compute.

### 2.2 Cost recovery analysis

**Refund ≠ cost recovery.** Stripe refunds the held amount to the
customer's card; they don't compensate the broker for compute already
spent. The broker is out the EC2 lease + LLM tokens.

Current cost ceiling: `held_amount_cents` set at submit (daemon.py:1284
re-derives it from the PI). The submit-time cost estimator
(`calculate_job_cost` at daemon.py:121-142) is **NOT called at
submit-time** — the PI is just verified for `requires_capture`
status and non-zero amount. So an attacker can submit a job whose
estimated cost (at the time of submission) is wildly higher than the
held amount, and the daemon accepts it because the only constraint
is "PI status is good + amount ≥ 0".

### 2.3 Current state

NOT FULLY MITIGATED.

- **Mitigated:** `verify_payment_intent` (daemon.py:145-203) requires
  the PI to be in `requires_capture` state with non-zero amount. So
  the attacker can't submit a PI that's already cancelled or fully
  captured.
- **Mitigated:** Per-IP rate limit (daemon.py:1022-1026) caps
  `RATE_LIMIT_PER_MINUTE` (default 10/min). This caps how many
  concurrent free-work attempts a single IP can launch but does NOT
  bound the cost per attempt — one submission per IP per minute, each
  consuming $2 of compute, is still a viable attack at scale across
  a botnet.
- **NOT MITIGATED:** No per-account cost ceiling. The daily token
  cap (`DEMO_TOKEN_CAP = 50000`) at daemon.py:1292 IS a per-account
  cap, but it operates only on tokens, not on `$`, and applies to
  `account_usage` which is keyed on
  `account_key_for(stripe_pi_id)` (daemon.py:1093). An attacker
  submitting one job per fresh PI bypasses the daily cap entirely.
- **NOT MITIGATED:** No `awaiting_topup` TTL has been specified. The
  spec calls for "wait for the client to authorise a NEW PI" — open
  question is "wait how long?". If the TTL is hours, the attack
  window is hours. If minutes, it's minutes.

### 2.4 Recommended fix

1. **Submit-time cost estimate gate.** In `validate_submit` /
   `submit_job`, after `verify_payment_intent` succeeds, call
   `calculate_job_cost(duration_ms=15*60*1000,  # full slot worst case
                        total_tokens=DEMO_TOKEN_CAP)` to compute the
   **maximum** the job could possibly cost. Require
   `pi_amount_cents >= max_cost_cents + 25% buffer`. Reject with
   HTTP 402 + `code: "held_amount_insufficient"` otherwise.
2. **Per-account cost ceiling.** Add a column to `account_usage`
   (or a new `account_cost_usage` table) that tracks `$` spend per
   account per day, with a cap (proposed: $10/day in demo, $100/day
   in production). Reset by Stripe customer.id, not by PI — this
   requires real Stripe customer lookup, which is currently a
   hashed-pi-id demo workaround (daemon.py:1287-1290 comment).
3. **`awaiting_topup` TTL.** Document and enforce a maximum
   `awaiting_topup` window (proposed: 15 minutes, matching the
   lease slot). When TTL elapses, refund original PI + any topup
   PI captured, transition to `abandoned`, persist a red-flag
   marker on the account so repeat offenders get rate-limited or
   banned.
4. **Auto-topup opt-in (future).** Allow customers to opt-in to
   automatic topup with a stored payment method, so legitimate
   jobs that overrun don't enter `awaiting_topup` at all. Out of
   scope for hackathon.

---

## 3. Attack Vector 2 — Result hostage (MEDIUM)

### 3.1 Threat

Attacker submits a job with a PI sufficient to start (say, $1.00)
but deliberately underfunds relative to actual cost. Job completes,
transitions to `awaiting_topup` with the result held. Attacker
contacts the broker: "Reduce your cost by 50% or I never top up, and
you lose $1.50 of compute (the lease + tokens used so far)". Broker
is in an asymmetric negotiation: refund the PI and abandon the job
(net loss) or hold the result indefinitely and bleed compute + DB
rows.

### 3.2 Current state

PARTIALLY MITIGATED by design but NOT TESTED.

- **Mitigated by `awaiting_topup` semantics:** Per the spec, the
  result is NOT released to the customer until the topup PI is
  captured. So the attacker does not get the result for free
  regardless of whether they top up or not — they walk away with
  $0 of result, just like Vector 1. The only difference is they
  *can* get the result if they pay.
- **NOT MITIGATED:** No documented operator playbook for handling
  this scenario. The current code has no escape valve — if a
  customer refuses to top up and never times out (e.g., the TTL is
  very long, or the TTL is broken), the broker holds the result
  indefinitely and the worker lease is consumed for the entire
  duration.
- **NOT MITIGATED:** No policy on whether the broker will ever
  return a result for a job that timed out / was abandoned. The
  current behaviour is "result is in the DB, customer can poll
  `/v1/jobs/{id}` and see it" — is that true even for `abandoned`
  jobs? Spec is unclear.

### 3.3 Recommended fix

1. **Explicit `awaiting_topup` TTL** (same as Vector 1 fix #3). A
   short TTL (15 min) makes hostage impossible — attacker has 15
   min to either pay or walk away.
2. **Result never released for `abandoned`/`timeout` jobs.** Add a
   check in `GET /v1/jobs/{id}` that returns 410 Gone + a
   "result_expired" code when state is in `(timeout, abandoned)`
   AND the current time is past the result-retention deadline.
   Force the customer to fetch results before TTL.
3. **Operator playbook.** Document that the broker WILL NOT
   negotiate on price; the cost is computed deterministically by
   `calculate_job_cost` and the customer either pays it or
   abandons. The Terms page should state this so attackers
   cannot plausibly threaten negotiation.

---

## 4. Attack Vector 3 — Webhook spam / DoS (HIGH)

### 4.1 Threat

Attacker submits a job with a webhook URL under their control. The
broker fires a webhook on every state transition (currently just
`completed`/`failed`/`timeout`). With the topup flow, the broker
will ALSO fire a webhook on `awaiting_topup` entry and on topup
TTL. An attacker who can force many shortfalls can amplify webhook
volume.

Amplification paths:
- Submit many small-PI jobs concurrently → many `awaiting_topup`
  webhooks (one per job).
- Submit a job that legitimately triggers multiple topup events
  (e.g., the worker re-runs and exceeds again — does the broker
  re-fire `awaiting_topup`?).
- Submit a job with a slow webhook target → the broker's webhook
  delivery blocks for 10s per attempt (daemon.py:2283 hardcoded
  `aiohttp.ClientTimeout(total=10)`), so 10s × N webhook targets =
  per-IP denial-of-service on the broker itself.

### 4.2 Current state

NOT MITIGATED.

- **Per-IP rate limit exists** (daemon.py:1022-1026) but applies
  to `POST /v1/jobs`, not to webhook delivery. An attacker who
  has already burned their submission quota can still force
  webhook storms if any of their jobs are still in flight.
- **No per-job webhook cap.** `_deliver_webhook` (daemon.py:2274)
  fires once per `_finalize_job` call. There is no counter that
  prevents re-fire on transient errors (note: the current code
  only fires once per job's `_finalize_job` because
  `_finalize_job` is called once per outbox file — but the
  topup flow may legitimately call `_finalize_job` twice: once
  for `running → awaiting_topup` and once for
  `awaiting_topup → completed`, which is fine but the webhook
  semantics need to be unambiguous).
- **Webhook delivery blocks the outbox poller.** `outbox_poller`
  (daemon.py:2104) is a single async loop; a single webhook
  delivery that hangs for 10s blocks ALL other job finalisations
  during that time. This is a DoS surface independent of the
  topup flow.

### 4.3 Recommended fix

1. **Async webhook delivery.** Move `_deliver_webhook` off the
   outbox-poller hot path: write the webhook intent to a queue,
   have a separate worker consume the queue with bounded
   parallelism (`asyncio.Semaphore(10)`) and a 5s timeout. The
   poller must not block on HTTP.
2. **Per-job webhook cap.** Track `webhook_attempts` per job in
   the DB. On each `_finalize_job` call, increment and bail if
   it exceeds 3 (the legitimate max: 1 for `awaiting_topup`, 1
   for `completed`, 1 for the timeout fallback).
3. **Webhook target reputation.** Hash the webhook URL hostname
   and rate-limit webhook deliveries to a single host across ALL
   jobs (not just per-job). 10 deliveries/sec/host is a
   reasonable cap. The current SSRF blocklist at
   daemon.py:906-970 protects against internal targets but
   doesn't cap external ones.
4. **Idempotent webhook payloads.** Include an
   `event_id` (UUID) in every webhook body so the receiver can
   dedupe. Currently the body is just
   `{job_id, state, result, artifact_urls}` (daemon.py:2276-2282)
   — a retry of the same state transition produces an identical
   body with no dedup key.

---

## 5. Attack Vector 4 — Chargeback abuse (HIGH)

### 5.1 Threat

Attacker submits a job with a legitimate-looking Stripe card.
Job completes; PI captures say $2. Result is released (delivered
via webhook + available via `/v1/jobs/{id}` for 24h). Attacker
files a chargeback with their bank claiming "unauthorised
charge". Stripe reverses the $2 + adds a $15 dispute fee. The
broker is out $17 and the attacker already has the result.

The topup flow specifically *amplifies* this attack: the
attacker can deliberately trigger an `awaiting_topup` state so
they have plausible deniability ("I didn't authorise that charge,
the broker double-charged me"). The topup PI was legitimately
authored by the customer, but the original PI was deliberately
underfunded to set up the dispute.

### 5.2 Current state

NOT MITIGATED.

- **Stripe chargeback window is up to 120 days.** Most banks
  honour 60-120 days. Our result retention is 24h
  (daemon.py:374-376: "A 24h lifecycle rule auto-deletes objects")
  and our webhook delivery is one-shot. After 24h the broker
  has no record of the result, but Stripe will still honour the
  chargeback — and since the broker has no signed evidence the
  result was delivered, the dispute is very likely to be ruled
  against the broker.
- **No dispute / chargeback webhook handler.** Stripe sends
  `charge.dispute.created` webhooks to a configured endpoint;
  the broker doesn't subscribe to these. So the broker may not
  even know a dispute was filed until the funds are clawed back.
- **No dispute evidence collection.** Even if the broker knew,
  it has no signed audit log showing the customer received the
  result. The worker_signature is on the result envelope, but
  the broker has no proof the customer FETCHED it.

### 5.3 Recommended fix

1. **Dispute webhook handler.** Subscribe to
   `charge.dispute.created` and `charge.dispute.closed`. On
   `created`, log the dispute + suspend the account. On
   `closed`, append to a permanent audit log.
2. **Result-retention ≥ chargeback window.** Either extend the
   24h S3 lifecycle to ≥120d OR explicitly cap customer
   transactions at $X per account per month such that
   chargeback exposure is bounded.
3. **Customer acknowledgment of receipt.** Add a `POST
   /v1/jobs/{id}/ack` endpoint that the customer MUST call to
   confirm receipt of the result. Without ack, the broker
   treats the result as undelivered and will refund (or hold
   in escrow) — this protects against "I never got it"
   chargebacks. Persist the ack timestamp + customer-supplied
   proof in the DB.
4. **Topup flow carries explicit anti-chargeback clause.** The
   customer-facing Terms page (kanban t_84384c7a) MUST state:
   "Topup charges are final; chargebacks for topup PIs that
   were captured and whose results were delivered (HTTP 200
   from your webhook OR explicit ack within 24h) are not
   eligible for refund and may result in account suspension."
5. **Fraud scoring.** Track per-account: chargebacks filed,
   abandoned jobs, topup PIs that later refunded. A simple
   rolling score (e.g., > 2 chargebacks = ban) protects
   against repeat offenders. Stripe Radar is the production
   answer for this; demo can use a simple DB-side heuristic.

### 5.4 Implementation status — B5 closed (t_69b52324, 2026-06-28)

All five recommended controls above are now live in
`broker-daemon/daemon.py` (mark NON-BLOCKING for the hackathon
demo per user sign-off on t_1015a43e):

| # | Control | Where | Test |
|---|---------|-------|------|
| 1 | Dispute webhook handler — subscribes to `charge.dispute.created` (bumps fraud score, may suspend) and `charge.dispute.closed` (append-only audit log row). Closed by default — 503 `webhook_disabled` when `STRIPE_WEBHOOK_SECRET` unset. HMAC-SHA256 sig with 5-min tolerance. | `daemon.py:handle_stripe_webhook` | `verify-chargeback-ack.py` W1-W6 |
| 2 | 24h retention kept as-is (matches `BROKER_ACK_WINDOW_HOURS`). The anti-chargeback clause + signed audit log are the operator's defence, not retention extension. | `SECURITY_REVIEW.md`, `terms.astro` | n/a |
| 3 | `POST /v1/jobs/{id}/ack` — Bearer-token auth via `BROKER_SKILLS_API_KEY`. Persists `acked_at` + `ack_proof` + `ack_ip` on the jobs row. Idempotent — replay returns original timestamp without overwrite. Only terminal-state jobs are ackable (409 `invalid_state` otherwise). Proof capped at 2048 bytes. | `daemon.py:handle_ack_job` | `verify-chargeback-ack.py` A1-A9 |
| 4 | Anti-chargeback clause in `tee-broker-site/src/pages/terms.astro` — exact wording matches this threat model. Last updated 2026-06-28. | `terms.astro` (Disputes & chargebacks section) | n/a (marketing copy) |
| 5 | Fraud scoring — `account_fraud_score` table, three signals (chargebacks_filed, abandoned_jobs, refunded_topups). `score > BROKER_FRAUD_BAN_THRESHOLD` (default 2) flips `suspended=1`; submit_job rejects with 403 `account_suspended`. | `daemon.py:_bump_fraud_score` + `daemon.py:_account_is_suspended` | `verify-chargeback-ack.py` F1-F5 |

Operator workflow when a dispute fires:

  - Stripe sends `charge.dispute.created` → broker verifies
    HMAC, inserts `dispute_events` row, bumps
    `account_fraud_score.chargebacks_filed` for the PI's
    account_key. If `score > threshold`, `suspended=1`.
  - Operator queries `SELECT * FROM dispute_events WHERE
    payment_intent='pi_xxx'` to reconstruct the dispute
    timeline (raw_payload column holds the full Stripe event
    JSON).
  - When the customer calls `POST /v1/jobs/{id}/ack` (or the
    webhook delivered with 200), the broker persists
    `acked_at`/`ack_proof`/`ack_ip` for evidence submission.
  - On `charge.dispute.closed`, broker appends a separate
    audit-log row keyed by the new `evt_xxx`. fraud_score is
    NOT bumped again — the abuse already counted when the
    dispute was created.

Production hardening still required (deferred):

  - Replace demo-mode SHA-256 account_key with real Stripe
    `customer.id` (per VULN-S7).
  - Wire `STRIPE_WEBHOOK_SECRET` into the CloudFormation
    template as a `NoEcho:true` parameter (matches the
    `STRIPE_SECRET_KEY` pattern salvaged in t_6b27237f).
  - Stripe Radar integration to complement the DB-side
    heuristic.

---

## 6. Attack Vector 5 — Topup PI spoofing (MEDIUM)

### 6.1 Threat

Attacker submits a job with PI A (their own, $0.01). Job completes,
transitions to `awaiting_topup`. Attacker submits a topup PI for
$2.00 — but the PI is PI B, which belongs to a *different
customer's* legitimate job (e.g., the attacker phished it from
the customer, or scraped it from a leaked log, or replayed it from
the customer's submission). Broker captures PI B and releases the
result to the attacker. The legitimate customer is now debited $2
for a job they did not authorise.

### 6.2 Current state

NOT MITIGATED.

- `submit_job` (daemon.py:1255-1457) does NOT bind
  `client_req_id` to a specific Stripe customer. Two different
  customers could submit jobs with overlapping or stolen PI IDs
  and the daemon would not detect it.
- `validate_submit` (daemon.py:1204+) only verifies the PI is
  in `requires_capture` state with non-zero amount — no
  customer-binding check.
- The current `account_key_for` (daemon.py:1093) hashes the PI
  itself, which means two customers using the SAME PI would
  collide — but two customers using DIFFERENT PIs are isolated.
  This does NOT protect against topup PI spoofing because the
  topup PI is a DIFFERENT PI from the original.

### 6.3 Recommended fix

1. **Customer binding at topup time.** When the topup endpoint
   receives a new PI, verify the topup PI's Stripe customer.id
   matches the original PI's Stripe customer.id. In demo mode
   (where there's no real customer.id), compare
   `account_key_for(topup_pi_id) == account_key_for(original_pi_id)`.
   Reject with HTTP 403 + `code: "topup_customer_mismatch"` if not.
2. **Topup PI must be in `requires_capture` state.** Same as
   original — already enforced by the existing
   `verify_payment_intent`.
3. **Topup PI must be for the EXACT shortfall amount (or close
   to it).** Reject if `topup_amount_cents != shortfall_cents`
   with a tolerance of ±5%. This prevents the attacker from
   attaching a $1000 topup PI to a $0.05 shortfall and capturing
   the overage for themselves via a subsequent refund abuse.
4. **Topup PI lifetime.** Require the topup PI to have been
   created within the last 15 minutes (i.e., it's a fresh
   authorisation, not a replayed old PI). Stripe `created`
   timestamp is part of the PaymentIntent object.

---

## 7. Attack Vector 6 — Race condition on topup capture (MEDIUM)

### 7.1 Threat

Two simultaneous topup requests arrive with different PIs:

- Request A: `topup_pi_id = pi_topup_aaaa` for $2.00
- Request B: `topup_pi_id = pi_topup_bbbb` for $2.00

The broker captures both, releasing $4.00 to the broker when
only $2.00 of shortfall existed. The attacker gets the result
for the price of one topup but charges twice. (Stripe keeps
both captures; the second $2.00 becomes an unintended
overpayment the customer has to dispute.)

OR — even worse — the attacker submits the SAME topup PI from
two concurrent requests; the broker double-captures it (Stripe
errors with `amount_exceeds_authorization` but only if the
amounts add up; otherwise silently succeeds twice).

### 7.2 Current state

NOT MITIGATED. The current `_finalize_job` does not have a
topup capture path at all.

### 7.3 Recommended fix

1. **Idempotency key per topup.** Generate a UUID per topup
   request (server-side, not client-supplied) and use it as the
   `idempotency_key` parameter on `PaymentIntent.capture`. Stripe
   deduplicates requests with the same key within 24h — so even
   if the broker double-fires, only one capture actually settles.
2. **Database-level state lock.** When transitioning out of
   `awaiting_topup`, use the same `BEGIN IMMEDIATE` pattern from
   the `client_req_id` idempotency fix (daemon.py:1304-1313) to
   read-and-update the job row atomically:
   ```
   BEGIN IMMEDIATE;
   SELECT state FROM jobs WHERE job_id = ?;  -- must be 'awaiting_topup'
   UPDATE jobs SET state='completed', stripe_topup_pi_id=?, ...;
   COMMIT;
   ```
   If the second request finds state != `awaiting_topup`, it
   bails with HTTP 409 + `code: "topup_already_settled"`.
3. **Capture ONCE, return idempotently.** The capture endpoint
   should be safe to call repeatedly with the same payload:
   first call captures, subsequent calls return the cached
   result. Use the job's `stripe_topup_pi_id` as the cache key
   once it's been set.

---

## 8. Attack Vector 7 — TTL manipulation / TOCTOU (LOW)

### 8.1 Threat

Attacker submits a job with a $0.01 PI. The broker runs the job
($1.50 of compute consumed). At T+14:59 the cron sweeper is
about to refund the PI and transition the job to `timeout`.
Attacker, watching the cron logs or just guessing timing, submits
a topup PI at T+14:59:30 — the broker accepts it, transitions
the job to `completed`, captures the topup, releases the result.
The attacker paid $2.00 for what should have been an abandoned
job (the broker was about to write off the $1.50 of compute).
Net: the attacker gets the result AND the broker loses money on
the compute.

This is a TOCTOU on the cron / state transition. Severity is
LOW because the attacker still has to authorise a topup PI
(so the broker is paid for the result), but they got the
result for the price of the topup instead of having it
abandoned with no result — which is what the TTL was supposed
to enforce.

### 8.2 Current state

NOT MITIGATED. There is no cron sweeper for `awaiting_topup`
jobs in the current daemon (the cron is post-hackathon per the
STATUS.md).

### 8.3 Recommended fix

1. **Atomic state transition under row lock.** The TTL refund
   and the topup acceptance must use the same `BEGIN IMMEDIATE`
   pattern. Whichever transaction commits first wins:
   - If the cron commits first: job is `timeout`, the topup
     endpoint sees `state != 'awaiting_topup'` and rejects with
     `topup_window_expired`.
   - If the topup commits first: job is `completed` and
     `stripe_topup_pi_id` is set; the cron sees
     `state != 'awaiting_topup'` and skips.
2. **Sweep interval > race window.** Make the cron sweep
   interval (proposed: every 60s) shorter than any plausible
   human reaction time (proposed: 30s minimum). Document this
   in the cron config.
3. **Topup window grace period.** Consider allowing topup up
   to 60s AFTER the cron has marked the job `timeout` — but
   with a surcharge (e.g., 10% penalty) so the attacker has
   no incentive to game the timing. Out of scope for hackathon.

---

## 9. Cross-cutting observations

### 9.1 Idempotency / state machine

The current `JOB_STATES` (daemon.py:903) is `("queued", "running",
"completed", "failed", "timeout")`. The topup flow will add at
minimum `("awaiting_topup")`, and likely `("abandoned")` for
jobs whose TTL elapsed without topup. The transition graph must
be:

```
queued → running → completed
                  ↘ awaiting_topup → completed  (topup accepted)
                                   ↘ abandoned    (TTL elapsed)
                  → failed
                  → timeout
```

The `_finalize_job` flow (daemon.py:2129-2251) currently uses
`WHERE job_id=? AND state IN ('queued', 'running')` (line 2239) —
this predicate MUST be updated to also match `awaiting_topup`
when the job is completing from the topup path.

### 9.2 PI → job linkage

The `llm_tokens` table (daemon.py:491-505) stores
`stripe_pi_id` per job. The topup flow will need a SECOND PI
(the topup PI) per job — either:
- A new column `stripe_topup_pi_id` on `jobs` (simplest), or
- A new table `topup_tokens` analogous to `llm_tokens`.

Recommend the column approach for hackathon simplicity; the
table approach if multi-topup (multiple shortfalls on the same
job) is supported.

### 9.3 Webhook payload shape

The current webhook body (daemon.py:2276-2282) is:
```
{job_id, state, result, artifact_urls}
```
The `awaiting_topup` webhook MUST include the shortfall amount
so the customer knows what to authorise:
```
{job_id, state: "awaiting_topup", shortfall_cents: 200,
 topup_url: "https://broker/v1/jobs/{id}/topup",
 topup_deadline: "2026-06-28T12:34:56Z"}
```
This is also the customer-facing contract for kanban
t_9fb71ad7 ("Verify payment block shape in webhook payloads +
front-end renderer"). Coordinate with that card's owner.

### 9.4 Demo mode exemption

The hackathon demo uses `payment.demo == true` (daemon.py:1525)
which short-circuits all Stripe calls. Demo-mode PIs are
explicitly exempt from the topup path per the constraints in
this task's body. The implementation should gate the topup
endpoint behind `if not client_is_demo` so a demo-mode PI
never enters `awaiting_topup` — instead, the result is
released immediately and the shortfall is silently absorbed
(or the demo just doesn't allow underfunded PIs).

### 9.5 Cost floor + Stripe fees

The companion `docs/cost-review/2026-06-28-accuracy.md`
already flags that a 1ms job with a $0.20 minimum capture
costs the broker $0.30 in Stripe fees alone. If the topup
flow allows the customer to topup with ANOTHER $0.20 PI,
that's another $0.30 in fees on a job that paid $0 in
compute. Per-topup Stripe fee must be factored into the
cost formula or absorbed as a flat broker markup.

---

## 10. Sign-off

**This threat model is BLOCKING for production go-live** unless
items B1–B7 in §1 are addressed. **It is NOT blocking for the
hackathon demo** because the demo runs under `payment.demo ==
true` and is exempt from the topup path.

User sign-off requested in a kanban comment on `t_1015a43e`.
Implementation owner (`t_9a705578`) should reconcile the final
topup implementation against this doc and either:
- Confirm each B1–B7 is implemented (link to commit), or
- Acknowledge it's tracked as a follow-up card (link).

Reviewer: kanban `t_1015a43e` worker (default profile), 2026-06-28.
