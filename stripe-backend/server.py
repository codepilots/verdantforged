#!/usr/bin/env python3
"""
VerdantForged Stripe Backend — tiny HTTP server for the demo.

Runs on port 8789. Exposes:
  POST /create-payment-intent   → creates a real Stripe test-mode PaymentIntent
  POST /create-transfer          → creates a real Stripe test-mode Transfer to a connected account
  POST /refund                   → refunds a PaymentIntent
  GET  /health                   → health check

The secret key is read from the STRIPE_SECRET_KEY environment variable.
CORS is open for the demo (restricted to the tunnel origin in production).

Usage:
  STRIPE_SECRET_KEY=sk_test_... python3 server.py
"""

import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

import stripe

# ── Config ──────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", "8790"))
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

# Allowed origins for CORS (add your tunnel URL here)
ALLOWED_ORIGINS = [
    "https://codepilots.github.io",
    "https://verdantforged.dev",
    "http://localhost:4321",
    "http://localhost:8789",
    "*"  # fallback — demo mode, allow all
]

if not STRIPE_SECRET_KEY:
    print("WARNING: STRIPE_SECRET_KEY not set — server will start but API calls will fail.")
    print("Set it with: STRIPE_SECRET_KEY=sk_test_... python3 server.py")
else:
    stripe.api_key = STRIPE_SECRET_KEY
    print(f"Stripe backend ready on port {PORT} (test mode)")


class StripeHandler(BaseHTTPRequestHandler):
    def _cors_headers(self):
        origin = self.headers.get("Origin", "")
        if "*" in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "stripe_configured": bool(STRIPE_SECRET_KEY),
                "mode": "test",
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not STRIPE_SECRET_KEY:
            self._send_json(500, {"error": "STRIPE_SECRET_KEY not set on server"})
            return

        body = self._read_body()

        try:
            if self.path == "/create-payment-intent":
                amount_cents = int(body.get("amount_cents", 0))
                description = body.get("description", "VerdantForged demo")
                currency = body.get("currency", "usd")

                intent = stripe.PaymentIntent.create(
                    amount=amount_cents,
                    currency=currency,
                    description=description,
                    payment_method_types=["card"],
                    confirm=False,
                    metadata={
                        "source": "verdantforged-demo",
                        "session_id": body.get("session_id", ""),
                    },
                )
                self._send_json(200, {
                    "id": intent.id,
                    "amount": intent.amount,
                    "currency": intent.currency,
                    "client_secret": intent.client_secret,
                    "status": intent.status,
                })

            elif self.path == "/create-transfer":
                amount_cents = int(body.get("amount_cents", 0))
                destination = body.get("destination", "")  # acct_... connected account
                transfer_group = body.get("transfer_group", "")
                description = body.get("description", "")

                if not destination:
                    self._send_json(400, {"error": "destination (acct_*) required"})
                    return

                # NOTE: stripe-py 15.x dropped the `transfer_type` parameter
                # (it was never needed for `stripe_account` transfers — the
                # default is already the destination-account transfer). Sending
                # it now raises InvalidRequestError, so we omit it. The
                # transfer_group is set in metadata for grouping/MPP narrative;
                # the top-level `transfer_group` field is reserved for
                # PaymentIntents that fund the platform balance.
                transfer = stripe.Transfer.create(
                    amount=amount_cents,
                    currency="usd",
                    destination=destination,
                    description=description,
                    metadata={
                        "source": "verdantforged-demo",
                        "transfer_group": transfer_group,
                    },
                )
                self._send_json(200, {
                    "id": transfer.id,
                    "amount": transfer.amount,
                    "destination": transfer.destination,
                    "currency": transfer.currency,
                })

            elif self.path == "/refund":
                payment_intent_id = body.get("payment_intent_id", "")
                if not payment_intent_id:
                    self._send_json(400, {"error": "payment_intent_id required"})
                    return

                refund = stripe.Refund.create(
                    payment_intent=payment_intent_id,
                    metadata={"source": "verdantforged-demo"},
                )
                self._send_json(200, {
                    "id": refund.id,
                    "amount": refund.amount,
                    "status": refund.status,
                    "payment_intent": refund.payment_intent,
                })

            else:
                self._send_json(404, {"error": f"unknown path: {self.path}"})

        except stripe.error.StripeError as e:
            print(f"Stripe API error: {e}", file=sys.stderr)
            self._send_json(400, {
                "error": str(e),
                "type": type(e).__name__,
            })
        except Exception as e:
            print(f"Server error: {e}", file=sys.stderr)
            self._send_json(500, {"error": str(e)})

    def log_message(self, format, *args):
        # Minimal logging
        print(f"[{self.address_string()}] {format % args}")


def main():
    server = HTTPServer(("0.0.0.0", PORT), StripeHandler)
    print(f"VerdantForged Stripe backend listening on :{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()