# Stripe Payment Demo User Guide

This guide walks you through setting up and running a Stripe payment demo using the Stripe Link CLI (`@stripe/link-cli`) in test mode. 

---

## Prerequisites & Key Nuances

* **Link CLI Authentication:** Running commands in `--test` mode removes the need for a real payment method (it uses the test card `4242 4242 4242 4242` automatically), but **it does not remove the need for authentication**. You must log in first.
* **Authentication Error:** If you see a `Not authenticated` error, ensure you have successfully completed the authentication step.
* Note that this route is only available in the USA currently, other users can try the system using the demo passthrough.
---

## Step-by-Step Integration Guide

### 1. Authenticate with Stripe Link CLI
Log in to your Stripe Link CLI account:

```bash
npx @stripe/link-cli auth login --client-name "VerdantForged test"
```

### 2. Create a Test-Mode Shared Payment Token (SPT)
Generate a test-mode shared payment token request by running:

```bash
npx @stripe/link-cli spend-request create \
  --test \
  --credential-type shared_payment_token \
  --network-id <YOUR_NETWORK_ID> \
  --amount 130 \
  --currency usd \
  --context "Testing the VerdantForged ACS/SPT broker flow in test mode. No real funds should move." \
  --line-item "name:TEE broker job,quantity:1,unit_amount:130" \
  --total "type:total,display_text:Total,amount:130" \
  --approve
```

### 3. Run the Broker Job with the Token
Provide the generated `spt_...` token to the broker e2e script:

```bash
python3 scripts/run_file_job_e2e.py --spt <YOUR_SPT_TOKEN> --file ../BUGS.md --file deploy_skill_library.sh
```

---

## Easiest Smoke Test (Built-in Demo)

If you want a quick verification without manually building a request, you can run the built-in demo:

```bash
npx @stripe/link-cli demo --only-spt
```

This command guides you through the machine-payment flow and automatically handles request construction.

