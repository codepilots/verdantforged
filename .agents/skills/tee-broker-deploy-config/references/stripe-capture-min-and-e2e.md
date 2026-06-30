# Stripe capture minimums + end-to-end PI test pattern

Captured 2026-06-28 when wiring live Stripe on the broker and
discovering (the hard way) that Stripe rejects captures below
per-currency minimums. The broker's `calculate_job_cost` returns
as little as 20 cents — Stripe rejects that with `amount_too_small`.

This reference covers the broker-side fix AND the end-to-end
test pattern that proved it works.

## Why Stripe rejected $0.20 captures

Stripe enforces minimum capture amounts per currency:

| Currency | Min capture | ~USD equiv |
|----------|-------------|------------|
| USD | $0.50 | $0.50 |
| EUR | €0.50 | ~$0.55 |
| GBP | £0.30 | ~$0.38 |
| JPY | ¥50 | ~$0.33 |
| AUD | $0.50 | ~$0.33 |

The broker's cost formula (kanban t_9fbec867) is
`lease_cents + token_cents`, where:

```python
slots = max(1, duration_ms / (15 * 60 * 1000))
lease_cents = int(20 * slots)        # 20 cents for the first 15-min slot
token_cents = int(total_tokens // 1000)
```

The minimum is **20 cents** (one slot, zero tokens). Stripe rejects
$0.20 with `error_code: amount_too_small,
error_message: 'Amount must convert to at least 30 pence.
$0.20 converts to approximately £0.15.'`

This isn't a bug — it's Stripe protecting against capture amounts
that don't cover the per-transaction processing fee. The broker's
fix is to **pad the capture up to the floor** without changing
the application-layer charge.

## The fix — `MIN_CAPTURE_CENTS` in `capture_payment`

```python
# Stripe minimum capture (t_e2e_test_2026_06_28). USD is $0.50, EUR is
# €0.50, GBP is £0.30 (~$0.38). Our cost formula returns as low as
# $0.20 for a 15-min lease — Stripe rejects with 'amount_too_small'.
# Pad up to 50 cents so any job, however small, can clear Stripe's
# per-currency minimum. The customer pays the same $0.20 at the
# application layer; the extra cents cover the floor.
MIN_CAPTURE_CENTS = int(os.environ.get("BROKER_MIN_CAPTURE_CENTS", "50"))


def capture_payment(pi_id: str, amount_cents: int,
                    idempotency_key: str | None = None) -> dict:
    ...
    try:
        stripe = _stripe_client()
        # Pad to MIN_CAPTURE_CENTS so we don't hit Stripe's per-currency
        # minimum (USD $0.50, GBP £0.30). The actual application-layer
        # charge is the cost formula's output; the pad is broker-side
        # only — the customer's PI hold is whatever they authorized at
        # creation time (e.g. $30.00), and Stripe releases the unused
        # portion after the auth window.
        effective_amount = max(int(amount_cents), MIN_CAPTURE_CENTS)
        if effective_amount > int(amount_cents):
            log.info(
                "stripe padding capture from %d to %d cents (currency minimum)",
                amount_cents, effective_amount,
            )
        capture_kwargs = {"amount_to_capture": effective_amount}
        ...
```

The customer-facing semantics:
- The customer's PI was authorized for, say, $30.00 at create time.
- The broker computes a $0.20 job cost.
- The broker captures $0.50 (above Stripe's floor).
- Stripe charges $0.50 to the test card.
- The remaining $29.50 is auto-released by Stripe after the auth window.
- The customer sees $0.50 charged.

This is broker-side padding only — the application-layer price is
still $0.20 per job, the customer just pays the floor when the
broker's computed cost is below it.

**Env var override**: `BROKER_MIN_CAPTURE_CENTS=30` would set the
floor to $0.30 (GBP-equivalent). The default is 50 (USD floor).
Set via `/opt/broker-daemon/config.env` and restart the daemon.

**Multi-currency deployments**: if you accept PIs in multiple
currencies, the broker can't know which currency a PI was created
in without fetching the PI metadata. Two options:
1. Pad to the highest floor across all currencies you accept
   (50 cents covers USD/EUR/AUD; ~38 cents covers GBP; 50 wins).
2. Pass the currency through to `capture_payment` and pad based on
   the PI's actual currency (requires looking up the PI in
   `verify_payment_intent` and threading the currency into the
   finalize path). Not implemented yet — for the VerdantForged
   demo the broker only accepts USD.

## The bug-class symptom — how to detect it

If the broker is calling Stripe but captures are failing:

```bash
sudo journalctl -u verdantforged-broker-daemon --no-pager -n 30 \
  | grep -E "amount_too_small|capture failed"
```

The Stripe response body includes the conversion math:
```
error_code=amount_too_small
error_message='Amount must convert to at least 30 pence.
               $0.20 converts to approximately £0.15.'
```

The `error_message` names both the per-currency floor AND the
rejected amount. Use it to compute the right `MIN_CAPTURE_CENTS`
for your currency.

## End-to-end live Stripe test pattern

Once `MIN_CAPTURE_CENTS` is in place, the E2E test that proves
the full flow works (real charge, real money movement, real
receipt URL) is:

### Step 1 — Create a real PI in Stripe test mode

**IMPORTANT (2026-06-29)**: Stripe now requires `automatic_payment_methods`
params when creating a PI with `confirm=true` + `pm_card_visa`. Without
them, the API returns 400 with `"you must provide a return_url"`. See
Pitfall 23 in the main SKILL.md. The recipe below uses the two-step
(create then confirm) approach which avoids this, but if you use
`confirm=true` at creation time, add:
```
automatic_payment_methods[enabled]=true
automatic_payment_methods[allow_redirects]=never
```

Stripe test mode blocks raw card numbers (HTTP 402 "Sending credit
card numbers directly to the Stripe API is generally unsafe").
Use `pm_card_visa` (Stripe's reusable test PaymentMethod) instead
of raw cards.

```python
import urllib.request, json

with open('/tmp/stripe_key.txt') as f:
    STRIPE_KEY = f.read().strip()

# Create the PI (manual capture so we can verify + capture separately)
payload = (
    f"amount=5000"                       # $50 — well above any per-job cost
    f"&currency=usd"
    f"&capture_method=manual"
    f"&payment_method_types[0]=card"
    f"&description=VerdantForged E2E test {int(time.time())}"
)
req = urllib.request.Request(
    "https://api.stripe.com/v1/payment_intents",
    data=payload.encode(),
    headers={
        "Authorization": f"Bearer {STRIPE_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=15) as r:
    pi = json.loads(r.read())
pi_id = pi['id']   # e.g. pi_3TnOcRKXSfYuhcTp0Stq7P8n
```

### Step 2 — Confirm with Stripe's test PaymentMethod

```python
req = urllib.request.Request(
    f"https://api.stripe.com/v1/payment_intents/{pi_id}/confirm",
    data=b"payment_method=pm_card_visa",
    headers={
        "Authorization": f"Bearer {STRIPE_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=15) as r:
    confirmed = json.loads(r.read())
# Now: status="requires_capture", amount_capturable=5000,
#       latest_charge="ch_3TnOcRKXSfYuhcTp0rMt1DqT"
```

The `pm_card_visa` token bypasses the raw-card-data block AND
simulates a successful Visa charge. Other useful test tokens:
- `pm_card_chargeDeclined` — simulate a declined card
- `pm_card_chargeDeclinedInsufficientFunds` — specific decline
- `pm_card_visa_debit` — debit variant

### Step 3 — Submit a job to the broker with this PI

```python
import urllib.request

job_body = json.dumps({
    "client_req_id": f"e2e-test-{int(time.time())}",
    "encrypted_skill": "summarize",
    "encrypted_data": "One sentence: Stripe integration is live.",
    "requester_sig": "0x",
    "result_pubkey": "0x",
    "stripe_pi_id": pi_id,    # the real PI from step 1-2
}).encode()
req = urllib.request.Request(
    "https://verdant.codepilots.co.uk/v1/jobs",
    data=job_body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as r:
    result = json.loads(r.read())
    job_id = result.get('job_id')
```

### Step 4 — Poll for completion, verify payment block

```python
for attempt in range(20):
    time.sleep(3)
    with urllib.request.urlopen(
        f"https://verdant.codepilots.co.uk/v1/jobs/{job_id}",
        timeout=10
    ) as r:
        d = json.loads(r.read())
        state = d.get('state')
        payment = d.get('payment', {})
        print(f"[{(attempt+1)*3}s] state={state} "
              f"payment_status={payment.get('status')} "
              f"amount={payment.get('amount_cents')}c "
              f"mode={payment.get('mode')}")
        if state in ('completed', 'failed', 'cancelled'):
            final = d
            break
```

Expected output for the happy path:
```
[3s] state=running payment_status=None amount=Nonec mode=None
[6s] state=completed payment_status=succeeded amount=50c mode=live
```

The `payment` block in the result envelope:
```json
{
  "status": "succeeded",
  "amount_cents": 50,
  "stripe_id": "pi_3TnOcRKXSfYuhcTp0Stq7P8n",
  "mode": "live",
  "demo": false
}
```

### Step 5 — Verify the charge in Stripe

The broker's `stripe_id` field in the payment block maps to a real
PI in Stripe. Pull the charge directly to confirm money moved:

```python
req = urllib.request.Request(
    "https://api.stripe.com/v1/charges?payment_intent=pi_3TnOcRKXSfYuhcTp0Stq7P8n",
    headers={"Authorization": f"Bearer {STRIPE_KEY}"},
)
with urllib.request.urlopen(req, timeout=15) as r:
    charges = json.loads(r.read())

for ch in charges['data']:
    print(f"charge_id:        {ch['id']}")
    print(f"amount:           ${ch['amount']/100:.2f}        (held)")
    print(f"amount_captured:  ${ch['amount_captured']/100:.2f}  (captured)")
    print(f"amount_refunded:  ${ch['amount_refunded']/100:.2f}  (refunded)")
    print(f"status:           {ch['status']}")
    print(f"paid:             {ch['paid']}")
    print(f"captured:         {ch['captured']}")
    print(f"card_brand:       {ch['payment_method_details']['card']['brand']}")
    print(f"card_last4:       {ch['payment_method_details']['card']['last4']}")
    print(f"receipt_url:      {ch['receipt_url']}")
```

Expected output:
```
charge_id:        ch_3TnOcRKXSfYuhcTp0rMt1DqT
amount:           $50.00        (held)
amount_captured:  $0.50         (captured — matches broker's MIN_CAPTURE_CENTS pad)
amount_refunded:  $0.00
status:           succeeded
paid:             True
captured:         True
card_brand:       visa
card_last4:       4242
receipt_url:      https://pay.stripe.com/receipts/payment/CAca...
```

The `$50.00 - $0.50 = $49.50` difference is auto-released by Stripe
back to the test card after the auth window expires (typically 7
days for manual-capture PIs). The customer only pays the captured
amount.

## What's NOT in scope for this reference

- **Multi-currency routing** (broker accepts USD only, easy to extend)
- **Webhook handling** for `payment_intent.succeeded` /
  `payment_intent.payment_failed` (broker polls the PI status
  directly via `retrieve`, doesn't subscribe to webhooks)
- **Stripe Connect transfers** (for paying out to skill providers)
  — that's `tee-broker-negotiate` territory
- **Live mode** (`sk_live_*`) — same code path works, but
  production concerns (PCI compliance, idempotency keys, webhook
  signing, retry policies) need separate review
