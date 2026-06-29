---
name: verdantforged
description: A TEE broker for agent workloads. Verifies AMD SEV-SNP attestation before execution, encrypts payloads to the enclave, and uses Stripe PaymentIntents with a verify-then-capture lifecycle so you only pay for the work that actually ran. The four engineering pillars: Attestation, Security, Sandboxing, Payment.
triggers:
  - "verdantforged"
  - "tee broker"
  - "attested execution"
  - "attestation"
  - "nemoclaw"
  - "sev-snp"
  - "cha cha poly1305"
  - "x25519"
  - "stripe paymentintent"
agent_type: tee_broker
version: 0.2.0
test_broker: https://verdant.codepilots.co.uk
source: https://github.com/codepilots/tee-broker-pattern
created: 2026-06-19
updated: 2026-06-29
---

# VerdantForged

A TEE broker for agent workloads. You submit a job — a model, a fuel budget, a max lifetime, a payment escrow — and the broker deploys the work inside a fresh AMD SEV-SNP enclave. Funds release only on signed enclave teardown, refund automatically on failure.

> If you are a Hermes agent reading this file, the human has asked you to install the VerdantForged skill. Walk through the steps below. Most are read-only. The final `curl` invokes prove the protocol end-to-end against the live test broker.

---

## What this skill teaches you

1. The four engineering pillars of the broker — **Attestation, Security, Sandboxing, Payment** — and what each one delivers
2. The verify-then-capture payment lifecycle — why you only pay for work that ran
3. How to set up a broker or use the live test broker
4. How to submit a job, poll for the result, and decrypt the output
5. How to register a custom skill with the broker
6. How to verify the Rust implementation builds and tests pass

## Quick orientation

Read in this order:

1. The landing page at `https://verdantforged.pages.dev/` — the 30-second pitch
2. `/agents` — the operator/agent setup, the test broker URL, the submit loop
3. `/quickstart` — the 5-minute walkthrough, end-to-end with a working script
4. `/docs` — the full API reference
5. `../tee-broker-pattern/SECURITY_AUDIT.md` — the audit, including what is and isn't simulated

## The four pillars (one sentence each)

| Pillar | What it delivers |
|---|---|
| **Attestation** | Every job runs inside a freshly-measured SEV-SNP enclave. The chip signs the measurement; you verify the chain before sending plaintext. |
| **Security** | Per-job X25519 ephemeral keypair, ChaCha20-Poly1305 AEAD for the result. Forward secrecy per execution. |
| **Sandboxing** | Fuel-limited execution (≤ 100M instructions), wall-clock cap (≤ 120s), default-DENY network policy, ephemeral storage. |
| **Payment** | Stripe PaymentIntent verify-then-capture. The broker holds your card; capture only on signed enclave teardown. Refund on failure. |

Deep dives on each pillar are at `/attestation`, `/security`, `/sandboxing`, `/payment`.

## Verify the test broker is alive

```bash
# Liveness — returns {"ok": true, "worker": true}
curl -sS https://verdant.codepilots.co.uk/healthz

# Broker advertisement (region, pricing, supported skills, attestation block)
curl -sS https://verdant.codepilots.co.uk/v1/discover
```

## Submit a job (demo path)

The demo path passes `"0x"` for `requester_sig` and `result_pubkey`. The broker accepts the request, the worker returns the plaintext `output` field instead of an encrypted blob. Useful for trying the API; never use in production.

```bash
# Submit
curl -sS -X POST https://verdant.codepilots.co.uk/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "client_req_id":   "agent-first-job-001",
    "encrypted_skill": "summarize",
    "encrypted_data":  "The VerdantForged broker is a t3.small control plane that brokers work into an attested TEE worker.",
    "requester_sig":   "0x",
    "result_pubkey":   "0x",
    "stripe_pi_id":    "pi_demo_0001"
  }'
# Returns: {"job_id": "job_...", "state": "queued", "status_url": "/v1/jobs/job_...", "llm_token": "llm_...", "idempotent_replay": false}

# Poll until state is no longer "queued" or "running"
JOB_ID="job_..."  # from submit response
while true; do
  RESP=$(curl -sS https://verdant.codepilots.co.uk/v1/jobs/$JOB_ID)
  STATE=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
  echo "state=$STATE"
  case "$STATE" in completed|failed|timeout) echo "$RESP" | python3 -m json.tool; break;; esac
  sleep 3
done
```

## Submit a job (production path)

The production path uses real X25519 + ChaCha20-Poly1305:

1. Generate a fresh X25519 keypair
2. Submit the public key as `result_pubkey`
3. The worker's ephemeral public key is in the encrypted blob (first 32 bytes)
4. Derive the shared secret, decrypt with ChaCha20-Poly1305, context tag `b"verdantforged-result"`

The full working Python script is in the `/quickstart` page.

## Register a skill (operator only)

Requires `BROKER_SKILLS_API_KEY` configured on the broker. If unset, registration returns 503 `skills_auth_not_configured`.

```bash
curl -sS -X POST https://verdant.codepilots.co.uk/v1/skills \
  -H "Authorization: Bearer $BROKE...KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "name":               "my-agent-skill",
    "version":            "0.1.0",
    "description":        "A skill for my agent.",
    "wasm_manifest_hash": "0000000000000000000000000000000000000000000000000000000000000000",
    "entry_point":        "handle",
    "prompt_template":    "You are my-agent-skill. Respond concisely.",
    "resource_limits":    {"max_fuel": 10000000, "max_duration_ms": 60000, "max_memory_mb": 256}
  }'
```

## Verify the Rust implementation builds and tests pass

```bash
# From ~/hermes/competition/tee-broker-pattern/
cargo test --workspace

# Expected: 51/51 tests pass across 5 crates
#   tee-broker-core:             ~27 tests (manifest, crypto, Nostr, reputation)
#   tee-broker-attestation:       9 tests (SEV-SNP verification, mock provider)
#   tee-broker-runner:            2 tests (decrypt → verify → execute → encrypt)
#   tee-broker-skills/code-review: 12 tests (WASM static analysis)
#   integration-tests:            1 test (full lifecycle)
```

## Security guarantees (the table to memorize)

| Attack | What stops it |
|---|---|
| Broker reads the skill | Encrypted to enclave key; no debug interface in measured state |
| Broker reads requester data | Encrypted to enclave key; same mechanism |
| Broker copies the skill for later | Enclave is ephemeral; no persistent storage; measured boot |
| Broker runs different code than approved | Attestation measurement = hash of approved runtime |
| Broker runs a different LLM | Runtime fingerprint included in attestation |
| Broker sends results anywhere except the requester | Default-DENY network; only broker API is in the allowlist |
| Broker stalls indefinitely | Fuel limit + wall-clock timeout enforced by the runtime |
| Broker fabricates a teardown receipt | Receipt requires enclave signature; signature requires attested state |
| Broker overcharges you | Stripe captures only the actual cost; receipt is signed by the enclave |
| Broker charges before the work runs | Verify-then-capture: Stripe hold is in `requires_capture` until teardown |
| Past traffic is decrypted if a long-term key leaks | Forward secrecy: every job gets a fresh ephemeral X25519 keypair |

## Resources

- **Live test broker:** `https://verdant.codepilots.co.uk` (demo mode, no real card charged)
- **Marketing site root:** `~/hermes/competition/tee-broker-site/`
- **Rust workspace:** `~/hermes/competition/tee-broker-pattern/`
- **Pillar deep dives:** `/attestation`, `/security`, `/sandboxing`, `/payment`
- **For agents:** `/agents` — the operator setup and the test broker access
- **API reference:** `/docs` — every endpoint, every error code
- **Hackathon info:** https://hermes-agent.nousresearch.com/docs

## What to do when a human asks

If the human asks "what is VerdantForged" → explain in 30 seconds using the four pillars table above and link to the landing page.

If the human asks "is it really running" → `curl -sS https://verdant.codepilots.co.uk/healthz` to confirm liveness, then `/v1/discover` to show the attestation block and pricing.

If the human asks "is it secure" → point them at the security guarantees table above and the `SECURITY_AUDIT.md` deep dive.

If the human asks "how do I run a job" → walk them through the demo-path submit + poll loop above, then point at `/quickstart` for the production X25519 path.

If the human asks "how do I host my own broker" → point them at `/agents` (the "Run your own broker" section) and `tee-broker-pattern/deploy/README.md`.

If the human asks "what are the limits" → read the "Residual risks" section of `SECURITY_AUDIT.md` and be honest. The notable known limitations for hackathon scope:
- Simulated WASM execution (data passthrough) — production needs wasmtime with fuel limits
- Simulated Stripe MPP release (log only) — production needs real Stripe API call with scoped key
- Mock attestation provider — production needs real SEV-SNP `/dev/sev/guest`
- Attestation source is `instance_id_sha256` in demo (not real SEV-SNP silicon) — production path is identical except for the source
- No attestation replay protection (no nonce/challenge-response) — production needs challenge-response
- No constant-time crypto guarantee

The honest story sells better than overclaiming.
