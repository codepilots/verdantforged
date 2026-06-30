# Demo payment stub mode for regional-lockout cases

Use this when a user cannot authenticate with Stripe Link in their region but still needs to demo the broker's payment gate and job execution path.

What was added
- `BROKER_PAYMENT_STUB_MODE=1` enables a broker-side stub path.
- `POST /v1/demo/shared-payment-token` returns a synthetic `spt_demo_...` token.
- `scripts/run_file_job_e2e.py --demo-spt` mints the stub token and then runs the normal file-job flow.

Behavior
- The broker still emits the same 402 payment challenge when payment is missing.
- The stub token is accepted by the normal submit path only when stub mode is enabled.
- No Stripe Link auth is needed and no live funds move.

Operational notes
- Keep the stub off for real payment processing.
- Use this for demos, integration tests, and regional lockout workarounds.
- When deploying from `deploy.sh`, ensure `BROKER_PAYMENT_STUB_MODE` is exported into `config.env` during bootstrap so the live control plane sees the flag.
