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
  - "chacha poly1305"
  - "x25519"
  - "stripe paymentintent"
agent_type: tee_broker
version: 0.3.0
test_broker: https://verdant.codepilots.co.uk
source_dir: ~/hermes/competition/tee-broker-deploy/
pattern_dir: ~/hermes/competition/tee-broker-pattern/
created: 2026-06-19
updated: 2026-06-30
---

# VerdantForged

A TEE broker for agent workloads. You submit a job — a model, a fuel budget, a max lifetime, a Stripe PaymentIntent ID — and the broker deploys the work inside a freshly-booted AMD SEV-SNP worker. Stripe captures the actual cost on success, refunds the hold on failure, and triggers a topup window when the actual exceeds the held.

> If you are a Hermes agent reading this file, the human has asked you to install the VerdantForged skill. Walk through the steps below. Most are read-only. The final `curl` invokes prove the protocol end-to-end against the live test broker.

---

## What this skill teaches you

1. The four engineering pillars of the broker — **Attestation, Security, Sandboxing, Payment** — and what each one delivers
2. The verify-then-capture payment lifecycle — why you only pay for work that ran
3. How to set up a broker or use the live test broker
4. How to submit a job, poll for the result, and decrypt the output
5. How to register a custom skill with the broker
6. How to run the broker's verify-*.py test suite against the live deployment

## Quick orientation

Read in this order:

1. The landing page at `https://verdantforged.pages.dev/` — the 30-second pitch
2. `/agents` — the operator/agent setup, the test broker URL, the submit loop
3. `/quickstart` — the 5-minute walkthrough, end-to-end with a working Python script
4. `/docs` — the full API reference
5. `/security` — the cryptography deep dive (X25519 + ChaCha20-Poly1305 wire format)
6. `/payment-flow` — the four lifecycle paths (completed, failed, awaiting_topup, abandoned)
7. `/verify-attestation` — the verifier's checklist: 5 SEV-SNP checks + a 6th check that binds the NemoClaw Docker image digest to the worker's Ed25519 key, so a reviewer can pull the same image locally and verify it matches what the worker pulled. Source-code line numbers prove each step is what the broker actually does. Includes an honest "what can the operator lie about?" table.
8. `/topology` — the deployment picture in one diagram: 1 broker (t3.small systemd service, NOT in NemoClaw) + 1+ workers (m6a.xlarge SEV-SNP), with NemoClaw sandboxes as processes inside the worker EC2. Includes the "Shipping now" / "Future work — Option 2" split for signed-sandbox attestation.

## The four pillars (one sentence each)

| Pillar | What it delivers |
|---|---|
| **Attestation** | Every job runs inside a freshly-measured SEV-SNP worker (m6a.xlarge EC2). The chip signs the SHA-384 measurement; you verify the VCEK/VLEK cert chain before sending plaintext. |
| **Security** | Per-job X25519 ephemeral keypair, ChaCha20-Poly1305 AEAD over `x25519-hkdf-sha256-chacha20poly1305-v1`. Worker signs result with Ed25519; broker independently re-signs. |
| **Sandboxing** | wasmtime fuel meter (manifest-declared ≤ 10⁹, typical ≤ 10⁸) + epoch-interruption wall-clock cap (default 30s). OpenShell policy default-DENYs egress except to broker API and broker LLM proxy. |
| **Payment** | Stripe PaymentIntent verify-then-capture. The broker holds your card in `requires_capture`; captures `actual_cents` on completed, refunds the PI on failed/timeout, posts a topup PI request on shortfall. |

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
# Returns: {"job_id": "job_...", "state": "queued", "status_url": "/v1/jobs/job_...", "job_access_token": "jobtok_...", "idempotent_replay": false}

# Poll until state is no longer "queued" or "running"
JOB_ID="job_..."  # from submit response
while true; do
  RESP=$(curl -sS https://verdant.codepilots.co.uk/v1/jobs/$JOB_ID)
  STATE=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
  echo "state=$STATE"
  case "$STATE" in completed|failed|timeout|awaiting_topup) echo "$RESP" | python3 -m json.tool; break;; esac
  sleep 3
done
```

## Submit a job (production path)

The production path uses real X25519 + ChaCha20-Poly1305:

1. Generate a fresh X25519 keypair
2. Encrypt `skill` and `data` to the enclave's ephemeral X25519 pubkey (from `/v1/discover.attestation.enclave_pubkey`)
3. Submit your public key as `result_pubkey`
4. The worker's ephemeral public key is the first 32 bytes of the encrypted blob
5. Derive the shared secret, decrypt with ChaCha20-Poly1305, context tag `b"verdantforged-result"`

The full working Python script is in the `/quickstart` page.

## Register a skill (operator only)

Requires `BROKER_SKILLS_API_KEY` configured on the broker. If unset, registration returns 503 `skills_auth_not_configured`.

```bash
curl -sS -X POST https://verdant.codepilots.co.uk/v1/skills \
  -H "Authorization: Bearer $BROKER_SKILLS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "name":               "my-agent-skill",
    "version":            "0.1.0",
    "description":        "A skill for my agent.",
    "wasm_manifest_hash": "<64-hex SHA-384 of manifest>",
    "entry_point":        "handle",
    "prompt_template":    "You are my-agent-skill. Respond concisely.",
    "resource_limits":    {"max_fuel": 10000000, "max_duration_ms": 60000, "max_memory_mb": 256}
  }'
```

## Run the broker's verify-*.py test suite

The live broker ships a Python test suite under `tee-broker-deploy/tests/verify-*.py`. Each script is a standalone Python file that exercises one slice of the broker against the live deployment.

```bash
# From ~/hermes/competition/tee-broker-deploy/
pip install -r requirements.txt
pytest tests/ -v

# Or run individual verify scripts:
python3 tests/verify-crypto-e2e.py           # X25519 + ChaCha20-Poly1305 roundtrip
python3 tests/verify-attestation-audit.py    # SEV-SNP report verification
python3 tests/verify-stripe-integration.py   # verify + capture + refund + topup
python3 tests/verify-sandbox-execution.py    # fuel + epoch interruption + output caps
```

## Security guarantees (the table to memorize)

| Attack | What stops it |
|---|---|
| Broker reads the skill | Encrypted to enclave pubkey; plaintext only inside the attested worker |
| Broker reads requester data | Encrypted to enclave pubkey; same mechanism |
| Broker copies the skill for later | Worker is ephemeral (or warm-pool with no per-job persistence); EFS inbox deleted per-job |
| Broker runs different code than approved | Skill_hash re-verified by the worker before wasmtime starts; mismatch is refused |
| Broker runs a different LLM | LLM call routed through the broker proxy; real API key never enters the worker |
| Broker sends results anywhere except the requester | Default-DENY OpenShell policy; only broker API in the allowlist |
| Broker stalls indefinitely | Fuel limit + epoch-interruption wall-clock cap, both enforced by wasmtime |
| Broker fabricates a successful result | broker_signature Ed25519 over (result_hash \| skill_hash \| input_hash); requester verifies both worker_signature and broker_signature |
| Broker overcharges you | Stripe captures only the actual cost (padded to BROKER_MIN_CAPTURE_CENTS=50); full audit trail in the job's payment block |
| Broker charges before the work runs | Stripe hold stays in `requires_capture` until `_finalize_job` runs |
| Broker keeps your money in a dispute | Stripe's standard dispute process; broker holds the signed result envelope and full audit trail as evidence |
| Past traffic is decrypted if a long-term key leaks | Per-job ephemeral X25519 keypair on both sides; no persistent keys |

## Resources

- **Live test broker:** `https://verdant.codepilots.co.uk` (demo mode, no real card charged)
- **Marketing site root:** `~/hermes/competition/tee-broker-site/`
- **Live broker source:** `~/hermes/competition/tee-broker-deploy/` (Python control plane + worker poller + NemoClaw install + OpenShell policy + Stripe bootstrap)
- **Rust reference:** `~/hermes/competition/tee-broker-pattern/` (protocol reference implementation; not what's deployed)
- **Pillar deep dives:** `/attestation`, `/security`, `/sandboxing`, `/payment`
- **For agents:** `/agents` — the operator setup and the test broker access
- **API reference:** `/docs` — every endpoint, every error code
- **Hackathon info:** https://hermes-agent.nousresearch.com/docs

## What to do when a human asks

If the human asks "what is VerdantForged" → explain in 30 seconds using the four pillars table above and link to the landing page.

If the human asks "is it really running" → `curl -sS https://verdant.codepilots.co.uk/healthz` to confirm liveness, then `/v1/discover` to show the attestation block and pricing.

If the human asks "is it secure" → point them at the security guarantees table above and the `/security` deep dive on the site.

If the human asks "how do I run a job" → walk them through the demo-path submit + poll loop above, then point at `/quickstart` for the production X25519 path.

If the human asks "how do I host my own broker" → point them at `/agents` (the "Run your own broker" section) and `tee-broker-deploy/README.md`.

If the human asks "what are the limits" → be honest. Known limitations for hackathon scope:

- `MIN_CAPTURE_CENTS=50` — Stripe's minimum capture, so small jobs round up to $0.50
- `TOPUP_TTL_MINUTES=10` — the shortfall window; if the requester doesn't top up, the job is abandoned and the PI is refunded
- Result TTL 24h, presigned download URL TTL 15min
- `max_fuel` per skill is capped at 10⁹; per-execution `max_duration_ms` at 600s
- Worker is a single m6a.xlarge per region; concurrent jobs serialize on the EFS inbox

The honest story sells better than overclaiming.