# Payment Flow

This broker uses Stripe Agentic Commerce Suite / Machine Payments Protocol for agent-initiated payments.

## ACS / SPT workflow

1. Client submits `POST /v1/jobs` without a payment credential.
2. In live Stripe mode, the broker responds with `402 Payment Required` and a `WWW-Authenticate` challenge:

```http
WWW-Authenticate: Payment amount="130", currency="usd", method="stripe", networkId="profile_test_61UxANkhMMDN58EdHA6UxANjDhE9lwNXZMFWQJI6y3Mu"
```

3. The client/agent uses Stripe Link / ACS tooling to grant a Shared Payment Token (`spt_...`) for that challenge.
4. The client retries `POST /v1/jobs` with the same job payload plus one of:
   - JSON field `shared_payment_token: "spt_..."`
   - JSON field `spt: "spt_..."`
   - HTTP header `Payment: spt_...`
5. The broker keeps `STRIPE_SECRET_KEY` server-side, optionally inspects the SPT, then creates and confirms a Stripe PaymentIntent with:

```text
payment_method_data[shared_payment_granted_token]=spt_...
confirm=true
```

6. On successful charge, the broker stores the returned `pi_...` on the job and dispatches the attested worker.

## Demo / stub mode

If you cannot use Stripe Link in your region, enable the broker stub with:

```bash
export BROKER_PAYMENT_STUB_MODE=1
```

In that mode the broker still emits the same 402 challenge when payment is missing, but you can mint a synthetic token locally:

```bash
curl -sS -X POST https://verdant.codepilots.co.uk/v1/demo/shared-payment-token \
  -H 'Content-Type: application/json' \
  -d '{"amount_cents":130,"currency":"usd"}'
```

The response includes a `spt_demo_...` token. Retry `POST /v1/jobs` with that token in `shared_payment_token`, `spt`, or `Payment:` and the broker will run the normal job path without contacting Stripe.

The E2E helper also supports this directly:

```bash
python3 scripts/run_file_job_e2e.py --demo-spt --file ../BUGS.md --file deploy_skill_library.sh
```

This is intended for demos, integration tests, and regional lockout cases. Leave the stub disabled for real payment processing.

## Configuration

- `STRIPE_SECRET_KEY`: stored on AWS SSM Parameter Store and read by the control-plane bootstrap. Never pass this to clients.
- `STRIPE_NETWORK_ID` / `STRIPE_MERCHANT_PROFILE_ID`: merchant profile id used in the 402 challenge. Current test id:
  `profile_test_61UxANkhMMDN58EdHA6UxANjDhE9lwNXZMFWQJI6y3Mu`
- `STRIPE_ACS_VERSION`: default `2026-04-22.preview`.
- `STRIPE_CURRENCY`: default `usd`.

## Legacy mode

Older test scripts could create a PaymentIntent client-side and submit `stripe_pi_id`. That flow is no longer the live agent payment path because it requires client access to a Stripe secret. It is kept only as a local/demo compatibility path.

## Webhook payload shape

When a job is finalized (`state=completed`), the broker delivers a POST request to the configured webhook URL.

```json
{
  "job_id": "job_abc123",
  "state": "completed",
  "result": {
    "output": "The result of the TEE execution",
    "artifacts": {
      "files": [
        { "filename": "result.png", "size": 1024 }
      ]
    }
  },
  "artifact_urls": {
    "manifest": "https://broker.example.com/v1/jobs/job_abc123/artifacts",
    "files": {
      "result.png": "https://broker.example.com/v1/jobs/job_abc123/artifacts/result.png"
    }
  },
  "payment": {
    "status": "succeeded",
    "amount_cents": 150,
    "original_pi_id": "pi_123",
    "topup_pi_id": null,
    "shortfall_cents": 0
  }
}
```
