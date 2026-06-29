# Stripe Connect Setup for VerdantForged Demo

This document explains what needs to be done to complete the Stripe Connect integration for the VerdantForged demo.

## Current Status

The broker-mock.ts file has been updated to call the real Stripe backend for creating transfers instead of generating synthetic IDs. The changes include:

1. Added a `createStripeTransfer` helper function that calls the Stripe backend
2. Modified the transfer creation logic to use real Stripe transfers
3. The backend server.py already handles transfer groups correctly

## What Still Needs to Be Done

### 1. Create Real Stripe Connect Accounts

To complete the integration, you need to:

1. Log in to the Stripe dashboard at https://dashboard.stripe.com/
2. Go to Connect → Accounts
3. Create 1-2 connected accounts for the demo providers:
   - Acme Code Review Inc.
   - DocDigest Labs
   - (Additional providers as needed)

### 2. Update Account IDs in skill-catalog.ts

Once you have real Connect account IDs, update the `provider_acct` fields in `src/lib/skill-catalog.ts`:

```typescript
// Before (synthetic)
provider_acct: 'acct_1QAcmPLM5nrG2kAc',

// After (real)
provider_acct: 'acct_real_stripe_connect_id_here',
```

### 3. Configure Stripe Secret Key

Create a `.env` file in the `stripe-backend/` directory with your Stripe secret key:

```env
STRIPE_SECRET_KEY=sk_test_your_stripe_secret_key_here
```

### 4. Test the Integration

1. Start the Stripe backend server:
   ```bash
   cd stripe-backend
   python3 server.py
   ```

2. Run the demo and approve a plan
3. Check that real transfer IDs (starting with `tr_`) appear in the receipts panel
4. Verify transfers appear in the Stripe dashboard under the connected accounts

## Verification Steps

After completing the setup:

1. ✅ Approve a 2-step plan in the demo
2. ✅ See 2 real `tr_...` IDs surface in the receipts panel (not synthetic `tr_test_...`)
3. ✅ Verify real Connect transfers are visible in the Stripe dashboard under the connected accounts

## Troubleshooting

If transfers are still synthetic:

1. Check that the Stripe backend is running and accessible
2. Verify the STRIPE_SECRET_KEY is correctly configured
3. Check browser console for any errors in the fetch calls
4. Ensure the broker-mock.ts changes are properly compiled
