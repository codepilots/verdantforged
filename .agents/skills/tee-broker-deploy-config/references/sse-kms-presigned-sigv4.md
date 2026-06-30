# SSE-KMS Artifact Bucket + Presigned URL + SigV4

**Discovered:** 2026-06-30. Hit while running
`scripts/run_file_job_e2e.py` end-to-end against the live broker —
the artifact download step failed with
`InvalidArgument: Requests specifying Server Side Encryption with AWS
KMS managed keys require AWS Signature Version 4.`

**Affects:** any code path in the broker or worker that calls
`boto3.client("s3")` followed by `generate_presigned_url()` against
the artifact bucket. Currently:
- `broker-daemon/daemon.py:generate_presigned_url` (download URLs)
- `broker-daemon/daemon.py:generate_presigned_upload_url` (input-file
  upload URLs)
- `worker/poller.py:upload_artifacts_to_s3` (via `_get_s3_client`,
  `put_object` is unaffected by SigV4 directly but the client must
  match the signing version for the signed PUT)

## Root cause

The artifact bucket is configured with
`BucketEncryption: ServerSideEncryption: AWS::KMS: ...` in
CloudFormation (see `verify-artifacts-s3.py` L2). S3 refuses any
non-SigV4 request against an SSE-KMS bucket, **including the replay
of a presigned URL**. The broker's boto3 client was constructed
without `Config(signature_version="s3v4")`, so the default signer was
used. In some boto3/region combinations the default is SigV2 (older
configs) or a hybrid that does not match what SSE-KMS requires.

The error from S3 is misleading — it says "requests specifying SSE
with KMS require SigV4", but the request was not specifying SSE
explicitly. The bucket's default encryption was what triggered the
SigV4 requirement.

## The fix (committed 2026-06-30)

Pin SigV4 on the boto3 client in **both** sides:

```python
from botocore.client import Config as BotoConfig

# broker-daemon/daemon.py:_get_s3_client
s3_client = boto3.client(
    "s3",
    region_name=BROKER_REGION,
    config=BotoConfig(signature_version="s3v4"),
)

# worker/poller.py:_get_s3_client
S3_CLIENT = boto3.client(
    "s3",
    region_name=region or ARTIFACT_REGION,
    config=BotoConfig(signature_version="s3v4"),
)
```

## What NOT to do (the trap I almost fell into)

Do NOT add `ServerSideEncryption="aws:kms"` (or
`SSEKMSKeyId=...`) to the `Params=` dict of `generate_presigned_url`.
That forces the *client's* GET request to be SigV4-signed itself,
which a plain `requests`/`curl` cannot do. Returns a different
`InvalidArgument` at request time. The bucket's default encryption is
what protects at-rest data; the presigned URL does not need to carry
SSE headers because S3 decrypts server-side on read.

This is the same trap the `generate_presigned_upload_url` docstring
(daemon.py:855) warns about for the upload path:
> "We do NOT include ServerSideEncryption in the presigned URL params —
> doing so causes S3 to require SigV4 signing on the PUT request,
> which a plain HTTP client (curl, requests) cannot provide."

The same reasoning applies to download URLs, but the failure mode is
different because the bucket default is what triggers it on GET.
The right answer in both directions is the same: pin SigV4 on the
signing side, leave the client request alone.

## Why "just enable SSE-S3" is the wrong answer

SSE-S3 (AES256) is transparent and works with any signature version,
so flipping the bucket encryption off KMS would also fix the symptom.
But the bucket was KMS-encrypted on purpose (compliance, customer
expectation, audit trail). Switching to SSE-S3 is a security
regression and a marketing regression ("encryption at rest with
customer-managed keys" → "encryption at rest" is a meaningful
delta). The Config fix is 4 lines, doesn't touch the bucket, and
preserves the security posture.

## Verification matrix

Run before and after the change. The right number after the change
is **all four pass**:

| Test | Purpose | Expected after fix |
|------|---------|---------------------|
| `python3 tests/verify-artifacts-s3.py` | 63 checks including CloudFormation + presign | 63/63 pass |
| `python3 tests/verify-input-attachments.py` | Input-file upload presigner | 31/31 pass |
| `python3 tests/verify-artifacts.py` | End-to-end artifact pipeline (some pre-existing failures from env/fixture issues) | 32/42 pass — the 10 fails are pre-existing (verify by `git stash` + re-run) |
| `python3 scripts/run_file_job_e2e.py --demo-spt ...` | Live broker round-trip | The "ARTIFACTS" step prints `[ok]` per file and writes plaintexts to `--artifacts-dir` |

## Diagnosing "is this the SSE-KMS issue" from a new error

If a future session sees a different but related S3 error from the
broker, check for these signatures in order:

1. `InvalidArgument` + `require AWS Signature Version 4` → this doc.
2. `InvalidArgument` + `x-amz-server-side-encryption` mentioned → a
   caller added SSE headers to a presign. Revert.
3. `AccessDenied` + `kms:Decrypt` → bucket's KMS key policy does not
   grant the broker/worker role. Check `ControlPlaneRole` and
   `WorkerRole` IAM policies vs. the bucket's `BucketEncryption` key
   resource policy.
4. `SignatureDoesNotMatch` → the presigner signed with one set of
   credentials and the client presented different ones. Almost always
   means the broker and worker are in different accounts/roles than
   expected.

## Related references

- `worker/poller.py:encrypt_artifact` — what the worker puts in the
  blobs. Decryption recipe:
  `tee-broker-task-design/references/artifact-encryption-v1.md`.
- `scripts/run_file_job_e2e.py:download_and_decrypt_artifacts` —
  reference client that exercises the full download+decrypt path.
- `references/worker-identity-gate.md` — related stall when
  `/healthz` says the worker is ready but jobs hang in
  `awaiting_worker`.
- `references/attestation-verifier-build-notes.md` — also touches S3
  + KMS in the context of the worker publishing its public key.
