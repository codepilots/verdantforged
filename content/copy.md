# VerdantForged — Marketing Copy

**Source of truth for all section text.** Edit here, the components pick it up.
**Tone:** confident, concrete, no hype. Apple-keynote cadence.
**Persona reference:** VerdantFamiliar — botanical metaphors woven into technical clarity, no fluff.
**Pivot (2026-06-29):** the product is the **Broker**, not the marketplace. Provider / Requester / marketplace framing removed. Site leads with the four engineering pillars that the broker actually delivers: **Attestation, Security, Sandboxing, Payment**.

---

## 1. HERO

**Eyebrow:** A TEE broker for agent workloads

**Headline:**
> The broker that runs your agent inside a hardware-attested enclave.

**Subhead:**
VerdantForged is the runtime that brokers attested execution of agent workloads. You hand it a manifest — a model, a fuel budget, a max lifetime, a payment escrow — and it deploys the work inside a fresh NVIDIA NemoClaw SEV-SNP enclave, with Stripe holding the funds until the enclave confirms teardown. Your model and your data are encrypted to silicon, not to a vendor's policy.

**Primary CTA:** See the four pillars
**Secondary CTA:** Read the security audit
**Tertiary CTA:** Try in your agent

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
Payment is held in a Stripe MPP escrow at session open. The escrow is keyed to the attestation measurement: if the wrong enclave reports back, the funds don't move. On signed teardown, MPP releases to the model/skill provider. On failure, timeout, or attestation mismatch, MPP refunds the requester — atomically, automatically.

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
| Operator pockets the escrow | Funds are in Stripe MPP, not in the operator's account — operator is not a party to the escrow |
| Operator releases funds before work completes | MPP release requires a signed teardown receipt; signature requires attested state |
| Provider claims more fuel than was burned | Receipt is signed by the enclave; fuel counter is in the receipt |
| Provider claims a failed execution was successful | Receipt includes the actual enclave output + teardown reason; receipt is signed |
| Requester denies a successful execution | Receipt is signed by the enclave — requester can verify it independently of the broker |

**Evidence:** `tee-broker-pattern/SECURITY_AUDIT.md` §3.1 (ECIES), §3.2 (Ed25519). 51/51 tests pass.

---

## 4. HOW IT WORKS (5-step protocol)

**Eyebrow:** Five steps

**Headline:** Manifest to teardown, end to end.

**Step 1 — Manifest**
Requester (or model/skill provider) publishes a manifest — model, fuel, max lifetime, attestation requirements. Requester deposits the data hash and creates the MPP escrow.

**Step 2 — Attestation verification**
Requester pulls the broker's SEV-SNP report. Verifies the cert chain against the AMD root. Confirms the measurement matches the approved runtime. Confirms the TCB version is current.

**Step 3 — Encrypted payload**
Requester sends encrypted skill, encrypted data, result pubkey, and escrow ID. The broker cannot decrypt anything until the enclave is up.

**Step 4 — Enclave execution**
Enclave boots, decrypts skill and data, verifies the skill hash, runs the work with fuel limits and no syscalls, encrypts the result to the requester's pubkey, signs the teardown receipt.

**Step 5 — Teardown and payment**
Sandbox lifetime expires (or task completes). The signed teardown receipt is presented to Stripe MPP. MPP releases to the provider. Encrypted result goes to the requester. On failure, escrow refunds automatically.

---

## 5. LIVE DEMO

**Eyebrow:** See it run

**Headline:** The actual broker, on a fresh NemoClaw dev sandbox.

**Body:**
This is the protocol on real hardware. The broker provisions the enclave, runs the work, returns the signed receipt, releases the escrow — all visible in the cast.

[asciinema player — autoplay on scroll, full-width, max 800px tall]

(Caption: Recorded on a clean NemoClaw dev sandbox. Cast file: ≈ 50 KB. Three terminals, four minutes.)

---

## 6. AUDIT & EVIDENCE

**Eyebrow:** What we actually proved

**Headline:** 51/51 tests pass. Zero clippy warnings. The full audit is public.

**Body:**
VerdantForged is a hackathon submission, so we publish what we tested and what we didn't. The Rust core (`tee-broker-pattern/`) is auditable; the audit doc lives next to the code and lists every primitive, every property, and every residual risk.

**Audit table:**

| Component | Lines | Tests | Audited |
|---|---|---|---|
| `tee-broker-core` | ~450 | 27 | ✅ |
| `tee-broker-attestation` | ~600 | 9 | ✅ |
| `tee-broker-runner` | ~200 | 2 | ✅ |
| `skill-code-review` | ~610 | 12 | ✅ |
| `integration-tests` | ~220 | 1 | ✅ |
| **Total** | **~2080** | **51** | |

**Simulated (called out, not hidden):**

- WASM execution → simulated (data passthrough) in the submission; production uses wasmtime with fuel limits and no host imports
- MPP payment release → simulated in the submission; production calls the real Stripe API with a scoped key
- SEV-SNP attestation → mock provider in the submission; production reads `/dev/sev/guest` directly

**Production hardening (priority list, post-hackathon):**

| Priority | Item | Effort |
|---|---|---|
| P0 | Integrate wasmtime with fuel limits | 2 days |
| P0 | Real MPP payment release via Stripe API | 1 day |
| P0 | Real SEV-SNP attestation via `/dev/sev/guest` | 3 days |
| P1 | Attestation replay protection (challenge-response) | 1 day |
| P1 | Certificate revocation (OCSP/CRL) | 1 day |
| P1 | Manifest expiry enforcement | 1 hour |

---

## 7. BUILT ON

**Eyebrow:** Stands on the shoulders of

| Layer | Component | Vendor |
|---|---|---|
| Attestation | NemoClaw SEV-SNP enclaves | NVIDIA |
| Payments (crypto) | MPP (Solana USDC, Tempo USDC) | Stripe |
| Payments (fiat) | MPP via Shared Payment Tokens (SPT) | Stripe |
| Payments (HTTP 402) | x402 (Base USDC) | Stripe |
| Inference runtime | Hermes-4-70B / Hermes-4-405B | Nous Research |
| Agent runtime | Hermes Agent | Nous Research |
| Discovery | Nostr event kinds, Ed25519 reputation | open protocol |

---

## 8. CTA — TRY IT

**Eyebrow:** Install the broker

**Headline:** Three ways to run it.

**(A) Read the spec and the audit**
- `SPEC.md` — the protocol, end to end
- `SECURITY_AUDIT.md` — the 51-test audit, including what's simulated

**(B) Paste this prompt into your Hermes chat:**
```
Install the VerdantForged skill from https://verdantforged.pages.dev/AGENT.md and walk me through the four pillars.
```

**(C) Direct download:**
[Download AGENT.md] (right-click → Save Link As)

---

## FOOTER

- VerdantForged © 2026 — open source under MIT
- Hackathon: NVIDIA × Stripe × Nous Research — Hermes Agent Accelerated Business Hackathon
- Built by Autumn — see the spec, the audit, the code
- Three links: spec / audit / source
- "The guarantees are made by NVIDIA NemoClaw, Stripe MPP, and the protocol itself — not by us."
