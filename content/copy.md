# VerdantForged — Marketing Copy

**Source of truth for all section text.** Edit here, the components pick it up.
**Tone:** confident, concrete, no hype. Apple-keynote cadence.
**Persona reference:** VerdantFamiliar — botanical metaphors woven into technical clarity, no fluff.
**Pivot (2026-06-29):** the product is the **Broker**, not the marketplace. Provider / Requester / marketplace framing removed. Site leads with the four engineering pillars that the broker actually delivers: **Attestation, Security, Sandboxing, Payment**.

**Site map (2026-06-29):**

- `/` — landing page (hero → 4 pillars → how it works → security deep dive → demo → try)
- `/attestation/` — pillar 01 deep dive
- `/security/` — pillar 02 deep dive
- `/sandboxing/` — pillar 03 deep dive
- `/payment/` — pillar 04 deep dive
- `/agents/` — for AI agents and the humans running them
- `/docs` — REST API reference (10 endpoints)
- `/quickstart` — 5-minute walkthrough
- `/pricing` — cost model
- `/payment-flow` — 4 lifecycle paths
- `/terms` — hackathon terms stub

---

## 1. HERO

**Eyebrow:** A TEE broker for agent workloads

**Headline:**
> The broker that runs your agent inside a hardware-attested enclave.

**Subhead:**
VerdantForged is the runtime that brokers attested execution of agent
workloads. You hand it a job — a skill, an input, a payment escrow — and
it deploys the work inside a fresh AMD SEV-SNP enclave on a dedicated
TEE worker. Your skill and your data are encrypted to silicon, the
enclave signs the teardown receipt, and Stripe only captures on that
signed receipt. Try the live test broker or set up your own.

**Primary CTA:** See the four pillars
**Secondary CTA:** For agents
**Tertiary CTA:** Read the security audit

---

## 2. WHAT THE BROKER IS (new — replaces the old "Problem" section)

**Eyebrow:** What it is

**Headline:** A runtime for attested agent execution.

**Body:**
The broker is a single, addressable service. It accepts a workload, verifies the enclave is the one you asked for, runs the work against attested hardware, and emits a signed teardown receipt that releases payment.

What the broker is **not**: a marketplace, a router, a model aggregator, or a wrapper around a third-party cloud. The guarantees come from the hardware (SEV-SNP), the payment rail (Stripe), and the protocol — not from the operator running the broker.

**Four guarantees, one sentence each:**

| Pillar | The promise |
|---|---|
| **Attestation** | Every sandbox is a freshly measured SEV-SNP enclave. The measurement is signed by the silicon, not by the operator. |
| **Security** | End-to-end encryption. ECIES (X25519 + AES-256-GCM) for the payload, Ed25519 for the manifest, ephemeral keys per execution. |
| **Sandboxing** | wasmtime-class isolation with fuel limits, no host imports, ephemeral storage, default-DENY network egress. |
| **Payment** | Stripe MPP holds the escrow. Funds release on signed enclave teardown, refund automatically on failure or timeout. |

---

## 3. THE FOUR PILLARS (centerpiece section)

This is the engineering case. Each pillar gets the same treatment: what it is, what stops it, the audit evidence.

### 3.1 Attestation

**Eyebrow:** Pillar 01

**Headline:** The enclave is the only thing you trust.

**Body:**
Every workload runs inside a freshly booted NemoClaw SEV-SNP enclave. The chip itself signs the measurement — a hash of the initial register state plus the loaded runtime. The requester pulls the signed report, verifies the cert chain against the AMD root, and rejects the work if the measurement doesn't match the approved binary.

**What stops what:**

| Attack | What stops it |
|---|---|
| Operator swaps the runtime | Measurement mismatch — the report is signed by silicon the operator doesn't control |
| Replay of an old attestation | Requester issues a nonce; report is bound to nonce + measurement |
| Stale TCB / revoked chip | TCB version check; production path includes OCSP/CRL |

**Evidence:** `tee-broker-pattern/SECURITY_AUDIT.md` §4.3 — five independent attestation checks, all unit-tested.

### 3.2 Security

**Eyebrow:** Pillar 02

**Headline:** Encryption to silicon, not to policy.

**Body:**
The broker never holds plaintext. Skill source and requester data are encrypted to the enclave's public key before they leave the requester's machine. The only place decryption happens is inside the measured enclave, where the private key exists transiently for the lifetime of that execution. The result is encrypted to the requester's pubkey on the way out.

**Crypto stack:**

| Layer | Algorithm | Why |
|---|---|---|
| Key exchange | X25519 ECDH, ephemeral | Forward secrecy per execution |
| AEAD | AES-256-GCM | Confidentiality + integrity in one primitive |
| KDF | HKDF-SHA256 with context binding | Context separates skill / data / result |
| Manifest signature | Ed25519 | Compact, fast verification |
| Nonce | CSPRNG (`OsRng`) | Non-repeating per execution |

**What stops what:**

| Attack | What stops it |
|---|---|
| Broker reads the skill | Encrypted to enclave key; no debug interface in measured state |
| Broker reads requester data | Encrypted to enclave key; same mechanism |
| Broker copies the skill for later | Enclave is ephemeral; no persistent storage; measured boot |
| Broker sends the result to someone else | Result encrypted to requester's pubkey; enclave has no other key |
| Broker fabricates a "successful execution" receipt | Receipt requires enclave signature; signature requires attested state |

**Evidence:** `tee-broker-pattern/SECURITY_AUDIT.md` §3 — six properties per primitive, all unit-tested. 51/51 tests pass.

### 3.3 Sandboxing

**Eyebrow:** Pillar 03

**Headline:** A fresh, fuel-limited enclave for every workload.

**Body:**
The broker provisions a new enclave per execution. There is no shared state between workloads, no host filesystem the WASM can touch, no syscalls the runtime exposes. The execution has a fuel budget (instruction count), a wall-clock cap, and a memory cap — all enforced by the runtime, all measured into the attestation.

**Resource limits (enforced):**

| Resource | Cap | Source |
|---|---|---|
| Fuel (instructions) | ≤ 100M per execution | `ExecutionRequest` validation |
| Wall-clock lifetime | ≤ 120s per execution | `ExecutionRequest` validation |
| Skill output size | 1 MiB | Buffer-overflow guard |
| Payload size | ≤ 10 MB | `ExecutionRequest` validation |
| Memory | Per-skill (declared in manifest) | OpenShell policy |
| Network egress | Default-DENY; explicit allowlist (Stripe API) | OpenShell policy |

**What stops what:**

| Attack | What stops it |
|---|---|
| Skill runs forever (DoS) | Fuel limit + wall-clock timeout — both enforced by the runtime |
| Skill exfiltrates via the network | OpenShell default-DENY; only `api.stripe.com:443` is in the allowlist |
| Skill reads host filesystem | No host imports exposed to the WASM module |
| Skill overflows output buffer | 1 MiB output cap, checked before write |
| Skill persists between executions | Enclave is ephemeral; no persistent storage; measured boot |

**Evidence:** `tee-broker-pattern/SECURITY_AUDIT.md` §5 — bump allocator + `MAX_OUTPUT_SIZE` + fuel/fuel-limits validation.

### 3.4 Payment

**Eyebrow:** Pillar 04

**Headline:** The enclave has to sign before the funds move.

**Body:**
Payment is held in a Stripe PaymentIntent at session open, in status
`requires_capture`. The escrow is keyed to the attestation: if the wrong
enclave reports back, the funds don't move. On signed teardown, Stripe
captures the actual cost. On failure, timeout, or attestation mismatch,
Stripe refunds the full hold — atomically, automatically. No
per-execution charges, no prepaid balance, no broker custody of funds.

**Payment lifecycle:**

```
open session        deposit into Stripe MPP escrow (held against attestation)
         │
         ▼
enclave work        … fuel burns, network is default-DENY, output is signed …
         │
         ├─── enclave signs teardown receipt
         │            │
         │            ├─── receipt valid   → MPP releases to provider
         │            └─── receipt invalid → MPP refunds to requester
         │
         └─── timeout / failure
                      └─── MPP refunds to requester (atomic)
```

**What stops what:**

| Attack | What stops it |
|---|---|
| Operator pockets the escrow | Funds are in Stripe, not in the operator's account — operator is not a party to the escrow |
| Operator releases funds before work completes | Capture requires a signed teardown receipt; signature requires attested state |
| Provider claims more fuel than was burned | Receipt is signed by the enclave; fuel counter is in the receipt |
| Provider claims a failed execution was successful | Receipt includes the actual enclave output + teardown reason; receipt is signed |
| Requester denies a successful execution | Receipt is signed by the enclave — requester can verify it independently of the broker |

**Evidence:** `tee-broker-pattern/SECURITY_AUDIT.md` §3.1 (ECIES), §3.2 (Ed25519). 51/51 tests pass.

---

## 9. FOR AGENTS (new)

**Eyebrow:** For agents

**Headline:** Set up a broker. Submit a job. Get a result.

**Body:**
Everything an agent needs to run on the VerdantForged broker: the test
broker URL, the registration flow, the submit/poll/decrypt loop, and the
production setup if you want to host your own. Six steps, copy-pasteable
curl, exercised against the live broker at `https://verdant.codepilots.co.uk`.

**The six steps (full detail on `/agents`):**

1. Check the broker is alive — `/healthz`, `/v1/discover`, `/v1/skills`
2. Submit a job — `POST /v1/jobs` with the demo path (`"0x"` placeholders)
3. Poll for the result — `GET /v1/jobs/{job_id}` until state is no longer `queued` or `running`
4. Decrypt the result — production path uses X25519 + ChaCha20-Poly1305 (see `/quickstart` for the working script)
5. Register a skill (optional) — `POST /v1/skills` with `BROKER_SKILLS_API_KEY`
6. Call the LLM proxy (advanced) — `POST /v1/llm/chat/completions` with the per-job `llm_token`

**Beyond the test broker — running your own:**

| Component | Minimum | Notes |
|---|---|---|
| Control plane | t3.small | Daemon, queue, per-job LLM token issuer, Stripe lifecycle |
| TEE worker | EC2 m6a.xlarge (AMD SEV-SNP) | Cold boot ~6 min, terminates after `IDLE_BUFFER_MINUTES` of idle |
| Required env | `STRIPE_SECRET_KEY`, `BROKER_SKILLS_API_KEY`, `LLM_UPSTREAM_*` | The real LLM API key never enters the worker |
| Required infrastructure | AWS account with SEV-SNP-capable instance type, Stripe account (test or live) | Nothing else |

---

## 10. SECURITY DEEP DIVE (now a section, was a standalone)

The landing page includes a Security section that gives the deep dive
on the crypto primitives, the input validation, and the attestation
checks — every claim tied to a test name. See
`/security` for the beginner-friendly version (problem → how the broker
does it → what stops what → look at the code).

**Crypto stack (from `tee-broker-pattern/SECURITY_AUDIT.md`):**

| Layer | Algorithm | Why | Test |
|---|---|---|---|
| Key exchange | X25519 ECDH, ephemeral | Forward secrecy per execution | test_ecies_roundtrip |
| AEAD | AES-256-GCM | Confidentiality + integrity | test_aes_gcm_tamper_detection |
| KDF | HKDF-SHA256 with context binding | Context separates channels | test_wrong_context_fails |
| Manifest signature | Ed25519 (RFC 8032) | Compact, constant-time | test_manifest_signature_roundtrip |
| Hash | SHA-256 | NIST standard | test_sha256_known_vector |

**Production crypto stack (the live broker uses this):**

| Layer | Algorithm | Why |
|---|---|---|
| Key exchange | X25519 (per-job ephemeral) | Forward secrecy per execution |
| AEAD | ChaCha20-Poly1305 | Fast authenticated encryption, constant-time |
| Context tag | `b"verdantforged-result"` | Domain separation, prevents key reuse across message types |

**ExecutionRequest validation:**

- `skill_encrypted` not empty · `data_encrypted` not empty
- `max_fuel` in (0, 100M] · `max_duration_ms` in (0, 120s]
- `mpp_escrow_id` not empty · payload size ≤ 10 MB

**Attestation verification (6 checks):**

- Non-empty attestation
- Cert chain → AMD/Intel root
- Measurement matches expected
- TCB version ≥ minimum
- Policy hash matches
- Serialization roundtrip

51/51 tests pass. Zero clippy warnings. Two `unsafe` blocks, both
correctly annotated for the WASM FFI surface.

---

## 11. BUILT ON

**Eyebrow:** Stands on the shoulders of

| Layer | Component | Vendor |
|---|---|---|
| Attestation | NemoClaw SEV-SNP enclaves | NVIDIA |
| Payments | PaymentIntent verify-then-capture (MPP variant in audit) | Stripe |
| Inference runtime | Hermes-4-70B / Hermes-4-405B | Nous Research |
| Agent runtime | Hermes Agent | Nous Research |

---

## 12. CTA — TRY IT

**Eyebrow:** Install the broker

**Headline:** Three ways to run it.

**(A) Read the spec + audit**
- `SPEC.md` — the protocol, end to end
- `SECURITY_AUDIT.md` — the 51-test audit, including what's simulated

**(B) Paste this prompt into your Hermes chat:**
```
Install the VerdantForged broker from https://verdantforged.pages.dev/AGENT.md and walk me through the four pillars.
```

**(C) Direct download:**
[Download AGENT.md] (right-click → Save Link As)

**For agents:** see `/agents` for the end-to-end setup against the live test broker.

---

## FOOTER

- VerdantForged © 2026 — open source under MIT
- Hackathon: NVIDIA × Stripe × Nous Research — Hermes Agent Accelerated Business Hackathon
- Built by Autumn — see the spec, the audit, the code
- Four pillars → /attestation, /security, /sandboxing, /payment
- For agents → /agents
- Three links: spec / audit / source
- "The guarantees are made by NVIDIA NemoClaw, Stripe, and the protocol itself — not by us."
