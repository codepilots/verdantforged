# Trust model — what the six checks prove, and what they don't

This is the long-form explanation of the "what can the operator lie
about?" table on the marketing site's `/verify-attestation` page. It
exists so that an agent (or a curious human) can read one document and
understand exactly what residual trust they're accepting when they
choose to use a VerdantForged broker.

## The chain, in one picture

```
AMD ARK (pinned in AMD KDS, fetched live)
  └─ ARK signs AMD ASK
       └─ ASK signs VLEK (per-CPU-family cert)
            └─ VLEK signs the 1184-byte SEV-SNP report
                 └─ the report contains:
                      ├─ measurement  (SHA-384 of initial VM memory)
                      └─ report_data  (first 64 bytes = worker_binding HMAC)
                                      ↑
                                      hashes together:
                                        - WORKER_BINDING_DOMAIN (a constant)
                                        - enclave_pubkey (the worker's X25519 pubkey)
                                        - policy_hash   (the broker's OpenShell policy SHA-256)
                                      So the chip itself attests: "this report came
                                      from a worker with THIS pubkey, under THIS policy."
                                      └── worker X25519 pubkey encrypts inbound input
                                      └── worker Ed25519 key signs result envelopes
                                           (and, post-Plan-2, the NemoClaw image digest)
```

Walking this chain end-to-end is what Check 3 does. Each link is
verified by a separate check; if any link breaks, the verdict is FAIL.

## Per-check analysis

### Check 0 — `attestation_source` is real

**Proves:** The SEV-SNP report was read from a real AMD chip, not a
placeholder. The accepted sources are `tsm_configfs` (the modern Linux
TSM configfs interface, kernel ≥ 6.17 on Ubuntu 24.04) and `snpguest`
(the older AMD-provided tool).

**Doesn't prove:** Anything about *which* chip, or what software it ran.
Those are separate checks.

**Can the operator lie?** No. The `attestation_source` field is set by
`sev_snp.py` based on what the kernel reports, and the report is
included in the signed body of the SEV-SNP quote. Changing the source
field would invalidate the AMD signature (Check 2 would fail).

### Check 1 — Report is 1184 bytes

**Proves:** The report is the right size per the AMD SEV-SNP ABI.

**Doesn't prove:** Anything about the report's contents. A 1184-byte
random string would also pass this check.

**Can the operator lie?** No — the length is intrinsic to the wire
format, and the signed body must be the first 672 bytes for Check 2
to succeed.

### Check 2 — SNP signature verifies against the leaf cert

**Proves:** The AMD chip signed the first 672 bytes of the report, and
the leaf cert in `cert_chain` contains the matching public key. Without
this, the report could have been forged.

**Doesn't prove:** That the leaf cert is genuine (it could be a
self-signed cert the operator made). That's Check 3.

**Can the operator lie?** No. The signature is over the report body;
the public key is in the leaf cert. The math is unforgeable without
the chip's private key (which is fused into the silicon and never
extractable).

### Check 3 — Leaf cert chains to AMD ARK

**Proves:** The leaf cert was issued by AMD. The chain is fetched live
from `kdsintf.amd.com` (the AMD Key Distribution Service), so the
reviewer doesn't need to pin any AMD root cert in advance — they
implicitly trust AMD's KDS endpoint at the time of the check.

**Doesn't prove:** That the *specific* chip (identified by `chip_id`)
is the one the operator claims. The VLEK cert is per-CPU-family, not
per-CPU. To pin a specific chip, the reviewer would need to compare
the live `chip_id` to a known-good value. This skill does not yet
implement that check (TODO).

**Can the operator lie?** No — the chain is fetched from AMD, and the
signature is over the leaf cert. The operator can't present a
self-signed cert as if it were AMD-issued.

**Subtle:** the chain is fetched live, so the trust root is "AMD's KDS
is currently serving the right ARK." If you want to pin the AMD ARK
key in your environment for stricter trust (e.g. air-gapped review),
you'd need to extend `_verify_chain` to compare the root cert's
fingerprint to a pinned value. This skill does not yet implement that
(TODO).

### Check 4 — `worker_binding` HMAC in `report_data`

**Proves:** The worker's X25519 pubkey and the broker's OpenShell
policy hash are bound to the SEV-SNP report. An attacker who can
replay the report from a different worker (with a different key) or
under a different policy will fail this check.

**Doesn't prove:** That the worker is running the *right* NemoClaw
version, or that the OpenShell policy is the *right* policy for your
workload. The policy is operator-controlled; you have to read it
yourself and decide if you trust it.

**Can the operator lie?** About the policy, yes. The operator picks
which OpenShell policy to hash into `report_data`; the chip attests
to *some* policy, but the reviewer has to read it and decide if it's
acceptable. About the binding, no — if the operator changes the policy
or the worker's key, the HMAC changes, and Check 4 fails.

**Live-broker note:** the current VerdantForged broker at
`verdant.codepilots.co.uk` returns an empty `policy_hash` field,
which causes Check 4 to fail. This appears to be because the broker
isn't shipping `openshell/policy.yaml` at the expected path
(`/opt/broker-daemon/openshell/policy.yaml`). The Check 4 error
message suggests exactly this — see the script output. Once the
broker is fixed (which the operator has noted is in progress), Check
4 should pass.

### Check 5 — `measurement` matches pinned value

**Proves:** The worker booted an image whose initial memory hashes to
the value you pinned. If the operator publishes `min_measurement =
M0.7.2` for a NemoClaw v0.7.2 worker AMI, and the live broker shows
`measurement = M0.7.2`, then the worker booted that exact AMI.

**Doesn't prove:** That the AMI is the *genuine* NemoClaw v0.7.2
(operator could have shipped a custom AMI with a different `min_measurement`
and called it v0.7.2). The measurement binds the binary, not the
provenance.

**Can the operator lie?** About the AMI's provenance, yes. About
whether the live worker matches the published AMI, no — the chip
attests to the measurement, and the operator's published table is
what the reviewer pins against.

**Skipped by default:** if `--pinned-measurement` is not provided,
this check is reported as `optional` and does not affect the verdict.
A reviewer who's just checking "is this broker *attesting* to a real
report" doesn't need a pinned value; a reviewer who's checking
"is the worker running an image I trust" does.

### Check 6 — NemoClaw Docker image digest signed by worker

**Proves:** The worker pulled the NemoClaw Docker image whose digest
matches what the reviewer pulled locally. Combined with a local
`docker pull + docker images --digests`, the reviewer can verify
they got the same image.

**Doesn't prove:** That the NemoClaw image is the *genuine* NVIDIA
build of the claimed version. The image hash binds the binary; the
version string is a claim. An operator who ships a custom NemoClaw
build can publish its hash and call it v0.7.2 — the reviewer would
need a separate signed manifest from NVIDIA to detect this.

**Can the operator lie?** About the version string, yes. About the
image hash, no — the chip-signed `worker_binding` anchors the worker's
Ed25519 key, which signed the bundle, which contains the image hash.

**Requires Plan 2 to be deployed.** Currently the live broker does
not populate `result.sandbox.image_digest_sig`, so this check returns
optional-FAIL until Plan 2 ships. The skill handles this gracefully
(it's an optional check, doesn't affect the verdict).

## Summary table

| Check | What it catches | What it doesn't catch | Operator-trust-dependent? |
|-------|-----------------|------------------------|---------------------------|
| 0     | Stub/no-attestation, modified `attestation_source` field | — | No |
| 1     | Truncated/corrupt report | Random-byte 1184-byte payload | No |
| 2     | Forged report | — | No |
| 3     | Forged leaf cert, AMD chain not walked | Per-chip pinning (TODO), AMD ARK pinning (TODO) | KDS endpoint trust |
| 4     | Replay across workers, policy swap | Whether the policy is the *right* policy for the workload | Policy semantics |
| 5     | Worker booted a different AMI | Whether the pinned AMI is the *genuine* official image | Operator's published table |
| 6     | Worker pulled a different NemoClaw image | Whether the NemoClaw image is the *genuine* NVIDIA build | Operator's published tag |

## What it would take to close the residual trust

| Gap | What would close it | Who can do it |
|-----|---------------------|---------------|
| AMD ARK not pinned in verifier environment | Add ARK fingerprint check to `_verify_chain` | This skill (TODO) |
| `chip_id` not pinned to a specific known-good chip | Add `chip_id` comparison to a known-good set | This skill (TODO) |
| OpenShell policy not audited by reviewer | Reviewer reads `policy.yaml` (or its published hash) and decides if it's acceptable | Reviewer (manual) |
| Worker AMI provenance | AMD-KDS-style signed image manifest from the operator, OR reproducible AMI builds | Operator (out of our control) |
| NemoClaw image provenance | NemoClaw-signed image manifest (Docker Content Trust / Notary v2) | NVIDIA (out of our control) |
| NemoClaw version baked into the chip measurement | Bake NemoClaw into the EC2 AMI so it's part of initial memory | Operator (Plan 2's "Option 2") |

The bottom row of the gap table is the one the deploy-side Plan 2
addresses: with the worker signing the image digest, the reviewer
gets a self-verifiable chain (image hash bound to the chip-signed
worker key). The remaining "NemoClaw ships a signed manifest" gap
requires NVIDIA cooperation and is out of scope for our deploy.

## When the operator is fully trust-free

Never, in this design. Even with all six checks passing, the reviewer
is trusting:

1. AMD's KDS endpoint (Check 3)
2. The operator's published `min_measurement` (Check 5)
3. The operator's published `policy_hash` (Check 4)
4. The operator's published NemoClaw version tag (Check 6)
5. NemoClaw itself to not be malicious (no mitigation)
6. The EC2 firmware to be honest (no mitigation; standard TEE trust)

A reviewer who doesn't want to trust items 2-4 should run their own
worker (same AMI, same NemoClaw version) and compare the
attestation to the broker's. The checks then become a sanity check
that the broker is actually running what the reviewer would run.
