#!/usr/bin/env python3
"""
Failing test for the real-Connect transfer path (RED phase of TDD).

Asserts that POST /create-transfer on the local backend returns a real
Stripe transfer ID (shape: `tr_...`) for a known destination account.

This test is EXPECTED TO FAIL until both of these are true:
  1. The Stripe account is activated as a Connect platform
     (https://dashboard.stripe.com/connect).
  2. A real connected account exists and its ID is passed as `destination`.

Run with:
    python3 test_real_transfer.py

Exit code 0 = pass (real transfers are wired), non-zero = fail (still mocked).
"""
import json
import os
import sys
import urllib.request
import urllib.error

BACKEND = os.environ.get("STRIPE_BACKEND_URL", "http://127.0.0.1:8790")
# Update this to a real connected account ID once Connect is activated.
DESTINATION = os.environ.get("STRIPE_TEST_DEST", "acct_1QAcmPLM5nrG2kAc")


def main() -> int:
    body = json.dumps({
        "amount_cents": 320,  # above sandbox minimum
        "destination": DESTINATION,
        "transfer_group": "test_group_tdd",
        "description": "TDD red-phase test transfer",
    }).encode()
    req = urllib.request.Request(
        f"{BACKEND}/create-transfer",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"FAIL: backend returned HTTP {e.code}: {body}")
        return 1
    except Exception as e:
        print(f"FAIL: could not reach backend at {BACKEND}: {e}")
        return 1

    tid = data.get("id", "")
    if not tid.startswith("tr_"):
        print(f"FAIL: transfer id {tid!r} does not start with 'tr_' — not a real Stripe transfer")
        return 1
    if tid.startswith("tr_test_mock_"):
        print(f"FAIL: transfer id {tid!r} is a mock fallback ID — broker-mock synthesized it")
        return 1

    print(f"PASS: real Stripe transfer created: {tid}")
    print(f"  destination: {data.get('destination')}")
    print(f"  amount:      {data.get('amount')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())