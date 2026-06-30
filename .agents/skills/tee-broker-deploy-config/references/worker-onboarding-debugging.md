# Worker onboarding/debugging notes (2026-06-30)

## What happened
- The live worker was fixed by syncing the control-plane and EFS copies of the worker bootstrap from the local repo canonical version.
- The stale deployed bootstrap had too-short NemoClaw onboarding timeouts and was not matching the local files.
- A fresh worker boot completed successfully once the canonical bootstrap was restored.

## Useful live-state checks
- `/var/log/cloud-init-verdantforged-worker.marker` shows the stage progression.
- `/root/.nemoclaw/onboard-session.json` shows onboarding status and last completed step.
- `/opt/worker/.nemoclaw_metadata` is written near the end of onboarding and should exist on a healthy worker.
- `nemohermes list --json` may still show `sandboxes: []` during/after handoff; check the session file and processes too.

## Log sequence that indicates success
- `step 4: NemoClaw installed, nemohermes at /usr/bin/nemohermes`
- `step 4: NemoClaw sandbox OK (1 sandbox(es))`
- `step 4b: metadata written to /opt/worker/.nemoclaw_metadata`
- `step 5: poller installed from EFS`
- `step 5: poller active`
- `Worker setup complete`

## Pitfall
- A long sandbox build can take many minutes; don’t trust a short inline timeout on cloud-init/bootstrap. The deployed bootstrap must stay aligned with the repo canonical copy, and the current worker’s `Incorrect padding` job failures were a separate payload-decoding issue, not a boot failure.
