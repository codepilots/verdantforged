# Plan 2 attestation layer — NemoClaw image digest signature

Plan 2 (`PLAN_2_DEPLOYMENT.md` in the competition repo) extends the broker's
attestation from "the SEV-SNP report is real" (Check 0-4) to "the worker
actually pulled the NemoClaw Docker image it claims" (Check 6). The chain:

    SEV-SNP report → report_data[:128] → image_digest_sig
        → nemoclaw_image_digest → reviewer-verified local `docker pull`

## Files involved

| File | What it does |
| --- | --- |
| `worker/user-data.sh` (step 4b) | Captures NemoClaw version + Docker image + digest to `/opt/worker/.nemoclaw_metadata` |
| `worker/poller.py` (`_read_nemoclaw_metadata`, `_read_report_data_hex`, `_sign_image_digest_bundle`) | Reads metadata at job time, builds the canonical payload, signs with the worker's Ed25519 key |
| `worker/poller.py` (`publish_worker_keys`) | Publishes X25519 + Ed25519 pubkeys + the 3 NemoClaw fields to `/mnt/broker/logs/worker-keys.json` |
| `broker-daemon/daemon.py` (`/v1/discover`) | Exposes `worker_ed25519_pubkey`, `nemoclaw_version`, `nemoclaw_image`, `nemoclaw_image_digest` in `attestation` |
| `broker-daemon/static/openapi.json` | Schema for the new attestation fields |
| `deploy.sh` (step 4.5) | Refreshes metadata on already-running workers via boto3 SSM (existing workers do NOT re-run user-data) |
| `tee-broker-site/src/pages/verify-attestation.astro` | Marketing-side instructions for fetching `worker_ed25519_pubkey` |

## Canonical signature payload (MUST match verifier exactly)

```
f"{version}|{digest}|{sandbox_name}|{enclave_pubkey_b64}|{report_data_hex[:128]}"
```

- `version`         — NemoClaw version string (or "unknown")
- `digest`          — sha256 digest with `sha256:` prefix
- `sandbox_name`    — `result.sandbox.name` ("worker")
- `enclave_pubkey_b64` — BASE64 X25519 pubkey as published in `/v1/discover` (NOT raw 32 bytes)
- `report_data_hex[:128]` — full 64-byte report_data hex (128 chars)

Earlier versions of the code signed only `report_data[:64]` (32-byte half) —
this was a **mismatch with the verifier**, which uses `:128`. The fix is in
`worker/poller.py` `_sign_image_digest_bundle`; the same value is consumed by
`~/.hermes/skills/verdantforged/verify-attestation/scripts/verify.py` Check 6
and by the marketing site page.

## Hardening the metadata capture (user-data.sh step 4b)

Three failure modes seen live:

1. **Sandbox name, not the OpenShell image.** Earlier capture used `nemohermes list`
   or the sandbox name string. That returns `worker`, not the actual image. Fix:
   resolve via Docker labels:
   ```
   docker ps -a --filter "label=openshell.ai/sandbox-name=$NEMOCLAW_SANDBOX_NAME" \
     --format '{{.Image}}'
   ```
2. **Bad JSON.** `cat <<EOF` with embedded special chars (e.g. operator tokens)
   can break parsing. Fix: write the file with `python3 json.dumps`.
3. **Missing digest.** `docker images --digests` lists all images; the
   OpenShell-derived image may not have a RepoDigest if pulled by tag. Use
   `docker image inspect <image> --format '{{range .RepoDigests}}{{println .}}{{end}}'`,
   fall back to `{{.Id}}`.

## Why `image_digest_sig` can be empty even when the signature code is correct

`_sign_image_digest_bundle` short-circuits and returns `""` when any input
is `"unknown"` or empty. Inputs:

- `nemoclaw_version` and `nemoclaw_image_digest` come from `/opt/worker/.nemoclaw_metadata`
- `report_data_hex` comes from `/mnt/broker/logs/worker-attestation.json`
- `enclave_pubkey_b64` comes from the in-memory X25519 keypair

**If the worker is not booted with SEV-SNP enabled** (i.e. `attestation_source`
in `/v1/discover` is `stub`), `worker-attestation.json` is missing/empty,
`report_data_hex` is `""`, and the signature is empty. This is **fail-closed
correctness**, not a bug — there is nothing meaningful to sign without a real
report. The same condition makes Check 0 of the verifier fail.

To actually pass Check 6 end-to-end, the worker needs:

- An SEV-SNP-capable AMI (m6a.*/c6a.* in eu-west-1) booted with `AmdSevSnp=enabled`
  in `CpuOptions` (verify via `ec2 describe-instances --query '... CpuOptions.AmdSevSnp'`)
- A TEE-aware kernel that can run `snpguest` or read `/sys/kernel/config/tsm/`
- The NemoClaw Docker image baked into the AMI, so the digests match between
  the worker's report and a reviewer's local `docker pull`

The local AMI cache pattern (`verdantforged-nemoclaw-gold-worker-...`) is a
`create-image` of a known-good worker; `ami-099e2272620073023` in the deploy
script is a placeholder that gets overwritten whenever a fresh worker is baked.

## Verifying the signature layer without a TEE

You can prove the Plan 2 layer is correct even on a stub worker by:

1. Reading `/v1/discover.attestation.worker_ed25519_pubkey`
2. Reading `/mnt/broker/logs/worker-keys.json` (the same pubkey, persisted)
3. Constructing the canonical payload manually with the same fields
4. Verifying with `cryptography`'s Ed25519 verify

This does NOT prove the worker is in a TEE — that requires Check 0 — but it
proves the *binding* between the worker's Ed25519 identity and the NemoClaw
metadata is correctly signed and verifiable.

## Deploy-time fix for already-running workers

`./deploy.sh` now has a step 4.5 that finds broker-managed workers by tag
(`Project=verdantforged`, `ManagedBy=broker-daemon`, `Role=tee-worker`), filters
to SSM-online ones, and runs a Python SSM command that:

1. Resolves the OpenShell container via `docker ps -a --filter label=openshell.ai/sandbox-name=...`
2. Captures `nemohermes --version`
3. Writes `/opt/worker/.nemoclaw_metadata` (and `/mnt/broker/logs/worker-keys.json`
   with the same fields)
4. Uses boto3 SSM (NOT `aws ssm send-command`, which has a known local CLI
   bug — `badly formed help string` on aws-cli 2.31.35 + Python 3.14)

Disable with `REFRESH_EXISTING_WORKERS=0` if the operator wants to keep stale
worker metadata (e.g. for forensic preservation of a known-bad state).

## EFS vs running-worker code propagation (2026-06-30 live fix)

The `[:128]` fix was applied to the local source tree and pushed to EFS
(`/mnt/broker/logs/worker-poller.py`), but the running worker's
`/opt/worker/poller.py` still had `[:64]`. This caused live jobs to
produce signatures that failed verifier Check 6, even though local tests
passed and the EFS copy was correct.

Diagnosis: grep both copies via SSM. If EFS has `[:128]` but
`/opt/worker/` has `[:64]`, the worker is stale. Fix: copy EFS →
`/opt/worker/`, restart poller. See
`references/redeploy-runtime-copy-and-restart.md` for the full procedure.

General rule: EFS is the source of truth for new worker launches, but
`/opt/worker/` is the source of truth for running workers. Code changes
to any worker-side file must be explicitly propagated to each running
worker and the poller restarted. `deploy.sh` step 4.5 refreshes metadata
only, not code.

## End-to-end signature verification recipe

After fixing the code propagation, verify the full chain:

1. `GET /v1/discover` — extract `enclave_pubkey`, `worker_ed25519_pubkey`,
   `report_data`
2. `POST /v1/demo/shared-payment-token` (with empty body) — get SPT
3. `POST /v1/jobs` with `stripe_pi_id: <SPT>` and `Authorization: Bearer <SPT>`
   header — get `job_id` and `job_access_token`
4. `GET /v1/jobs/<job_id>` with `Authorization: Bearer <job_access_token>` —
   poll until `completed`
5. Extract `result.sandbox.image_digest_sig` and the sandbox fields
6. Reconstruct: `f"{version}|{digest}|{name}|{enclave_pub}|{report_data[:128]}".encode()`
7. Verify: `Ed25519PublicKey.from_public_bytes(b64decode(ed25519_pub)).verify(bytes.fromhex(sig), payload)`

Live-verified 2026-06-30: signature VERIFIES after EFS→worker copy + restart.

## Tests

- `tests/verify-sandbox-execution.py` — 13/13 covers `_sign_image_digest_bundle`
  (S10) and `publish_worker_keys` (S11) using the `:128` payload and the new
  NemoClaw field schema.
- `tests/verify-blind-audit.py` — 32/32 covers `/v1/discover` attestation block
  and the new NemoClaw fields.
- `tests/verify-deploy-script.sh` — 22/22 covers the deploy step 4.5 metadata
  refresh.
