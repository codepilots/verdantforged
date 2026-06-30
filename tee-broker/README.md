# VerdantForged TEE Broker

A split-deployment TEE broker: a low-cost, always-on **control plane** holds the queue, payment gate, LLM proxy, and TLS endpoint; a TEE-capable **worker** (running on an AWS m6a.xlarge instance with AMD SEV-SNP) is launched on demand when jobs arrive and terminated after a configurable idle buffer.

The real LLM API key never enters the worker — it stays in the broker's config. The broker issues per-job ephemeral tokens that the worker/sandbox uses to call the broker's `/v1/llm/chat/completions` proxy. Token usage is tracked per-job and per-account for billing.

A companion **Skill Library** service (`skill_library/`, port 8091) catalogs broker-compatible skills standalone. See [docs/skill-library-api.md](docs/skill-library-api.md) and [docs/skill-library-deploy.md](docs/skill-library-deploy.md). It runs alongside the broker — additive, no broker restart needed.

**Working notes, drift audit, and known gaps**: see [NOTES.md](NOTES.md). For known defects, see [BUGS.md](BUGS.md). For the live deploy status, see [STATUS.md](STATUS.md). For the gap between the demo site and the live broker, see [../tee-broker-site/SITE_VS_BROKER_AUDIT.md](../tee-broker-site/SITE_VS_BROKER_AUDIT.md).

## Architecture

```text
Requester (browser, CLI, agent)
       |
       | HTTPS POST /v1/jobs
       v
+--------------------------------------+
| Control plane (t3.small) ~$0.02/hr   |
|                                      |
|  - Caddy (auto-TLS via Let's Encrypt)|
|  - broker-daemon (Python aiohttp)    |
|    - job queue (SQLite + EFS)        |
|    - per-job LLM token generator     |
|    - LLM proxy (Gemini/Ollama/...)   |
|    - worker lifecycle (boto3)        |
|    - idle timer (10min default)      |
|    - outbox poller + webhook delivery|
|    - token usage tracking            |
|  - EFS mount: /mnt/broker            |
+----------+---------------------------+
           | boto3 run-instances / terminate
           | (private IP, security group restricted)
           v
+--------------------------------------+
| Worker (m6a.xlarge) ~$0.23/hr        |
| SEV-SNP capable                      |
|                                      |
|  - NemoClaw enclave + sandbox        |
|  - poller (5s loop on EFS inbox)     |
|  - launches jobs into the sandbox    |
|  - sandbox calls broker LLM proxy    |
|    with per-job token (NOT the key)  |
|  - EFS mount: /mnt/broker            |
+--------------------------------------+

```

The control plane is not attested — it's a queue manager and LLM proxy. Only the worker carries the SEV-SNP attestation. Requesters never see the real LLM key, and the worker never sees it either.

## Demo

**Live URL:** https://verdant.codepilots.co.uk

## Endpoints

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Three-sponsors UI |
| `/v1/discover` | GET | Broker capabilities, attestation, pricing |
| `/v1/jobs` | POST | Submit a job; returns `job_id` and one-time client access token |
| `/v1/jobs/{id}` | GET | Authenticated status, upload instructions, result, and billing |
| `/v1/jobs/{id}/ready` | POST | Commit encrypted file uploads and queue the job |
| `/v1/jobs/{id}/artifacts` | GET | Authenticated processed-file manifest and download URLs |
| `/v1/llm/chat/completions` | POST | **Worker-only proxy** — requires per-job Bearer token |
| `/v1/llm/usage/{job_id}` | GET | Token usage for a specific job |
| `/healthz` | GET | Health check |

## API Example

For encrypted input and processed output files, use the worker-first flow in
[docs/file-jobs.md](docs/file-jobs.md). File jobs first pay the ACS/SPT gate,
then return a one-time client access token, wait for an attestation-bound
worker key, accept encrypted S3 uploads, and return encrypted S3 output
artifacts.

```bash
# Submit a job
RESP=$(curl -sS -X POST [https://verdant.codepilots.co.uk/v1/jobs](https://verdant.codepilots.co.uk/v1/jobs) \
    -H 'Content-Type: application/json' \
    -d '{
      "client_req_id": "my-unique-id-001",
      "encrypted_skill": "summarize",
      "encrypted_data": "Text to summarize...",
      "requester_sig": "0x...",
      "result_pubkey": "0x..."
    }')

# Returns: {"job_id": "job_xxx", "state": "queued", "job_access_token": "jobtok_...", ...}

# Poll status
JOB_ID=$(printf '%s' "$RESP" | jq -r .job_id)
JOB_TOKEN=$(printf '%s' "$RESP" | jq -r .job_access_token)
curl -H "Authorization: Bearer $JOB_TOKEN" \
  "https://verdant.codepilots.co.uk/v1/jobs/$JOB_ID"

```

## File Layout

```text
tee-broker-deploy/
├── README.md                              # this file
├── SESSION_LOG.md                         # full hackathon session log
├── deploy.sh                              # one-shot deploy script
├── cloudformation-control-plane.yaml      # t3.small + EFS + EIP + IAM
├── cloudformation-worker.yaml             # m6a.xlarge (debug / permanent)
├── broker-daemon/
│   ├── daemon.py                          # control plane daemon (Python aiohttp)
│   ├── crypto.py                          # X25519 / Ed25519 helpers (result encryption,
│   │                                      #   broker_signature, requester_sig verification)
│   ├── requirements.txt
│   ├── caddy/Caddyfile                    # auto-TLS reverse proxy + static UI
│   └── static/
│       └── index.html                     # three-sponsors UI page
├── worker/
│   ├── user-data.sh                       # worker first-boot (6.7KB, IMDSv2-aware)
│   └── poller.py                          # poller extracted to EFS
├── scripts/
│   └── bootstrap-control-plane.sh         # post-CFN setup
└── tests/
    ├── verify-all-tasks.sh                # 14 checks (tasks 1-6)
    ├── verify-worker-robust.sh            # 18 checks (user-data + poller)
    ├── verify-aws-audit.sh                # 12 checks (hourly watchdog)
    ├── verify-llm-router.sh               # 10 checks (broker proxy + tokens)
    ├── verify-llm-proxy-security.py       # 11 checks (per-job tokens, key never leaves broker)
    ├── verify-attestation-audit.py        # 16+19 checks (spec gap audit)
    └── verify-crypto-e2e.py               # 19 checks (X25519 encryption, Ed25519 sigs)

```

## How the job path works

1. Client submits `POST /v1/jobs`.
2. If the request is missing payment credentials, the broker returns `402 Payment Required` with a `WWW-Authenticate` challenge that includes the amount, currency, method, and merchant `networkId`.
3. The client mints a Stripe Shared Payment Token (`spt_...`) and retries the same request with that token.
4. The broker validates the token server-side, creates/confirms the Stripe PaymentIntent, then queues the job.
5. The control plane launches a worker on demand, waits for attestation + NemoClaw bootstrap, and hands the job to the worker poller.
6. The poller dispatches the task into the NemoClaw sandbox. The sandbox calls the broker's LLM proxy using the per-job token, so the real LLM key never leaves the control plane.
7. When the result is ready, the broker returns the encrypted result manifest and keeps the billing record on the control plane.

## The LLM Proxy Pattern

This is the key architectural decision of the project. Without it, every worker would need its own copy of the real LLM API key — a security and billing nightmare.

**Flow:**

1. Client submits job → broker generates `llm_token`, stores it in SQLite, and includes it in the EFS envelope.
2. Worker poller picks up the envelope and reads the `llm_token`.
3. Worker calls `https://verdant.codepilots.co.uk/v1/llm/chat/completions` with `Authorization: Bearer llm_token`.
4. Broker validates the token, checks the account daily cap, and forwards to the real LLM (Gemini, Ollama, OpenAI, etc.) using its private key.
5. Broker records usage in three places: per-token, per-account-per-day, and per-job.
6. Returns LLM response with added `_billing` metadata.
7. Worker writes the result to the EFS outbox, and the control-plane outbox-poller picks it up.

**Why this matters:**

* Workers are ephemeral and untrusted — they can read their own envelope but should never see the real LLM key.
* Each job has its own budget cap — runaway jobs can't drain the account.
* Per-job usage is auditable for billing reconciliation.
* Swap LLM providers (Gemini ↔ Ollama ↔ OpenAI) without changing workers.

## Configured LLM (Demo)

The broker is currently configured to use Gemini 2.5 Flash in live deploys and
falls back to the configured provider in `/opt/broker-daemon/config.env`.

```env
BROKER_LLM_API_KEY=<provider-api-key>
BROKER_LLM_BASE_URL=<provider-base-url>
BROKER_LLM_MODEL=<provider-model>

```

These live in `/opt/broker-daemon/config.env` on the control plane (sourced via the systemd `EnvironmentFile` directive). Change them to swap providers without touching the worker.

## Payment flow

See [docs/payment-flow.md](docs/payment-flow.md) for the ACS/SPT flow.
In short: the broker does not expect the client to create a PaymentIntent.
The client receives a 402 challenge, mints an SPT, and the broker creates the
PaymentIntent itself using its secret key. If you cannot use Stripe Link in
your region, set `BROKER_PAYMENT_STUB_MODE=1` and use the broker's stubbed
`/v1/demo/shared-payment-token` route or `--demo-spt` in the E2E client.
For file uploads, the E2E helper now also checks `/healthz` while waiting for
`awaiting_inputs` so it can tolerate a slow cold-starting worker before it
starts encrypting and uploading files. If the broker reports a live worker but
the job still says `awaiting_worker`, the remaining blocker is worker key
publication / attestation binding rather than the spawn itself.

## Three Sponsors

The UI page at `https://verdant.codepilots.co.uk/` showcases:

| Sponsor | Role |
| --- | --- |
| **NVIDIA** | NemoClaw sandbox, which runs on the attestation hardware on AWS. |
| **Stripe** | Payment Infrastructure — Per-session leasing at $0.20/15min. |
| **Nous Research** | Agent Intelligence — Hermes Agent orchestrates the pipeline. |

## Deploy

```bash
cd ~/hermes/competition/tee-broker-deploy
./deploy.sh

```

The script will:

1. Look up your default VPC and an Ubuntu 24.04 AMI for the control plane.
2. Deploy/update the control-plane CloudFormation stack (~3-5 min).
3. Push the local repo tarball to the control-plane instance via SSM.
4. Persist provided secrets/config to SSM Parameter Store (`STRIPE_SECRET_KEY`, `BROKER_LLM_API_KEY`, `BROKER_ONBOARD_TOKEN`) without writing them to EFS.
5. Run `scripts/bootstrap-control-plane.sh` on the instance. That script installs the daemon/static UI/Caddyfile, regenerates `/opt/broker-daemon/config.env`, pushes `worker/user-data.sh` to both `/opt/broker-daemon/worker-bootstrap.sh` and `/mnt/broker/logs/worker-bootstrap.sh`, and pushes `worker/poller.py`, `worker/worker-agent.py`, `worker/sev_snp.py`, `openshell-policy.yaml`, and bundled worker skills to EFS.
6. Restart Caddy and `verdantforged-broker-daemon.service`.
7. Smoke-test the health endpoint.

A code-only redeploy preserves existing live secrets and `BROKER_WORKER_AMI` from `/opt/broker-daemon/config.env` unless you explicitly override them. A clean deploy uses the baked gold worker AMI by default so workers cold-start from the cached NemoClaw/OpenShell image rather than rebuilding the 4GB sandbox layers.

**Defaults:**

* Region: `eu-west-1`
* Stack: `verdantforged-broker-control`
* Control plane instance: `t3.small`
* Worker instance: `m6a.xlarge`
* Worker AMI: `ami-099e2272620073023` (`verdantforged-nemoclaw-gold-worker-20260630T163844Z-i-0cd8b60358d6d5509`)
* Idle buffer: 10 minutes

Override any of these via env:

```bash
REGION=eu-west-1 \
INSTANCE_TYPE=t3.micro \
WORKER_INSTANCE_TYPE=m6a.2xlarge \
WORKER_AMI_ID=ami-099e2272620073023 \
IDLE_BUFFER_MINUTES=20 \
DOMAIN_NAME=broker.example.com \
KEY_NAME=my-key \
./deploy.sh

# Optional: provide/rotate deploy-time secrets. deploy.sh stores them in SSM
# SecureString parameters and bootstrap-control-plane.sh fetches/preserves them
# when rebuilding /opt/broker-daemon/config.env.
BROKER_LLM_API_KEY=... \
BROKER_ONBOARD_TOKEN=$(openssl rand -base64 36 | tr -d '\n=' | cut -c1-48) \
./deploy.sh

```

## Tests

The `tests/` directory contains ad-hoc verification scripts:

```bash
# Run all tests
cd ~/hermes/competition/tee-broker-deploy/tests
for t in verify-*.sh; do bash "$t"; done

```

| Test | Checks | Purpose |
| --- | --- | --- |
| `verify-all-tasks.sh` | 14 | Tasks 1-6: worker robustness, attestation, Stripe, enclave, started_at, UI |
| `verify-worker-robust.sh` | 18 | User-data.sh structure + embedded poller behavior |
| `verify-aws-audit.sh` | 12 | Hourly AWS orphan-resource watchdog |
| `verify-llm-router.sh` | 10 | Broker LLM proxy + per-job tokens + usage tracking |
| `verify-llm-proxy-security.py` | 11 | Per-job tokens unique; invalid tokens rejected; broker holds Ollama key |
| `verify-attestation-audit.py` | 16 PASS / 19 WARN | Broker attestation vs `tee-broker-pattern` spec |
| `verify-crypto-e2e.py` | 19 | X25519 result encryption + Ed25519 broker signature + requester_sig verification |

Each test is self-contained — no fixtures, no test DB. They hit the live broker API and check static source structure. 

## Cost Shape

| Component | Always-on | At idle (10 min after last job) |
| --- | --- | --- |
| t3.small control plane | $0.0208/hr ≈ $15/mo | same |
| m6a.xlarge worker | $0.226/hr ≈ $170/mo | $0 (terminated) |
| EFS | ~$0.30/GB-mo | same |
| EBS gp3 20 GB | ~$1.6/mo | same |
| EIP | free while attached | same |

## Rate limits and quotas

The broker enforces three independent limits on `POST /v1/jobs`, each
returning HTTP 429 with a distinct `code` field so clients can tell them
apart:

| Limit                       | Default     | Env var                       | `code`           | Scope   |
|-----------------------------|-------------|-------------------------------|------------------|---------|
| Per-IP rate limit           | 10 jobs/min | `BROKER_RATE_LIMIT_PER_MINUTE`| `rate_limited`   | IP      |
| Per-account token cap       | 50k/day     | `DEMO_TOKEN_CAP`              | `token_cap_exceeded` | Account |
| Per-account daily job cap   | 5 jobs/day  | `BROKER_DAILY_JOB_CAP`        | `daily_cap`      | Account |

The **daily job cap** (VULN-LLMTK, kanban t_a18827b6) closes the
"refund-eats-compute" hole: the broker refunds failed jobs but the EC2
minutes already burned. Without a count cap, an attacker could spam
failed jobs to drain the broker's compute budget. The cap counts every
accepted submit (regardless of outcome), so failed jobs still cost the
attacker their daily budget. Day rollover happens naturally by the
`day_utc` key change — no cron needed. The account key is hashed
(`sha256(stripe_pi_id | BROKER_ACCOUNT_HASH_SECRET)[:16]`,
VULN-S7, t_c6beba80), so an attacker can't mint `pi_evil_1..pi_evil_N`
for fresh budgets.

Override examples:

```bash
# Raise the daily job cap to 50 (production-grade limit).
export BROKER_DAILY_JOB_CAP=50

# Disable the per-IP rate limiter entirely (only for tests / demos).
export BROKER_RATE_LIMIT_DISABLED=1
```

429 responses include a `reason` field that mirrors the `code` for
human-readable logging, plus `retry_after_seconds` on `rate_limited`
responses.

## Rate limits and quotas

The broker enforces three independent limits on `POST /v1/jobs`, each
returning HTTP 429 with a distinct `code` field so clients can tell them
apart:

| Limit                       | Default     | Env var                       | `code`           | Scope   |
|-----------------------------|-------------|-------------------------------|------------------|---------|
| Per-IP rate limit           | 10 jobs/min | `BROKER_RATE_LIMIT_PER_MINUTE`| `rate_limited`   | IP      |
| Per-account token cap       | 50k/day     | `DEMO_TOKEN_CAP`              | `token_cap_exceeded` | Account |
| Per-account daily job cap   | 5 jobs/day  | `BROKER_DAILY_JOB_CAP`        | `daily_cap`      | Account |

The **daily job cap** (VULN-LLMTK, kanban t_a18827b6) closes the
"refund-eats-compute" hole: the broker refunds failed jobs but the EC2
minutes already burned. Without a count cap, an attacker could spam
failed jobs to drain the broker's compute budget. The cap counts every
accepted submit (regardless of outcome), so failed jobs still cost the
attacker their daily budget. Day rollover happens naturally by the
`day_utc` key change — no cron needed. The account key is hashed
(`sha256(stripe_pi_id | BROKER_ACCOUNT_HASH_SECRET)[:16]`,
VULN-S7, t_c6beba80), so an attacker can't mint `pi_evil_1..pi_evil_N`
for fresh budgets.

Override examples:

```bash
# Raise the daily job cap to 50 (production-grade limit).
export BROKER_DAILY_JOB_CAP=50

# Disable the per-IP rate limiter entirely (only for tests / demos).
export BROKER_RATE_LIMIT_DISABLED=1
```

429 responses include a `reason` field that mirrors the `code` for
human-readable logging, plus `retry_after_seconds` on `rate_limited`
responses.

## Monitoring

Cron job `aws-audit-hourly` runs every 60 mins. It:

* Lists running EC2 instances, active CFN stacks, and unattached EIPs across 9 regions.
* Exits silently when nothing billable is running.
* Alerts via Telegram/Discord if it finds resources not in the whitelist.

## What This Template Does NOT Do Yet

* **Worker cold-start validation**: the live control plane is configured to use the baked gold AMI (`ami-099e2272620073023`) and bootstrap pushes the local worker config to EFS. After any change to `worker/user-data.sh`, `worker/poller.py`, `worker/worker-agent.py`, or `broker-daemon/daemon.py`, run a file-job E2E to prove the new AMI/bootstrap path still reaches `execution_mode=nemoclaw-sandbox`.
* **Stripe MPP release**: The daemon validates `stripe_pi_id` format (must start with `pi_`) but never calls Stripe to release escrow. That's the worker's job in production.
* **Real SEV-SNP attestation report**: `/v1/discover` returns a `min_measurement` (SHA-256 of instance ID) but does not include the raw SEV-SNP attestation report, cert chain to the AMD root, or enclave public key. For production-grade attestation:
1. Switch to a region with SEV-SNP support (eu-west-2 doesn't have it; eu-west-1, eu-central-1, eu-north-1 do). Attempting SEV-SNP in an unsupported region returns `UnsupportedOperation` from RunInstances.
2. Use an instance type that supports confidential computing (m6a.xlarge does, when SEV-SNP is enabled in the region).
3. Launch with `CpuOptions={"AmdSevSnp": "Enabled"}` — see `worker/sev_snp.py` for the parsing code (1184-byte SNP report, measurement at offset 392, cert chain via `snpguest fetch-ca`).
4. Build a worker AMI with `linux-modules-extra` (ccp module) and `snpguest` installed.


## Current limitations

* **NemoClaw install takes ~15 minutes** on cold boot (Docker image pull). Acceptable for a hackathon demo, but production would benefit from a pre-warmed AMI with NemoClaw pre-installed.

## Cryptographic Security

### 1. Per-Job Result Encryption (X25519 + ChaCha20-Poly1305)

The requester provides an X25519 public key in the `result_pubkey` field at job submission. The worker encrypts the LLM output (plus model, usage, execution_mode) to this key before writing the result to the outbox. The encryption uses an **ephemeral-static X25519 ECDH** pattern:

```text
result_encrypted = base64(
    worker_eph_pubkey_32 ‖ nonce_12 ‖ ChaCha20Poly1305(
        nonce,
        payload_json,
        aad="verdantforged-result",
        key=X25519(worker_priv, requester_pub)
    )
)

```

The requester decrypts by deriving the shared secret from their private key and the worker's ephemeral public key (both in `result_pubkey_ephemeral`).

Implementation: `broker-daemon/crypto.py` (broker side) and `worker/poller.py::execute_in_envelope` (worker side).

### 2. Broker Signature (Ed25519)

The worker has its own Ed25519 keypair persisted at `/opt/worker/keys/worker_signing.priv` (mode 0600). On every result, the worker signs:

```text
broker_signature = Ed25519_sign(
    worker_signing_key,
    payload = "{result_hash}|{skill_hash}|{input_hash}"
)

```

Where:

* `skill_hash` = SHA-256(skill name)
* `input_hash` = SHA-256(input data)
* `result_hash` = SHA-256(canonical signed payload)

A requester can verify the signature by checking it against the worker's public key (which the broker would expose as part of the attestation in production). The 64-byte signature length confirms Ed25519.

### 3. Requester Signature Verification (Ed25519, Opt-in)

If the requester provides a `requester_pubkey` (Ed25519) at job submission, the broker verifies that `requester_sig` is a valid Ed25519 signature over:

```text
canonical = "{skill_hash}|{input_hash}|{result_pubkey}|{stripe_pi_id}|{timestamp}"

```

Invalid signatures are rejected with HTTP 400. This is **opt-in** — demo clients passing dummy `"0x"` strings are still accepted (backward compat). For production, set `BROKER_REQUIRE_REQUESTER_SIG=1` to make it mandatory.

Implementation: `broker-daemon/daemon.py::validate_submit` and `broker-daemon/crypto.py::verify_requester_sig`.

### 4. Content-Addressed Skill and Input Hashes

Every result includes:

* `skill_hash`: SHA-256 of the skill name (e.g., `SHA-256("summarize")`)
* `input_hash`: SHA-256 of the input data
* `result_hash`: SHA-256 of the canonical signed payload

These allow the requester to detect tampering — any change to the input or skill would produce a different hash, breaking the broker signature.

### 5. Execution Metering

Every result includes:

* `duration_ms`: wall-clock time from `time.monotonic()` start to end.
* `fuel_used`: mock fuel counter (1 unit / ms).

### 6. IMDSv2 Metadata Fetch

The worker user-data now fetches an IMDSv2 session token via `PUT /latest/api/token` before fetching instance metadata. Without this, all curl calls to `169.254.169.254` return HTTP 401 on modern EC2 instances.

```bash
IMDS_TOKEN=$(curl -X PUT [http://169.254.169.254/latest/api/token](http://169.254.169.254/latest/api/token) -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" [http://169.254.169.254/latest/meta-data/instance-id](http://169.254.169.254/latest/meta-data/instance-id))

```

The poller (`worker/poller.py::get_sev_snp_measurement`) uses the same pattern in Python.

## Teardown

```bash
aws cloudformation delete-stack --stack-name verdantforged-broker-control --region eu-west-1

```

This terminates the control plane, deletes the EFS filesystem, releases the EIP, and removes the IAM role. **It does NOT delete a running worker** — terminate it manually first:

```bash
# Find any running workers
aws ec2 describe-instances --region eu-west-1 \
    --filters "Name=tag:Project,Values=verdantforged" "Name=tag:Role,Values=tee-worker" \
              "Name=instance-state-name,Values=running,pending" \
    --query "Reservations[].Instances[].[InstanceId,State.Name]" --output table

# Terminate
aws ec2 terminate-instances --region eu-west-1 --instance-ids i-xxxxx

```

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `curl http://<EIP>/healthz` returns 503 | Caddy not provisioned (no DomainName) | Set DOMAIN_NAME and reload Caddy, or use the EIP directly. |
| TLS cert fails to issue | DNS not pointing at EIP, or Let's Encrypt rate limit | `dig +short $DOMAIN_NAME` should return the EIP. Wait 5 mins and `systemctl reload caddy`. |
| Worker doesn't launch | IAM role or SG misconfigured | Check `/var/log/broker-daemon.log` for `worker launch failed`. |
| Worker boots but doesn't process jobs | EFS mount failed | Check `/var/log/cloud-init-verdantforged-worker.marker` on worker. |
| Job stays "queued" forever | Worker never registered | Check `cat /mnt/broker/logs/worker-heartbeat.json` on the worker. |
| `execution_mode: "broker-proxy-failed"` | LLM upstream error | Check daemon log; verify `BROKER_LLM_API_KEY` and `BROKER_LLM_BASE_URL`. |
| Gemini returns empty content | Free-tier rate limit hit, `finish_reason: "length"` | Wait a few minutes, increase `max_tokens` in poller. |


```
