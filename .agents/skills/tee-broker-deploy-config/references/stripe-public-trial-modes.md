# Stripe payment-intent trial modes and ACS/SPT test flow

Use this note when deciding how to let outside users try the broker’s payment flow.

Current implementation facts:
- The broker now uses an ACS-style 402 challenge on `POST /v1/jobs` when payment is missing.
- The `WWW-Authenticate` header includes `amount`, `currency`, `method=stripe`, and `networkId`.
- The client then mints a Stripe Shared Payment Token (`spt_...`) and retries the job request.
- The broker creates/confirms the PaymentIntent server-side using its own Stripe secret; clients never create the PaymentIntent directly.

Link CLI test-mode facts:
- `npx @stripe/link-cli spend-request create --credential-type shared_payment_token --test ...` creates test-mode credentials.
- `--test` uses test card data, so no real payment method is needed.
- `link-cli auth login` is still required first; test mode does not bypass Link authentication.
- `link-cli demo --only-spt` is the quickest interactive smoke test for the machine-payment flow.
- `link-cli spend-request create` requires `--network-id`, `--amount`, `--currency`, and a 100+ character `--context`.

Practical options:
1. Demo mode: leave Stripe unset and validate payload shape without touching Stripe.
2. Shared test account: keep one test secret on the broker and let users submit jobs against that shared account.
3. Bring-your-own Stripe: use per-user credential routing (product change) if the broker must charge separate accounts.
4. ACS/SPT test mode: use Link CLI `--test` + shared_payment_token to mint an SPT for broker-side end-to-end testing.

Important implication:
- A user should not create a PaymentIntent with their own Stripe key and expect the current broker to manage it unless the broker is authorized on that Stripe account.
- For public demos and automated tests, prefer demo mode or ACS/SPT test mode; BYO Stripe is a product change, not a deploy toggle.

Suggested verification loop:
1. `link-cli auth login`
2. `link-cli spend-request create --test --credential-type shared_payment_token ...`
3. Submit `POST /v1/jobs` with the resulting `spt_...`
4. Confirm the broker accepts the token, creates the PaymentIntent, and proceeds to worker execution.
