# Demo SPT charge gate — BROKER_TEST_SPT_ISSUER hybrid mode bug

Use this when `--demo-spt` E2E runs fail with HTTP 402 `agent payment failed: Stripe ACS API error 400: No such shared_payment_token: 'spt_demo_...'`.

Observed symptom
- `POST /v1/demo/shared-payment-token` returns 200 with a valid `spt_demo_...` token.
- `POST /v1/jobs` with that token returns 402 `agent payment failed: Stripe ACS API error 400: resource_missing: No such shared_payment_token`.
- The demo token was minted successfully but the broker sent it to the real Stripe ACS API instead of routing it to the demo stub branch.

Root cause (discovered 2026-06-30)

The `BROKER_TEST_SPT_ISSUER` hybrid mode was added in commit 8ce2806 to allow demo SPT minting alongside real Stripe. The fix touched three sites in `broker-daemon/daemon.py` but the first charge gate was missed:

1. Token extraction (`_agent_payment_token_from_request`): PATCHED — `_accept_demo()` includes `BROKER_TEST_SPT_ISSUER`, so `spt_demo_...` is extracted from the request body/headers.

2. Demo mint endpoint (`demo_shared_payment_token`): PATCHED — returns 200 when `BROKER_TEST_SPT_ISSUER` is on, even with `STRIPE_SECRET_KEY` set.

3. Charge routing in `submit_job`: PARTIALLY PATCHED —
   - The real-Stripe branch: `if STRIPE_SECRET_KEY and not payment_stub_mode:` — does NOT exclude `spt_demo_...` tokens when `BROKER_TEST_SPT_ISSUER` is on, so demo tokens get sent to real Stripe and fail.
   - The demo stub branch: `elif (payment_stub_mode or BROKER_TEST_SPT_ISSUER) and spt_token.startswith("spt_demo_"):` — correct but never reached because the first `if` catches the token first.

The bug: `payment_stub_mode` is set to `BROKER_PAYMENT_STUB_MODE` only (line ~2784). It does not include `BROKER_TEST_SPT_ISSUER`. So when `BROKER_TEST_SPT_ISSUER=1` and `BROKER_PAYMENT_STUB_MODE=0`:
- `_accept_demo()` returns True → token extracted
- `STRIPE_SECRET_KEY and not payment_stub_mode` → True → real Stripe charge attempted → 400

Fix (applied commit 66c49b7, 2026-06-30)

Four sites in `broker-daemon/daemon.py` needed patching — the original
diagnosis only caught site 1, but sites 2-4 were the same class of bug
(hybrid mode has real `STRIPE_SECRET_KEY` set, so `not STRIPE_SECRET_KEY`
demo guards are False, and `pi_demo_...` IDs leak into live Stripe calls):

1. **submit_job charge gate** (~line 2799): The primary bug. Change:
   ```python
   if STRIPE_SECRET_KEY and not payment_stub_mode:
   ```
   to:
   ```python
   if STRIPE_SECRET_KEY and not payment_stub_mode and not (
       BROKER_TEST_SPT_ISSUER and spt_token.startswith("spt_demo_")
   ):
   ```
   Demo tokens now skip the real Stripe call and fall through to the
   `elif` demo stub branch.

2. **capture_payment** (~line 477): `pi_demo_...` IDs must short-circuit
   to the demo return. Changed `if not STRIPE_SECRET_KEY:` to
   `if not STRIPE_SECRET_KEY or pi_id.startswith("pi_demo_"):`.
   Without this, demo jobs completing in hybrid mode would call
   `stripe.PaymentIntent.capture()` on a non-existent PI.

3. **refund_payment** (~line 628): Same fix — `pi_demo_...` IDs must
   not reach `stripe.Refund.create()`.

4. **verify_payment_intent** (~line 399): Same fix — `pi_demo_...` IDs
   must return `(True, "stripe_disabled", 0)` without calling
   `stripe.PaymentIntent.retrieve()`.

General pattern: any function that gates on `not STRIPE_SECRET_KEY` to
decide demo vs live must also check for `pi_demo_...` prefix, because
hybrid mode (`BROKER_TEST_SPT_ISSUER=1`) has real Stripe keys set but
still produces synthetic `pi_demo_...` IDs for demo-token jobs.

Diagnostic technique

When diagnosing broker payment failures, always check all gates that a
demo SPT must pass through:

1. Extraction: Is `BROKER_TEST_SPT_ISSUER` (or `BROKER_PAYMENT_STUB_MODE`
   or empty `STRIPE_SECRET_KEY`) enabling `_accept_demo()`?
2. Mint endpoint: Does `/v1/demo/shared-payment-token` return 200 or 404?
3. Charge routing (submit_job): Does the first
   `if STRIPE_SECRET_KEY and not payment_stub_mode:` branch short-circuit
   before the `elif` demo stub branch?
4. capture_payment / refund_payment / verify_payment_intent: Do these
   functions short-circuit on `pi_demo_...` IDs, or do they fall through
   to real Stripe API calls?

The `pi_demo_` prefix check is the key pattern. Any function that gates
demo vs live on `not STRIPE_SECRET_KEY` will fail in hybrid mode because
`STRIPE_SECRET_KEY` is set. The `pi_demo_` prefix is the reliable signal.

Use `curl -sv` against the live broker to reproduce:
```bash
# Mint demo SPT
curl -s https://broker.example/v1/demo/shared-payment-token \
  -X POST -H 'Content-Type: application/json' \
  -d '{"amount_cents":500,"currency":"usd","networkId":"demo_network"}'

# Submit job with that SPT — observe 402 with Stripe error
curl -sv https://broker.example/v1/jobs \
  -X POST -H 'Content-Type: application/json' \
  -d '{"client_req_id":"diag-1","encrypted_skill":"code-review","encrypted_data":"test","requester_sig":"0x","result_pubkey":"dGVzdA==","input_files":[],"shared_payment_token":"spt_demo_..."}'
```

If the error message contains `Stripe ACS API error 400: resource_missing: No such shared_payment_token`, the first charge gate is not excluding demo tokens. If the error is a clean `payment required` 402 with `WWW-Authenticate`, the token was never extracted (check `_accept_demo()` conditions).

## Correct SPT usage pattern for E2E job submission (2026-06-30)

The demo SPT is minted via `POST /v1/demo/shared-payment-token` (empty body,
returns `spt_demo_...` in the `spt` field). The SPT must be passed as the
`stripe_pi_id` field in the `POST /v1/jobs` body — NOT as the
`Authorization` header. The job response includes a `job_access_token`
(`jobtok_...`); that token is used as the `Authorization: Bearer` header
for polling `GET /v1/jobs/<job_id>`.

Full working sequence:
1. `POST /v1/demo/shared-payment-token` (empty body) -> `spt`
2. `POST /v1/jobs` with `{"stripe_pi_id": "<spt>", ...}` -> `job_id`, `job_access_token`
3. `GET /v1/jobs/<job_id>` with `Authorization: Bearer <...040e`

Common mistake: passing the SPT in the `Authorization` header instead of
`stripe_pi_id`. This results in HTTP 402 because the broker's payment gate
extracts the SPT from the body, not the header.