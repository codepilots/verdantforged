# File-upload E2E notes (2026-06-29)

This reference captures the live file-upload job flow that was successfully exercised on the VerdantForged control plane.

## Working flow

1. Create a real Stripe PaymentIntent (`confirm=true`, `pm_card_visa`, and `automatic_payment_methods[enabled]=true`, `automatic_payment_methods[allow_redirects]=never`).
2. Submit `POST /v1/jobs` with:
   - `encrypted_skill` set to a Hermes-agent-style task (e.g. `code-review`)
   - `result_pubkey` set to a client-generated X25519 public key
   - `input_files[]` metadata for each attachment
3. Poll until `state == awaiting_inputs`.
4. Encrypt each file with the worker's X25519 public key using the wire format:
   - ephemeral X25519 pubkey (32 bytes)
   - nonce (12 bytes)
   - ChaCha20Poly1305 ciphertext + tag
   - AAD: `verdantforged-file-v1\0{direction}\0{job_id}\0{filename}`
5. PUT ciphertext to the presigned URL.
6. Call `POST /v1/jobs/{job_id}/ready`.
7. Poll until `completed`.
8. Fetch `/v1/jobs/{job_id}/artifacts` and decode the result artifact with the client private key.

## Key findings from the live run

- The worker adopts quickly once the daemon can verify its identity.
- The public `/healthz` endpoint is now useful during cold start because it shows `worker_status`, `worker_boot_stage`, and an ETA.
- File jobs depend on `_load_verified_worker_identity()` succeeding; if attestation or policy binding is off, they remain stuck in `awaiting_worker`.
- Direct presigned PUT uploads only worked after the bucket was switched away from SSE-KMS; plain `requests.put()` could not satisfy the KMS/SigV4 requirements.
- The final job completed in NemoClaw sandbox mode and returned one encrypted artifact (`output.txt`).

## Common failure modes

- Stripe PI creation without `automatic_payment_methods[enabled]=true` and `allow_redirects=never` returns a 400.
- KMS-encrypted presigned PUT URLs can fail with `InvalidArgument` unless the client can provide SigV4; for this flow, prefer bucket default encryption that does not force extra request headers.
- If the worker identity is rejected, file jobs never advance beyond `awaiting_worker`.

## Verification helper

The test script used in this session lives at `/tmp/e2e_file_upload_test.py` during the run; the long-term template should be kept in the skill's `templates/` directory.
