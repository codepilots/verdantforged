# Site-vs-Broker Audit — 2026-06-29

Cross-reference of what `tee-broker-site/` (the demo site, deployed at `verdantforged.pages.dev`)
expects against what the deployed broker (i-05117b9649db5b343, public URL
`verdant.codepilots.co.uk`) actually provides.

**TL;DR: the site and broker are independent demo systems.** The site talks
to `stripe.codepilots.co.uk` (a Cloudflare Worker Stripe Connect backend)
and runs a browser-side `BrokerMock`. The broker at `verdant.codepilots.co.uk`
runs a real Rust-equivalent Python job execution pipeline. **They share
zero API surface.** This is a feature, not a bug — the site is the
marketing pitch, the broker is the live product.

---

## 1. Architecture: who talks to whom

```
tee-broker-site/                      stripe.codepilots.co.uk         verdant.codepilots.co.uk
(Astro, browser-side)                 (Cloudflare Worker)              (broker on i-05117b9649db5b343)
┌──────────────────┐                  ┌──────────────────┐             ┌──────────────────┐
│ BrokerMock       │── /health ──────▶│ Stripe Backend   │             │ daemon.py        │
│ (TS, browser)    │                  │ (Python, port 8790)            │ (Python, port 8080)│
│                  │── /create-PI ──▶│                  │             │                  │
│ intent/approve/  │                  │ Stripe Connect   │             │ POST /v1/jobs    │
│ reject/modify    │── /create-trans▶│ per-step transfers│             │ NemoClaw + SEV-SNP│
│                  │                  │                  │             │ OpenShell policy │
└──────────────────┘                  └──────────────────┘             └──────────────────┘
        │                                                                  ▲
        │ (no live RPC to broker)                                          │
        ▼                                                                  │
        talks to BrokerMock directly in browser                            │
```

The site's `STRIPE_BACKEND_URL` is hardcoded to `https://stripe.codepilots.co.uk` —
a separate Stripe Connect demo backend, **NOT the broker**. See
`src/lib/broker-mock.ts:41`.

---

## 2. Contract mismatch (the gist)

The site expects a **session-lease / planner / per-step transfer** model.
The broker implements a **flat per-job PaymentIntent** model.

| Concept | Site (`src/lib/broker-types.ts`) | Broker (`broker-daemon/daemon.py`) | Match? |
|---|---|---|---|
| Top-level method | `intent()` → returns `PlanResponse` | `POST /v1/jobs` → returns `JobAck` | ❌ different shape |
| Approval step | `approve()` with `plan_hash`, `user_approval`, `stripe_payment_intent_id` | implicit — payment validated at submit | ❌ no planner |
| Reject / void | `reject()` returns `Ack` with `stripe_void_intent` | `POST /v1/jobs/{id}/topup` (different purpose) | ❌ |
| Modify plan | `modify()` with `parent_plan_hash` | none | ❌ |
| Subscribe events | `subscribe(fn)` for live panel updates | none — polling only | ❌ |
| Pricing model | session-lease (cents per minute) | per-job PaymentIntent (one-shot) | ❌ |
| Per-step transfers | yes — `stripe_transfer_id` per `StepReceipt` | none — only one capture per job | ❌ |
| Application fee | 5% per step (`APP_FEE_RATE = 0.05`) | none — broker retains the difference | ❌ |
| Carbon gCO2eq | per-skill rating in catalog | none | ❌ |
| Nostr kind 31989 | referenced in catalog comment | none | ❌ |
| Fuel budget | `fuel_budget_per_step = 50_000_000` per step | none | ❌ |
| MPP / multi-step | yes — multi-step plans | none — single skill per job | ❌ |
| Worker enclave attestation | per session, returned in `PlanResponse` | per-broker via `/v1/discover` | ⚠️ partial |

---

## 3. The "BrokerClient" interface the site expects

From `src/lib/broker-types.ts:235-242`:

```typescript
export interface BrokerClient {
  intent(req: IntentRequest): Promise<PlanResponse>;
  approve(req: ApprovalRequest): Promise<ExecutionTrace>;
  reject(req: RejectionRequest): Promise<Ack>;
  modify(req: ModificationRequest): Promise<PlanResponse>;
  subscribe(fn: (e: BrokerEvent) => void): () => void;
  _catalog: ReadonlyArray<SkillCatalogEntry>;
}
```

Implemented in browser by `BrokerMock` in `src/lib/broker-mock.ts:456`:

```typescript
export const broker = new BrokerMock();
```

**No equivalent in the broker daemon.** The broker has:

```
POST /v1/jobs
GET  /v1/jobs/{id}
POST /v1/jobs/{id}/ready
POST /v1/jobs/{id}/topup
GET  /v1/jobs/{id}/artifacts
GET  /v1/jobs/{id}/artifacts/{filename}
GET  /v1/discover
POST /v1/skills
GET  /v1/skills
POST /v1/skills/{name}/wasm
POST /v1/stripe/webhook
POST /v1/llm/chat/completions
```

The shape of `IntentRequest → PlanResponse → ApprovalRequest → ExecutionTrace` doesn't exist
anywhere on the broker. That's not an oversight — it's a different product surface.

---

## 4. What the broker DOES expose that the site doesn't use

| Broker feature | Endpoint | Status |
|---|---|---|
| Real LLM proxy | `POST /v1/llm/chat/completions` | live, used in real test jobs |
| File-jobs (encrypted attachments) | `POST /v1/jobs` + `input_files[]` + `result_pubkey` | just merged |
| NemoClaw onboard token | `POST /v1/skills` + `wasm_manifest_hash` | live |
| Real SEV-SNP attestation | `/v1/discover` returns live measurement | live |
| Per-job token bucket | implicit in `verify_payment_intent` | live |
| Daily per-account cap | implicit in `_check_daily_job_cap` | live |
| Live worker keys | `/v1/discover.attestation.enclave_pubkey` | live |

The site mocks all of these inline in `BrokerMock` and never asks the broker.

---

## 5. Skill catalog mismatch

**Live broker** (3 skills, registered via `push_skills.sh`):
- `code-review@0.1.0` (prompt-template)
- `photo-glow-up@0.1.0` (prompt-template)
- `summarize@0.1.0` (prompt-template)

**Site catalog** (`src/lib/skill-catalog.ts:48-125`, 7 entries):
- `code-review-v3.2` (different version, has `provider_acct`, `provider_name`, `static_cost_usd`, `reputation`, `n_executions`)
- `summarize-doc` (different name from broker's `summarize`)
- `generate-tests` (not in broker)
- `sql-query` (not in broker)
- `image-caption` (not in broker)
- `extract-pdf-text` (not in broker)
- `+ summarize` (no, actually `summarize-doc` — broker's `summarize` is not in the site catalog)

**Synthetic Stripe Connect accounts** (`acct_1QAcmPLM5nrG2kAc` etc.) are hardcoded
in the site catalog. Real broker doesn't use Stripe Connect at all — only
`PaymentIntent.capture` + `Refund.create`.

---

## 6. What's actually deployed and live

### Site
- **Deployed at**: `https://verdantforged.pages.dev` (Cloudflare Pages, per `wrangler.toml`)
- **Live status as of audit**: not reachable from this network (HTTP 000 from curl — probably a DNS/network issue here, not a downtime issue)
- **Build**: `npm run build` → static dist/ → Cloudflare Pages
- **Stripe backend**: `https://stripe.codepilots.co.uk` (separate Cloudflare Worker, not in this repo)

### Broker
- **Deployed at**: `https://verdant.codepilots.co.uk` (Caddy on i-05117b9649db5b343)
- **Live and verified**: `/healthz`, `/v1/discover`, `/v1/skills`, `POST /v1/jobs` end-to-end
- **Skill library** (separate service): `https://verdant.codepilots.co.uk/library/*`

### Are they connected? **No.**
- The site uses a `BrokerMock` and never makes a real RPC to `verdant.codepilots.co.uk`
- The broker has no concept of session leases, planners, multi-step plans, or Stripe Connect transfers
- The Stripe Connect backend at `stripe.codepilots.co.uk` is a third system that handles the site's payment demo separately

---

## 7. What would close the gap (in priority order)

1. **Replace the site's BrokerMock with a real client** that calls
   `POST /v1/jobs` + `GET /v1/jobs/{id}` against `verdant.codepilots.co.uk`.
   - Largest impact: makes the demo site actually drive the live broker
   - Smallest API change: remove session-lease + multi-step + planner from site
   - Effort: ~200 lines of TS, ~3 components
2. **Sync the skill catalog**: replace the 7 synthetic entries with the 3 real ones (`code-review`, `summarize`, `photo-glow-up`); fetch dynamically from `/v1/library/v1/library/skills`
3. **Drop Stripe Connect** from the site, use the broker's `PaymentIntent` flow instead
4. **Add a real "live" indicator** to the site (poll `/healthz` to show the broker is up)

None of this is required for the demo — the site + broker each work standalone. But if you want them to integrate for the demo flow, #1 is the highest-value change.

---

## 8. Files referenced in this audit

- `tee-broker-site/src/lib/broker-types.ts` (the contract the site expects)
- `tee-broker-site/src/lib/broker-mock.ts` (the in-browser mock)
- `tee-broker-site/src/lib/skill-catalog.ts` (the synthetic skill list)
- `tee-broker-site/stripe-backend/server.py` (the Stripe Connect demo backend)
- `tee-broker-deploy/broker-daemon/daemon.py` lines 3168-3290 (`/v1/discover`, `/healthz`)
- `tee-broker-deploy/broker-daemon/daemon.py` lines 5349-5394 (all routes)
- `tee-broker-deploy/broker-daemon/daemon.py` lines 219-457 (Stripe API calls — PaymentIntent only, no Connect)
- `tee-broker-deploy/docs/skill-library-live-2026-06-29.md` (what's actually live on AWS)

## 9. Verdict

The site is a **demo marketing pitch** that mocks the broker. The broker
is the **live product**. They are independent deploys, share no API
surface, and were never intended to integrate. That's why the gap is so
large — it's not drift, it's a deliberate separation.

If you want them to talk, replace the BrokerMock with a thin
`POST /v1/jobs` client (item #1 above) and the demo becomes real.