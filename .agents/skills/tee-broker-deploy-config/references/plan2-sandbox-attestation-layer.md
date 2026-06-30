# Plan 2 sandbox attestation layer implementation notes

Use this when implementing or reviewing the additional NemoClaw sandbox attestation layer in `tee-broker-deploy`.

## Canonical signature contract

The sandbox image digest signature payload is:

```python
f"{version}|{digest}|{sandbox_name}|{enclave_pubkey_b64}|{report_data_hex[:128]}".encode()
```

Important distinctions:

- `enclave_pubkey_b64` is the worker X25519 public key string from `/v1/discover.attestation.enclave_pubkey`. It remains the client input-encryption key and is also included in the signed payload as the worker identity binding field.
- The signature key is NOT the X25519 key. Sign with the worker Ed25519 signing key.
- `/v1/discover.attestation.worker_ed25519_pubkey` must expose the base64 Ed25519 public key used to verify `sandbox.image_digest_sig`.
- Use `report_data_hex[:128]`, not `[:64]`, so worker output, docs, site snippets, and verifier scripts agree.

## Fields `/v1/discover` should surface

In addition to the existing attestation fields, expose these from `worker-keys.json`:

- `worker_ed25519_pubkey` from `ed25519_pubkey_b64`
- `nemoclaw_version`
- `nemoclaw_image`
- `nemoclaw_image_digest`

Keep `enclave_pubkey` as X25519 for backwards-compatible encryption semantics; do not reuse it as an Ed25519 verifier key.

## Worker metadata capture hardening

For `/opt/worker/.nemoclaw_metadata`:

- Prefer `docker image inspect <image> --format '{{range .RepoDigests}}{{println .}}{{end}}'` and parse the digest after `@`.
- Fall back to `docker images --digests <image> --format '{{.Digest}}'`, excluding `<none>`.
- Write metadata with `python3 -c`/heredoc + `json.dumps`, not shell-expanded JSON, so image/version strings with quotes or backslashes do not corrupt the file.

## Tests to update/run

- `tee-broker-deploy/tests/verify-sandbox-execution.py`: verify `image_digest_sig` against `report_data[:128]`.
- `tee-broker-deploy/tests/verify-blind-audit.py`: assert `/v1/discover` exposes `worker_ed25519_pubkey` and NemoClaw metadata.
- Validate `broker-daemon/static/openapi.json` with `python3 -m json.tool`.
- Validate `worker/user-data.sh` with `bash -n`.
- Compile touched Python with `python3 -m py_compile`.

## Docs/site sync checklist

When the contract changes, update every consumer in the same pass:

- `PLAN_1_DOCUMENTATION.md`
- `PLAN_2_DEPLOYMENT.md`
- `tee-broker-site/src/pages/verify-attestation.astro`
- local verifier skill script/docs, if present
- `references/attestation-verifier-build-notes.md`
- `references/attestation-trust-model.md`
