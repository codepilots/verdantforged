# Gold worker AMI + deploy config propagation

Session-derived procedure for VerdantForged TEE Broker cold-start optimization and clean redeploy safety.

## What to preserve in the gold AMI

Bake from a live worker only after NemoClaw/OpenShell completed the expensive sandbox build and the worker has successfully loaded the sandbox worker agent. Preserve installed packages and Docker layers such as:

- `ghcr.io/nvidia/nemoclaw/hermes-sandbox-base:<version>`
- `openshell/sandbox-from:<id>`

Scrub per-instance state before `create_image`:

- `/mnt/broker/jobs/inbox/*`
- `/mnt/broker/jobs/outbox/*`
- `/opt/worker/keys/*`
- live worker heartbeat / attestation JSONs
- NemoClaw session/registry state under root if it contains per-instance identity
- cloud-init logs if they contain transient bootstrap details

Keep a recoverable S3 backup of EFS/job queues before destructive cleanup.

## Clean deploy invariants

A local `deploy.sh` run must push both server config and worker bootstrap artifacts, not just CloudFormation:

1. Pass the intended worker AMI into CloudFormation as `WorkerAmiId`.
2. Export the same value as `BROKER_WORKER_AMI` into the bootstrap environment.
3. `bootstrap-control-plane.sh` must regenerate `/opt/broker-daemon/config.env` preserving existing secrets and the baked AMI value.
4. `bootstrap-control-plane.sh` must push canonical worker files to both server and EFS paths:
   - `/opt/broker-daemon/worker-bootstrap.sh`
   - `/mnt/broker/logs/worker-bootstrap.sh`
   - worker poller/agent/policy/skills under `/mnt/broker/logs/` or the expected worker EFS layout.
5. Restart `verdantforged-broker-daemon.service` after config updates.

## Secret/config propagation

For clean redeploys, handle SecureString parameters as a set, not one-off secrets. The control-plane IAM role must be able to read every broker parameter the bootstrap fetches:

- `/verdantforged/broker/stripe-secret-key`
- `/verdantforged/broker/llm-api-key`
- `/verdantforged/broker/onboard-token`

`deploy.sh` may persist provided values to SSM, then clear the local bootstrap env copy. `bootstrap-control-plane.sh` should fetch from SSM if absent, preserve existing config values on code-only redeploys, and generate an onboard token only for a true clean deploy.

## Verification checklist

Run static checks before declaring the deploy path fixed:

- `bash -n deploy.sh`
- `bash -n scripts/bootstrap-control-plane.sh`
- project static deploy verification script, if present
- `aws cloudformation validate-template --region <region> --template-body file://cloudformation-control-plane.yaml`
- byte-level check that redaction did not write literal `***` into edited scripts/docs

Then do a real cold-start file-job E2E from the new AMI and confirm:

- worker boots from the baked AMI
- worker receives the canonical EFS bootstrap/poller/agent files
- `execution_mode=nemoclaw-sandbox`
- non-empty returned `output.txt` / decrypted result files
