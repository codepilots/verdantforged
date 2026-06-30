---
name: verdantforged-verify-attestation
version: 0.1.0
description: Verify a VerdantForged broker's attestation end-to-end. Fetches /v1/discover, runs the 5 SEV-SNP checks (attestation_source, report length, SNP signature vs leaf cert, chain to AMD ARK, worker_binding HMAC) plus optional Check 6 (NemoClaw Docker image digest signed by worker's Ed25519 key). Prints a single VERDICT: PASS/FAIL line and exits 0/1. Use when a human or another agent asks "is this broker trustworthy," "verify the attestation," "check the SEV-SNP report," or before sending sensitive data to a broker you haven't audited yet.
category: verdantforged
tags: [tee-broker, verdantforged, attestation, sev-snp, amd, kds, ed25519, nemoclaw, verifier]
metadata:
  hermes:
    related_skills: [tee-broker, tee-broker-deploy-config, tee-broker-marketplace-ux]
    changelog:
      - 0.1.0 (2026-06-30) — Initial release. Six checks, single Python script, AMD VLEK chain verification, optional NemoClaw image digest check.
---

# VerdantForged attestation verifier

A self-contained verifier that walks a reviewer (human or agent) through
the six checks documented at the marketing site's `/verify-attestation`
page. Produces a single `VERDICT: PASS` or `VERDICT: FAIL (check N: ...)`
line and exits 0 or 1, so it can be wired into CI / scheduled jobs /
agent workflows without parsing output.

## When to use this skill

Use this skill when:

- A human asks "is this broker trustworthy?" or "verify the attestation"
- An agent is about to call a broker it hasn't audited yet
- A scheduled job runs weekly to check the broker is still publishing the
  expected attestation
- You're debugging why a result envelope's worker_signature isn't matching
  expectations and want to confirm the worker's attestation is intact
- The deploy side just rolled a new worker and you want to confirm the new
  AMI is producing the expected measurement

Do NOT use this skill when:

- The user wants to *use* the broker (submit a job) — that's a separate
  skill, not in this category yet
- The user wants to run the broker's own tests/verify-*.py suite — those
  are broker-side tests, not verifier-side checks
- The user wants to check the broker's pricing, SLA, or business terms —
  this skill only checks the cryptographic attestation

## What you actually do

Run the script:

```bash
python3 ~/.hermes/skills/verdantforged/verify-attestation/scripts/verify.py \
    https://verdant.codepilots.co.uk
```

The script:

1. Fetches `GET /v1/discover` from the broker
2. Runs six checks (see below)
3. Prints `VERDICT: PASS` or `VERDICT: FAIL (...)` on the last line
4. Exits 0 on pass, 1 on any required-check failure, 2 on invocation error

The script is self-contained. It needs `cryptography >= 38` and `requests`,
which are standard in the agent's venv. No broker credentials, no
NemoClaw access, no AWS access required.

## The six checks

| # | Name | Required? | What it proves |
|---|------|-----------|----------------|
| 0 | `attestation_source` is real | Required | The report came from a real SEV-SNP source (`tsm_configfs` or `snpguest`), not a placeholder stub |
| 1 | Report is 1184 bytes | Required | The SEV-SNP report is the right size per the AMD spec |
| 2 | SNP signature verifies against the leaf cert | Required | The AMD chip signed the report and the leaf cert in `cert_chain` matches |
| 3 | Leaf cert chains to AMD ARK root | Required | The cert chain walks back to a known AMD root (fetched live from `kdsintf.amd.com`) |
| 4 | `worker_binding` HMAC in `report_data` | Required | The worker's X25519 pubkey + the broker's `policy_hash` are bound to the report (proves the worker's pubkey hasn't been swapped out) |
| 5 | `measurement` matches pinned value | Optional (only if `--pinned-measurement` given) | The worker booted an approved image (only catches a different *measurement*; a different *version* with a different measurement is also caught) |
| 6 | NemoClaw Docker image digest signed by worker | Optional (only if `--with-result-envelope` given) | The worker pulled the same NemoClaw image the reviewer pulled (only catches a different *image*; doesn't catch operator-shipping-a-custom-NemoClaw-and-calling-it-v0.7.2) |

The full check definitions and source-code line numbers are at the
marketing site's `/verify-attestation` page.

## Common usage patterns

### 1. Basic verification (no pinned value, no envelope)

```bash
python3 ~/.hermes/skills/verdantforged/verify-attestation/scripts/verify.py \
    https://verdant.codepilots.co.uk
```

Runs Checks 0-4. Prints `VERDICT: PASS` if the broker is publishing a
real SEV-SNP attestation with a valid chain. Exits 0 on pass.

### 2. With a pinned measurement

```bash
python3 ~/.hermes/skills/verdantforged/verify-attestation/scripts/verify.py \
    https://verdant.codepilots.co.uk \
    --pinned-measurement abc123...   # 96-hex SHA-384
```

Pins the expected `measurement` to a value you trust (e.g. from the
operator's published release). If the live worker booted a different
image, the measurement won't match and the script will fail Check 5.

### 3. With a result envelope (Check 6)

```bash
python3 ~/.hermes/skills/verdantforged/verify-attestation/scripts/verify.py \
    https://verdant.codepilots.co.uk \
    --with-result-envelope envelope.json
```

Pass the JSON of a completed-job result envelope. The script verifies
that `envelope.sandbox.image_digest_sig` is a valid Ed25519 signature
from `/v1/discover.attestation.worker_ed25519_pubkey`, over `(version |
image_digest | sandbox_name | enclave_pubkey | report_data[:128])`.
Combined with a local `docker pull + docker images --digests`, this proves
the worker pulled the same NemoClaw image you pulled.

### 4. From CI / cron

```bash
if ! python3 ~/.hermes/skills/verdantforged/verify-attestation/scripts/verify.py \
        https://verdant.codepilots.co.uk > /var/log/verifier.log 2>&1; then
    pagedowngrade # or whatever your alerting looks like
fi
```

The script's exit code is the verdict; the log is human-readable
context for the alert.

## Output

The script prints colored output if stdout is a TTY (disable with
`NO_COLOR=1`). Each check prints one line:

```
  ✓ Check 0 — attestation_source is real (source=tsm_configfs)
  ✓ Check 1 — SNP report is 1184 bytes
  ✓ Check 2 — SNP signature verifies against leaf cert
  ✗ Check 3 — VLEK/VCEK chains to AMD ARK — leaf cert does not chain to AMD ARK via Milan
  ! Check 4 — worker_binding HMAC in report_data (optional) — missing field(s): policy_hash
```

The last line is always `VERDICT: PASS` or `VERDICT: FAIL (check N: ...)`,
suitable for grep / awk in scripts.

## What this skill does NOT do

- **It does not decide whether to USE the broker.** That's a policy
  decision. This skill tells you what you can verify; whether to trust
  the operator's published references is your call.
- **It does not check the broker's pricing, SLA, or business terms.**
- **It does not run the broker's tests/verify-*.py suite** (those are
  broker-side tests, not verifier-side checks).
- **It does not submit jobs to the broker.** That's a separate skill.
- **It does not handle pinned-key trust anchors for AMD ARK.** AMD ARK
  is fetched live from `kdsintf.amd.com` each run, so the trust root
  is whatever that endpoint currently serves. If you want to pin the
  AMD ARK key in your environment for stricter trust, you'll need to
  extend the script's `_verify_chain` to also check the root cert
  against a pinned fingerprint (TODO: not implemented yet).

## Trust model

The full "what can the operator lie about?" analysis is in
`references/trust-model.md`. The short version:

- **Check 0-3** are objective: chip-signed report, AMD-rooted chain.
  An operator who controls the broker cannot forge these without
  compromising AMD's signing infrastructure.
- **Check 4** (worker_binding) is objective: the worker's X25519 key
  is bound to the report via the HMAC, so a report from a different
  worker (with a different key) won't match.
- **Check 5** (pinned measurement) is operator-trust-dependent: the
  pinned value comes from the operator. If you trust the operator's
  published measurement, you can verify any worker matches it.
- **Check 6** (NemoClaw image digest) is operator-trust-dependent: the
  version tag is a claim; the image hash binds the binary but doesn't
  prove "this is the genuine NVIDIA build of v0.7.2."

The honest limit: until NemoClaw ships a signed image manifest, the
requester is trusting the operator's published references. The
six checks constrain what the operator can do (they can't lie about
the SEV-SNP measurement, can't have the worker pull a different
image, can't swap the sandbox name out from under you), but they do
not make the operator trust-free.

## Files

- `scripts/verify.py` — the actual verifier (single-file Python script)
- `references/trust-model.md` — the "what can the operator lie about?"
  matrix and explanations
- `SKILL.md` — this file
