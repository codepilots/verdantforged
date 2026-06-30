# Worker cold-start / file-upload gate

Use this when a file-job E2E run times out while waiting for `awaiting_inputs`.

First distinguish two different failures:

1. Cold-start / spawn delay
- `/healthz` reports `worker_status=offline`, or no worker instance yet.
- The worker is still booting, mounting EFS, installing runtime pieces, or starting the poller.
- The E2E helper should keep waiting and print boot diagnostics.

2. Worker live but identity gate blocked
- `/healthz` reports `worker_status=idle` and `worker_boot_stage=ready`.
- The job remains in `awaiting_worker`.
- This means the broker has not accepted the worker upload key / attestation binding yet; see `references/worker-identity-gate.md`.

E2E helper guidance:
- While waiting for `awaiting_inputs`, poll `/healthz` each loop.
- Log the full boot diagnostics: `worker_instance_id`, `worker_boot_stage`, `worker_boot_detail`, `worker_boot_elapsed_seconds`, `worker_boot_eta_seconds`, `worker_uptime_seconds`, `worker_idle_seconds`.
- Also print job-level `worker_wait` diagnostics when present: status, detail, and instance id.
- Treat `awaiting_worker` + live `/healthz` as an identity/attestation-binding delay, not a spawn delay.
- Only upload once the job is `awaiting_inputs` and the worker is not reported offline.

This is a diagnostic aid for cold-start / attestation ambiguity. Do not change the client to upload early.