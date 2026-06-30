# E2E script attestation schema compatibility (2026-06-30)

## Schema transition

The broker API moved attestation data from `input_upload.encryption.snp_quote`
(legacy) into a structured `input_upload.attestation` object. The new schema
also surfaces sandbox-runtime metadata at the top-level job result as
`result.sandbox`.

```
Old:
  input_upload.encryption.snp_quote -> bytes

New:
  input_upload.attestation -> {
    tee_type, measurement, report, cert_chain,
    report_data, source, snp_quote
  }
  result.sandbox -> {
    name, attested, network_policy, inference_route,
    error, nemoclaw_version, nemoclaw_image,
    nemoclaw_image_digest, image_digest_sig
  }
  result.execution_mode -> "nemoclaw-sandbox" | "nemoclaw-sandbox-stub" |
                           "no-nemoclaw-failclosed" | ...
```

## Why this matters

Ops tooling that ignores `result.sandbox` will miss:
- NemoClaw version drift (`nemoclaw_version`)
- Docker image digest/signature mismatch (`nemoclaw_image_digest`,
  `image_digest_sig`)
- `inference_route` (which LLM provider the worker actually used)

The `error` field in `result.sandbox` is non-null when the worker hit the
`no-nemoclaw` fail-closed path.

## LLM provider exhaustion signals

| Provider | Exhaustion signal | Broker effect |
|----------|------------------|----------------|
| Ollama   | HTTP 401 (invalid / revoked key) | `llm_error` = provider auth failure |
| Nous Portal | HTTP 404 "account balance is too low" | `llm_error` = unpaid / zero balance |

The broker stores exactly one API key per provider in its config.env (or SSM).
Topping up a different account does not update the stored key; it must be
refreshed explicitly and the daemon restarted.

## NemoClaw stub-mode interactions

```
failure trio:
  NEMOCLAW_SANDBOX_NAME='stub'
  BROKER_NEMOCLAW_STUB_MODE=0   <- default, fail-closed
  shim_present=True
worker error: "no-nemoclaw: worker has no NemoClaw sandbox
  (NEMOCLAW_SANDBOX_NAME='stub', stub_mode=False, shim_present=True)
  — refusing to fall back to a non-attested host-side LLM call.
  Check user-data.sh step 4 / worker-bootstrap.sh and rerun the job.
  To enable demo fallback, set BROKER_NEMOCLAW_STUB_MODE=1 in the broker's config.env."
```

Resolutions:
1. Fix user-data / bootstrap so NemoClaw onboards to a real sandbox name.
2. Set `BROKER_NEMOCLAW_STUB_MODE=1` for demo fallback (non-attested path).

Worker-side signs that onboarding succeeded:
- `/opt/worker/.nemoclaw_metadata` exists and contains `sandbox_name`
- logs show `step 4: NemoClaw sandbox OK (1 sandbox(es))`
- worker `execution_mode` becomes `nemoclaw-sandbox` (not `stub` or `failclosed`)

User-data step 4 checks to verify:
- Installs `nemohermes` to `/usr/bin/nemohermes`
- Runs `nemohermes onboard --non-interactive` with a `--sandbox-name` that
  resolves to an actual NemoClaw sandbox (not `stub`) when `stub_mode=False`
- Does NOT set `NEMOCLAW_SANDBOX_NAME=stub` in a way that overrides the
  non-interactive onboard default unless `BROKER_NEMOCLAW_STUB_MODE=1` is
  also set on the broker.
