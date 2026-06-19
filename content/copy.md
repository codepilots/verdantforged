# VerdantForged — Marketing Copy

**Source of truth for all section text.** Edit here, the components pick it up.
**Tone:** confident, concrete, no hype. Apple-keynote cadence.
**Persona reference:** VerdantFamiliar — botanical metaphors woven into technical clarity, no fluff.

---

## 1. HERO

**Eyebrow:** A sandbox marketplace for agents

**Headline:**
> Three agents. One attested enclave. Pay only for the sandbox you use.

**Subhead:**
VerdantForged is a peer-to-peer marketplace where a Skill Provider publishes a sandbox configuration, a Requester pays to provision it, and an attested Environment Broker deploys and runs it inside an NVIDIA NemoClaw SEV-SNP enclave — using Nous inference as the runtime. Your skill stays your skill. Their data stays their data. You pay only for the seconds the sandbox was alive.

**Primary CTA:** See the protocol (1 min)
**Secondary CTA:** Try it in your browser
**Tertiary CTA:** Read the spec

---

## 2. THE PROBLEM

**Eyebrow:** Today

**Headline:** Agents can't safely run on each other's behalf.

**Body:**
Running an agent's code on your hardware means trusting the agent. Running your code on someone else's hardware means trusting the operator. Most marketplaces solve this with policy, audits, and reputation systems that the operator can override.

VerdantForged solves it with silicon. The broker runs the sandbox inside a hardware-attested enclave. The skill is encrypted to the enclave. The data is encrypted to the enclave. The result is encrypted to the requester. Payment releases only after the enclave confirms execution finished.

**Two-column comparison:**

| Today | With VerdantForged |
|-------|--------------------|
| Operator can read your skill | Encrypted to enclave key; no debug interface |
| Operator can read requester data | Encrypted to enclave key; same mechanism |
| Pay before or after execution | Pay only for verified sandbox lifetime |
| Trust the operator | Trust the silicon |
| Single point of failure | Three independent parties |

---

## 3. THE SOLUTION

**Eyebrow:** The pattern

**Headline:** Three roles. One attested sandbox.

**Body:**
The Skill Provider publishes a manifest — what tools the sandbox needs, what model it runs, what fuel and memory it consumes, what the maximum lifetime is. The Requester reviews the manifest, agrees to terms, and pays via Stripe x402 or MPP. The Broker provisions a NemoClaw enclave, deploys the sandbox (Hermes Agent + Nous inference runtime + the Provider's WASM skill), runs it, releases payment on verified teardown.

```
┌──────────────┐                    ┌──────────────┐
│              │  SandboxManifest   │              │
│   Provider   │ ─────────────────► │  Requester   │
│              │ ◄───────────────── │              │
│              │  Accept + x402/MPP │              │
└──────────────┘                    └──────┬───────┘
                                          │ verify attestation
                                          ▼
                                   ┌──────────────┐
                                   │   Broker     │
                                   │  (NemoClaw   │
                                   │   enclave)   │
                                   │              │
                                   │  ┌────────┐  │
                                   │  │Hermes  │  │
                                   │  │Agent + │  │
                                   │  │Nous LLM│  │
                                   │  │+ skill │  │
                                   │  └────────┘  │
                                   └──────┬───────┘
                                          │ encrypted result + payment release
                                          ▼
                                   Payment → Provider
                                   Result  → Requester
```

---

## 4. HOW IT WORKS

**Eyebrow:** Five steps

**Headline:** Manifest to teardown, end to end.

**Step 1 — Manifest (P2P, off-chain)**
Skill Provider publishes a SandboxManifest — the WASM skill, the inference model (e.g. Nous Hermes-4-70B), fuel budget, max lifetime, attestation requirements. Requester accepts and creates an x402 or MPP escrow.

**Step 2 — Attestation verification**
Requester pulls the Broker's SEV-SNP attestation. Verifies the cert chain against the AMD root. Confirms the measurement matches the approved skill-runner binary. Confirms the runtime matches the published Nous model card.

**Step 3 — Encrypted payload**
Requester sends encrypted skill, encrypted data, result pubkey, and escrow ID. Broker cannot decrypt anything outside the attested enclave.

**Step 4 — Enclave execution**
NemoClaw boots, decrypts the skill, decrypts the data, verifies the skill hash, instantiates Hermes Agent with the requested Nous model, runs the skill against the data with fuel limits and no syscalls, encrypts the result to the requester's pubkey.

**Step 5 — Teardown and payment**
Sandbox lifetime expires or task completes. Enclave emits a verified teardown receipt. MPP/x402 releases payment to the Provider. Encrypted result delivered to Requester. If execution fails or times out, escrow refunds automatically.

---

## 5. LIVE DEMO

**Eyebrow:** See it run

**Headline:** A sandbox, from manifest to result.

**Body:**
This is the actual protocol flow on a fresh NemoClaw dev sandbox. The Provider publishes a code-review manifest. The Requester fetches the attestation, creates an MPP escrow, submits encrypted code. The Broker provisions Hermes + Nous Hermes-4-70B, runs the review, returns the result, releases payment.

[asciinema player — autoplay on scroll, full-width, max 800px tall]

(Caption: Recorded on a clean NemoClaw dev sandbox. Cast file: ≈ 50 KB. Three terminals, four minutes.)

---

## 6. SECURITY

**Eyebrow:** What's actually enforced

**Headline:** Trust the silicon, not the operator.

**Body:**
Every guarantee below is enforced by the enclave or by Stripe's machine-payments stack — not by policy, not by goodwill, not by the marketplace operator.

| Attack | What stops it |
|--------|---------------|
| Broker reads your skill source | Encrypted to enclave key; no debug interface |
| Broker reads requester data | Encrypted to enclave key; same mechanism |
| Broker copies your skill for later | Enclave is ephemeral; no persistent storage; measured boot |
| Broker sends the result to someone else | Result encrypted to requester's pubkey; enclave has no other key |
| Broker runs different code than you approved | Attestation measurement = hash of approved binary |
| Broker runs a different LLM than the manifest says | Runtime fingerprint included in attestation |
| Broker sends results anywhere except the requester | OpenShell default-DENY policy; only Stripe API endpoints allowed |
| Broker stalls indefinitely | Fuel limit + sandbox lifetime cap enforced by NemoClaw |
| Broker fabricates a "successful execution" receipt | Receipt requires enclave signature; signature requires attested state |

**Footer line:**
VerdantForged is the marketplace. The guarantees are made by NVIDIA NemoClaw, Stripe x402/MPP, and the protocol itself — not by us.

---

## 7. BUILT ON

**Eyebrow:** Stands on the shoulders of

**Stack (text-only for now, see PROPOSAL.md §Open Decisions):**

| Layer | Component | Vendor |
|-------|-----------|--------|
| Attestation | NemoClaw SEV-SNP enclaves | NVIDIA |
| Payments (crypto) | x402 (Base USDC) + MPP (Solana USDC, Tempo USDC) | Stripe |
| Payments (fiat) | MPP via Shared Payment Tokens (SPT) | Stripe |
| Inference runtime | Hermes-4-70B / Hermes-4-405B | Nous Research |
| Agent runtime | Hermes Agent | Nous Research |
| Discovery | Nostr event kinds, Ed25519 reputation | open protocol |

---

## 8. SKILL SHOWCASE (NEW)

**Eyebrow:** The first sandbox

**Headline:** Inference is the runtime, not the product.

**Body:**
The first concrete sandbox flowing through VerdantForged runs **Nous Hermes-4** inside an attested enclave. The Skill Provider publishes the code-review skill. The Broker provisions Hermes Agent + Hermes-4-70B + the skill. The Requester pays for the sandbox lifetime. Future sandboxes can swap the model, the skill, or both — without changing the protocol.

**Showcase skill card:**
- **Name:** `code-review-attested`
- **Runtime:** Hermes Agent + Nous Hermes-4-70B
- **Sandbox limits:** 4096 fuel · 5 min max · 2 GB memory cap
- **Price:** $0.001 per second of sandbox lifetime (paid via x402)
- **Result delivery:** Encrypted to requester's pubkey; teardown receipt on completion

**Register your own skill (post-hackathon):**
VerdantForged is open to other teams and other agents. Publish a SandboxManifest (WASM skill + model choice + limits) and Nostr-publish it. The marketplace is yours.

---

## 9. CTA — TRY IT IN YOUR AGENT (UPDATED)

**Eyebrow:** Install the skill

**Headline:** Three ways to try it.

**(A) Try it in your browser** ← NEW
Click below. We load a cutdown Hermes Agent compiled to Python-in-WebAssembly (Pyodide) right in this page. It reads AGENT.md, summarizes the protocol, then opens an **inline dashboard** — click "Open sandbox" to provision a `python-3.11-base` enclave and watch the timer tick. You can make skill calls, extend the lease, or close and see the refund released. No install. No signup. Pyodide load takes 2-5 seconds.

**[ Try in browser ]** (opens BrowserAgentSplash overlay with inline dashboard)

**(B) Copy this prompt into your Hermes chat:**
```
Install the VerdantForged skill from https://verdantforged.pages.dev/AGENT.md and walk me through what it does.
```

**(C) Direct download:**
[Download AGENT.md] (right-click → Save Link As)

---
---
## 10. SESSION DASHBOARD (NEW — sits above the chat)

**Eyebrow:** What the requester sees

**Headline:**
> One tab. One sandbox. One timer.

**Subhead:**
Open a sandbox, the local LLM runs your session. Every skill call, every second of enclave compute, every byte of Nous inference flows through one VerdantForged escrow — and shows up on one dashboard. When the timer runs out, the sandbox burns down and Stripe MPP refunds the unused balance.

**Mock snapshot (rendered into SessionDashboard.astro):**

| Field | Value |
|---|---|
| Environment | `python-3.11-base` (attested SEV-SNP) |
| Session ID | `sess_7f3a…b9c2` |
| Time remaining | **10:00** of 15:00 |
| Skill calls | 3 |
| Broker compute | $0.0400 |
| Skill costs | $0.0075 |
| Nous inference | $0.0123 |
| **Total spent** | **$0.0598** |
| Max budget | $0.5000 |
| Refund on close | ~$0.44 |

**Three affordances below the snapshot:**
- `Extend session` (adds 15 min, escrow increments pro-rata)
- `Close session` (triggers burn-down + MPP refund release)
- `View teardown receipt` (signed Nostr event, decrypts to receipt)

**Footnote:** The dashboard is the same UI Hermes Agent renders for itself. The local LLM decides when to escalate — it does not see your Stripe API key.

---
## FOOTER

- VerdantForged © 2026 — open source under MIT
- Hackathon: NVIDIA × Stripe × Nous Research — Hermes Agent Accelerated Business Hackathon
- Built by Autumn — see the protocol, the implementation, and the security audit
- Three links: protocol / implementation / security audit
- "Powered by NemoClaw, x402/MPP, and Hermes Agent."
