# Live config propagation pitfall

When a deployment adds a new runtime env var, verify both places:
1. `deploy.sh` must export the variable into the bootstrap payload.
2. `scripts/bootstrap-control-plane.sh` must write the variable into `/opt/broker-daemon/config.env`.

Why this matters
- A code redeploy can refresh `/opt/broker-daemon/daemon.py` while leaving `/opt/broker-daemon/config.env` stale.
- The daemon reads config from `config.env` at boot, so the new code may be present but the feature stays disabled.
- For the demo payment stub, that exact mismatch caused `/v1/demo/shared-payment-token` to 404 even though the daemon code already contained the route.

Verification sequence
- Use SSM to inspect `/opt/broker-daemon/config.env` on the live instance.
- Confirm the new env var is present and has the expected value.
- Confirm the daemon file on disk contains the new route or code path.
- Restart the daemon only after the config file itself is correct.

Fix pattern
- Add the env var to `deploy.sh`.
- Add the env var to `scripts/bootstrap-control-plane.sh`.
- Redeploy.
- If the live instance is already out of sync, patch `config.env` and restart the daemon once before assuming the code is broken.
