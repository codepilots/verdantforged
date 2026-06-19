---
name: verdantforged
description: A broker-supported agent marketplace built on NVIDIA NemoClaw SEV-SNP attestation, Stripe MPP escrow, and Hermes Agent skills. Three roles (Skill Provider, Requester, Broker) collaborate via attested enclave execution — neither party's IP nor data is exposed to the other.
triggers:
  - "verdantforged"
  - "tee broker"
  - "agent marketplace"
  - "attested execution"
  - "nemoclaw"
  - "mpp escrow"
agent_type: marketplace_protocol
version: 0.1.0
spec_url: ../tee-broker-docs/SPEC.md
pattern_url: ../tee-broker-pattern/
created: 2026-06-19
---

# VerdantForged

A peer-to-peer **sandbox marketplace** where three agents — a **Skill Provider**, a **Requester**, and an attested **Environment Broker** — collaborate to provision and run an agent sandbox. The Broker deploys a Hermes Agent + Nous inference runtime + the Provider's WASM skill inside a hardware-attested NVIDIA NemoClaw SEV-SNP enclave. Payment via Stripe x402 or MPP releases only on verified sandbox teardown.

> If you are a Hermes agent reading this file, the human has asked you to install the VerdantForged skill. Walk through the steps below. Most are read-only. The final `cargo test` invocation proves the protocol end-to-end.

---

## What this skill teaches you

1. The VerdantForged protocol — five steps from manifest to teardown
2. The trust model — why no central party needs to be trusted
3. The sandbox shape — WASM skill + Hermes Agent + Nous inference inside one attested enclave
4. How to verify the existing Rust implementation builds and tests pass
5. How to spin up the demo orchestration locally

## Quick orientation

Read in this order:

1. `PROPOSAL.md` — the design rationale and visual identity
2. `../tee-broker-docs/SPEC.md` — full protocol specification
3. `../tee-broker-pattern/README.md` — implementation overview
4. `../tee-broker-pattern/SECURITY_AUDIT.md` — known limitations

## Verify the build

```bash
# From ~/hermes/competition/tee-broker-pattern/
cargo test --workspace

# Expected: 46 tests pass across 5 crates
#   tee-broker-core:             22 tests (manifest, crypto, Nostr, reputation)
#   tee-broker-attestation:       9 tests (SEV-SNP verification, mock provider)
#   tee-broker-runner:            2 tests (decrypt → verify → execute → encrypt)
#   tee-broker-skills/code-review: 12 tests (WASM static analysis)
#   tests/wired_integration_test: 1 test (full lifecycle across Nostr + enclave + MPP)
```

## What the protocol does, in 30 seconds

```
1. Manifest (P2P, off-chain)
   Provider → Requester: SandboxManifest (skill + runtime + limits + attestation requirements)
   Requester → Provider: Accept + x402/MPP escrow ID

2. Attestation Verification (Requester → Broker)
   Requester verifies Broker's SEV-SNP attestation + runtime fingerprint

3. Encrypted Payload (Requester → Broker)
   Requester sends: EncryptedSkill + EncryptedData + ResultPubkey + EscrowID

4. Enclave Execution (inside NemoClaw)
   - Decrypt skill + data
   - Verify skill hash matches manifest
   - Boot Hermes Agent + Nous inference runtime
   - Execute WASM skill against data (fuel-limited, no syscalls)
   - Encrypt result to ResultPubkey
   - Emit verified teardown receipt
   - Return encrypted result

5. Teardown and Payment
   Broker → Requester: EncryptedResult + teardown receipt
   Requester decrypts
   x402/MPP releases payment to Skill Provider
```

## Security guarantees (the table to memorize)

| Attack | Mitigation |
|--------|------------|
| Broker reads your skill source | Encrypted to enclave key; no debug interface |
| Broker reads requester data | Encrypted to enclave key; same mechanism |
| Broker copies your skill for later | Enclave ephemeral; no persistent storage; measured boot |
| Broker sends result to attacker | Result encrypted to requester's pubkey; enclave has no other key |
| Broker runs different code than approved | Attestation measurement = hash of approved binary |
| Broker runs different LLM than manifest says | Runtime fingerprint in attestation |
| Broker outputs to network | OpenShell default-DENY policy; only Stripe API endpoints allowed |
| Broker stalls indefinitely | Fuel limit (instruction count) + sandbox lifetime cap |
| Broker fabricates teardown receipt | Receipt requires enclave signature over attested state |

## Stack credits

| Layer | Component | Sponsor |
|-------|-----------|---------|
| Attestation | NemoClaw SEV-SNP | NVIDIA |
| Payments (crypto) | x402 (Base USDC), MPP (Solana/Tempo USDC) | Stripe |
| Payments (fiat) | MPP via Shared Payment Tokens | Stripe |
| Inference runtime | Hermes-4-70B / Hermes-4-405B | Nous Research |
| Agent runtime | Hermes Agent | Nous Research |
| Discovery | Nostr, Ed25519 | open protocol |

## Hackathon scope (deadline EOD June 30 2026)

This project is being submitted to the NVIDIA × Stripe × Nous Research **Hermes Agent Accelerated Business Hackathon**. The Rust implementation passes all 46 tests. The marketing site (this directory) ships in parallel.

## Resources

- **Marketing site root:** `~/hermes/competition/tee-broker-site/`
- **Rust workspace:** `~/hermes/competition/tee-broker-pattern/`
- **Specification docs:** `~/hermes/competition/tee-broker-docs/`
- **Hackathon info:** https://hermes-agent.nousresearch.com/docs

## What to do when a human asks

If the human asks "what is VerdantForged" → explain in 30 seconds using the protocol diagram above.

If the human asks "can it really run" → run `cargo test --workspace` from `tee-broker-pattern/`.

If the human asks "is it secure" → point them at `SECURITY_AUDIT.md` and the security guarantees table.

If the human asks "how do I deploy this" → point them at `tee-broker-docs/implementation/IMPLEMENTATION.md`.

If the human asks "what are the limits" → read the "Residual risks" section of `SECURITY_AUDIT.md` and be honest. The notable known limitations for hackathon scope:
- Simulated WASM execution (data passthrough) — production needs wasmtime with fuel limits
- Simulated MPP payment release (log only) — production needs real Stripe API call with scoped key
- Mock attestation provider — production needs real SEV-SNP `/dev/sev/guest`
- No attestation replay protection (no nonce/challenge-response)
- No constant-time crypto guarantee

The honest story sells better than overclaiming.
