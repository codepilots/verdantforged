# VerdantForged Showcase Skills — 4 Showpieces for Competition Judges

**Date**: 2026-06-27  
**Hackathon**: NVIDIA x Stripe x Nous Research — deadline EOD 2026-06-30

## Design Philosophy

One showpiece skill per sponsor pillar, plus one cross-cutting skill that ties them together. Each skill must:
- Be demonstrable to a judge in under 60 seconds via curl
- Exercise a unique architectural feature of the TEE broker (not just "LLM in a box")
- Be implementable as a `prompt_template` skill (single-turn, no WASM runtime needed)
- Produce a verifiable artifact (signature, receipt, encrypted output) that proves the crypto works

---

## Skill 1: attestation-verifier (NVIDIA pillar — attestation)

**Name**: `attestation-verifier`  
**Pillar**: NVIDIA / AMD SEV-SNP attestation  
**Tagline**: "Prove the TEE is real — verify the attestation report"

**What it does**: Takes a broker's attestation report (from `/v1/discover`) and produces a human-readable verification verdict. Parses the SEV-SNP report structure, checks the measurement against a known-good value, verifies the cert chain signature, and returns a signed pass/fail result.

**Input**: JSON attestation block from `/v1/discover` (tee_type, measurement, report, cert_chain, chip_id, policy_hash)

**Process**:
1. Parse the attestation report (1184-byte SEV-SNP structure or instance_id_sha256 fallback)
2. Check measurement is not "stub-no-measurement" (real TEE verification)
3. If cert_chain present: verify VCEK → ASK → ARK signature chain (AMD root certs)
4. If policy_hash present: compare against expected OpenShell policy hash
5. LLM call to format a human-readable verdict ("VERIFIED: This broker runs on AMD SEV-SNP chip #xxx with measurement yyy")
6. Sign the verdict with Ed25519 (broker key)

**Output**: JSON with `verdict` (pass/fail), `details` (human-readable), `broker_signature` (Ed25519)

**Judge demo** (30 seconds):
```bash
# 1. Get the broker's attestation
curl https://verdant.codepilots.co.uk/v1/discover | jq .attestation

# 2. Submit it for verification
curl -X POST https://verdant.codepilots.co.uk/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "client_req_id": "verify-1",
    "encrypted_skill": "attestation-verifier",
    "encrypted_data": "<attestation JSON from step 1>",
    "requester_sig": "0x",
    "result_pubkey": "0x",
    "stripe_pi_id": "pi_demo_verify"
  }'

# 3. Poll for the verdict
curl https://verdant.codepilots.co.uk/v1/jobs/<job_id>
```

**Why judges care**: This is the "trust but verify" story. The broker doesn't just claim it runs in a TEE — anyone can verify the attestation cryptographically. NVIDIA's Remote Attestation Service is the foundation; this skill makes it tangible.

---

## Skill 2: token-receipt (Stripe pillar — payment)

**Name**: `token-receipt`  
**Pillar**: Stripe payment / billing  
**Tagline**: "Pay as token burns — every token accounted, every dollar signed"

**What it does**: Takes a completed job_id, looks up the actual LLM token usage from the broker's accounting tables, calculates the cost based on a transparent pricing model, and generates an itemized receipt signed with Ed25519. This mirrors Stripe's "Pay As Token Burns" billing model from Sessions 2026.

**Input**: A completed job_id

**Process**:
1. Query `llm_tokens` table for the job's token usage (prompt_tokens, completion_tokens, total)
2. Query `jobs` table for duration (started_at → finished_at)
3. Calculate cost:
   - Session lease: $0.20 per 15-minute slot (prorated)
   - Token cost: $0.001 per 1K tokens
   - Total = lease + tokens
4. LLM call to format a human-readable receipt
5. Sign the receipt with Ed25519 (broker key)
6. Include the stripe_pi_id from the original job

**Output**: JSON receipt with `job_id`, `token_breakdown`, `cost_breakdown`, `total_usd`, `stripe_pi_id`, `broker_signature`, `signed_at`

**Judge demo** (45 seconds):
```bash
# 1. Run a real job first
curl -X POST https://verdant.codepilots.co.uk/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "client_req_id": "demo-job-1",
    "encrypted_skill": "summarize",
    "encrypted_data": "The quick brown fox jumps over the lazy dog.",
    "requester_sig": "0x",
    "result_pubkey": "0x",
    "stripe_pi_id": "pi_demo_billing"
  }'

# 2. Wait for completion, then get the receipt
curl -X POST https://verdant.codepilots.co.uk/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "client_req_id": "receipt-1",
    "encrypted_skill": "token-receipt",
    "encrypted_data": "<job_id from step 1>",
    "requester_sig": "0x",
    "result_pubkey": "0x",
    "stripe_pi_id": "pi_demo_billing"
  }'

# 3. Poll for the signed receipt
curl https://verdant.codepilots.co.uk/v1/jobs/<receipt_job_id>
```

**Why judges care**: This is the "Machine Payments Protocol" story. Every token is accounted, every dollar is cryptographically signed. The receipt is non-repudiable — the broker can't deny how many tokens it consumed. This is Stripe's vision for agentic AI economy billing.

---

## Skill 3: blind-audit (Nous pillar — agent intelligence + crypto)

**Name**: `blind-audit`  
**Pillar**: Nous Research agent intelligence + cryptographic envelope  
**Tagline**: "Your code is encrypted — even the broker can't read it"

**What it does**: Receives source code encrypted to the worker's X25519 public key (obtained from `/v1/discover`), decrypts it inside the TEE, sends it through the LLM proxy for security analysis, encrypts the audit report to the requester's public key, and signs the result. The broker never sees the plaintext source code.

**Input**: Source code encrypted to the worker's X25519 ephemeral pubkey (from `/v1/discover.attestation.enclave_pubkey` or a dedicated endpoint)

**Process**:
1. Worker receives encrypted source code in `encrypted_data` field
2. Worker decrypts using its X25519 private key (inside the TEE — broker can't see this)
3. Worker sends decrypted code to LLM proxy with per-job token: "Perform a security audit of this code. Identify: (1) injection vulnerabilities, (2) authentication flaws, (3) crypto weaknesses, (4) input validation gaps. Rate severity 1-5."
4. LLM returns analysis via broker proxy
5. Worker encrypts the analysis to `result_pubkey` (X25519 + ChaCha20Poly1305)
6. Worker signs the result envelope with Ed25519
7. Broker forwards encrypted+signed result — broker cannot read the audit

**Output**: Encrypted audit report (base64 X25519+ChaCha20Poly1305), Ed25519 signature, result_hash

**Judge demo** (60 seconds):
```bash
# 1. Get the worker's public key
curl https://verdant.codepilots.co.uk/v1/discover | jq .attestation.enclave_pubkey

# 2. Encrypt your code to that key (client-side)
python3 -c "
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
import base64, os
# ... encrypt code to worker pubkey ...
"

# 3. Submit the blind audit
curl -X POST https://verdant.codepilots.co.uk/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "client_req_id": "audit-1",
    "encrypted_skill": "blind-audit",
    "encrypted_data": "<encrypted source code>",
    "requester_sig": "0x",
    "result_pubkey": "<your X25519 public key>",
    "stripe_pi_id": "pi_demo_audit"
  }'

# 4. Decrypt the result with your private key
curl https://verdant.codepilots.co.uk/v1/jobs/<job_id> | jq .result.result_encrypted
```

**Why judges care**: This is the "Confidential AI" story. The broker is a dumb relay — it routes encrypted envelopes but can never read the payload. The LLM call happens inside the TEE, the analysis is encrypted before it leaves. Even if the broker is compromised, the source code and audit report are protected. This is what NVIDIA's confidential computing is for.

---

## Skill 4: skill-discoverer (cross-cutting — marketplace + orchestration)

**Name**: `skill-discoverer`  
**Pillar**: Cross-cutting — agent marketplace, attestation verification, payment  
**Tagline**: "A dumb client walks into a TEE marketplace and walks out with a result"

**What it does**: Takes a natural-language task description, queries the broker's `/v1/discover` endpoint for available skills, uses the LLM to select the best matching skill, verifies the broker's attestation, and produces a pre-filled job submission JSON ready to execute. This demonstrates the full broker protocol: discover → verify → select → submit.

**Input**: Natural-language task description (e.g., "I need to review my Python web app for security issues")

**Process**:
1. Fetch `/v1/discover` to get the broker's attestation, supported skills, and pricing
2. Verify attestation is not "stub-no-measurement" (refuse if unattested)
3. LLM call: "Given these available skills: [list]. Which skill best handles this task: '<user request>'? Return the skill name and a brief rationale."
4. Format a pre-filled job submission JSON with the selected skill
5. Include the attestation verification result in the output
6. Include estimated cost based on pricing model

**Output**: JSON with `recommended_skill`, `rationale`, `attestation_verified`, `estimated_cost_usd`, `job_submission_template` (pre-filled JSON)

**Judge demo** (45 seconds):
```bash
# 1. Ask the marketplace what to do
curl -X POST https://verdant.codepilots.co.uk/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "client_req_id": "discover-1",
    "encrypted_skill": "skill-discoverer",
    "encrypted_data": "I need to audit my Python Flask API for security vulnerabilities",
    "requester_sig": "0x",
    "result_pubkey": "0x",
    "stripe_pi_id": "pi_demo_discover"
  }'

# 2. Get the recommendation + pre-filled submission
curl https://verdant.codepilots.co.uk/v1/jobs/<job_id>
# Returns: {"recommended_skill": "blind-audit", "attestation_verified": true, ...}
```

**Why judges care**: This is the "agent marketplace" story. A client with zero knowledge of the broker's capabilities can describe what it wants and get back a ready-to-execute job. It exercises the full protocol — discovery, attestation verification, skill matching, payment routing — in a single round-trip. This is what the Nous + Stripe + NVIDIA convergence looks like in practice.

---

## Publishing a showcase skill to the Skill Library (2026-06-29)

The standalone Skill Library service at `http://127.0.0.1:8091` (or wherever it's deployed) catalogs broker-compatible skills. Use `scripts/push_skills.sh` to push a skill folder into the library, optionally forwarding to the live broker in one step:

```bash
./scripts/push_skills.sh \
    --source-dir worker/skills/photo-glow-up \
    --library-url http://127.0.0.1:8091 \
    --api-key "$SKILL_LIBRARY_API_KEY" \
    --build --sync
```

The `--build` flag compiles a Rust crate (`Cargo.toml` present) to `wasm32-wasip1` release before upload; `--sync` forwards the registered card to the live broker via `/v1/library/skills/{ref}/sync-to-broker` (which calls `POST /v1/skills` and `POST /v1/skills/{name}/wasm`).

To browse from another agent or operator: install the companion skill at `~/.hermes/skills/devops/skill-library-browse/` and use its three scripts (`skill_library_list.sh`, `skill_library_install.sh`, `skill_library_download.sh`). See `docs/skill-library-deploy.md`.

---

## Implementation Notes

All 4 skills are `prompt_template` skills (no WASM needed). Each requires:
1. Register via `POST /v1/skills` (endpoint already exists and is wired)
2. Add to the poller's `skill_prompts` dict in `worker/poller.py`
3. Update `/v1/discover` `supported_skills` list in `daemon.py`
4. Write a judge-facing demo script (curl commands above)

**Priority order for implementation**:
1. `attestation-verifier` — simplest, pure LLM formatting, highest judge impact
2. `token-receipt` — exercises billing tables, Stripe story
3. `blind-audit` — exercises crypto envelope, most complex to demo
4. `skill-discoverer` — exercises full protocol, depends on others being registered

**Dependency**: The critical security fix task (t_c6beba80) should be done first — especially VULN-S2 (add auth to POST /v1/skills) and VULN-S3 (remove plaintext output) so the crypto story holds for blind-audit.