---
name: tee-broker-security
version: 1.1.0
description: Threat model, security audit checklist, and incident response for TEE Broker marketplace
category: tee-broker
tags: [tee-broker, security, threat-model, audit, incident-response, sev-snp, nemoclaw]
---

# Changelog

- **1.1.0 (2026-06-30):** Added "NemoClaw image trust chain" section covering the gap that the SEV-SNP measurement is of EC2 AMI initial memory, NOT of the NemoClaw Docker image (which lands at runtime). Three mitigation levels: operator-published measurement table, worker signs image digest with Ed25519 bound to SEV-SNP report, bake NemoClaw into AMI. Added corresponding pitfall #26. New reference file: `references/nemoclaw-image-trust-chain.md`.
- **1.0.0:** Initial release.

# Pitfall #26 — The NemoClaw image is outside the SEV-SNP measurement

The chip's `measurement` field (`report_bytes[144:192]`, SHA-384) is computed over the **EC2 AMI's initial memory contents at launch** — kernel, initramfs, services loaded before the chip took the measurement. It does NOT cover the **NemoClaw Docker image**, which `nemohermes` downloads from NVIDIA's CDN at runtime, AFTER the launch measurement is taken.

**Symptoms of the trap:**
- A requester pins `min_measurement` and verifies Step 5 of `/verify-attestation`, but the operator shipped a custom NemoClaw build, or a MITM swapped the image at download time. The SEV-SNP attestation says "all good" because the AMI is unchanged.
- A marketing site describes the enclave as "the NemoClaw instance" without disambiguating the *NemoClaw control-plane sandbox* (where the broker daemon runs in some deployments) from the *NemoClaw execution sandbox* (where per-LLM-job sandboxes spawn inside the worker). The two share a name but are at different trust levels and have different attestation surfaces.

**Fix pattern (deployed in `tee-broker-deploy`):**

1. After `nemohermes onboard`, capture `nemohermes --version`, the image name from `nemohermes list --json`, and the SHA-256 digest from `docker images --digests`. Write to `/opt/worker/.nemoclaw_metadata` on the worker. (~25 lines in `worker/user-data.sh`.)
2. Worker Ed25519 key (already bound to the SEV-SNP report via `worker_binding` HMAC in `report_data[:64]`) signs the bundle `version|digest|sandbox_name|enclave_pubkey|report_data[:64]`. Include the signed bundle in the result envelope's `sandbox` block. (~40 lines in `worker/poller.py`.)
3. Reviewer-side check: pull the same NemoClaw image locally (`docker pull nemoclaw/nemoclaw:VERSION`), compute the digest, fetch the worker's signed claim from the result envelope, verify the Ed25519 signature against the worker's published pubkey, and compare digests. Matches → worker pulled the image the reviewer expected. Mismatch → caught.

**Why the trust chain works:** the worker's Ed25519 key is generated and published in `worker-keys.json` at boot. The same record carries the worker's X25519 pubkey. The X25519 pubkey is bound to the SEV-SNP report via the `worker_binding` HMAC in `report_data[:64]`. The SEV-SNP report is signed by the AMD chip. The chip's signature chains to the VLEK/VCEK cert chain, which chains to AMD's ARK root pinned in the verifier's environment. The signature on `image_digest_sig` is therefore hardware-attested end-to-end: the same chip that measured the AMI also bound the worker's key, and that key signed the image digest.

**What this fix does NOT close:** the operator can still ship a custom NemoClaw build, publish a measurement for it, and call it "v0.7.2" — the requester is comparing against the wrong reference. Closing that gap requires a signed manifest from the image vendor (NVIDIA), which NemoClaw does not currently ship.

**Reference:** `references/nemoclaw-image-trust-chain.md` for the full analysis, including the reviewer-side verify snippet and the three mitigation levels.

# TEE Broker: Security & Threat Model

This skill documents the threat model, security invariants, audit requirements, and incident response procedures for the TEE Broker marketplace.

---

## Threat Model (STRIDE)

### Spoofing
| Threat | Mitigation |
|--------|------------|
| Fake broker announces false capabilities | BrokerAnnouncement signed, verifier_pubkey independent, heartbeat sigs |
| Fake provider publishes malicious skill | skill_code_hash verified at execution, reproducible builds required |
| Fake attestation with colluding verifier | verifier_pubkey bound in BrokerAnnouncement, verifier_sig over measurement |
| MITM on enclave_pubkey | enclave_pubkey_sig by broker in Quote (31998) |

**Residual Risk:** Compromised broker key → all broker events suspect. **Mitigation:** Key rotation procedure + subclaw governance removal.

### Tampering
| Threat | Mitigation |
|--------|------------|
| Provider swaps WASM after publishing hash | Broker verifies sha256(fetched_wasm) == skill_code_hash before every execution |
| Requester modifies input after hashing | input_hash in Intent, verified against attestation input_hash |
| Broker modifies output before hashing | output_hash in attestation, requester decrypts and verifies |
| Replay old Intent/Quote | nonce + created_at/expires_at in Intent, broker nonce window |

**Residual Risk:** Enclave memory corruption → wrong output_hash. **Mitigation:** SEV-SNP/TDX memory encryption + integrity.

### Repudiation
| Threat | Mitigation |
|--------|------------|
| Provider denies executing | ExecutionAttestation signed by broker, linked to Intent |
| Requester denies paying | ZapRequest on-chain, ZapReceipt with preimage |
| Broker denies receiving payment | HTLC on-chain, preimage reveals payment |
| Arbitrator denies ruling | Ruling published as Nostr event, auditable |

**Residual Risk:** Off-chain agreements not captured. **Requirement:** All material terms on-chain/Nostr.

### Information Disclosure
| Threat | Mitigation |
|--------|------------|
| Requester data leaked from enclave | X25519 encryption to enclave pubkey, no egress, wasmtime sandbox |
| Provider WASM reverse-engineered | WASM not published, only hash; enclave memory encrypted |
| Payment amounts correlated to jobs | MPP splits, variable timing, zap description generic |

**Residual Risk:** Side-channels (timing, memory access patterns). **Mitigation:** Constant-time WASM patterns, enclave isolation.

### Denial of Service
| Threat | Mitigation |
|--------|------------|
| Broker overwhelmed with Intents | Rate limiting, nonce verification, bond requirement for high-volume |
| Enclave exhausted (memory/CPU) | Resource bounds in SkillAnnouncement, wasmtime limits, queue_depth monitoring |
| MPP invoice flooding | 1-sat zap required for invoice, rate limits |
| Subclaw spam | Membership requires bond, admin approval |

**Residual Risk:** Hardware failure. **Mitigation:** Multi-region broker fallback.

### Elevation of Privilege
| Threat | Mitigation |
|--------|------------|
| Provider escapes enclave | SEV-SNP/TDX hardware isolation, wasmtime sandbox, no syscalls |
| Broker accesses requester plaintext | Enclave decrypts, broker only sees ciphertext |
| Arbitrator overrules valid execution | Ruling transparency, appeal to governance, arbitrator bond slashing |

### The NemoClaw image trust chain (2026-06-30)

**The gap this addresses:** the SEV-SNP report's `measurement` field (`report_bytes[144:192]`, SHA-384) is computed over the **EC2 AMI's initial memory contents at launch** — kernel, initramfs, services loaded before the chip took the measurement. It does NOT cover the **NemoClaw Docker image**, which `nemohermes` downloads from NVIDIA's CDN at runtime, AFTER the launch measurement is taken. The chip has no idea which NemoClaw image ended up running inside the worker.

**Why this matters for the threat model:** a requester who pins `min_measurement` and verifies Step 5 of `/verify-attestation` is verifying the *EC2 AMI*, not the *NemoClaw sandbox*. The operator (or a MITM on the nemohermes download) can ship any NemoClaw image, and the SEV-SNP attestation will say "all good" because the AMI is unchanged.

**Three mitigation levels — pick based on your threat model:**

| Level | What the requester checks | Catches | Does not catch | Operator cost |
|---|---|---|---|---|
| **1. Operator publishes a measurement table** | Pin the published `min_measurement` value for the (NemoClaw version, EC2 AMI id) combination you expect | Operator shipping an unapproved AMI | Operator publishing a measurement for a custom NemoClaw build and calling it "v0.7.2" (the requester is comparing against the wrong reference) | None — operator adds a row to the table per release |
| **2. Worker signs the NemoClaw image digest** (recommended) | Pin the `min_measurement` AND compare the worker-signed `nemoclaw_image_digest` against `docker pull nemoclaw/nemoclaw:VERSION && docker images --digests` | Operator pulling a different NemoClaw image than the one the requester expected; MITM on the nemohermes download | Same as level 1 — operator controls the tag and the reference | ~50 lines in `worker/user-data.sh` + `worker/poller.py` to capture and sign; one new test in `tests/verify-sandbox-execution.py` |
| **3. Bake NemoClaw into the AMI** (hardware-attested) | Pin the `min_measurement` — it now covers the NemoClaw image because the image is part of initial memory | Everything level 2 catches, plus: operator claiming "v0.7.2" with a custom build (because the chip measured the actual bytes) | Operator building a malicious NemoClaw from source and calling it v0.7.2 (the requester is comparing against a measurement the operator published; if the operator published a measurement for a malicious build, the requester pins the wrong value) | Modify Packer / AMI build to extract NemoClaw image into `/var/lib/docker` at build time; AMI grows by ~1–2 GB; rebuild on every NemoClaw bump |

**The honest limit (mitigation 3 included):** every check above assumes the requester is comparing against a *trusted reference* — a measurement table, a NemoClaw Docker image, a version string — that came from somewhere the requester already trusts. The only thing that closes that loop is a signed manifest from the image vendor (NVIDIA) — `sha256:abc123` is the NemoClaw v0.7.2 image, signed by NVIDIA's release key. NemoClaw does not currently ship such a manifest. Until it does, **the requester is trusting the operator's published references.** The mitigations meaningfully constrain what the operator can do (can't lie about the SEV-SNP measurement, can't have the worker pull a different image, can't swap the sandbox name out from under you), but they do not make the operator trust-free.

**Why this section is here and not in `tee-broker-pattern`:** the protocol/architecture skill describes the *design* of the enclave and the WASM execution model. This skill describes *what can go wrong* and *what's the residual trust*. The NemoClaw image trust chain is a threat-class question — the requester needs to know which check proves what, and which trust they're accepting. See `references/nemoclaw-image-trust-chain.md` for the full analysis (worker-side signature payload format, reviewer-side verification code, the `docker images --digests` vs EC2-Nitro-VMM-output distinction, and the future-NVIDIA-signed-manifest path).

---

## Security Invariants (Must Never Be Violated)

| # | Invariant | Enforcement |
|---|-----------|-------------|
| 1 | `skill_code_hash` in 31989 == `code_hash` in 31996 == sha256(executed_wasm) | Broker verification at execution |
| 2 | `verifier_pubkey` in 31991 == `verifier_pubkey` in 31992 | Requester validation |
| 3 | `verifier_sig` in 31991 valid over `tee_measurement` | Requester validation |
| 4 | `enclave_pubkey_sig` in 31998 valid over `enclave_pubkey` by broker key | Requester validation |
| 5 | `nonce` in 31997 unique per broker 24hr window | Broker enforcement |
| 6 | HTLC confirmed before enclave start | Broker enforcement |
| 7 | `output_hash` in 31991 == sha256(decrypted_output) | Requester verification |
| 8 | `quote_msats` = `broker_fee_msats` + `provider_receives_msats` | Quote validation |
| 9 | `bond_msats` ≥ governance minimum for active providers/brokers | Subclaw governance |
| 10 | All HTTPS URLs — no localhost, no HTTP | Client validation |

### EC2 worker pitfalls (hardening-deployment specific)

**IMDSv2 token required.** Modern EC2 instances (anything launched since ~2020)
default to IMDSv2. Any `curl http://169.254.169.254/latest/meta-data/...` from a
fresh worker returns **HTTP 401** unless you first `PUT` to `/latest/api/token`
and include the resulting token as `X-aws-ec2-metadata-token`. Symptoms: heartbeat
files show `"instance_id": "unknown"`, `"private_ip": "0.0.0.0"`, poller's SEV-SNP
measurement is `"stub-no-measurement"`. Fix pattern (shell + Python) is in
`references/cryptographic-hardening.md` §5.

**Per-job ephemeral tokens, not API keys, in the worker envelope.** If the
upstream API key (LLM, payment processor, model API) ever appears in
`/mnt/broker/logs/llm-api-key`, in user-data env exports, or anywhere the worker
can read, it's compromised — workers are untrusted and disposable. The standard
pattern is broker-side proxy + per-job ephemeral tokens with 10-min TTL.
Full pattern, including the trust boundary diagram, in
`references/cryptographic-hardening.md` §1.

---

## See Also

- `references/nemoclaw-image-trust-chain.md` — **The NemoClaw image is outside the SEV-SNP measurement (2026-06-30). The chip measures the EC2 AMI's initial memory; `nemohermes` downloads the NemoClaw Docker image at runtime, AFTER the measurement. Worker-side signing with the existing Ed25519 key (bound to the SEV-SNP report via `worker_binding`) is the practical mitigation. Includes the signature payload format, reviewer-side verify code, and the future NVIDIA-signed-manifest path.**
- `references/cryptographic-hardening.md` — implementation patterns for
  broker-side LLM proxies, X25519 + ChaCha20-Poly1305 result encryption,
  Ed25519 broker signatures, requester_sig verification, IMDSv2 token
  fetch, and live-broker test conventions
- `tee-broker-protocol` — Canonical kinds, tags, validation rules
- `tee-broker-negotiate` — Secure lifecycle implementation
- `tee-broker-find-broker` — Broker verification with security checks
- `tee-broker-offer-service` — Provider security requirements
- `tee-broker-task-design` — Schema security (injection prevention)

---

## Audit Requirements

### Pre-Mainnet Audit Checklist

#### Cryptographic
- [ ] All signatures use Nostr secp256k1 (schnorr)
- [ ] X25519 for enclave encryption (ECIES)
- [ ] No custom crypto implementations
- [ ] Key derivation follows BIP-341 (Taproot) or NIP-44
- [ ] Nonce generation uses CSPRNG (OS entropy)

#### TEE/Enclave
- [ ] SEV-SNP: AMD Milan/Genoa CPUs, firmware ≥ 0.17.0
- [ ] TDX: Intel Xeon Scalable Sapphire Rapids, microcode latest
- [ ] wasmtime version pinned, no dynamic linking
- [ ] Enclave memory encryption enabled
- [ ] No host syscalls from WASM (wasmtime `WasiCtx` only)
- [ ] Attestation verification uses official AMD/Intel libraries

#### Smart Contract / Lightning
- [ ] HTLC expiry > max_execution_time + buffer
- [ ] MPP routes validated (no circular routes)
- [ ] Preimage revelation atomic with attestation verification
- [ ] Fee calculation matches `fee_bps` in BrokerAnnouncement

#### Protocol
- [ ] All event kinds validated against `tee-broker-protocol` schema
- [ ] Replay protection tested (nonce collision, timestamp skew)
- [ ] Versioning enforced (major = new `d` tag)
- [ ] Dispute resolution tested (arbitrator selection, ruling execution)

#### Operational
- [ ] Multi-region broker deployment
- [ ] Automated heartbeat monitoring with alerts
- [ ] Incident response runbook documented
- [ ] Key rotation procedure tested
- [ ] Backup/restore for broker state tested

### Ongoing Audits
- **Weekly:** Heartbeat health, bond balances, dispute queue
- **Monthly:** Attestation verification success rate, slashing events
- **Quarterly:** Full protocol compliance review, dependency updates
- **Annually:** Third-party penetration test, TEE hardware recertification

---

## Incident Response Procedures

### Severity Levels

| Level | Criteria | Response Time | Escalation |
|-------|----------|---------------|------------|
| SEV-1 | Funds at risk, attestation forge, key compromise | 15 min | All admins, arbitrators, subclaw governance |
| SEV-2 | Broker offline > 5 min, verification failing | 1 hour | Broker ops, backup broker activation |
| SEV-3 | Performance degraded, queue backup | 4 hours | Broker ops |
| SEV-4 | Minor config issue, doc update | Next business day | Maintainer |

### SEV-1: Attestation Forgery / Key Compromise
```
1. DETECT: Invalid verifier_sig, verifier_pubkey mismatch, or broker key on wrong relay
2. CONTAIN: 
   - Publish DisputeEvent (31999) type="attestation_invalid"
   - Subclaw admins vote emergency removal (2/3 multi-sig)
   - Backup brokers activate (fallback strategy)
3. ERADICATE:
   - Rotate compromised keys
   - Re-verify all pending attestations
   - Slash broker bond (100%)
4. RECOVER:
   - New broker announces with new keys
   - Requesters re-negotiate in-flight jobs
   - Post-incident report published
```

### SEV-1: Provider Malicious WASM
```
1. DETECT: skill_code_hash mismatch at execution, output_hash mismatch
2. CONTAIN:
   - Broker halts provider's jobs
   - Publish DisputeEvent type="output_hash_mismatch"
3. ERADICATE:
   - Slash provider bond (100%)
   - Mark skill deprecated, publish successor
4. RECOVER:
   - Requesters refunded via HTLC expiry
   - New skill version deployed with verified build
```

### SEV-2: Broker Offline
```
1. DETECT: No heartbeat > 120s, health endpoint failing
2. CONTAIN:
   - Requesters failover to backup broker (pre-configured)
   - In-flight jobs: if HTLC not settled, wait for expiry/refund
3. ERADICATE:
   - Investigate root cause
   - Fix and restart
4. RECOVER:
   - Republish heartbeats
   - Verify attestation verifier operational
```

### SEV-2: Verifier Compromise
```
1. DETECT: verifier_sig valid but verifier_pubkey not in BrokerAnnouncement
2. CONTAIN:
   - Reject all attestations from compromised verifier
   - Broker rotates to backup verifier (pre-registered)
3. ERADICATE:
   - Update BrokerAnnouncement with new verifier_pubkey
4. RECOVER:
   - Re-verify recent attestations with new verifier
```

---

## Security Testing

### Unit Tests (CI Pipeline)
```bash
# Schema validation
cargo test validate_skill_announcement
cargo test validate_attestation
cargo test validate_quote
cargo test validate_intent

# Replay protection
cargo test nonce_collision_rejected
cargo test timestamp_skew_rejected

# Signature verification
cargo test enclave_pubkey_sig_verification
cargo test verifier_sig_verification
cargo test heartbeat_sig_verification

# Economic
cargo test bond_slashing_conditions
cargo test stake_weighted_reputation
```

### Integration Tests (Testnet)
```bash
# Full lifecycle
./testnet.sh full_lifecycle_alice_bob

# Dispute resolution
./testnet.sh dispute_output_hash_mismatch
./testnet.sh dispute_attestation_invalid

# Failover
./testnet.sh broker_failover

# MPP payments
./testnet.sh mpp_large_payment
```

### Fuzzing
```bash
cargo fuzz run parse_event
cargo fuzz run validate_schema
cargo fuzz run verify_attestation
```

### Chaos Engineering
- Kill broker mid-execution → verify HTLC refund
- Corrupt attestation → verify rejection
- Network partition → verify graceful degradation
- Clock skew → verify timestamp validation

---

## Secure Development Practices

1. **Dependencies:** `cargo audit` in CI, pinned versions, minimal deps
2. **Code Review:** 2-person review for all protocol changes
3. **Secrets:** No secrets in code, use env vars / vault
4. **Logging:** Structured logs, no PII, no private keys
5. **Updates:** Staged rollout (canary → 10% → 100%), rollback < 5 min

---

## Responsible Disclosure

- **Email:** security@tee-broker.org (PGP key on website)
- **Scope:** All tee-broker protocol implementations
- **Rewards:** Based on severity (SEV-1: up to 1M sats)
- **Timeline:** Acknowledge 24h, fix 90d, disclose after fix