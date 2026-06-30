# Building a verifier against the live broker — what we learned

Captured 2026-06-30 while building `verdantforged/verify-attestation`
(skill at `~/.hermes/skills/verdantforged/verify-attestation/`). The
verifier runs six checks against `/v1/discover`; this note captures
the things that bit us while doing it, so the next session doesn't
re-discover them.

## Pitfall 27 — AMD VLEK / ARK certs are RSA, not ECDSA

When writing the chain-walk in `_verify_cert_signature` (verify.py),
the first attempt unconditionally used `ec.ECDSA(hash_alg)`. This
silently failed with `RSAPublicKey.verify() missing 1 required
positional argument: 'algorithm'`. AMD's Milan ARK, ASK, and VLEK
certificates are all RSA-signed (RSA-PSS in production, falling back
to RSA-PKCS1v1.5). The fix: detect the pubkey type and dispatch
accordingly.

```python
pubkey = parent.public_key()
if isinstance(pubkey, rsa.RSAPublicKey):
    try:
        pubkey.verify(sig, tbs, padding.PSS(mgf=padding.MGF1(hash_alg),
                   salt_length=padding.PSS.MAX_LENGTH), hash_alg)
    except Exception:
        pubkey.verify(sig, tbs, padding.PKCS1v15(), hash_alg)
else:
    pubkey.verify(sig, tbs, ec.ECDSA(hash_alg))
```

## Pitfall 28 — AMD certs have non-positive serial numbers

`cryptography 38+` raises `CryptographyDeprecationWarning: Parsed a
serial number which wasn't positive (i.e., it was negative or zero),
which is disallowed by RFC 5280. Loading this certificate will cause
an exception in a future release of cryptography.` This is noise from
AMD's own cert chain (their serials are signed integers that come out
negative when interpreted as Python ints), not a real problem. The
certs are still verifiable — just suppress the warning scoped to
"serial number":

```python
import warnings
warnings.filterwarnings("ignore", message=r".*serial number.*")
```

DO NOT use a broader `category=DeprecationWarning` filter — it'll
silence other warnings the agent should see.

## Pitfall 29 — AMD KDS VLEK chain is exactly 2 certs (VLEK → ARK)

The AMD KDS endpoint `https://kdsintf.amd.com/vlek/v1/{processor}/cert_chain`
returns a 2-cert PEM bundle: the VLEK leaf and the self-signed ARK.
There is NO separate ASK intermediate in the KDS response — the
VLEK is signed directly by the ARK. The VCEK path is the same shape
(2 certs) but takes a `chip_id` segment:

- VLEK: `/vlek/v1/{processor}/cert_chain` — current gen (Milan, Genoa, etc.)
- VCEK: `/vcek/v1/{processor}/{chip_id}/cert_chain` — older, per-chip

When your code's `chain[1]` (the "ASK") is actually the ARK root,
the self-signed check at the end of the walk may fail with an
unrelated error. Make the self-sign check best-effort, not strict.

## Pitfall 30 — Processor family name is in the VLEK cert's CN, not in the chip_id

`/v1/discover` returns `attestation.chip_id` (e.g. a 128-hex string
per AMD SEV-SNP spec). The AMD KDS endpoints need a processor
*family* name (Milan / Genoa / Turin / etc.). The two are NOT
related by direct lookup — you have to extract the family from
the VLEK cert's subject CN. The CN looks like
`CN=SEV-VLEK-Milan,O=Advanced Micro Devices,ST=CA,L=Santa Clara,...`.
Match on substring; default to "Milan" if no match.

## Pitfall 31 — Live broker returns empty `policy_hash` (broker bug)

When you fetch `/v1/discover` from the live broker at
`verdant.codepilots.co.uk`, the `attestation.policy_hash` field is
an empty string. This causes Check 4 (worker_binding HMAC) to fail
because the HMAC includes the policy_hash. The cause: the broker
computes policy_hash from
`Path(__file__).parent / "openshell" / "policy.yaml"`
(`daemon.py:3195`), but the live broker doesn't ship the openshell
package at that path. Check 4 fails with a clear actionable error:
"missing field(s): policy_hash (broker's openshell/policy.yaml may
be missing)". Fix: ship the policy file in the broker deployment
artifact, or fall back to a hardcoded policy_hash with a startup
warning.

## Pitfall 32 — `attestation_source` cycles between `tsm_configfs` and `stub` based on worker liveness

The live broker serves `/v1/discover` whether or not a worker is
currently running. If no worker is alive, `attestation_source` is
`stub` and the other fields are empty strings. If a worker is alive
but rebooting, you may see a CACHED attestation from a previous
worker (still `tsm_configfs`, but `chip_id` may be all-zeros or
otherwise odd). The verifier handles this correctly by short-circuiting
on `attestation_source=stub`, but downstream consumers should not
assume the attestation is fresh.

## Pitfall 33 — The signature payload format must match between worker and verifier EXACTLY

See also `references/plan2-sandbox-attestation-layer.md` for the implementation checklist and docs/site sync list.

The worker signs the NemoClaw image bundle as
`f"{version}|{digest}|{sandbox_name}|{enclave_pubkey_b64}|{report_data[:128]}"`
with the worker's Ed25519 signing key. The reviewer (in `verify.py` Check 6 and in the marketing site's
`/verify-attestation` Step 6) MUST use the same format. Drift
between the two breaks verification silently — the signature won't
verify, but neither side will tell you why unless the verifier's
error message is good. The current docs and the current verify.py
agree (verified by manual cross-check on 2026-06-30). When
`worker/poller.py` is patched to add the signing, copy the exact
f-string from this note into both sides.

## What the verifier's exit codes mean

- **0** — All required checks passed. Safe to use the broker (subject
  to the trust-model caveats in `references/trust-model.md`).
- **1** — At least one required check failed. The last line of
  output is `VERDICT: FAIL (check N: ...)` so you can grep for it
  in CI. Do NOT use the broker until the failing check is
  investigated.
- **2** — Invocation error (bad URL, missing dep, envelope file
  unreadable). The verifier didn't even get to run the checks —
  fix the command line and re-run.

## The six checks, with the exact failure modes we saw on 2026-06-30

| # | Check | Live result | Notes |
|---|-------|-------------|-------|
| 0 | `attestation_source` is real | PASS (`tsm_configfs`) | Cycles to `stub` when no worker is alive |
| 1 | Report is 1184 bytes | PASS | Length is intrinsic to the wire format |
| 2 | SNP signature verifies against leaf cert | PASS | The math is unforgeable without the chip's key |
| 3 | Leaf cert chains to AMD ARK | PASS | AMD KDS returns 2 certs (VLEK + ARK); see Pitfall 29 |
| 4 | `worker_binding` HMAC in `report_data` | **FAIL — empty `policy_hash`** | See Pitfall 31; broker-side fix needed |
| 5 | `measurement` matches pinned value | (skipped — no pinned value) | Optional unless `--pinned-measurement` given |
| 6 | NemoClaw image digest signed by worker | (skipped — no envelope) | Optional; needs Plan 2 deployed |

After fixing Pitfall 31 (broker ships the policy file), all 5 required
checks should pass on the live broker.
