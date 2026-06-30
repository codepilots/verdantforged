# VerdantForged — Marketing Copy

**Source of truth for section text.** Edit here first, then update the rendered Astro components/pages.
**Tone:** confident, concrete, no hype. Apple-keynote cadence.
**Pivot (2026-06-29):** the product is the **Broker**, not the marketplace. Provider / Requester / marketplace framing removed. Site leads with the four engineering pillars the live Python broker actually implements: **Attestation, Security, Sandboxing, Payment**.

**Site map:**

- `/` — landing page: hero → 4 pillars → how it works → security deep dive → demo → try
- `/attestation/` — pillar 01 deep dive
- `/security/` — pillar 02 deep dive
- `/sandboxing/` — pillar 03 deep dive
- `/payment/` — pillar 04 deep dive
- `/agents/` — for AI agents and the humans running them
- `/docs/` — REST API reference
- `/quickstart/` — 5-minute walkthrough
- `/pricing/` — cost model
- `/payment-flow/` — payment lifecycle paths
- `/verify-attestation/` — verifier walkthrough
- `/terms/` — hackathon terms stub

---

## 1. HERO

**Eyebrow:** A TEE broker for agent workloads

**Headline:**
> The broker that runs your agent inside a hardware-attested worker.

**Subhead:**
VerdantForged brokers attested execution of agent workloads. You hand it a job — a skill, encrypted input, and a Stripe PaymentIntent — and the control plane routes the work to an AMD SEV-SNP TEE worker. The worker decrypts inside the TEE boundary, executes through the registered skill path, signs the result, and the broker captures or refunds the PaymentIntent when the job finalizes.

**Primary CTA:** See the four pillars
**Secondary CTA:** For agents
**Tertiary CTA:** Verify attestation

---

## 2. WHAT THE BROKER IS

**Eyebrow:** What it is

**Headline:** A runtime for attested agent execution.

**Body:**
The broker is a single, addressable service. It accepts a workload, validates the encrypted job envelope, checks the available worker identity/attestation metadata, routes execution to an AMD SEV-SNP worker, and emits signed result metadata that the requester can verify.

What the broker is **not**: a marketplace, a router, a model aggregator, or a wrapper around a third-party cloud. The guarantees come from the SEV-SNP worker, the cryptographic job envelope, the NemoClaw sandbox path, the broker/worker signatures, and Stripe's verify-then-capture lifecycle.

**Four guarantees, one sentence each:**

| Pillar | The promise |
|---|---|
| **Attestation** | The worker is an EC2 m6a.xlarge with AMD SEV-SNP enabled; reports, measurements, policy hash, and worker binding are exposed for verification. |
| **Security** | Inputs are encrypted with `x25519-hkdf-sha256-chacha20poly1305-v1`; worker and broker Ed25519 signatures bind `result_hash`, `skill_hash`, and `input_hash`. |
| **Sandboxing** | Registered skills execute through the live worker dispatch tree: WASM via wasmtime when registered, prompt-template jobs through NemoClaw `nemohermes exec`, and built-in fallback paths only where explicitly implemented. |
| **Payment** | Stripe PaymentIntents are verified before job acceptance and captured on `completed`, refunded on `failed`/`timeout`, or paused as `awaiting_topup` on shortfall. |

---

## 3. THE FOUR PILLARS

This is the engineering case. Each pillar states what is implemented and what it stops.

### 3.1 Attestation

**Eyebrow:** Pillar 01

**Headline:** Trust the measured worker, not the operator.

**Body:**
Jobs run on a dedicated AMD SEV-SNP worker VM. The worker exposes attestation metadata including measurement, policy hash, report data, cert chain, enclave public key, and source. The broker checks the SNP report signature against the supplied VCEK/VLEK certificate; the requester/verifier performs the full AMD-root-chain and measurement policy checks.

**What stops what:**

| Attack | What stops it |
|---|---|
| Operator swaps the worker runtime | Measurement and policy hash mismatch in the report metadata |
| Replay or unrelated worker identity | Worker binding ties identity metadata to the attestation path |
| Fake attestation payload | SNP report signature check against the supplied certificate, plus requester-side AMD chain validation |

**Evidence:** `tee-broker-deploy/worker/sev_snp.py`, `broker-daemon/daemon.py:_verify_snp_quote_signature`, `broker-daemon/daemon.py:_load_verified_worker_identity`, and the `/verify-attestation/` walkthrough.

### 3.2 Security

**Eyebrow:** Pillar 02

**Headline:** Encryption to the worker boundary, signatures on the way out.

**Body:**
The requester encrypts input to the worker's X25519 public key before submitting the job. The worker decrypts inside the TEE boundary, executes the selected skill path, encrypts the result back to the requester, and signs the output metadata. The broker independently signs final result hashes for non-repudiation.

**Crypto stack:**

| Layer | Algorithm | Why |
|---|---|---|
| Key exchange | X25519 ECDH | Per-job shared secret with the worker identity |
| KDF | HKDF-SHA256 with channel AAD | Separates input, result, and artifact channels |
| AEAD | ChaCha20-Poly1305 | Authenticated encryption for payload confidentiality and integrity |
| Worker identity | Ed25519 | Worker signs result metadata |
| Broker non-repudiation | Ed25519 | Broker signs `result_hash|skill_hash|input_hash` on finalize |
| Hash | SHA-256 | Stable digest for result, input, and skill binding |

**What stops what:**

| Attack | What stops it |
|---|---|
| Broker reads encrypted input | Payload is encrypted to the worker public key |
| Broker changes result metadata | Worker and broker signatures bind the output hashes |
| Result sent to the wrong requester | Result encryption uses the requester result public key |
| Old payload replayed in another channel | HKDF/AAD channel separation and per-job envelopes |

**Evidence:** `tee-broker-deploy/broker-daemon/crypto.py`, `tee-broker-deploy/worker/poller.py`, and `tee-broker-deploy/tests/verify-crypto-e2e.py` / `verify-ephemeral-static-ecdh.py`.

### 3.3 Sandboxing

**Eyebrow:** Pillar 03

**Headline:** Jobs run through explicit worker dispatch paths.

**Body:**
The live worker dispatch tree is explicit: registered WASM skills run via wasmtime with manifest limits, tool-loop skills use the tool-calling path, prompt-template work is dispatched into a NemoClaw sandbox with `nemohermes exec`, and legacy host fallback is only used when the worker is not onboarded to NemoClaw. Network policy is enforced at the OpenShell/NemoClaw layer for sandboxed jobs.

**Resource and routing controls:**

| Control | Live behavior |
|---|---|
| Skill selection | `skill_hash` / registered skill metadata route the job |
| WASM limits | Manifest fuel and duration caps; defaults and hard caps enforced by worker code |
| NemoClaw path | `dispatch_to_sandbox` runs `nemohermes exec` when sandbox onboarding succeeded |
| Output limits | Worker caps per-file and total artifact/output sizes before publishing |
| Network policy | Default-deny policy for sandboxed execution, with broker-mediated LLM access |

**What stops what:**

| Attack | What stops it |
|---|---|
| Job bypasses registered skill routing | Worker re-checks skill metadata and selected execution path |
| Prompt job calls arbitrary network directly | NemoClaw/OpenShell policy and broker-mediated LLM route |
| Worker publishes unbounded artifacts | Per-file and total output caps before artifact publication |
| Host fallback hides a missing sandbox | Fail-closed `no-nemoclaw` behavior when stub mode is disabled |

**Evidence:** `tee-broker-deploy/worker/poller.py:execute_in_envelope`, `dispatch_to_sandbox`, and `build_attestation_verdict`.

### 3.4 Payment

**Eyebrow:** Pillar 04

**Headline:** Stripe capture happens only after job finalization.

**Body:**
The requester supplies a Stripe PaymentIntent. The broker verifies the PaymentIntent before accepting the job. When the job finalizes as `completed`, `_finalize_job` captures the calculated actual cost, padded only to the configured minimum capture amount. Failed or timed-out jobs are refunded. If the authorized amount is too low, the job enters `awaiting_topup` with a shortfall amount and top-up window.

**Payment lifecycle:**

```
submit job          verify PaymentIntent is capturable / acceptable
     │
     ▼
worker executes     result envelope lands in the outbox
     │
     ▼
broker finalizes    completed → capture actual cost
                    failed/timeout → refund
                    shortfall → awaiting_topup
```

**What stops what:**

| Attack | What stops it |
|---|---|
| Operator captures before work completes | Capture is triggered from `_finalize_job`, not from job submission |
| Operator pockets funds directly | Funds remain with Stripe until capture/refund |
| Operator fabricates a successful result | Worker and broker signatures bind result/input/skill hashes |
| Broker overcharges below card-network floor | Capture is actual cost padded only to `BROKER_MIN_CAPTURE_CENTS` |

**Evidence:** `tee-broker-deploy/broker-daemon/daemon.py:_finalize_job`, `verify-stripe-integration.py`, and `/payment-flow/`.

---

## 9. FOR AGENTS

**Eyebrow:** For agents

**Headline:** Set up a broker. Submit a job. Get a result.

**Body:**
Everything an agent needs to run on the VerdantForged broker: the test broker URL, skill registration flow, submit/poll/decrypt loop, and the production setup if you host your own. The live test broker is `https://verdant.codepilots.co.uk`.

**The six steps (full detail on `/agents/`):**

1. Check the broker is alive — `/healthz`, `/v1/discover`, `/v1/skills`
2. Submit a job — `POST /v1/jobs` with encrypted payload fields
3. Poll for the result — `GET /v1/jobs/{job_id}` until terminal state
4. Decrypt the result — X25519 + HKDF-SHA256 + ChaCha20-Poly1305
5. Register a skill (optional) — `POST /v1/skills` with `BROKER_SKILLS_API_KEY`
6. Call the LLM proxy (advanced) — `POST /v1/llm/chat/completions` with the per-job `llm_token`

**Beyond the test broker — running your own:**

| Component | Minimum | Notes |
|---|---|---|
| Control plane | t3.small | Python daemon, queue, per-job LLM token issuer, Stripe lifecycle |
| TEE worker | EC2 m6a.xlarge with SEV-SNP | Cold boot is several minutes; worker may terminate after idle buffer |
| Required env | `STRIPE_SECRET_KEY`, `BROKER_SKILLS_API_KEY`, `BROKER_LLM_*` | LLM provider credentials live in broker config, not requester payloads |
| Required infrastructure | AWS account with SEV-SNP-capable instance type, Stripe account | Demo mode can run without real card capture |

---

## 10. SECURITY DEEP DIVE

The landing page includes a Security section covering crypto primitives, input validation, and attestation checks. Every rendered claim should tie to the live Python broker or a `tee-broker-deploy/tests/verify-*.py` script.

**Production crypto stack:**

| Layer | Algorithm | Why | Evidence |
|---|---|---|---|
| Key exchange | X25519 ECDH | Shared secret with worker identity | `verify-ephemeral-static-ecdh.py` |
| KDF | HKDF-SHA256 with AAD | Channel separation | `verify-crypto-e2e.py` |
| AEAD | ChaCha20-Poly1305 | Authenticated encryption | `verify-crypto-e2e.py` |
| Worker signature | Ed25519 | Worker result binding | `worker/poller.py` |
| Broker signature | Ed25519 | Broker finalize binding | `daemon.py:_finalize_job` |
| Attestation report | AMD SEV-SNP report signature | Hardware-backed worker evidence | `verify-sev-snp-framework.py` |

**Submit validation:**

- encrypted skill/data fields are present
- `result_pubkey` is well-formed
- `requester_sig` is present where required
- `stripe_pi_id` is format-valid and acceptable to Stripe logic
- optional WASM URI / registered skill metadata matches the supported path
- payload and artifact sizes remain within live worker limits

**Attestation verification:**

- non-empty SNP report
- measurement/policy metadata present
- report data bound to worker key
- broker-side report signature check against supplied cert
- requester-side AMD ARK/root-chain validation
- optional image digest / NemoClaw metadata checks when present

---

## 11. BUILT ON

**Eyebrow:** Stands on the shoulders of

| Layer | Component | Vendor |
|---|---|---|
| Attestation substrate | AMD SEV-SNP on EC2 m6a.xlarge | AWS / AMD |
| Sandboxed agent execution | NemoClaw + OpenShell policy + `nemohermes exec` | NVIDIA / Nous ecosystem |
| Payments | Stripe PaymentIntent verify-then-capture | Stripe |
| Inference runtime | Hermes / Nous-compatible LLM provider path | Nous Research |
| Agent runtime | Hermes Agent | Nous Research |

---

## 12. CTA — TRY IT

**Eyebrow:** Try the broker

**Headline:** Three ways to inspect it.

**(A) Read the live docs**
- `/docs/` — REST API reference
- `/verify-attestation/` — attestation verifier walkthrough
- `/security/` — crypto/security deep dive

**(B) Paste this prompt into your Hermes chat:**
```
Read /AGENT.md from the VerdantForged broker at https://verdant.codepilots.co.uk and walk me through the four pillars: attestation, security, sandboxing, payment.
```

**(C) Direct download:**
`https://verdant.codepilots.co.uk/AGENT.md`

**For agents:** see `/agents/` for the end-to-end setup against the live test broker.

---

## FOOTER

- VerdantForged © 2026 — open source under MIT
- Hackathon: NVIDIA × Stripe × Nous Research — Hermes Agent Accelerated Business Hackathon
- Four pillars → `/attestation/`, `/security/`, `/sandboxing/`, `/payment/`
- For agents → `/agents/`
- The guarantees are made by the measured worker, the cryptographic protocol, Stripe's payment lifecycle, and the verifier — not by marketing copy.
