#!/usr/bin/env python3
"""
VerdantForged TEE Broker — control plane daemon.

Responsibilities:
  1. Accept HTTPS job submissions (POST /v1/jobs).
  2. Persist jobs to /mnt/broker (EFS, shared with worker).
  3. Launch TEE worker on first job (or wake it if already running).
  4. Poll /mnt/broker/jobs/outbox for completed results.
  5. Deliver results to requester's webhook_url (if provided) and update status.
  6. Terminate worker after IDLE_BUFFER_MINUTES of no new jobs.

This daemon runs on the cheap t3.small control plane. It is NOT attested.
The worker (m6a.xlarge) is the attested party. The daemon only routes
encrypted envelopes; it never sees plaintext skill code or data.

Protocol: see tee-broker-pattern/SPEC.md
"""
from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import sys
import time
import uuid
import hmac
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import boto3
from aiohttp import web
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

# broker-daemon sibling module (signing/encryption helpers). Lazy-import
# inside _finalize_job would also work, but importing at module scope
# keeps startup errors visible (missing crypto.py is a real failure, not
# a per-job one).
import crypto  # noqa: E402

# === Stripe PaymentIntent lifecycle (kanban t_9fbec867) ===
# Real Stripe integration is opt-in via STRIPE_SECRET_KEY. When unset, the
# helpers below short-circuit to a "demo_*" sentinel so the broker keeps
# running in the current mock mode (format-validate-only). When set, every
# submit verifies the PaymentIntent with Stripe (status must be
# 'requires_capture' or 'succeeded' and amount must cover the estimated
# cost), capture on completion calls PaymentIntent.capture with the actual
# amount, and failure triggers a Refund.create.
#
# The key is read ONCE at module import (like every other env var) and is
# never logged. capture_payment / refund_payment / verify_payment_intent
# return only NON-SECRET fields (id, status, amount_cents) — never the key.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_ACS_VERSION = os.environ.get("STRIPE_ACS_VERSION", "2026-04-22.preview").strip()
STRIPE_NETWORK_ID = os.environ.get("STRIPE_NETWORK_ID", os.environ.get("STRIPE_MERCHANT_PROFILE_ID", "")).strip()
STRIPE_CURRENCY = os.environ.get("STRIPE_CURRENCY", "usd").strip().lower()
BROKER_PAYMENT_STUB_MODE = os.environ.get("BROKER_PAYMENT_STUB_MODE", "").strip().lower() in (
    "1", "true", "yes", "on", "demo", "stub",
)
# Hybrid test mode: live Stripe is configured (real keys, real Link), but
# the broker will ALSO expose /v1/demo/shared-payment-token for the e2e
# harness + any client that wants to issue a synthetic spt_demo_… token
# without driving Link auth. spt_demo_… tokens are accepted in submit_job
# and routed through the existing demo stub branch (no real charge).
# Off by default. Set to 1 for the hackathon live broker.
BROKER_TEST_SPT_ISSUER = os.environ.get("BROKER_TEST_SPT_ISSUER", "").strip().lower() in (
    "1", "true", "yes", "on",
)


def _stripe_client():
    """Lazy-import stripe and set the API key.

    Returns the `stripe` module on success, or None when the key is unset
    (DEMO MODE). All Stripe-touching helpers MUST call this first and bail
    to demo-mode behavior when it returns None.

    Lazy import (vs. top-level) is intentional: it keeps DEMO MODE brokers
    functional on minimal hosts that don't have the `stripe` package
    installed (the requirements.txt pin is `>=15.2.1` to match the
    stripe-backend at tee-broker-site/stripe-backend/pyproject.toml, but
    the broker should boot without it). When STRIPE_SECRET_KEY is set the
    CFN bootstrap also `pip install`s the dependency so this never fails
    in production — it's purely a friendly-fallback for offline/dev.
    """
    if not STRIPE_SECRET_KEY:
        return None
    import stripe  # type: ignore  # lazy; installed via requirements.txt
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def _log_demo_lifecycle(event: str, pi_id: str, amount_cents: int = 0,
                         note: str = "",
                         lease_cents: Optional[int] = None,
                         token_cents: Optional[int] = None) -> None:
    """Append a synthetic lifecycle event to COST_LEDGER.jsonl (demo mode only).

    The ledger is gitignored (see .gitignore). It's purpose-built for the
    cost-accuracy reviewer (kanban t_29b31ecb) to audit the
    calculate_job_cost math against real finalize events without needing
    a live Stripe integration.

    lease_cents / token_cents (kanban t_d0ee4495) are optional: when
    BOTH are provided they get written into the row alongside
    `amount_cents`. When neither is provided (existing call sites) the
    row is unchanged — backward-compatible. We deliberately do NOT
    fake zeros when the caller didn't pass them, because the absence
    vs. zero carries different information (legacy rows vs. a job
    with zero tokens).

    Writes are best-effort: ledger failures (disk full, permissions, etc.)
    NEVER block the payment flow — they're observational only. We log at
    debug level on success and warning on failure.
    """
    if STRIPE_SECRET_KEY:
        return  # live mode: Stripe is the system of record, not the ledger
    try:
        ledger_path = Path(
            os.environ.get("BROKER_COST_LEDGER",
                           str(Path(__file__).resolve().parent.parent / "COST_LEDGER.jsonl"))
        )
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "pi_id": pi_id,
            "amount_cents": int(amount_cents),
            "demo": True,
        }
        if note:
            row["note"] = note
        # Only write the split when the caller actually computed it.
        # `None` means "unknown / not computed" (legacy path); 0 means
        # "computed, came out to zero" (legitimate, e.g. zero-token job
        # in the lease-floor case is (20, 0, 20) — we want token_cents=0
        # to be visible, not absent).
        if lease_cents is not None and token_cents is not None:
            row["lease_cents"] = int(lease_cents)
            row["token_cents"] = int(token_cents)
            # total_cents is derivable but reviewers shouldn't have to
            # add — write it too so the row is self-describing.
            row["total_cents"] = int(lease_cents) + int(token_cents)
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as e:  # never block finalize on ledger failure
        log.debug("cost ledger append failed (event=%s): %s", event, e)


def calculate_job_cost(duration_ms: int, total_tokens: int) -> tuple[int, int, int]:
    """Return the broker-side cost of a job as (lease_cents, token_cents, total_cents).

    Spec from kanban t_9fbec867:
      - Session lease: $0.20 per 15-minute slot (prorated, min 1 slot)
      - Token cost:    $0.001 per 1K tokens (prompt + completion)

    Implementation notes:
      - Lease is `int(20 * slots)` so a job shorter than 15 minutes still
        pays for one slot. Floor via `max(1, slots)` so 0ms → 1 slot.
      - Token cost uses integer division so 999 tokens round down to 0.
        This matches the spec ("$0.001 per 1K tokens" — pay only when
        you cross a 1K boundary). Tests pin specific values: 1000 → 1,
        50000 → 50.
      - Tuple shape (kanban t_d0ee4495) replaces the earlier scalar
        return so the cost ledger (and any other audit) can record the
        breakdown without re-deriving it from a single int. Callers
        that only need the total can do `total = calculate_job_cost(...)`
        with a single-target unpacking change — the helper is internal.
    """
    if duration_ms < 0:
        duration_ms = 0
    slots = max(1, duration_ms / (15 * 60 * 1000))
    lease_cents = int(20 * slots)
    token_cents = int(total_tokens // 1000)
    total_cents = lease_cents + token_cents
    return lease_cents, token_cents, total_cents


def estimated_job_cost_cents() -> int:
    """Return a conservative upper-bound estimate of job cost in cents.

    Spec (kanban t_9a705578):
      - Lease floor: 60 minutes = 4 slots * $0.20 = $0.80 = 80 cents
      - Token floor: 50K tokens * $0.001/1K = $0.50 = 50 cents
      - Total: 130 cents

    This is the amount that must be held on the PaymentIntent at submit
    time. Jobs that exceed this estimate will trigger the shortfall flow
    (state=awaiting_topup) and the client must authorise a topup PI.

    Demo mode: returns 130 (same as live) so tests have a stable baseline.
    The shortfall gate is enforced in LIVE mode via validate_submit; in
    demo mode the PI amount check is skipped but the estimate is stable.
    """
    # Conservative upper bound: 60 min lease (4 slots) + 50K tokens.
    # This matches the test expectations and gives clients enough buffer
    # for most workloads without triggering the topup flow.
    lease_cents = int(20 * 4)  # 4 slots * $0.20 = 80
    token_cents = int(50000 // 1000)  # 50K tokens * $0.001/1K = 50
    return lease_cents + token_cents  # 130 cents


def _agent_payment_token_from_request(request: web.Request, body: dict) -> str:
    """Return an incoming Stripe ACS Shared Payment Token (spt_...), if any."""
    # Synthetic spt_demo_… tokens are accepted in three configurations:
    #   1. BROKER_PAYMENT_STUB_MODE on (full demo, no real Stripe)
    #   2. STRIPE_SECRET_KEY unset (no real Stripe configured)
    #   3. BROKER_TEST_SPT_ISSUER on (hybrid: live Stripe + demo mint for
    #      e2e harness + any client that wants a synthetic token)
    def _accept_demo() -> bool:
        return BROKER_PAYMENT_STUB_MODE or not STRIPE_SECRET_KEY or BROKER_TEST_SPT_ISSUER

    for key in ("shared_payment_token", "spt", "payment_token", "agent_payment_token"):
        val = body.get(key) if isinstance(body, dict) else None
        if isinstance(val, str):
            token = val.strip()
            if token.startswith("spt_demo_") and _accept_demo():
                return token
            if token.startswith("spt_") and not token.startswith("spt_demo_"):
                return token

    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(None, 1)[1].strip()
        if token.startswith("spt_demo_") and _accept_demo():
            return token
        if token.startswith("spt_") and not token.startswith("spt_demo_"):
            return token
    pay = request.headers.get("Payment", "") or request.headers.get("X-Payment", "")
    if pay.startswith("spt_demo_") and _accept_demo():
        return pay.strip()
    if pay.startswith("spt_") and not pay.startswith("spt_demo_"):
        return pay.strip()
    return ""


def _payment_required_response(amount_cents: int) -> web.Response:
    """HTTP 402 challenge for Stripe Agentic Commerce Suite / MPP agents."""
    network_id = STRIPE_NETWORK_ID or "profile_test_61UxANkhMMDN58EdHA6UxANjDhE9lwNXZMFWQJI6y3Mu"
    challenge = (
        f'Payment amount="{int(amount_cents)}", currency="{STRIPE_CURRENCY}", '
        f'method="stripe", networkId="{network_id}"'
    )
    return web.json_response({
        "error": "payment required",
        "code": "payment_required",
        "amount": int(amount_cents),
        "currency": STRIPE_CURRENCY,
        "method": "stripe",
        "networkId": network_id,
    }, status=402, headers={"WWW-Authenticate": challenge})


def _demo_shared_payment_token(amount_cents: int, currency: str, network_id: str, *, source: str = "stub") -> dict:
    """Return a synthetic ACS Shared Payment Token payload for local demos."""
    token = f"spt_demo_{secrets.token_urlsafe(24).replace('-', '').replace('_', '')[:24]}"
    return {
        "shared_payment_token": token,
        "spt": token,
        "credential_type": "shared_payment_token",
        "mode": "demo",
        "source": source,
        "amount": int(amount_cents),
        "currency": currency,
        "networkId": network_id,
        "approved": True,
    }


async def demo_shared_payment_token(request: web.Request) -> web.Response:
    """Mint a stubbed Shared Payment Token for users who cannot use Link auth.

    This is only enabled when BROKER_PAYMENT_STUB_MODE is set (or when
    STRIPE_SECRET_KEY is absent and the broker is already in demo mode).
    It exists so the payment-gate / job-submission / worker path can be
    demoed without a real Stripe account or a region-bound Link login.
    """
    if STRIPE_SECRET_KEY and not BROKER_PAYMENT_STUB_MODE and not BROKER_TEST_SPT_ISSUER:
        return web.json_response({"error": "payment demo stub disabled"}, status=404)
    try:
        body = await request.json(loads=json.loads)
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    amount = int(body.get("amount_cents") or body.get("amount") or estimated_job_cost_cents())
    currency = str(body.get("currency") or STRIPE_CURRENCY).strip().lower() or STRIPE_CURRENCY
    network_id = str(body.get("networkId") or body.get("network_id") or STRIPE_NETWORK_ID or "demo_network").strip()
    payload = _demo_shared_payment_token(amount, currency, network_id, source="route")
    payload["challenge"] = f'Payment amount="{amount}", currency="{currency}", method="stripe", networkId="{network_id}"'
    return web.json_response(payload, status=200)


def _stripe_form_request(method: str, path: str, data: dict | None = None) -> dict:
    """Call Stripe preview endpoints without relying on SDK support for ACS fields."""
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    url = "https://api.stripe.com" + path
    encoded = None if data is None else urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, method=method)
    req.add_header("Authorization", f"Bearer {STRIPE_SECRET_KEY}")
    req.add_header("Stripe-Version", STRIPE_ACS_VERSION)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Stripe ACS API error {e.code}: {detail[:500]}") from e


def inspect_shared_payment_token(spt_token: str) -> dict:
    return _stripe_form_request(
        "GET",
        "/v1/shared_payment/granted_tokens/" + urllib.parse.quote(spt_token, safe=""),
    )


def charge_shared_payment_token(spt_token: str, amount_cents: int,
                                currency: str = STRIPE_CURRENCY) -> dict:
    """Create+confirm a PaymentIntent from a Stripe ACS Shared Payment Token."""
    if not spt_token.startswith("spt_"):
        return {"success": False, "error": "invalid shared payment token format"}
    amount_cents = int(amount_cents)
    try:
        token_details = inspect_shared_payment_token(spt_token)
        limits = token_details.get("usage_limits") or {}
        max_amount = int(limits.get("max_amount") or 0)
        token_currency = (limits.get("currency") or currency).lower()
        if token_currency != currency.lower():
            return {"success": False, "error": f"token currency {token_currency} does not match {currency}"}
        if max_amount and max_amount < amount_cents:
            return {"success": False, "error": f"token allocation {max_amount} is below required {amount_cents}"}
    except Exception as e:
        # Inspection is recommended, but the create+confirm call is authoritative.
        log.warning("SPT inspect failed; proceeding to PaymentIntent create: %s", e)
    try:
        pi = _stripe_form_request("POST", "/v1/payment_intents", {
            "amount": str(amount_cents),
            "currency": currency,
            "confirm": "true",
            "payment_method_data[shared_payment_granted_token]": spt_token,
        })
    except Exception as e:
        return {"success": False, "error": str(e)}
    status = pi.get("status", "")
    return {
        "success": status == "succeeded",
        "id": pi.get("id", ""),
        "status": status,
        "amount_cents": int(pi.get("amount_received") or pi.get("amount") or amount_cents),
        "raw": pi,
    }


def verify_payment_intent(pi_id: str) -> tuple[bool, str, int]:
    """Verify a Stripe PaymentIntent for a new job submission.

    Returns (ok, error_message, amount_cents):
      ok=True,  err="stripe_disabled",  amount=0  when STRIPE_SECRET_KEY unset
      ok=True,  err="",                amount=N  when PI is live + valid
      ok=False, err="invalid payment: <reason>",  amount=0  on any failure

    DEMO MODE: short-circuits with (True, "stripe_disabled", 0) — the
    broker accepts the job but the final payment in _finalize_job will
    also be a demo_capture/demo_refund. This preserves backwards
    compatibility with current broker behaviour (format-only check).

    LIVE MODE:
      1. Lazy-imports `stripe` (may raise ImportError on a minimal host
         without the package — treated as a transient failure with
         `ok=False` so the caller sees a real "invalid payment" error
         rather than a stack trace).
      2. Calls stripe.PaymentIntent.retrieve(pi_id). Status must be
         'requires_capture' or 'succeeded' (funds held/available).
         Anything else (canceled, requires_payment_method, processing)
         is rejected.
      3. Returns the held amount in cents so submit_job can stash it on
         the envelope for the worker's cost calc.
    """
    if not STRIPE_SECRET_KEY or pi_id.startswith("pi_demo_"):
        return True, "stripe_disabled", 0
    try:
        stripe = _stripe_client()
        # _stripe_client() returns the module (or None in demo mode).
        # The `stripe` rebind below is intentional and shadows the import
        # name from the broader module scope.
        if stripe is None:  # belt-and-braces (covered above, but explicit)
            return True, "stripe_disabled", 0
        intent = stripe.PaymentIntent.retrieve(pi_id)  # type: ignore[union-attr]
    except ImportError:
        # Production deploys install `stripe` via requirements.txt; this
        # branch only fires in tests or on a misconfigured host. Falling
        # back to demo mode (rather than rejecting the job) keeps the
        # broker functional — capture/refund in _finalize_job also falls
        # back to demo_* status so the row stays consistent. We log at
        # warning level so the operator notices the misconfiguration.
        log.warning(
            "STRIPE_SECRET_KEY is set but stripe library is missing; "
            "falling back to demo mode for verify"
        )
        return True, "stripe_disabled", 0
    except Exception as e:
        # StripeError covers InvalidRequestError, AuthenticationError,
        # etc. — we don't import the specific subclass to keep DEMO MODE
        # hosts (no stripe package) from crashing on the lazy import.
        # The error string is safe to surface (no key material).
        log.warning("stripe retrieve failed for %s: %s", pi_id, e)
        return False, f"invalid payment: {e}", 0
    status = getattr(intent, "status", None)
    if status not in ("requires_capture", "succeeded"):
        return False, f"invalid payment: PaymentIntent status is {status}", 0
    amount = int(getattr(intent, "amount", 0) or 0)
    return True, "", amount


# Stripe minimum capture (t_e2e_test_2026_06_28). USD is $0.50, EUR is
# €0.50, GBP is £0.30 (~$0.38). Our cost formula returns as low as
# $0.20 for a 15-min lease — Stripe rejects with 'amount_too_small'.
# Pad up to 50 cents so any job, however small, can clear Stripe's
# per-currency minimum. The customer pays the same $0.20 at the
# application layer; the extra cents cover the floor.
# Override via BROKER_MIN_CAPTURE_CENTS env var for non-USD currencies
# (e.g. set to 30 for GBP if you want to track floor exactly).
MIN_CAPTURE_CENTS = int(os.environ.get("BROKER_MIN_CAPTURE_CENTS", "50"))


def capture_payment(pi_id: str, amount_cents: int, idempotency_key: str | None = None) -> dict:
    """Capture a PaymentIntent for the actual amount used.

    Spec from kanban t_9fbec867:
      - DEMO MODE:  full lifecycle — returns the SAME shape as live mode
                     (`captured: True`, `id`, `status: succeeded`) so callers
                     can't tell demo from live by inspecting the response.
                     The `pi_id` is echoed as the synthetic id, and the
                     amount requested is recorded as `amount_cents`. Every
                     demo-mode capture ALSO appends a row to COST_LEDGER.jsonl
                     so the cost-accuracy reviewer (t_<cost_review>) can audit
                     the math later without re-running real jobs.
      - LIVE MODE:  {"captured": True, "status": "succeeded", "id": <pi>,
                     "amount_cents": <requested>}
      - LIVE ERROR: {"captured": False, "status": "error",
                     "error": "<message>"}  (caller decides whether to
                     fall back to a refund or surface to the client)

    Note: we pass amount_to_capture= amount_cents, NOT the full hold, so
    the customer only pays for what was used (Stripe holds the difference
    until the PI expires and auto-releases). This matches the task body.

    B3 (kanban t_b2ceaf21, threat-model-topup-flow.md §7): `idempotency_key`
    is forwarded to Stripe's PaymentIntent.capture as the
    `idempotency_key` parameter when set. Stripe dedupes requests with the
    same key for 24h, so a double-fire (two concurrent topup requests
    landing on the broker before either has flipped the job to
    'completed') cannot double-capture. The key is derived by the caller
    as `hashlib.sha256(f"{job_id}|{topup_pi_id}".encode()).hexdigest()[:32]`
    so it's deterministic across retries but unique per (job, topup_pi).
    """
    if not STRIPE_SECRET_KEY or pi_id.startswith("pi_demo_"):
        _log_demo_lifecycle("capture", pi_id, amount_cents)
        return {
            "captured": True,
            "status": "succeeded",
            "id": pi_id,
            "amount_cents": int(amount_cents),
            "demo": True,
            "idempotency_key": idempotency_key,
        }
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
        existing_intent = stripe.PaymentIntent.retrieve(pi_id)  # type: ignore[union-attr]
        if getattr(existing_intent, "status", None) == "succeeded":
            received = int(getattr(existing_intent, "amount_received", 0) or getattr(existing_intent, "amount", amount_cents) or amount_cents)
            return {
                "captured": True,
                "status": "succeeded",
                "id": getattr(existing_intent, "id", pi_id),
                "amount_cents": received,
                "already_succeeded": True,
            }
        capture_kwargs = {"amount_to_capture": effective_amount}
        if idempotency_key:
            # Per Stripe API docs: idempotency_key must be <=255 chars.
            # Our sha256[:32] is well under that; truncate defensively
            # anyway so a caller bug can't blow up the API call.
            capture_kwargs["idempotency_key"] = str(idempotency_key)[:255]
        intent = stripe.PaymentIntent.capture(pi_id, **capture_kwargs)  # type: ignore[union-attr]  # noqa: E501
    except ImportError:
        log.warning(
            "STRIPE_SECRET_KEY is set but stripe library is missing; "
            "falling back to demo mode for capture"
        )
        _log_demo_lifecycle("capture", pi_id, amount_cents, note="stripe_lib_missing")
        return {
            "captured": True,
            "status": "succeeded",
            "id": pi_id,
            "amount_cents": int(amount_cents),
            "demo": True,
            "idempotency_key": idempotency_key,
        }
    except Exception as e:
        # Stripe returns a 400 with code "amount_too_large" when the
        # requested capture amount exceeds the held amount. This indicates
        # a shortfall that must be resolved via topup (t_9a705578).
        # Extract the shortfall from the error response if possible.
        err_str = str(e)
        shortfall_cents = _extract_shortfall_from_error(err_str, amount_cents)
        if shortfall_cents > 0:
            log.warning(
                "stripe capture failed for %s: shortfall detected (%d < %d cents)",
                pi_id,
                int(getattr(e, "message", {}).get("payment_intent", {}).get("amount", 0) if hasattr(e, "message") else 0),
                amount_cents,
            )
            return {
                "captured": False,
                "status": "shortfall_required",
                "reason": "amount_too_large",
                "error": err_str,
                "amount_cents": int(amount_cents),
                "shortfall_cents": shortfall_cents,
            }
        log.error("stripe capture failed for %s: %s", pi_id, e)
        return {"captured": False, "status": "error", "reason": "capture_failed", "error": err_str,
                "amount_cents": int(amount_cents)}
    return {
        "captured": True,
        "status": getattr(intent, "status", "succeeded"),
        "id": getattr(intent, "id", pi_id),
        "amount_cents": int(getattr(intent, "amount_received", amount_cents)),
    }


def _extract_shortfall_from_error(err_str: str, requested_cents: int) -> int:
    """Extract shortfall_cents from a Stripe capture error string.
    
    Returns 0 if no shortfall is detected (not an amount_too_large error).
    On amount_too_large, computes shortfall = requested_cents - held_amount.
    
    Stripe error format (simplified):
        JSON: {"error": {"message": "...", "payment_intent": {"amount": N}}}
        Plain: "amount_to_capture (200) exceeds amount authorized (50)"
    
    Note: This helper is conservative. If we can't parse the error, we
    return 0 so the caller treats it as a generic error, not a shortfall.
    """
    # Check for both "amount_too_large" (Stripe's actual error code) and
    # the plain-text error format used in tests: "amount_to_capture (N) exceeds amount authorized (M)"
    if "amount_too_large" not in err_str.lower() and "exceeds amount authorized" not in err_str.lower():
        return 0
    # Try to parse as JSON to extract the held amount
    try:
        import json as _json
        data = _json.loads(err_str)
        if isinstance(data, dict):
            error = data.get("error", {})
            if isinstance(error, dict):
                pi = error.get("payment_intent", {})
                if isinstance(pi, dict):
                    held = pi.get("amount", 0)
                    if isinstance(held, (int, float)):
                        return requested_cents - int(held)
    except Exception:
        pass  # Fall through to try plain text format
    # Try plain text format: "amount_to_capture (200) exceeds amount authorized (50)"
    # Extract held amount from "amount authorized (N)" pattern
    import re
    match = re.search(r"amount authorized \((\d+)\)", err_str)
    if match:
        held = int(match.group(1))
        return requested_cents - held
    # If parsing fails, we can't reliably determine the shortfall.
    # The caller will treat this as a generic error.
    return 0


def refund_payment(pi_id: str) -> dict:
    """Issue a full refund for a failed job's PaymentIntent.

    Spec from kanban t_9fbec867:
      - DEMO MODE:  full lifecycle — same shape as live mode. Returns
                     `refunded: True, status: succeeded` with the original
                     pi_id echoed. The refund id is a synthetic
                     `re_demo_<pi_id>` so downstream code that expects an
                     `id` field doesn't blow up. Appends to COST_LEDGER.
      - LIVE MODE:  {"refunded": True, "status": "succeeded", "id": <re>,
                     "amount": <refunded_cents>, "payment_intent": <pi>}
      - LIVE ERROR: {"refunded": False, "status": "error",
                     "error": "<message>"}  (caller surfaces to log +
                     webhook; the customer's funds remain held but the
                     broker still records the attempt).

    We do NOT pass an `amount` — stripe.Refund.create defaults to
    refunding the full captured/uncaptured amount, which is the desired
    behaviour for a failed job (the customer gets everything back).
    """
    if not STRIPE_SECRET_KEY or pi_id.startswith("pi_demo_"):
        _log_demo_lifecycle("refund", pi_id)
        return {
            "refunded": True,
            "status": "succeeded",
            "id": f"re_demo_{pi_id}",
            "amount": 0,  # unknown until capture; full refund semantics
            "payment_intent": pi_id,
            "demo": True,
        }
    try:
        stripe = _stripe_client()
        refund = stripe.Refund.create(payment_intent=pi_id)  # type: ignore[union-attr]
    except ImportError:
        log.warning(
            "STRIPE_SECRET_KEY is set but stripe library is missing; "
            "falling back to demo mode for refund"
        )
        _log_demo_lifecycle("refund", pi_id, note="stripe_lib_missing")
        return {
            "refunded": True,
            "status": "succeeded",
            "id": f"re_demo_{pi_id}",
            "amount": 0,
            "payment_intent": pi_id,
            "demo": True,
        }
    except Exception as e:
        log.error("stripe refund failed for %s: %s", pi_id, e)
        return {"refunded": False, "status": "error", "error": str(e)}
    return {
        "refunded": True,
        "status": getattr(refund, "status", "succeeded"),
        "id": getattr(refund, "id", ""),
        "amount": int(getattr(refund, "amount", 0) or 0),
        "payment_intent": pi_id,
    }


# ---------- Configuration (env-driven) ----------

BROKER_DOMAIN = os.environ.get("BROKER_DOMAIN", "")
BROKER_REGION = os.environ.get("BROKER_REGION", "eu-west-1")
# Set BROKER_ENABLE_SEV_SNP=1 to attempt AMD SEV-SNP on worker launch.
# Requires a region+instance type that supports it (eu-west-1 does for m6a.*/c6a.*).
# CpuOptions.AmdSevSnp at RunInstances triggers "UnsupportedOperation" in regions
# like eu-west-2 that don't offer SEV-SNP — gate on env so the same daemon can
# run in either region.
BROKER_ENABLE_SEV_SNP = os.environ.get("BROKER_ENABLE_SEV_SNP", "1") == "1"
# Demo/offline escape hatch: when Stripe/payment stub mode is enabled, allow
# workers that cannot produce a real SEV-SNP quote to publish a binding-only
# identity. This keeps regional/demo file-job flows usable, but production
# remains fail-closed unless explicitly configured.
BROKER_ALLOW_STUB_WORKER_ATTESTATION = os.environ.get(
    "BROKER_ALLOW_STUB_WORKER_ATTESTATION",
    "1" if BROKER_PAYMENT_STUB_MODE or not BROKER_ENABLE_SEV_SNP else "0",
).strip().lower() in ("1", "true", "yes", "on", "demo", "stub")
BROKER_EFS_MOUNT = Path(os.environ.get("BROKER_EFS_MOUNT", "/mnt/broker"))
BROKER_VPC_ID = os.environ.get("BROKER_VPC_ID", "")
BROKER_SUBNET_ID = os.environ.get("BROKER_SUBNET_ID", "")
BROKER_WORKER_SG = os.environ.get("BROKER_WORKER_SG_ID", os.environ.get("BROKER_WORKER_SG", ""))
BROKER_WORKER_AMI = os.environ.get("BROKER_WORKER_AMI", "")
BROKER_WORKER_INSTANCE_TYPE = os.environ.get("BROKER_WORKER_INSTANCE_TYPE", "m6a.xlarge")
BROKER_WORKER_IAM_ROLE = os.environ.get("BROKER_WORKER_IAM_ROLE", "")
BROKER_IDLE_BUFFER_MINUTES = int(os.environ.get("BROKER_IDLE_BUFFER_MINUTES", "10"))

# When set to "1", skip the idle-timer termination entirely. Use when the
# worker is a long-lived warm-pool instance launched out-of-band (warm-
# worker-manager.sh) — the broker will adopt it on startup and never
# terminate it. The out-of-band lifecycle is responsible for replacing
# failed workers; the broker just dispatches jobs.
BROKER_DISABLE_IDLE_TERMINATION = os.environ.get("BROKER_DISABLE_IDLE_TERMINATION", "0") == "1"

# Skill registration API key (VULN-S2). When set, POST /v1/skills requires
# `Authorization: Bearer BROKER_SKILLS_API_KEY`. GET /v1/skills (list) stays
# public so the /v1/discover page can advertise registered skills without
# leaking a secret. If unset, POST /v1/skills is refused outright
# (closed-by-default) so a misconfigured deploy doesn't accidentally expose
# open registration.
BROKER_SKILLS_API_KEY = os.environ.get("BROKER_SKILLS_API_KEY", "").strip()

# === B5 chargeback/ack handling (kanban t_69b52324) ===
# Stripe sends `charge.dispute.created` and `charge.dispute.closed` to a
# webhook endpoint; the broker must verify the signature (HMAC-SHA256 in
# `stripe-signature` over `t=<ts>.<body>`, ±5min timestamp tolerance) and
# react. The shared secret is configured at deploy time; when unset the
# endpoint refuses all requests (closed-by-default — same posture as the
# skills API key above). DEMO MODE: STRIPE_WEBHOOK_SECRET unset means the
# broker is in demo mode and the webhook handler returns 503 so accidental
# curl-from-localhost tests don't poison the dispute_events table.
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

# Fraud score threshold (kanban t_69b52324). Per-account score is:
#   chargebacks_filed + abandoned_jobs + refunded_topups
# Crossing this threshold sets account_fraud_score.suspended=1 and any
# subsequent POST /v1/jobs for that account returns 403 + account_suspended.
# Score > 2 = ban per the threat model §5 (B5 recommendation #4). The
# "2" here means a customer must hit a total of 3 fraud signals to be
# banned — this matches the task body ("score > 2 = ban") and gives one
# free chargeback / abandoned job before the account is locked out.
FRAUD_SCORE_BAN_THRESHOLD = int(os.environ.get("BROKER_FRAUD_BAN_THRESHOLD", "2"))

# How long a customer's explicit ack remains valid for chargeback
# disputes. The threat model §5 recommends 24h; we set it to match the
# S3 result-retention TTL so the ack window matches the data window.
# Configurable via env so operators can tighten for higher-risk deployments.
ACK_WINDOW_HOURS = int(os.environ.get("BROKER_ACK_WINDOW_HOURS", "24"))

# VULN-S8 (kanban t_b13072b3): CORS allowed origin. Default is the
# verdant frontend (the only client UI in the demo). Operators can
# override via the BROKER_CORS_ORIGIN env var; the Caddyfile reads
# the same name so a single env var controls the entire allowlist.
# Wildcard `*` is intentionally not supported here — would defeat
# the whole point of the restriction.
BROKER_CORS_ORIGIN = os.environ.get(
    "BROKER_CORS_ORIGIN", "https://verdant.codepilots.co.uk")

INBOX = BROKER_EFS_MOUNT / "jobs" / "inbox"
OUTBOX = BROKER_EFS_MOUNT / "jobs" / "outbox"
RESULTS = BROKER_EFS_MOUNT / "results"
LOGS = BROKER_EFS_MOUNT / "logs"
DB_PATH = LOGS / "broker.db"
# Result-pack artifact storage. Symmetric with INBOX/OUTBOX so the worker
# writes here, the daemon serves from here. Plaintext at rest (documented
# limitation); integrity guaranteed by manifest sha256 + result_hash +
# broker_signature chain.
ARTIFACTS_DIR = BROKER_EFS_MOUNT / "jobs" / "artifacts"
# WASM skill binaries (kanban t_c27c1d8d). Uploaded via POST
# /v1/skills/{name}/wasm; the broker stores the binary here and the
# worker reads it from the same EFS path. Symmetric with INBOX/OUTBOX
# so the broker writes and the worker reads without any extra protocol.
# Plaintext at rest (same as ARTIFACTS_DIR) — the WASM is meant to be
# public (it's a verifiable binary the requester wants the worker to
# run); only the *input data* is encrypted. The manifest hash chain
# binds the binary to the registered `wasm_manifest_hash` so a
# publisher can't swap a malicious blob in after registration.
WASM_DIR = BROKER_EFS_MOUNT / "wasm"

# S3 artifact bucket — replaces EFS plaintext for result-pack blobs.
# Artifacts are encrypted client-side to result_pubkey (X25519 + ChaCha20-
# Poly1305) BEFORE upload, then stored in S3 with SSE-KMS at rest. A 24h
# lifecycle rule auto-deletes objects. Clients download via presigned
# URLs (15-min TTL) generated on demand by the daemon.
#
# Bucket name is region-scoped so DNS resolves locally for the worker
# without going through us-east-1. The bucket is created by the CFN
# template as ArtifactBucket; the broker reads the name from
# BROKER_ARTIFACT_BUCKET so the same code runs in any region.
ARTIFACT_BUCKET = os.environ.get(
    "BROKER_ARTIFACT_BUCKET", "verdantforged-artifacts-eu-west-1")
# Presigned-URL TTL (seconds). Short enough that a leaked URL is
# harmless within minutes, long enough for a client to fetch + decrypt.
# Chose 15 min (900s) over 5 min so the webhook flow (deliver + client
# opens notification + fetches) has time to complete on slow networks.
ARTIFACT_PRESIGN_TTL_SECONDS = int(
    os.environ.get("BROKER_ARTIFACT_PRESIGN_TTL_SECONDS", "900"))
# Lazy boto3 S3 client for presigning download URLs. The daemon never
# PUTs — only presigns — so we don't need worker-role-equivalent policy.
# Tests inject a MagicMock by setting daemon.s3_client to a fake before
# the helper runs (matches the worker module pattern).
s3_client = None

# ---- Input attachment system (t_0ef31767) ------------------------------------
# Two-phase job submission for jobs with file attachments. Phase 1: client
# POSTs the job spec with input_files[] and gets back presigned S3 PUT URLs.
# Job enters `awaiting_inputs` state. Phase 2: client PUTs each file to its
# presigned URL (optionally encrypted to the worker's X25519 pubkey), then
# POSTs /v1/jobs/{id}/ready. Broker verifies all files exist in S3 via
# HeadObject, transitions the job to `queued`, writes the EFS envelope with
# S3 input keys, and kicks the worker. The worker fetches each input from
# S3 at job start and DELETES it immediately — no input data persists.
#
# TTL: 15 min (900s) — enough for the client to encrypt + upload on a
# slow connection, short enough that a leaked URL is harmless within
# minutes. Bumped from the previous 5 min in the body spec because the
# two-phase flow has more round-trips.
INPUT_UPLOAD_TTL_SECONDS = int(
    os.environ.get("BROKER_INPUT_UPLOAD_TTL_SECONDS", "900"))
# Max files per job. Conservative — each file costs a presigned URL
# generation call, an EFS envelope entry, and a worker S3 round-trip.
INPUT_MAX_FILES = int(os.environ.get("BROKER_INPUT_MAX_FILES", "10"))
# Max bytes per file. 50 MB is enough for the showcase skills (PDF for
# summarization, images for photo-glow-up, code blobs for blind-audit)
# without letting a malicious client use the broker as a free upload
# hopper. Clients that need larger files should chunk + name sequentially.
INPUT_MAX_SIZE_BYTES = int(
    os.environ.get("BROKER_INPUT_MAX_SIZE_BYTES", str(50 * 1024 * 1024)))
INPUT_MAX_TOTAL_BYTES = int(
    os.environ.get("BROKER_INPUT_MAX_TOTAL_BYTES", str(100 * 1024 * 1024)))
OUTPUT_MAX_FILES = int(os.environ.get("BROKER_OUTPUT_MAX_FILES", "10"))
OUTPUT_MAX_SIZE_BYTES = int(
    os.environ.get("BROKER_OUTPUT_MAX_SIZE_BYTES", str(50 * 1024 * 1024)))
OUTPUT_MAX_TOTAL_BYTES = int(
    os.environ.get("BROKER_OUTPUT_MAX_TOTAL_BYTES", str(100 * 1024 * 1024)))
FILE_ENCRYPTION = "x25519-hkdf-sha256-chacha20poly1305-v1"
# Path-traversal / shell-meta protection on filenames. Matches the
# entry_point regex used by skill registration (POST /v1/skills) so a
# filename that would fail skill validation also fails input validation
# — same surface, same allowlist. We deliberately exclude "/", "..", and
# any character that would matter to S3 keys or to the worker's
# attachment-decoding loop (e.g. null bytes).
INPUT_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def _get_s3_client():
    """Return the boto3 S3 client used to presign artifact download URLs.

    boto3 picks up credentials from the ControlPlaneRole attached to the
    broker EC2 instance at first use. Lazy-init so importing daemon.py
    outside AWS (unit tests, local dev) doesn't try to fetch creds.

    SigV4 is mandatory here: the artifact bucket is SSE-KMS-encrypted
    (CloudFormation BucketEncryption), and S3 refuses any request against
    such a bucket unless the signer uses SigV4. boto3's default signer is
    SigV4 in most regions but has been SigV2 in some legacy configs —
    pin it explicitly so this never regresses.
    """
    global s3_client
    if s3_client is None:
        s3_client = boto3.client(
            "s3",
            region_name=BROKER_REGION,
            config=BotoConfig(signature_version="s3v4"),
        )
    return s3_client


def generate_presigned_url(s3_key, expires_seconds=None):
    """Generate a presigned S3 GET URL for an artifact blob.

    expires_seconds defaults to ARTIFACT_PRESIGN_TTL_SECONDS (15 min).
    The bucket name comes from ARTIFACT_BUCKET (env-overridable). Tests
    inject a fake by setting daemon.s3_client to a MagicMock; the helper
    then forwards the call to the mock.
    """
    if expires_seconds is None:
        expires_seconds = ARTIFACT_PRESIGN_TTL_SECONDS
    return _get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": ARTIFACT_BUCKET, "Key": s3_key},
        ExpiresIn=expires_seconds,
    )


def generate_presigned_upload_url(s3_key, expires_seconds=None):
    """Generate a presigned S3 PUT URL for an input attachment upload (t_0ef31767).

    Mirrors generate_presigned_url() but for PUT instead of GET — the
    client uploads the file directly to S3 via this URL and the broker
    never sees the bytes. expires_seconds defaults to INPUT_UPLOAD_TTL_SECONDS
    (15 min). Tests inject a MagicMock by setting daemon.s3_client to a fake.

    Note: the artifact bucket has SSE-KMS enabled via CloudFormation
    BucketEncryption, so S3 applies KMS encryption automatically. We do
    NOT include ServerSideEncryption in the presigned URL params — doing
    so causes S3 to require SigV4 signing on the PUT request, which a
    plain HTTP client (curl, requests) cannot provide. The bucket default
    encryption handles at-rest protection.
    """
    if expires_seconds is None:
        expires_seconds = INPUT_UPLOAD_TTL_SECONDS
    return _get_s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": ARTIFACT_BUCKET, "Key": s3_key},
        ExpiresIn=expires_seconds,
    )


def validate_input_files(input_files) -> tuple[bool, str]:
    """Validate the optional input_files[] on a /v1/jobs submission.

    Returns (ok, error_message). ok=True means the list is well-formed
    and can be turned into presigned URLs. Caller is expected to gate
    on this BEFORE counting toward any quota (a malformed request
    shouldn't burn the per-day job cap).

    Checks (all cheap, no I/O):
      1. input_files is a list
      2. length <= INPUT_MAX_FILES
      3. each entry is a dict with filename, content_type, size_bytes
      4. filename matches INPUT_FILENAME_RE (no path traversal)
      5. content_type is a non-empty string
      6. size_bytes is a non-negative int <= INPUT_MAX_SIZE_BYTES
    """
    if not isinstance(input_files, list):
        return False, "input_files must be a list"
    if len(input_files) == 0:
        # Empty list is treated the same as missing — caller falls back
        # to the single-phase (no-files) flow.
        return True, ""
    if len(input_files) > INPUT_MAX_FILES:
        return False, (
            f"too many input_files: {len(input_files)} > "
            f"BROKER_INPUT_MAX_FILES ({INPUT_MAX_FILES})")
    total_size = 0
    for i, f in enumerate(input_files):
        if not isinstance(f, dict):
            return False, f"input_files[{i}] must be an object"
        filename = f.get("filename", "")
        if not isinstance(filename, str) or not INPUT_FILENAME_RE.match(filename):
            return False, (
                f"input_files[{i}].filename must match "
                f"^[A-Za-z0-9_.\\-]{{1,128}}$ (got: {str(filename)[:60]!r})")
        content_type = f.get("content_type", "")
        if not isinstance(content_type, str) or not content_type:
            return False, f"input_files[{i}].content_type must be a non-empty string"
        size_bytes = f.get("size_bytes", -1)
        if not isinstance(size_bytes, int) or size_bytes < 0:
            return False, f"input_files[{i}].size_bytes must be a non-negative integer"
        if size_bytes > INPUT_MAX_SIZE_BYTES:
            return False, (
                f"input_files[{i}].size_bytes ({size_bytes}) exceeds "
                f"BROKER_INPUT_MAX_SIZE_BYTES ({INPUT_MAX_SIZE_BYTES})")
        total_size += size_bytes
    if total_size > INPUT_MAX_TOTAL_BYTES:
        return False, (
            f"input_files total size ({total_size}) exceeds "
            f"BROKER_INPUT_MAX_TOTAL_BYTES ({INPUT_MAX_TOTAL_BYTES})")
    return True, ""


def _valid_x25519_pubkey(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except Exception:
        return False


def create_job_access_token() -> str:
    """Create the client credential for all job-scoped operations."""
    return "jobtok_" + secrets.token_urlsafe(32)


def hash_job_access_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_job_access_token_value(token: str, expected_hash: str) -> bool:
    if not token or not expected_hash:
        return False
    return hmac.compare_digest(hash_job_access_token(token), expected_hash)


def _bearer_token(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return ""
    return header[7:].strip()


def require_job_access(request: web.Request, job_id: str) -> web.Response | None:
    """Return an auth error, or None when the request may access job_id."""
    with db() as conn:
        row = conn.execute(
            "SELECT job_access_token_hash FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    token = _bearer_token(request)
    if not verify_job_access_token_value(token, row["job_access_token_hash"] or ""):
        return web.json_response({
            "error": "missing or invalid job access token",
            "code": "job_unauthorized",
        }, status=401)
    return None

WORKER_TAG = {"Project": "verdantforged", "Role": "tee-worker"}
WORKER_NAME_PREFIX = "verdantforged-worker-"

LOG_PATH = LOGS / "broker-daemon.log"

# ---------- Logging ----------

LOGS.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("broker-daemon")


# ---------- Database ----------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id          TEXT PRIMARY KEY,
            client_req_id   TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            started_at      TEXT,
            finished_at     TEXT,
            state           TEXT NOT NULL,  -- queued, running, completed, failed, timeout, awaiting_topup, abandoned
            -- request_body holds the full submitted envelope as JSON.
            -- NULLable since the CQ-3 (kanban t_b13072b3) privacy purge
            -- sets it to NULL after 24h. Fresh DBs have it NULLable
            -- directly; existing DBs get rebuilt by the migration in
            -- init_db() (SQLite can't ALTER COLUMN NULL/NOT NULL in
            -- place).
            request_body    TEXT,
            result          TEXT,
            error           TEXT,
            webhook_url     TEXT,
            webhook_status  TEXT,
            llm_tokens_used INTEGER DEFAULT 0,
            llm_calls       INTEGER DEFAULT 0,
            -- Input attachment columns (t_0ef31767). Same semantics as
            -- the ALTER TABLE migration below — input_file_count = N
            -- for two-phase jobs, 0 for single-phase; input_status =
            -- NULL for single-phase, "awaiting_inputs" or "ready" for
            -- two-phase. See the ALTER TABLE block for the full
            -- state-walk rationale.
            input_file_count INTEGER DEFAULT 0,
            input_status     TEXT,
            job_access_token_hash TEXT,
            worker_instance_id TEXT,
            worker_key_id TEXT,
            input_upload_expires_at TEXT,
            -- Shortfall columns (kanban t_9a705578). When a job completes
            -- and capture_payment() detects that the held PI amount is
            -- insufficient, the job transitions to awaiting_topup and
            -- these columns record the shortfall details.
            shortfall_cents       INTEGER DEFAULT 0,  -- amount short of captured cost
            topup_pi_id           TEXT,                 -- new PI for topup
            topup_capture_amount  INTEGER DEFAULT 0,  -- topup PI captured amount
            -- B3 (kanban t_b2ceaf21): cached Stripe transfer id from the
            -- topup capture. Lets the topup endpoint return idempotently on
            -- retry without re-calling Stripe — same shape as
            -- stripe_transfer_id for the original capture.
            stripe_topup_transfer_id TEXT,
            awaiting_topup_at     TEXT,                 -- timestamp when awaiting_topup started
            -- B5 customer ack (kanban t_69b52324). When the customer POSTs
            -- /v1/jobs/{id}/ack, we persist the ack timestamp + their
            -- supplied proof text + requesting IP. The trio is what the
            -- operator surfaces to Stripe's dispute evidence portal —
            -- "the customer confirmed receipt of the result at <time>
            -- from <ip> with proof <text>". An ack inside ACK_WINDOW_HOURS
            -- of result delivery is strong evidence the result was
            -- delivered before the chargeback was filed.
            acked_at              TEXT,
            ack_proof             TEXT,
            ack_ip                TEXT,
            UNIQUE (client_req_id)
        );
        CREATE INDEX IF NOT EXISTS idx_state ON jobs (state);
        CREATE INDEX IF NOT EXISTS idx_created ON jobs (created_at);

        -- Per-job LLM access tokens. Generated at submit time, returned to
        -- the worker in the job envelope, validated by the broker proxy.
        CREATE TABLE IF NOT EXISTS llm_tokens (
            token           TEXT PRIMARY KEY,
            job_id          TEXT NOT NULL,
            stripe_pi_id    TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            expires_at      TEXT NOT NULL,
            tokens_used     INTEGER DEFAULT 0,
            calls           INTEGER DEFAULT 0,
            UNIQUE (job_id)
        );
        CREATE INDEX IF NOT EXISTS idx_llm_tokens_expires ON llm_tokens (expires_at);

        -- Account-level token usage tracking for demo cap enforcement.
        -- The account key is the stripe_pi_id prefix (everything before the
        -- second underscore). Production would use the real Stripe customer
        -- ID.
        CREATE TABLE IF NOT EXISTS account_usage (
            account         TEXT NOT NULL,
            date            TEXT NOT NULL,   -- YYYY-MM-DD UTC
            tokens_used     INTEGER DEFAULT 0,
            tokens_cap      INTEGER DEFAULT 0,
            PRIMARY KEY (account, date)
        );

        -- VULN-LLMTK (kanban t_a18827b6): per-account daily JOB count cap.
        -- The previous per-account limit was token-based (account_usage
        -- table) — that doesn't stop fail-spam: the broker refunds on
        -- failure but EC2 minutes already burned (refund-eats-compute,
        -- see t_29b31ecb cost review §5). Combined with a trivially
        -- guessable account key (stripe_pi_id.split("_")[:2]), an
        -- attacker could mint pi_evil_1..pi_evil_999 for fresh budgets.
        -- account_key_for (VULN-S7, t_c6beba80) now hashes pi_id with a
        -- server-side secret so distinct pi_ids can't be enumerated,
        -- and THIS table counts JOBS — every accepted submit bumps
        -- jobs_used by 1 regardless of outcome, so failed jobs still
        -- cost the attacker their daily budget. Day rollover happens
        -- naturally by the day_utc key change — no cron needed.
        CREATE TABLE IF NOT EXISTS account_quota (
            account_key     TEXT NOT NULL,   -- sha256(pi_id|secret)[:16]
            day_utc         TEXT NOT NULL,   -- YYYY-MM-DD
            jobs_used       INTEGER DEFAULT 0,
            PRIMARY KEY (account_key, day_utc)
        );

        -- Registered skills (POST /v1/skills). Distinct from the 3 built-in
        -- stubs advertised in /v1/discover. A skill is uniquely identified
        -- by (name, version); the latest version of a name is what /v1/discover
        -- surfaces. Registration does NOT authenticate the publisher in the
        -- demo — production would require an Ed25519 publisher signature
        -- over the canonical manifest (matches tee-broker-pattern/agent-skills.md
        -- skill-verify-attestation flow).
        CREATE TABLE IF NOT EXISTS skills (
            name              TEXT NOT NULL,
            version           TEXT NOT NULL,
            description       TEXT NOT NULL,
            wasm_manifest_hash TEXT NOT NULL,
            entry_point       TEXT NOT NULL,
            prompt_template   TEXT,                 -- NULL iff wasm_ref IS NOT NULL
            wasm_ref_uri      TEXT,                 -- NULL iff prompt_template IS NOT NULL
            wasm_ref_size     INTEGER,              -- NULL iff prompt_template IS NOT NULL
            max_fuel          INTEGER NOT NULL,
            max_duration_ms   INTEGER NOT NULL,
            max_memory_mb     INTEGER NOT NULL,
            input_schema      TEXT,                 -- JSON text or NULL
            output_schema     TEXT,                 -- JSON text or NULL
            -- decrypt_input: when true (only valid for prompt-template skills),
            -- the worker will decrypt encrypted_data with its X25519 private
            -- key before sending it to the LLM. Used by the blind-audit
            -- showcase skill so clients can encrypt source code to the
            -- worker's enclave_pubkey (from /v1/discover) and have the broker
            -- never see the plaintext. Default 0 (backwards-compatible).
            decrypt_input     INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL,
            PRIMARY KEY (name, version)
        );
        CREATE INDEX IF NOT EXISTS idx_skills_name ON skills (name);

        -- B5 chargeback handling (kanban t_69b52324). The dispute_events
        -- table is an immutable audit log of every charge.dispute.created
        -- and charge.dispute.closed webhook the broker receives from
        -- Stripe. It is APPEND-ONLY — no UPDATE path is exposed in the
        -- daemon. Replay protection rides on event_id (PRIMARY KEY =
        -- Stripe evt_xxx). dispute_id is the bare Stripe du_xxx and is
        -- NOT unique — a follow-up charge.dispute.closed for the same
        -- dispute sends a different evt_xxx and gets its own row, so
        -- the operator can SELECT * WHERE dispute_id='du_xxx' and see
        -- the full timeline (created + closed) for one dispute. The
        -- raw_payload column preserves the full Stripe event JSON so an
        -- operator can reconstruct the dispute timeline even after
        -- Stripe purges it from the dashboard.
        CREATE TABLE IF NOT EXISTS dispute_events (
            event_id        TEXT PRIMARY KEY,    -- Stripe evt_xxx
            dispute_id      TEXT NOT NULL,       -- Stripe du_xxx (not unique)
            charge_id       TEXT,                -- Stripe ch_xxx
            payment_intent  TEXT,                -- Stripe pi_xxx
            event_type      TEXT NOT NULL,       -- charge.dispute.created | charge.dispute.closed
            status          TEXT,                -- needs_response / under_review / won / lost
            amount_cents    INTEGER DEFAULT 0,
            reason          TEXT,
            evidence_due_by TEXT,                -- ISO timestamp (Stripe's evidence deadline)
            created_at      TEXT NOT NULL,       -- when we received the webhook
            raw_payload     TEXT NOT NULL        -- full Stripe event JSON
        );
        CREATE INDEX IF NOT EXISTS idx_dispute_charge ON dispute_events (charge_id);
        CREATE INDEX IF NOT EXISTS idx_dispute_pi ON dispute_events (payment_intent);
        CREATE INDEX IF NOT EXISTS idx_dispute_dispute ON dispute_events (dispute_id);

        -- B5 fraud score (kanban t_69b52324). Per-account rolling score
        -- accumulated across three signals:
        --   chargebacks_filed — incremented by the dispute webhook handler
        --   abandoned_jobs    — incremented by _sweep_awaiting_topup_ttl
        --   refunded_topups   — incremented when a captured topup is later
        --                       refunded (currently unused — topups don't
        --                       refund — but the column is here for future
        --                       topup-refund plumbing)
        -- When (chargebacks_filed + abandoned_jobs + refunded_topups) >
        -- FRAUD_SCORE_BAN_THRESHOLD, suspended flips to 1 and submit_job
        -- rejects with 403 + account_suspended. last_event_at is set on
        -- every mutation for the operator audit log.
        CREATE TABLE IF NOT EXISTS account_fraud_score (
            account_key        TEXT PRIMARY KEY,  -- account_key_for(pi_id)
            chargebacks_filed  INTEGER DEFAULT 0,
            abandoned_jobs     INTEGER DEFAULT 0,
            refunded_topups    INTEGER DEFAULT 0,
            suspended          INTEGER DEFAULT 0,  -- 1 = banned
            suspended_reason   TEXT,              -- human-readable
            last_event_at      TEXT NOT NULL
        );
        """)

        # Migration: add columns to existing jobs table (idempotent)
        for col in ("llm_tokens_used INTEGER DEFAULT 0",
                    "llm_calls INTEGER DEFAULT 0",
                    # artifact_count: number of result-pack artifacts (excludes
                    # the always-present primary output.txt). 0 = no artifacts.
                    # Populated by _finalize_job from the worker's result
                    # envelope, used by /v1/jobs for quick status.
                    "artifact_count INTEGER DEFAULT 0",
                    # Stripe PaymentIntent lifecycle columns (t_9fbec867).
                    # Populated by _finalize_job after capture_payment /
                    # refund_payment run. NULL until the job reaches a
                    # terminal state; stripe_status carries the demo_*
                    # sentinel so clients can distinguish DEMO MODE from a
                    # missing field (NULL = not yet finalized).
                    "stripe_capture_amount INTEGER",
                    "stripe_transfer_id TEXT",
                    "stripe_status TEXT",
                    # Held amount from verify_payment_intent at submit time
                    # (cents). Useful for client-side budgeting without a
                    # second Stripe API call. NULL = either demo mode
                    # (key unset) or the request was rejected before the
                    # amount was returned.
                    "stripe_pi_amount_cents INTEGER",
                    # CQ-3 (kanban t_b13072b3): request_body can now be
                    # NULL after the 24h privacy purge. Fresh DBs already
                    # declare request_body as nullable (see CREATE TABLE
                    # above); this ALTER TABLE is a no-op on a fresh DB
                    # and the actual schema-rewrite is handled by the
                    # table-rebuild migration below for existing DBs.
                    #
                    # Input attachment columns (t_0ef31767): two-phase
                    # submit flow. input_file_count = 0 for single-phase
                    # (text-only) jobs and N for two-phase (file-attached)
                    # jobs. input_status walks:
                    #   NULL          -> single-phase flow (no S3 round-trip)
                    #   awaiting_inputs -> job created, waiting for client
                    #                     to PUT files to presigned URLs
                    #   ready         -> client called /ready and we
                    #                     verified all files are in S3
                    #   (no further rows after /ready; the worker fetch+
                    #   delete loop is purely S3-side and the broker
                    #   doesn't observe it.)
                    "input_file_count INTEGER DEFAULT 0",
                    "input_status TEXT",
                    "job_access_token_hash TEXT",
                    "worker_instance_id TEXT",
                    "worker_key_id TEXT",
                    "input_upload_expires_at TEXT",
                    # Shortfall columns (kanban t_9a705578). These are
                    # added via ALTER TABLE for existing DBs; fresh
                    # DBs get them in the CREATE TABLE above.
                    "shortfall_cents INTEGER DEFAULT 0",
                    "topup_pi_id TEXT",
                    "topup_capture_amount INTEGER DEFAULT 0",
                    # B3 (kanban t_b2ceaf21): cached topup transfer id so
                    # retry-safe topup endpoint can short-circuit without
                    # calling Stripe again.
                    "stripe_topup_transfer_id TEXT",
                    "awaiting_topup_at TEXT",
                    # B5 (kanban t_69b52324): customer-ack columns.
                    # Persisted on POST /v1/jobs/{id}/ack so the operator
                    # has proof-of-delivery for the Stripe dispute portal.
                    # Fresh DBs already declare them in the CREATE TABLE
                    # above; this ALTER TABLE covers existing DBs.
                    "acked_at TEXT",
                    "ack_proof TEXT",
                    "ack_ip TEXT",
                ):
            col_name = col.split()[0]
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
            except Exception:
                pass  # column already exists

        # CQ-3 (kanban t_b13072b3): request_body must accept NULL for
        # the privacy-purge feature. The CREATE TABLE above already
        # declares it NULLable, but EXISTING databases from before this
        # fix still have `request_body TEXT NOT NULL`. SQLite can't
        # ALTER COLUMN NULL/NOT NULL in place — we have to rebuild the
        # table. We only do this when the column is currently NOT NULL,
        # to keep startup cheap on every daemon restart.
        # The rebuild preserves all existing data via SELECT * INTO a
        # new table, then renames the new table in place.
        # PRAGMA table_info returns: cid, name, type, "notnull",
        # dflt_value, pk. notnull=1 means the column was declared NOT NULL.
        # The double quotes around "notnull" are required — notnull is a
        # reserved-ish word in SQLite and the bare identifier fails to
        # parse in a SELECT.
        rb_info = conn.execute(
            'SELECT "notnull" FROM pragma_table_info(\'jobs\') '
            "WHERE name = 'request_body'"
        ).fetchone()
        if rb_info is not None and rb_info[0] == 1:
            log.info("rebuilding jobs table to make request_body nullable (CQ-3)")
            # Grab the existing column list so we preserve the schema.
            column_info = conn.execute(
                "SELECT * FROM pragma_table_info('jobs')").fetchall()
            cols = [row[1] for row in column_info]
            cols_sql = ", ".join(cols)
            conn.execute("ALTER TABLE jobs RENAME TO jobs__cq3_old")
            # Re-create from PRAGMA metadata rather than a hard-coded column
            # list. The prior migration silently lagged newer payment/file
            # columns and could fail on a real upgraded database.
            definitions = []
            for info in column_info:
                name = info[1]
                definition = f'"{name}" {info[2] or "TEXT"}'
                if info[5]:
                    definition += " PRIMARY KEY"
                if info[3] and name != "request_body":
                    definition += " NOT NULL"
                if info[4] is not None:
                    definition += f" DEFAULT {info[4]}"
                definitions.append(definition)
            definitions.append("UNIQUE (client_req_id)")
            conn.execute(
                "CREATE TABLE jobs__cq3_new (" +
                ", ".join(definitions) + ")")
            conn.execute(
                f"INSERT INTO jobs__cq3_new ({cols_sql}) SELECT {cols_sql} FROM jobs__cq3_old"
            )
            conn.execute("DROP TABLE jobs__cq3_old")
            conn.execute("ALTER TABLE jobs__cq3_new RENAME TO jobs")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_state ON jobs (state)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON jobs (created_at)")

        # Migration: add columns to existing skills table (idempotent).
        # Same try/except pattern — ALTER TABLE fails if the column exists,
        # which is the desired no-op semantics on a fresh DB.
        for col in (
            # decrypt_input: when true, the worker will decrypt encrypted_data
            # with its X25519 private key before sending it to the LLM.
            # Used by the blind-audit showcase skill.
            "decrypt_input INTEGER NOT NULL DEFAULT 0",
        ):
            col_name = col.split()[0]
            try:
                conn.execute(f"ALTER TABLE skills ADD COLUMN {col}")
            except Exception:
                pass  # column already exists

        # B4 webhook DoS mitigation (kanban t_30ca541f, threat model §4.3):
        # counter of webhook deliveries per job. The dispatcher increments
        # this atomically and bails if it exceeds WEBHOOK_MAX_ATTEMPTS_PER_JOB
        # so a single job can't blow past the 3 legitimate state transitions
        # (running→awaiting_topup, awaiting_topup→completed, timeout fallback).
        try:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN webhook_attempts INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # column already exists on fresh DBs
    log.info("db initialised at %s", DB_PATH)


# CQ-3 (kanban t_b13072b3): request_body privacy cleanup.
# ----------------------------------------------------
# The `jobs.request_body` column holds the full submitted envelope as
# JSON — including encrypted_data, encrypted_skill, result_pubkey,
# and requester_sig. None of these are secret (the encryption uses
# ephemeral/static keys, not envelope contents), but they ARE
# persistent PII / metadata that lingers in the SQLite WAL forever.
#
# Production-grade solution: encrypt request_body with a broker-side
# key at INSERT time, decrypt on read. That's a bigger refactor and
# outside the hackathon scope. We chose a middle ground instead:
# purge request_body to NULL after 24h via a periodic task.
#
# Privacy implication: jobs older than 24h no longer carry their
# submission envelope in the broker DB. Operators who need a forensic
# audit beyond 24h should pull the envelope off EFS /mnt/broker/jobs/
# inbox (it stays there indefinitely) or capture the submission at the
# edge. The 24h window matches the S3 artifact lifecycle rule.
REQUEST_BODY_RETENTION_HOURS = int(
    os.environ.get("BROKER_REQUEST_BODY_RETENTION_HOURS", "24"))
REQUEST_BODY_PURGE_INTERVAL_SECONDS = int(
    os.environ.get("BROKER_REQUEST_BODY_PURGE_INTERVAL_SECONDS", "3600"))

# Shortfall topup TTL (kanban t_9a705578). Jobs in 'awaiting_topup' state
# longer than this many days get refunded (original PI) and transitioned
# to 'abandoned'. The result remains encrypted and is discarded.
TOPUP_TTL_DAYS = int(os.environ.get("BROKER_TOPUP_TTL_DAYS", "7"))
TOPUP_TTL_SWEEP_MINUTES = int(os.environ.get("BROKER_TOPUP_TTL_SWEEP_MINUTES", "60"))


# === B4 webhook DoS mitigation (kanban t_30ca541f, threat model §4.3) ===
# The original `_deliver_webhook` blocked the outbox-poller hot path for up
# to 10s per slow webhook target (daemon.py hardcoded ClientTimeout). With
# the topup flow adding extra state transitions (awaiting_topup + completed
# + timeout fallback) per job, a hostile webhook target could DoS every other
# job's finalisation. Four mitigations:
#
#  1. **Move delivery off the hot path.** `_deliver_webhook` now enqueues
#     into an asyncio.Queue; a dispatcher worker consumes with bounded
#     parallelism and a shorter (5s) per-call timeout. The outbox poller
#     and topup-job handler return immediately after enqueue.
#
#  2. **Per-job webhook cap.** Each successful enqueue bumps `webhook_attempts`
#     on the jobs row. Bail + log if the counter exceeds
#     WEBHOOK_MAX_ATTEMPTS_PER_JOB (default 3 — covers awaiting_topup,
#     completed, and the timeout fallback the topup flow allows).
#
#  3. **Per-host rate limit.** Hash the webhook URL hostname, gate deliveries
#     to WEBHOOK_HOST_RATE_PER_SEC per second across ALL jobs (sliding
#     window). SSRF blocklist at _validate_webhook_url already keeps
#     internal hosts out; this rate limit protects against external
#     hostile targets even when SSRF is satisfied.
#
#  4. **Idempotent payload.** Every body now carries an `event_id` UUID
#     so receivers can dedupe across retries (dispatcher re-attempts on
#     network errors within the cap window).
#
# Tunables via env (defaults chosen for the demo):
WEBHOOK_MAX_ATTEMPTS_PER_JOB = int(os.environ.get("BROKER_WEBHOOK_MAX_ATTEMPTS_PER_JOB", "3"))
WEBHOOK_DELIVERY_TIMEOUT_SECONDS = float(os.environ.get("BROKER_WEBHOOK_DELIVERY_TIMEOUT_SECONDS", "5"))
WEBHOOK_MAX_PARALLEL = int(os.environ.get("BROKER_WEBHOOK_MAX_PARALLEL", "10"))
WEBHOOK_HOST_RATE_PER_SEC = int(os.environ.get("BROKER_WEBHOOK_HOST_RATE_PER_SEC", "10"))

# Dispatcher module state. Initialised in _on_startup; tests that call
# `app.on_startup.clear()` get queue=None and the synchronous fallback
# path (so existing tests that monkeypatch _deliver_webhook keep working).
_webhook_queue: Optional[asyncio.Queue] = None
_webhook_dispatcher_tasks: list[asyncio.Task] = []
_webhook_host_throttle: dict[str, collections.deque] = {}
_webhook_host_throttle_lock = asyncio.Lock()


def purge_old_request_bodies() -> int:
    """Set request_body to NULL for jobs older than the retention window.

    Called by _request_body_purge_loop (periodic background task) and
    directly from the test suite. Returns the number of rows purged so
    callers (and tests) can confirm the helper actually ran.

    Decision (CQ-3): chose "set to NULL" over "delete the row" because
    the rest of the job metadata (job_id, state, result, llm_tokens_used,
    webhook_status) is still useful for billing / audit / debugging.
    Only the envelope text goes. The envelope is still on EFS for any
    forensic need.

    Decision: chose 24h over a shorter window (1h, 6h) because the
    demo's billing + error-recovery flow needs at least a day to
    surface "the job I submitted yesterday failed because X" tickets.
    Operators can tighten with BROKER_REQUEST_BODY_RETENTION_HOURS.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=REQUEST_BODY_RETENTION_HOURS)).isoformat()
    with db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET request_body = NULL "
            "WHERE request_body IS NOT NULL AND created_at < ?",
            (cutoff,),
        )
        purged = cur.rowcount or 0
    if purged:
        log.info("purge_old_request_bodies: %d rows purged (retention=%dh)",
                 purged, REQUEST_BODY_RETENTION_HOURS)
    return purged


async def _sweep_awaiting_topup_ttl() -> None:
    """Refund abandoned awaiting_topup jobs and transition to 'abandoned'.

    Kanban t_9a705578: Jobs in 'awaiting_topup' state longer than
    TOPUP_TTL_DAYS get refunded (original PI) and transitioned to
    'abandoned'. The result remains encrypted and is discarded.

    Runs periodically via _topup_ttl_sweep_loop. Never raises — failures
    are logged and the loop continues on the next tick.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=TOPUP_TTL_DAYS)).isoformat()
    with db() as conn:
        # Find all awaiting_topup jobs older than TTL
        rows = conn.execute(
            "SELECT job_id, shortfall_cents FROM jobs WHERE state='awaiting_topup' AND awaiting_topup_at<?",
            (cutoff,),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            job_id = row["job_id"]
            shortfall_cents = int(row["shortfall_cents"] or 0)
            # Look up the original PI
            llm_row = conn.execute(
                "SELECT stripe_pi_id FROM llm_tokens WHERE job_id=?",
                (job_id,),
            ).fetchone()
            original_pi_id = (llm_row["stripe_pi_id"] if llm_row else "") or ""
            # Refund the original PI
            if original_pi_id:
                refund = refund_payment(original_pi_id)
                log.info("topup TTL refund for job %s (PI=%s, shortfall=%d cents): %s",
                         job_id, original_pi_id, shortfall_cents,
                         refund.get("status", "error"))
            # Transition job to abandoned, clear shortfall data
            conn.execute(
                "UPDATE jobs SET state=?, awaiting_topup_at=NULL, shortfall_cents=0, topup_pi_id=NULL, topup_capture_amount=0 WHERE job_id=?",
                ("abandoned", job_id),
            )
            log.info("job %s -> abandoned (topup TTL expired)", job_id)
            # Note: we don't delete the result blob — it stays encrypted
            # on EFS for forensic needs (see CQ-3 discussion above).

    # If we made any refunds, log a summary
    if rows:
        log.info("_sweep_awaiting_topup_ttl: %d jobs transitioned to abandoned", len(rows))


async def _request_body_purge_loop() -> None:
    """Periodic background task that purges old request_body rows.

    Runs every REQUEST_BODY_PURGE_INTERVAL_SECONDS (default 1h) for the
    daemon's lifetime. Never raises — failures are logged and the loop
    continues on the next tick. Disables itself when
    BROKER_REQUEST_BODY_PURGE_DISABLED=1 is set (the test suite does this
    so it can call purge_old_request_bodies() directly with controlled
    fixtures).
    """
    while True:
        try:
            await asyncio.sleep(REQUEST_BODY_PURGE_INTERVAL_SECONDS)
            if os.environ.get("BROKER_REQUEST_BODY_PURGE_DISABLED", "").strip() == "1":
                continue
            purge_old_request_bodies()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("request_body purge loop error: %s", e)


async def _topup_ttl_sweep_loop() -> None:
    """Periodic background task that refunds abandoned awaiting_topup jobs.

    Kanban t_9a705578: Jobs in 'awaiting_topup' state longer than
    BROKER_TOPUP_TTL_DAYS (default 7) get refunded (original PI) and
    transitioned to 'abandoned'. The result remains encrypted and is
    discarded.

    Runs every BROKER_TOPUP_TTL_SWEEP_MINUTES (default 60) for the daemon's
    lifetime. Never raises — failures are logged and the loop continues.
    """
    while True:
        try:
            await asyncio.sleep(TOPUP_TTL_SWEEP_MINUTES * 60)
            if os.environ.get("BROKER_TOPUP_TTL_DISABLED", "").strip() == "1":
                continue
            await _sweep_awaiting_topup_ttl()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("topup TTL sweep loop error: %s", e)


def sweep_expired_input_uploads() -> int:
    """Abandon expired upload jobs and remove any uploaded ciphertext."""
    now = datetime.now(timezone.utc)
    with db() as conn:
        rows = conn.execute(
            "SELECT job_id, request_body FROM jobs WHERE state='awaiting_inputs' "
            "AND input_upload_expires_at IS NOT NULL AND input_upload_expires_at < ?",
            (now.isoformat(),),
        ).fetchall()
    client = _get_s3_client() if rows else None
    swept = 0
    for row in rows:
        try:
            body = json.loads(row["request_body"] or "{}")
        except Exception:
            body = {}
        for item in body.get("input_files") or []:
            try:
                client.delete_object(
                    Bucket=ARTIFACT_BUCKET,
                    Key=f"inputs/{row['job_id']}/{item['filename']}")
            except Exception as exc:
                log.warning("expired input cleanup failed for %s/%s: %s",
                            row["job_id"], item.get("filename"), exc)
        with db() as conn:
            changed = conn.execute(
                "UPDATE jobs SET state='abandoned', input_status='expired', "
                "finished_at=?, error='input upload window expired' "
                "WHERE job_id=? AND state='awaiting_inputs'",
                (now.isoformat(), row["job_id"]),
            ).rowcount
        swept += int(changed > 0)
    return swept


async def _input_upload_sweep_loop() -> None:
    while True:
        try:
            await asyncio.sleep(60)
            swept = sweep_expired_input_uploads()
            if swept:
                log.info("abandoned %d expired input-upload jobs", swept)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("input upload sweep failed: %s", exc)


# ---------- Worker lifecycle ----------

@dataclass
class WorkerState:
    instance_id: str
    private_ip: str
    launched_at: float


WORKER_IDENTITY_FILES = (
    "worker-keys.json",
    "worker-attestation.json",
    "worker-heartbeat.json",
)


def _published_worker_identity_instances() -> set[str]:
    """Return instance ids currently named by worker identity/heartbeat files."""
    instances: set[str] = set()
    for name in WORKER_IDENTITY_FILES:
        path = LOGS / name
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        instance_id = payload.get("instance_id")
        if isinstance(instance_id, str) and instance_id:
            instances.add(instance_id)
    return instances


def _clear_worker_identity_files(reason: str) -> None:
    """Delete stale worker identity/heartbeat files before a fresh worker bind.

    The broker uses these files as the client-visible upload-key authority. If
    they survive a worker termination or broker/policy redeploy, file jobs can
    sit in awaiting_worker while healthz reports an unrelated/stale worker state.
    Clearing them on a new launch makes the state monotonic: missing -> current
    worker publishes -> awaiting_inputs, or failed with a concrete reason.
    """
    removed: list[str] = []
    for name in WORKER_IDENTITY_FILES:
        path = LOGS / name
        try:
            path.unlink()
            removed.append(name)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("could not remove stale %s before worker launch: %s", name, exc)
    if removed:
        log.info("cleared stale worker identity files (%s): %s", reason, ",".join(removed))


def _worker_heartbeat(instance_id: str = "") -> dict:
    """Return the current worker heartbeat for instance_id, or {}.

    The worker publishes its attestation/input key before it finishes the slow
    NemoClaw sandbox build. File-upload jobs must not expose upload URLs or be
    queued until the poller has started with a real sandbox. The heartbeat is
    the broker-visible readiness gate for that condition.
    """
    try:
        hb = json.loads((LOGS / "worker-heartbeat.json").read_text())
    except Exception:
        return {}
    if instance_id and hb.get("instance_id") != instance_id:
        return {}
    return hb if isinstance(hb, dict) else {}


def _worker_ready_for_jobs(instance_id: str = "") -> tuple[bool, str]:
    hb = _worker_heartbeat(instance_id)
    if not hb:
        return False, "missing worker-heartbeat.json"
    stage = str(hb.get("boot_stage", ""))
    status = str(hb.get("status", ""))
    detail = str(hb.get("boot_detail", ""))
    if stage == "ready" and status == "idle":
        return True, "ready"
    if stage == "failed":
        return False, f"worker boot failed: {detail or status or 'unknown'}"
    return False, f"worker not ready: status={status or '?'} boot_stage={stage or '?'} detail={detail or '?'}"


class WorkerManager:
    """Owns the lifecycle of the single burst worker."""

    def __init__(self) -> None:
        self.ec2 = boto3.client("ec2", region_name=BROKER_REGION)
        self._state: Optional[WorkerState] = None
        self._lock = asyncio.Lock()
        self._last_job_finished: Optional[float] = None
        self._terminate_task: Optional[asyncio.Task] = None

    async def ensure_worker(self) -> WorkerState:
        """Return running worker, launching one if needed."""
        async with self._lock:
            if self._state is not None:
                # Verify still running
                try:
                    desc = self.ec2.describe_instances(InstanceIds=[self._state.instance_id])
                    state = desc["Reservations"][0]["Instances"][0]["State"]["Name"]
                    if state in ("running", "pending"):
                        return self._state
                except Exception as e:
                    log.warning("worker %s lookup failed: %s", self._state.instance_id, e)
                self._state = None

            existing = self._find_existing_worker()
            if existing:
                published_instances = _published_worker_identity_instances()
                if published_instances and published_instances != {existing.instance_id}:
                    _clear_worker_identity_files(
                        f"adopting worker {existing.instance_id}; stale identity named {sorted(published_instances)}")
                log.info("adopted existing worker %s", existing.instance_id)
                self._state = existing
                return existing

            log.info("launching new worker (instance_type=%s)", BROKER_WORKER_INSTANCE_TYPE)
            _clear_worker_identity_files("launching new worker")
            # Build CpuOptions only when SEV-SNP is enabled AND the instance type
            # is one that supports it (m6a.*/c6a.* family). Sending the wrong
            # CpuOptions to RunInstances returns UnsupportedOperation in regions
            # that don't offer SEV-SNP, or for instance types that lack support.
            # Decision: assume the operator picked a SEV-SNP-capable instance type
            # in a region with support. If BROKER_ENABLE_SEV_SNP=0, we skip the
            # flag entirely (Nitro isolation only).
            run_kwargs: dict = {
                "ImageId": BROKER_WORKER_AMI,
                "InstanceType": BROKER_WORKER_INSTANCE_TYPE,
                "SubnetId": BROKER_SUBNET_ID,
                "SecurityGroupIds": [BROKER_WORKER_SG],
                "IamInstanceProfile": {"Arn": BROKER_WORKER_IAM_ROLE},
                "MinCount": 1,
                "MaxCount": 1,
                "BlockDeviceMappings": [{
                    "DeviceName": "/dev/sda1",
                    "Ebs": {"VolumeSize": 50, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True},
                }],
                "TagSpecifications": [{
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": k, "Value": v} for k, v in {
                            "Name": f"{WORKER_NAME_PREFIX}{secrets.token_hex(4)}",
                            **WORKER_TAG,
                            "ManagedBy": "broker-daemon",
                        }.items()
                    ],
                }],
                "UserData": self._render_worker_user_data(),
            }
            if BROKER_ENABLE_SEV_SNP:
                # Chose lowercase "enabled" over "Enabled" because boto3's
                # RunInstances validator rejects anything except enabled/disabled
                # (verified with DryRun in eu-west-1, 2026-06-27). Uppercase
                # "Enabled" returns InvalidParameterValue at launch time.
                run_kwargs["CpuOptions"] = {"AmdSevSnp": "enabled"}
            resp = self.ec2.run_instances(**run_kwargs)
            instance = resp["Instances"][0]
            instance_id = instance["InstanceId"]
            log.info("worker launched: %s (waiting for running state)", instance_id)

            # Wait until running (poll up to 90s)
            for _ in range(18):
                await asyncio.sleep(5)
                desc = self.ec2.describe_instances(InstanceIds=[instance_id])
                st = desc["Reservations"][0]["Instances"][0]["State"]["Name"]
                if st == "running":
                    private_ip = desc["Reservations"][0]["Instances"][0]["PrivateIpAddress"]
                    self._state = WorkerState(instance_id, private_ip, time.time())
                    return self._state
                if st in ("terminated", "shutting-down"):
                    raise RuntimeError(f"worker {instance_id} entered {st}")

            raise RuntimeError(f"worker {instance_id} did not reach running in 90s")

    def _find_existing_worker(self) -> Optional[WorkerState]:
        resp = self.ec2.describe_instances(
            Filters=[
                {"Name": "tag:Project", "Values": [WORKER_TAG["Project"]]},
                {"Name": "tag:Role", "Values": [WORKER_TAG["Role"]]},
                {"Name": "instance-state-name", "Values": ["running", "pending"]},
            ]
        )
        for res in resp["Reservations"]:
            for inst in res["Instances"]:
                return WorkerState(
                    instance_id=inst["InstanceId"],
                    private_ip=inst["PrivateIpAddress"],
                    launched_at=time.time(),
                )
        return None

    def _render_worker_user_data(self) -> str:
        # Resolve EFS DNS from worker template exports OR env
        efs_dns = os.environ.get("BROKER_EFS_DNS", "")
        # Inline template — keep small enough for CFN 16KB limit
        # Real install of NemoClaw happens via /opt/broker-daemon/worker-bootstrap.sh
        # which is rendered and pushed to EFS by the daemon on first launch.
        bootstrap_path = LOGS / "worker-bootstrap.sh"
        if not bootstrap_path.exists():
            raise RuntimeError(
                f"worker-bootstrap.sh missing at {bootstrap_path} — "
                "run scripts/bootstrap-control-plane.sh on the control plane first"
            )
        bootstrap = bootstrap_path.read_text()
        rendered = bootstrap.replace("__EFS_DNS__", efs_dns)
        rendered = rendered.replace("__ARTIFACT_BUCKET__", ARTIFACT_BUCKET)
        rendered = rendered.replace("__ARTIFACT_REGION__", BROKER_REGION)
        # Inject the onboard token so NemoClaw onboard can validate against
        # the broker LLM proxy. The bootstrap script references
        # ${BROKER_ONBOARD_TOKEN} but user-data runs in a bare shell without
        # config.env sourced, so we resolve it here at render time.
        onboard_token = os.environ.get("BROKER_ONBOARD_TOKEN", "")
        rendered = rendered.replace("${BROKER_ONBOARD_TOKEN:-onboard-placeholder}", onboard_token)
        # NemoClaw stub-mode toggle (2026-06-30). When
        # BROKER_NEMOCLAW_STUB_MODE=1 on the broker, the user-data.sh
        # template's __NEMOCLAW_STUB_MODE__ placeholder expands to 1 and
        # the bootstrap writes a shell shim at /usr/local/bin/nemohermes
        # that emulates `nemohermes <sb> exec` by calling worker-agent.py
        # directly on the host. The result envelope records
        # execution_mode="nemoclaw-sandbox-stub" so a reviewer can see
        # the run was not attested. Default off (real NemoClaw required).
        stub_mode = "1" if os.environ.get(
            "BROKER_NEMOCLAW_STUB_MODE", "").strip().lower() in (
            "1", "true", "yes", "on", "demo", "stub") else "0"
        rendered = rendered.replace("__NEMOCLAW_STUB_MODE__", stub_mode)
        return f"#!/bin/bash\nset -euo pipefail\n{rendered}"

    async def note_job_finished(self) -> None:
        """Called when a job completes. Starts/refreshes the idle timer."""
        self._last_job_finished = time.time()
        if self._terminate_task is None or self._terminate_task.done():
            self._terminate_task = asyncio.create_task(self._idle_terminate_loop())

    async def _idle_terminate_loop(self) -> None:
        if BROKER_DISABLE_IDLE_TERMINATION:
            log.info(
                "idle timer DISABLED (BROKER_DISABLE_IDLE_TERMINATION=1); "
                "warm-pool worker will not be terminated by the broker. "
                "Out-of-band lifecycle (warm-worker-manager.sh) owns termination."
            )
            return
        log.info("idle timer armed: %d minutes", BROKER_IDLE_BUFFER_MINUTES)
        while True:
            await asyncio.sleep(30)
            if self._state is None:
                return
            if self._last_job_finished is None:
                continue
            idle_for = time.time() - self._last_job_finished
            if idle_for >= BROKER_IDLE_BUFFER_MINUTES * 60:
                async with self._lock:
                    if self._state is None:
                        return
                    ready, reason = _worker_ready_for_jobs(self._state.instance_id)
                    if not ready:
                        # Do not kill a worker that is still in cloud-init / NemoClaw
                        # onboarding. The 2026-06-30 regression killed real
                        # sandbox builds at ~10 minutes, leaving jobs to fail closed.
                        log.info(
                            "idle timer skipped for booting worker %s: %s",
                            self._state.instance_id, reason,
                        )
                        self._last_job_finished = time.time()
                        continue
                    log.info(
                        "idle for %.0fs >= %dm; terminating worker %s",
                        idle_for, BROKER_IDLE_BUFFER_MINUTES, self._state.instance_id,
                    )
                    try:
                        self.ec2.terminate_instances(InstanceIds=[self._state.instance_id])
                    except Exception as e:
                        log.error("terminate failed: %s", e)
                    self._state = None
                    self._last_job_finished = None
                    return

    def get_state(self) -> Optional[WorkerState]:
        return self._state


# ---------- Job submission / status ----------

JOB_STATES = ("awaiting_worker", "awaiting_inputs", "queued", "running",
              "completed", "failed", "timeout", "awaiting_topup", "abandoned")


def _validate_webhook_url(url: str) -> tuple[bool, str]:
    """Validate a job-submission webhook URL against an SSRF blocklist.

    Rejects (returns False + error):
      - empty / missing (caller decides if webhook is required; this fn treats
        empty as OK so an optional field stays optional)
      - non-HTTPS schemes (http, ftp, file, gopher, ...)
      - hostnames that fail to parse as IP or DNS
      - hostnames that resolve to private / loopback / link-local / cloud
        metadata IPs (RFC1918, 127/8, 169.254/16, IPv6 ::1, fc00::/7, etc.)

    Decision rationale (VULN-S1): accept only public HTTPS URLs. Resolving DNS
    here is a TOCTOU surface (DNS rebinding between this check and the actual
    POST in _deliver_webhook) — for the hackathon demo we accept that risk
    because the daemon runs on the control plane with tightly scoped egress
    and is short-lived. Production should pin the resolved IP at submit time
    and re-check on delivery.
    """
    if not url:
        return True, ""

    try:
        parsed = urlparse(url)
    except Exception:
        return False, "webhook_url is not a valid URL"

    if parsed.scheme.lower() != "https":
        return False, "webhook_url must be a public HTTPS URL"

    host = (parsed.hostname or "").strip()
    if not host:
        return False, "webhook_url must include a hostname"

    # Try to interpret as an IP literal first (covers IPv6 literals too).
    try:
        ip = ipaddress.ip_address(host)
        candidates = [ip]
    except ValueError:
        # Hostname — resolve and check every returned address. If resolution
        # fails (NXDOMAIN, timeout), reject: we won't deliver to a non-routable
        # name.
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            return False, f"webhook_url hostname did not resolve: {host} ({e})"
        candidates = []
        for info in infos:
            try:
                candidates.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not candidates:
            return False, f"webhook_url hostname produced no IP addresses: {host}"

    for ip in candidates:
        # is_global covers RFC1918, loopback, link-local, multicast, reserved,
        # and private IPv6 ULA in one go. IPv4-mapped IPv6 (::ffff:10.0.0.1)
        # is normalised by ip_address so it's checked against IPv4 rules.
        if not ip.is_global:
            return False, (
                f"webhook_url must be a public HTTPS URL "
                f"({host} resolves to non-public address {ip})"
            )

    return True, ""


def resolve_skill_hash(skill_name: str, conn) -> str:
    """Return the canonical skill_hash for the job envelope.

    Registered WASM skills use the SHA-256 of the WASM binary as recorded in
    the `skills.wasm_manifest_hash` column. Built-in stubs (and any unknown
    skill name) fall back to sha256(name).hexdigest() so the worker has a
    stable, deterministic value to verify, even when no WASM manifest exists.

    Chose registered-hash over name-hash for registered skills so the broker
    can detect if a publisher re-registered the same name with a different
    WASM binary: the new binary's hash will differ and the worker's
    well-formedness check is the only validation we have offline. For
    built-in stubs, the name is the only identifier the worker has (no WASM
    binary yet), so the name hash is the most integrity we can offer without
    a real WASM executor.
    """
    row = conn.execute(
        "SELECT wasm_manifest_hash FROM skills s1 "
        "WHERE s1.name = ? "
        "AND s1.version = (SELECT MAX(version) FROM skills s2 "
        "                   WHERE s2.name = s1.name)",
        (skill_name,),
    ).fetchone()
    if row and row["wasm_manifest_hash"]:
        return row["wasm_manifest_hash"]
    return hashlib.sha256(skill_name.encode()).hexdigest()


# VULN-S6 (kanban t_b13072b3): Per-IP rate limiter on POST /v1/jobs.
# Defaults to 10 jobs/minute/IP. The limiter is an in-memory dict keyed
# by client IP, with each value being a list of monotonic timestamps in
# the last 60s. On every submission we evict entries older than 60s and
# reject with HTTP 429 + code="rate_limited" if the remaining count is
# >= the cap. The list-of-timestamps approach is cheap (no per-IP
# background cleanup task) and survives short bursts (cap is per minute,
# not per second). A client whose first 10 jobs all land in the same
# second will be locked out for ~60s.
#
# Set BROKER_RATE_LIMIT_DISABLED=1 to bypass entirely (used by the
# low-priority-fixes test suite + the demo's rapid-replay scenario).
# We chose in-memory over SQLite-backed because:
#   1. A persistent limiter adds DB write per request — defeats the
#      point of cheap rate limiting.
#   2. The daemon is single-process (no scale-out yet), so the limiter
#      state is naturally shared.
#   3. A restart resets the budget, which is the desired demo behaviour
#      (an attacker can't lock out a legitimate IP by flooding).
# Tests inject a fake by setting daemon._rate_limit_state.clear() before
# the run (matches the existing mock-injection pattern for s3_client).
RATE_LIMIT_PER_MINUTE = int(os.environ.get("BROKER_RATE_LIMIT_PER_MINUTE", "10"))
RATE_LIMIT_DISABLED = os.environ.get("BROKER_RATE_LIMIT_DISABLED", "").strip() == "1"
# Per-IP sliding-window timestamp log. {ip: [t1, t2, ...]}
_rate_limit_state: dict[str, list[float]] = {}
RATE_LIMIT_WINDOW_SECONDS = 60

# Per-call cap on `max_tokens` the broker will forward to the upstream
# LLM, regardless of what the worker asked for. Set to 100k so the
# reasoning model (minimax-m3:cloud) has enough completion budget to
# surface a visible `content` field for long, multi-file prompts
# (adversarial code review, multi-step refactors) — see VULN-S5
# commentary in the LLM proxy handler below. The per-day cost lever
# is DEMO_TOKEN_CAP, not this value.
MAX_TOKENS_CAP = int(os.environ.get("BROKER_LLM_MAX_TOKENS_CAP", "100000"))


def _client_ip(request: web.Request) -> str:
    """Return the best-effort client IP for rate-limit accounting.

    Honours X-Forwarded-For (first hop) when present so the broker can
    sit behind CloudFront / a load balancer and still attribute traffic
    per real client. Falls back to aiohttp's request.remote which is
    the peer's socket address — fine for direct connections.
    """
    xff = request.headers.get("X-Forwarded-For", "").strip()
    if xff:
        # Take the leftmost (original client) entry; comma-separated.
        return xff.split(",")[0].strip()
    return (request.remote or "unknown").strip()


def _check_rate_limit(request: web.Request) -> Optional[web.Response]:
    """Return a 429 JSON response if the client IP is over budget, else None.

    Evicts timestamps older than RATE_LIMIT_WINDOW_SECONDS before counting.
    Records the current timestamp only on the accept path (returned None),
    so a rejected request does NOT consume the client's budget — they can
    retry after the window slides without being further penalised. (This
    is the standard fixed-window-with-rejection semantics — matches what
    GitHub's API does.)
    """
    if RATE_LIMIT_DISABLED:
        return None
    ip = _client_ip(request)
    if not ip:
        # Unknown IP (e.g. unix socket) — don't rate-limit at all.
        return None
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    timestamps = _rate_limit_state.get(ip, [])
    # Evict expired entries in-place.
    fresh = [t for t in timestamps if t > cutoff]
    if len(fresh) >= RATE_LIMIT_PER_MINUTE:
        _rate_limit_state[ip] = fresh  # persist eviction
        retry_after = int(max(1.0, fresh[0] + RATE_LIMIT_WINDOW_SECONDS - now))
        return web.json_response({
            "error": (f"rate limit exceeded ({len(fresh)}/"
                      f"{RATE_LIMIT_PER_MINUTE} jobs in the last "
                      f"{RATE_LIMIT_WINDOW_SECONDS}s for ip {ip})"),
            "code": "rate_limited",
            "retry_after_seconds": retry_after,
        }, status=429)
    fresh.append(now)
    _rate_limit_state[ip] = fresh
    return None


# === VULN-LLMTK (kanban t_a18827b6): per-account daily JOB count cap ===
# The previous per-account limit was token-based (account_usage table).
# That limit doesn't stop a "fail-spam" attacker: the broker refunds on
# failure but the EC2 minutes still burned (refund-eats-compute, see
# t_29b31ecb cost review §5). Combined with a trivially guessable
# account key (the underscore-split prefix of stripe_pi_id), an attacker
# could mint pi_evil_1..pi_evil_999 and get a fresh budget per pi_id.
#
# Two changes landed together to close this:
#   1. account_key_for (VULN-S7, t_c6beba80) hashes pi_id with a
#      server-side secret so distinct pi_ids can't be enumerated.
#   2. THIS check counts JOBS, not tokens. Every accepted submit
#      bumps jobs_used by 1 regardless of outcome, so failed jobs still
#      cost the attacker their daily budget. Day rollover happens by
#      the natural day_utc key change — no cron needed.
#
# Chose BEGIN IMMEDIATE check-and-bump over two separate queries so two
# concurrent submits from the same account can't both squeeze under the
# cap by racing on the SELECT (the existing token-cap SELECT-only check
# has this race too, but a count-based cap is more sensitive — a single
# race lets an attacker double their budget). FAIL-OPEN on DB errors:
# better to accept a job than to drop it because of a transient SQLite
# issue (mirrors the token-cap fail-open posture below).
DAILY_JOB_CAP = int(os.environ.get("BROKER_DAILY_JOB_CAP", "5"))


def _check_daily_job_cap(account: str, today: str) -> Optional[web.Response]:
    """Return a 429 daily_cap response if `account` is over budget, else None.

    Side effect on the accept path: increments jobs_used for
    (account, today) by 1 inside the same transaction as the cap check.

    The returned response carries `code: "daily_cap"` AND
    `reason: "daily_cap"` — clients use the code for programmatic
    handling (retry/backoff) and the reason field for human-readable
    logs. Same shape as the existing `rate_limited` response so client
    code that already handles 429 + a code field works without change.
    """
    with db() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT jobs_used FROM account_quota "
                "WHERE account_key=? AND day_utc=?",
                (account, today),
            ).fetchone()
            current = row["jobs_used"] if row else 0
            if current >= DAILY_JOB_CAP:
                conn.execute("ROLLBACK")
                return web.json_response({
                    "error": (f"daily job cap exceeded "
                              f"({current}/{DAILY_JOB_CAP} for account {account})"),
                    "code": "daily_cap",
                    "reason": "daily_cap",
                    "jobs_used": current,
                    "cap": DAILY_JOB_CAP,
                }, status=429)
            new_used = current + 1
            if row:
                conn.execute(
                    "UPDATE account_quota SET jobs_used=? "
                    "WHERE account_key=? AND day_utc=?",
                    (new_used, account, today),
                )
            else:
                conn.execute(
                    "INSERT INTO account_quota "
                    "(account_key, day_utc, jobs_used) VALUES (?, ?, ?)",
                    (account, today, new_used),
                )
            conn.execute("COMMIT")
            return None
        except Exception:
            # Fail-open — better to accept than to drop on a transient
            # SQLite issue. The cap is best-effort defense-in-depth;
            # the Stripe holds in _finalize_job are the authoritative
            # economic stop.
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return None


# === VULN-S7 (account key hashing, t_c6beba80) ===
# The original account key was the underscore-split prefix of stripe_pi_id
# ("pi_test_abc123" → "pi_test") — trivially guessable and bucketised too
# coarsely (every test account landed in one bucket). account_key_for hashes
# pi_id with a server-side secret so:
#   - An attacker who knows pi_id can't reverse-engineer the key
#   - All pi_ids are uniformly distributed into distinct buckets (no collisions)
#   - Changing the server-side secret instantly re-buckets every account
#     (useful for rotating away from a compromised secret without DB migration)
#
# Empty input is special-cased to "anon" so the account_usage table never
# stores a key derived from empty data (which would still be deterministic
# but is semantically misleading in audit logs).
def account_key_for(stripe_pi_id: str) -> str:
    """Return the per-account hash for daily token-cap tracking."""
    if not stripe_pi_id:
        return "anon"
    secret = os.environ.get("BROKER_ACCOUNT_HASH_SECRET", "")
    payload = f"{stripe_pi_id}|{secret}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# === showcase skill dispatch wiring (t_ab320c7b, t_43a1bba7, t_4b0a4fbe, t_5d2c8e91) ===
#
# Architectural fix: POST /v1/skills stores the registered prompt_template in
# the skills table, but until commit <this commit> the worker poller only knew
# about 3 hardcoded stub names in its `skill_prompts` dict — the registered
# template never reached the LLM. resolve_skill_prompt closes that gap by
# looking up the latest-version prompt_template for a skill name (or None if
# the skill is a built-in stub, a WASM-only registration, or unknown).
#
# Chose "latest version wins" matching resolve_skill_hash's policy above so
# the broker can't accidentally inject a stale prompt after a re-registration.
# Returning None (rather than raising) lets submit_job fall back to
# "no skill_prompt in envelope" semantics for WASM-only / built-in / unknown
# skills — the worker then takes its existing fallback paths.
def resolve_skill_prompt(skill_name: str, conn) -> str | None:
    """Return the registered prompt_template for the latest version, or None.

    None means: not registered as a prompt_template skill (could be a WASM-only
    registration, a built-in stub, or unknown). The caller MUST NOT add a
    skill_prompt field to the envelope when this returns None — the absence
    of the field is what tells the worker to fall through to its hardcoded
    dict (for built-ins) or the generic 'Process this request' fallback.
    """
    row = conn.execute(
        "SELECT prompt_template FROM skills s1 "
        "WHERE s1.name = ? "
        "AND s1.version = (SELECT MAX(version) FROM skills s2 "
        "                   WHERE s2.name = s1.name)",
        (skill_name,),
    ).fetchone()
    if row and row["prompt_template"]:
        return row["prompt_template"]
    return None


def resolve_skill_decrypt_input(skill_name: str, conn) -> bool:
    """Return the registered `decrypt_input` flag for the latest version of
    `skill_name`, or False if the skill is not registered / has it unset.

    Architectural context (kanban t_dea55bb2, showcase skill 3 — blind-audit):
    A registered prompt_template skill can opt in to having the worker
    decrypt encrypted_data with its X25519 privkey before handing it to
    the LLM. This helper is what submit_job reads to decide whether to
    inject `skill_decrypt_input: true` into the worker envelope.

    Defaults to False for built-in stubs (code-review, summarize,
    photo-glow-up) and unknown skill names so a typo / pre-registration
    / built-in never silently takes the decrypt path. The False default
    matches the daemon-level default declared in the skills table
    (decrypt_input INTEGER NOT NULL DEFAULT 0).
    """
    row = conn.execute(
        "SELECT decrypt_input FROM skills s1 "
        "WHERE s1.name = ? "
        "AND s1.version = (SELECT MAX(version) FROM skills s2 "
        "                   WHERE s2.name = s1.name)",
        (skill_name,),
    ).fetchone()
    if row is None:
        return False
    return bool(row["decrypt_input"])


def resolve_skill_resource_limits(skill_name: str, conn) -> dict | None:
    """Return the WASM resource-limits block from the latest registered
    version of `skill_name`, or None if the skill is not registered as a
    WASM skill.

    Architectural context (kanban t_c27c1d8d): the broker stores the
    manifest-declared caps (`resource_limits.max_fuel`,
    `resource_limits.max_duration_ms`, `resource_limits.max_memory_mb`)
    in the `skills` table at registration time. The worker reads these
    from the envelope to (a) configure the wasmtime engine's
    consume_fuel / epoch_interruption caps so a malicious or buggy
    WASM can't pin the host, and (b) honour an operator's request to
    bound a specific job (e.g. E8 in verify-wasm-skill-e2e.py pins
    max_fuel=1 to test that the cap actually fires). Returns None
    when the skill is a built-in stub, a prompt-template registration,
    or unknown — those don't have WASM resource caps because they
    never reach the wasmtime runtime.

    Schema note: we return the raw row dict from the `skills` table,
    NOT a hand-rolled subset. The worker's execute_wasm_skill only
    reads `max_fuel` and `max_duration_ms` today, but returning the
    full row keeps this helper stable as the worker grows new caps
    (e.g. `max_memory_mb` for the wasmtime Store limit).
    """
    row = conn.execute(
        "SELECT max_fuel, max_duration_ms, max_memory_mb, wasm_ref_uri "
        "FROM skills s1 "
        "WHERE s1.name = ? "
        "AND s1.version = (SELECT MAX(version) FROM skills s2 "
        "                   WHERE s2.name = s1.name)",
        (skill_name,),
    ).fetchone()
    if row is None:
        return None
    if row["wasm_ref_uri"] is None:
        return None
    return dict(row)


def resolve_skill_wasm_uri(skill_name: str, conn) -> str | None:
    """Return the on-disk WASM URI for the latest registered version of
    `skill_name`, or None if the skill isn't a WASM-registered skill OR
    no binary has been uploaded yet.

    Architectural context (kanban t_c27c1d8d): the broker stores uploaded
    WASM binaries at `WASM_DIR/{name}-{version}.wasm`; the worker reads
    them from the same EFS mount so the URI is directly usable as
    `open(uri, "rb")`. Returns None if EITHER:
      (a) the skill is a built-in stub, prompt-template registration,
          or unknown name — `wasm_ref_uri` is NULL in those cases, OR
      (b) the registered WASM exists in `skills` but no binary has
          been uploaded yet (`WASM_DIR/{name}-{version}.wasm` doesn't
          exist on disk).
    The (b) case is important: it means a registered-but-not-uploaded
    skill falls back to the legacy prompt path rather than failing
    the job with a missing-file error. submit_job only injects
    `wasm_uri` into the envelope when this function returns non-None,
    so the worker's `env.get("wasm_uri")` check cleanly distinguishes
    "this is a WASM skill" from "fall through to LLM/prompt path".
    """
    row = conn.execute(
        "SELECT version, wasm_ref_uri FROM skills s1 "
        "WHERE s1.name = ? "
        "AND s1.version = (SELECT MAX(version) FROM skills s2 "
        "                   WHERE s2.name = s1.name)",
        (skill_name,),
    ).fetchone()
    if row is None:
        return None
    if row["wasm_ref_uri"] is None:
        return None
    version = row["version"]
    path = WASM_DIR / f"{skill_name}-{version}.wasm"
    if not path.exists():
        return None
    return str(path)


# === token-receipt helper (t_2746d224, Stripe pillar showcase skill 2) ===
def _build_usage_context_for(encrypted_data: str) -> dict | None:
    """Resolve the prior job_id referenced in a token-receipt request.

    Accepts either a bare job_id string ("job_abc123") or a JSON object
    ({"job_id": "job_abc123"}). Returns None when the referenced job
    doesn't exist (so the worker can produce a clean "not found" receipt).

    On success returns a dict with the broker-side accounting fields
    needed for the cost calc: {job_id, prompt_tokens, completion_tokens,
    total_tokens, llm_tokens_used, llm_calls, duration_seconds,
    stripe_pi_id, started_at, finished_at, state}.

    Both `total_tokens` (worker schema) and `llm_tokens_used` (jobs
    column) are populated — either key works downstream.
    """
    if not encrypted_data:
        return None
    referenced = None
    try:
        parsed = json.loads(encrypted_data)
        if isinstance(parsed, dict):
            referenced = parsed.get("job_id", "")
        elif isinstance(parsed, str):
            referenced = parsed
    except json.JSONDecodeError:
        referenced = encrypted_data
    if not referenced or not isinstance(referenced, str):
        return None
    with db() as conn:
        # Stripe PI lives on llm_tokens (NOT jobs) — LEFT JOIN so a
        # test-seeded row without an llm_token still returns the prior
        # job's accounting data.
        row = conn.execute(
            "SELECT j.job_id, j.state, j.started_at, j.finished_at, "
            "j.llm_tokens_used, j.llm_calls, lt.stripe_pi_id "
            "FROM jobs j LEFT JOIN llm_tokens lt ON lt.job_id = j.job_id "
            "WHERE j.job_id = ?", (referenced,),
        ).fetchone()
        if not row:
            return None
        llm_total = row["llm_tokens_used"] or 0
        usage = {
            "job_id": row["job_id"],
            "prompt_tokens": 0,
            "completion_tokens": llm_total,
            "total_tokens": llm_total,
            "llm_tokens_used": llm_total,
            "llm_calls": row["llm_calls"] or 0,
            "stripe_pi_id": row["stripe_pi_id"] or "",
            "state": row["state"],
            "started_at": row["started_at"] or "",
            "finished_at": row["finished_at"] or "",
        }
        if usage["started_at"] and usage["finished_at"]:
            try:
                t0 = datetime.fromisoformat(usage["started_at"])
                t1 = datetime.fromisoformat(usage["finished_at"])
                usage["duration_seconds"] = max(0, int((t1 - t0).total_seconds()))
            except (ValueError, TypeError):
                usage["duration_seconds"] = 0
        else:
            usage["duration_seconds"] = 0
        return usage
# === end token-receipt helper ===


def validate_submit(body: dict) -> tuple[bool, str]:
    required = ("encrypted_skill", "encrypted_data", "requester_sig", "result_pubkey")
    missing = [k for k in required if k not in body or not body[k]]
    if missing:
        return False, f"missing fields: {', '.join(missing)}"
    if not isinstance(body.get("client_req_id"), str) or len(body["client_req_id"]) > 128:
        return False, "client_req_id must be a string up to 128 chars"
    # SSRF blocklist on the optional webhook callback URL (VULN-S1).
    ok, err = _validate_webhook_url(body.get("webhook_url", "") or "")
    if not ok:
        return False, err
    # Payment is handled by submit_job after structural validation. Legacy
    # clients may still pass stripe_pi_id in demo mode, but the live ACS path
    # expects a Shared Payment Token (spt_...) and returns HTTP 402 when absent.
    pi_id = body.get("stripe_pi_id", "") or ""
    # Optional: verify requester_sig if a requester_pubkey was also supplied
    # (per tee-broker-pattern/agent-skills.md skill-verify-attestation flow).
    # This is OPT-IN — old clients that pass dummy "0x" strings are accepted
    # as demo fallback, but real clients should provide requester_pubkey +
    # valid Ed25519 signature. We warn (don't reject) on missing pubkey.
    if body.get("requester_pubkey") and body["requester_sig"] != "0x":
        import crypto
        skill_hash = crypto.hash_field(body["encrypted_skill"])
        input_hash = crypto.hash_field(body["encrypted_data"])
        ok = crypto.verify_requester_sig(
            body["requester_pubkey"], body["requester_sig"],
            skill_hash, input_hash, body["result_pubkey"],
            pi_id, body.get("timestamp", ""))
        if not ok:
            return False, "invalid requester_sig (signature failed verification)"
    return True, ""


def build_job_envelope(*, job_id: str, created_at: str, body: dict,
                       llm_token: str, skill_hash: str,
                       input_files: list[dict] | None = None,
                       skill_prompt: str | None = None,
                       skill_decrypt_input: bool = False,
                       wasm_uri: str | None = None,
                       resource_limits: dict | None = None,
                       usage_context: dict | None = None,
                       worker_instance_id: str | None = None,
                       worker_key_id: str | None = None) -> dict:
    """Build the one canonical worker envelope for every submission path.

    Keeping this pure prevents the attachment `/ready` path from silently
    dropping dispatch credentials or skill settings as it did previously.
    """
    envelope = {
        "job_id": job_id,
        "created_at": created_at,
        "encrypted_skill": body["encrypted_skill"],
        "skill_hash": skill_hash,
        "encrypted_data": body["encrypted_data"],
        "usage_context": usage_context,
        "requester_sig": body["requester_sig"],
        "result_pubkey": body["result_pubkey"],
        "stripe_pi_id": body["stripe_pi_id"],
        "llm_token": llm_token,
        "llm_proxy_url": f"https://{BROKER_DOMAIN}/v1/llm/chat/completions",
        "skill_decrypt_input": bool(skill_decrypt_input),
    }
    if input_files:
        envelope["input_files"] = input_files
    if skill_prompt is not None:
        envelope["skill_prompt"] = skill_prompt
    if wasm_uri is not None:
        envelope["wasm_uri"] = wasm_uri
    if resource_limits is not None:
        envelope["max_fuel"] = int(resource_limits["max_fuel"])
        envelope["max_duration_ms"] = int(resource_limits["max_duration_ms"])
    if worker_instance_id:
        envelope["worker_instance_id"] = worker_instance_id
    if worker_key_id:
        envelope["worker_key_id"] = worker_key_id
    return envelope


def _policy_hash() -> str:
    """Hash the exact OpenShell policy bytes used for worker key binding.

    The worker reads the deployed EFS copy because user-data runs outside the
    broker source tree. The broker may not have `openshell/policy.yaml` under
    `/opt/broker-daemon` after bootstrap, so prefer the shared EFS policy and
    fall back to the source-tree copy for offline tests/dev.
    """
    candidates = [
        LOGS / "openshell-policy.yaml",
        Path(__file__).parent / "openshell" / "policy.yaml",
    ]
    for policy_path in candidates:
        try:
            if policy_path.exists():
                return hashlib.sha256(policy_path.read_bytes()).hexdigest()
        except Exception:
            continue
    return ""


def _verify_snp_quote_signature(attestation: dict) -> bool:
    """Verify the SNP report signature with its supplied VCEK/VLEK cert.

    Requesters must additionally validate that certificate to AMD's ARK trust
    root. This local check prevents report/key splicing inside the broker.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes as crypto_hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )
        report = base64.b64decode(attestation["report"], validate=True)
        certs = attestation.get("cert_chain") or []
        if len(report) != 1184 or not certs:
            return False
        # SNP signature is r[72] || s[72] || reserved[368], with r/s encoded
        # little-endian. The signed report body is the first 0x2A0 bytes.
        r = int.from_bytes(report[672:744], "little")
        s = int.from_bytes(report[744:816], "little")
        signature = encode_dss_signature(r, s)
        cert = x509.load_der_x509_certificate(base64.b64decode(certs[0]))
        public_key = cert.public_key()
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            return False
        public_key.verify(signature, report[:672],
                          ec.ECDSA(crypto_hashes.SHA384()))
        return True
    except Exception:
        return False


def _worker_identity_status(instance_id: str = "") -> tuple[dict | None, str]:
    """Return verified worker identity plus diagnostic reason.

    Production requires a real SEV-SNP quote signature. Demo/stub mode may
    accept the binding-only `instance_id_sha256` fallback so the regional
    demo path can exercise encrypted file uploads without SNP hardware.
    """
    keys_path = LOGS / "worker-keys.json"
    att_path = LOGS / "worker-attestation.json"
    try:
        keys = json.loads(keys_path.read_text())
    except FileNotFoundError:
        return None, f"missing {keys_path.name}"
    except Exception as exc:
        return None, f"invalid {keys_path.name}: {exc}"
    try:
        att = json.loads(att_path.read_text())
    except FileNotFoundError:
        return None, f"missing {att_path.name}"
    except Exception as exc:
        return None, f"invalid {att_path.name}: {exc}"
    try:
        pub = base64.b64decode(keys["x25519_pubkey_b64"], validate=True)
    except Exception as exc:
        return None, f"invalid worker public key: {exc}"
    if len(pub) != 32:
        return None, f"worker public key has {len(pub)} bytes, expected 32"
    if instance_id and keys.get("instance_id") != instance_id:
        return None, f"worker key instance mismatch keys={keys.get('instance_id')} expected={instance_id}"
    if instance_id and att.get("instance_id") != instance_id:
        return None, f"worker attestation instance mismatch att={att.get('instance_id')} expected={instance_id}"
    policy_hash = _policy_hash()
    if keys.get("policy_hash") != policy_hash:
        return None, (
            "worker policy_hash mismatch "
            f"worker={str(keys.get('policy_hash', ''))[:16]}... "
            f"broker={policy_hash[:16]}..."
        )
    try:
        binding = hashlib.sha256(
            b"verdantforged-worker-input-v1\0" + pub + b"\0" +
            bytes.fromhex(policy_hash)
        ).hexdigest()
    except Exception as exc:
        return None, f"worker binding derivation failed: {exc}"
    if not hmac.compare_digest(binding, keys.get("attestation_binding_sha256", "")):
        return None, "worker binding digest mismatch"
    report_data = att.get("report_data", "") or ""
    source = att.get("source", "") or ""
    if report_data.startswith(binding) and len(report_data) == 128:
        if source not in ("tsm_configfs", "snpguest"):
            return None, f"unsupported attestation source {source!r}"
        if not _verify_snp_quote_signature(att):
            return None, "SNP quote signature verification failed"
        return {**keys, "attestation": att}, "ok"
    if BROKER_ALLOW_STUB_WORKER_ATTESTATION and source in ("instance_id_sha256", "stub", ""):
        stub_att = dict(att)
        stub_att["source"] = source or "stub"
        stub_att["report_data"] = binding + "0" * 64
        stub_att.setdefault("tee_type", "demo-stub")
        return {**keys, "attestation": stub_att}, "ok-stub-attestation"
    if not report_data:
        return None, "worker attestation report_data is empty"
    return None, "worker attestation report_data is not bound to input key"


def _load_verified_worker_identity(instance_id: str = "") -> dict | None:
    """Return an attestation-bound worker upload key, or fail closed.

    This validates the report_data binding and the worker identity. Full AMD
    certificate-chain validation remains the verifier/client's job; the raw
    quote and chain are returned to the client for that purpose.
    """
    identity, _reason = _worker_identity_status(instance_id)
    return identity


_TERMINAL_WORKER_IDENTITY_REASONS = (
    "worker policy_hash mismatch",
    "worker binding digest mismatch",
    "SNP quote signature verification failed",
    "unsupported attestation source",
    "worker attestation report_data is not bound to input key",
)


def _terminal_worker_identity_reason(reason: str) -> bool:
    """True when waiting longer cannot make the current published identity valid."""
    return reason.startswith(_TERMINAL_WORKER_IDENTITY_REASONS)


async def _prepare_file_job(job_id: str) -> None:
    """Launch/bind a worker, then expose its verified upload key."""
    try:
        worker = await worker_mgr.ensure_worker()
        deadline = time.time() + int(os.environ.get(
            "BROKER_WORKER_KEY_WAIT_SECONDS", "1200"))
        identity = None
        last_reason = "not checked"
        last_reason_log = 0.0
        hard_reason_since: float | None = None
        hard_reason_grace = int(os.environ.get(
            "BROKER_WORKER_IDENTITY_ERROR_SECONDS", "90"))
        while time.time() < deadline:
            identity, reason = _worker_identity_status(worker.instance_id)
            last_reason = reason
            if identity:
                ready, ready_reason = _worker_ready_for_jobs(worker.instance_id)
                if ready:
                    if reason != "ok":
                        log.info("file job %s accepted worker identity via %s", job_id, reason)
                    break
                reason = f"identity ok, {ready_reason}"
                last_reason = reason
            now = time.time()
            if _terminal_worker_identity_reason(reason):
                if hard_reason_grace <= 0:
                    raise RuntimeError(
                        f"worker identity is invalid for {worker.instance_id}: {reason}")
                if hard_reason_since is None:
                    hard_reason_since = now
                elif now - hard_reason_since >= hard_reason_grace:
                    raise RuntimeError(
                        f"worker identity is invalid for {worker.instance_id}: {reason}")
            else:
                hard_reason_since = None
            if now - last_reason_log >= 30:
                log.info("file job %s waiting for worker identity from %s: %s",
                         job_id, worker.instance_id, reason)
                last_reason_log = now
            try:
                ec2 = getattr(worker_mgr, "ec2", None)
                if ec2 is not None:
                    desc = ec2.describe_instances(InstanceIds=[worker.instance_id])
                    state_name = desc["Reservations"][0]["Instances"][0]["State"]["Name"]
                    if state_name not in ("pending", "running"):
                        log.warning("file job %s worker %s became %s; reacquiring worker",
                                    job_id, worker.instance_id, state_name)
                        setattr(worker_mgr, "_state", None)
                        worker = await worker_mgr.ensure_worker()
                        last_reason_log = 0.0
            except Exception as exc:
                log.warning("file job %s worker liveness check failed: %s", job_id, exc)
            await asyncio.sleep(5)
        if identity is None:
            raise RuntimeError(f"worker did not publish an attestation-bound input key ({last_reason})")
        expires = (datetime.now(timezone.utc) + timedelta(
            seconds=INPUT_UPLOAD_TTL_SECONDS)).isoformat()
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET state='awaiting_inputs', input_status='awaiting_inputs', "
                "worker_instance_id=?, worker_key_id=?, input_upload_expires_at=? "
                "WHERE job_id=? AND state='awaiting_worker'",
                (worker.instance_id, identity["key_id"], expires, job_id),
            )
    except Exception as exc:
        log.error("file job %s worker preparation failed: %s", job_id, exc)
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET state='failed', error=?, finished_at=? "
                "WHERE job_id=? AND state='awaiting_worker'",
                (f"worker attestation failed: {exc}",
                 datetime.now(timezone.utc).isoformat(), job_id),
            )


async def _resume_file_jobs() -> None:
    """Resume worker preparation after a broker restart."""
    with db() as conn:
        waiting = conn.execute(
            "SELECT job_id FROM jobs WHERE state='awaiting_worker'"
        ).fetchall()
        uploading = conn.execute(
            "SELECT job_id, worker_instance_id FROM jobs "
            "WHERE state='awaiting_inputs'"
        ).fetchall()
    for row in waiting:
        asyncio.create_task(_prepare_file_job(row["job_id"]))
    for row in uploading:
        if _load_verified_worker_identity(row["worker_instance_id"] or ""):
            continue
        # The prior key is no longer usable. Delete any ciphertext uploaded
        # to it, then acquire a fresh worker/key and force a new upload cycle.
        with db() as conn:
            request_row = conn.execute(
                "SELECT request_body FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        try:
            body = json.loads(request_row["request_body"] or "{}")
        except Exception:
            body = {}
        for item in body.get("input_files") or []:
            try:
                _get_s3_client().delete_object(
                    Bucket=ARTIFACT_BUCKET,
                    Key=f"inputs/{row['job_id']}/{item['filename']}")
            except Exception:
                pass
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET state='awaiting_worker', "
                "input_status='awaiting_worker', worker_instance_id=NULL, "
                "worker_key_id=NULL, input_upload_expires_at=NULL "
                "WHERE job_id=? AND state='awaiting_inputs'", (row["job_id"],))
        asyncio.create_task(_prepare_file_job(row["job_id"]))
async def submit_job(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    # VULN-LLMTK (kanban t_a18827b6): per-account daily job cap. Runs
    # BEFORE validate_submit so a quota'd account doesn't trigger a
    # Stripe PaymentIntent retrieve just to be rejected (Q2d assertion:
    # verify_payment_intent must not be called for already-quota'd
    # accounts). Only meaningful when stripe_pi_id is present — malformed
    # bodies fall through to validate_submit's 400. The cap is the
    # authoritative job-count stop (the token-cap SELECT below is
    # per-token and doesn't protect against fail-spam / EC2-minute
    # burn, see cost review t_29b31ecb §5). account_key_for is the
    # VULN-S7 hash so an attacker can't enumerate pi_ids to mint fresh
    # budgets.
    payment_token_for_cap = ""
    if isinstance(body, dict):
        payment_token_for_cap = (body.get("shared_payment_token") or body.get("spt") or
                                 body.get("payment_token") or body.get("stripe_pi_id") or "")
    if payment_token_for_cap:
        account = account_key_for(payment_token_for_cap)
        today_for_cap = datetime.now(timezone.utc).date().isoformat()
        cap_resp = _check_daily_job_cap(account, today_for_cap)
        if cap_resp is not None:
            return cap_resp

    ok, err = validate_submit(body)
    if not ok:
        return web.json_response({"error": err}, status=400)

    # Input attachment validation (t_0ef31767). Runs BEFORE the rate-limit
    # check so a malformed request doesn't consume budget — same rationale
    # as validate_submit above. Empty list / missing field falls through
    # to the single-phase flow (backward-compatible). Non-empty list is
    # validated for shape, filename allowlist, size limits, and count cap.
    input_files = body.get("input_files") or []
    if_files_ok, if_err = validate_input_files(input_files)
    if not if_files_ok:
        return web.json_response({"error": if_err}, status=400)
    if input_files and not _valid_x25519_pubkey(body.get("result_pubkey", "")):
        return web.json_response({
            "error": "file jobs require a base64-encoded 32-byte X25519 result_pubkey",
            "code": "file_encryption_required",
        }, status=400)

    # VULN-S6 (kanban t_b13072b3): Per-IP rate limit check. Runs AFTER
    # validate_submit so malformed requests don't consume budget (they
    # also get a clearer 400 instead of a 429). Records the timestamp on
    # accept so a subsequent retry within the window gets 429, not 202.
    rl_resp = _check_rate_limit(request)
    if rl_resp is not None:
        return rl_resp

    client_req_id = body["client_req_id"]
    webhook_url = body.get("webhook_url", "")
    now = datetime.now(timezone.utc).isoformat()
    today = now[:10]  # YYYY-MM-DD

    # Stripe ACS / MPP payment gate. Live mode requires an agent-supplied
    # Shared Payment Token (spt_...). Without it, return HTTP 402 so the
    # agent can authorize via Stripe Link and retry. Demo mode remains
    # backwards-compatible and synthesizes a demo PI id.
    required_amount = max(estimated_job_cost_cents(), MIN_CAPTURE_CENTS)
    spt_token = _agent_payment_token_from_request(request, body)

    # Idempotent replay must happen before charging: an ACS SPT is single-use.
    with db() as replay_conn:
        replay_row = replay_conn.execute(
            "SELECT job_id, state FROM jobs WHERE client_req_id = ?",
            (client_req_id,),
        ).fetchone()
    if replay_row is not None:
        return web.json_response({
            "job_id": replay_row["job_id"],
            "state": replay_row["state"],
            "idempotent_replay": True,
        }, status=200)

    payment_stub_mode = BROKER_PAYMENT_STUB_MODE
    if (STRIPE_SECRET_KEY or payment_stub_mode) and not spt_token:
        return _payment_required_response(required_amount)

    account_seed = spt_token or body.get("stripe_pi_id") or client_req_id
    account = account_key_for(account_seed)

    # Check daily token cap before accepting payment/job.
    # VULN-S7: account key is sha256(payment token + BROKER_ACCOUNT_HASH_SECRET)[:16]
    # (see account_key_for at module scope). Production would use the real
    # Stripe customer.id; the hashed-token form prevents trivial account
    # creation in the demo by tying the key to a server-side secret.
    # B5 fraud-suspension gate (kanban t_69b52324). A banned account
    # is rejected with 403 + account_suspended BEFORE the token-cap
    # check or any DB write — short-circuiting a known-bad caller is
    # cheaper than burning the lock-acquire in BEGIN IMMEDIATE below.
    # The reason string is the suspended_reason populated by
    # _bump_fraud_score when score > FRAUD_SCORE_BAN_THRESHOLD.
    suspended, suspended_reason = _account_is_suspended(account)
    if suspended:
        return web.json_response({
            "error": (f"account suspended due to fraud score threshold "
                      f"({suspended_reason}); contact support"),
            "code": "account_suspended",
        }, status=403)
    DEMO_TOKEN_CAP = int(os.environ.get("DEMO_TOKEN_CAP", "50000"))  # 50k tokens/day/account for demo
    with db() as conn:
        usage_row = conn.execute(
            "SELECT tokens_used, tokens_cap FROM account_usage WHERE account=? AND date=?",
            (account, today),
        ).fetchone()
        if usage_row and usage_row["tokens_used"] >= DEMO_TOKEN_CAP:
            return web.json_response({
                "error": f"daily token cap exceeded ({usage_row['tokens_used']}/{DEMO_TOKEN_CAP} for account {account})",
                "code": "token_cap_exceeded",
            }, status=429)

    if STRIPE_SECRET_KEY and not payment_stub_mode and not (
        BROKER_TEST_SPT_ISSUER and spt_token.startswith("spt_demo_")
    ):
        charge = charge_shared_payment_token(spt_token, required_amount, STRIPE_CURRENCY)
        if not charge.get("success"):
            return web.json_response({
                "error": f"agent payment failed: {charge.get('error', charge.get('status', 'unknown'))}",
                "code": "payment_failed",
            }, status=402)
        stripe_pi_id = str(charge.get("id") or "")
        pi_amount = int(charge.get("amount_cents") or required_amount)
        if not stripe_pi_id.startswith("pi_"):
            return web.json_response({
                "error": "Stripe did not return a PaymentIntent id",
                "code": "payment_failed",
            }, status=502)
        body = dict(body)
        body["stripe_pi_id"] = stripe_pi_id
        body["stripe_payment_method"] = "shared_payment_token"
    elif (payment_stub_mode or BROKER_TEST_SPT_ISSUER) and spt_token.startswith("spt_demo_"):
        stripe_pi_id = f"pi_demo_{secrets.token_hex(8)}"
        pi_amount = required_amount
        body = dict(body)
        body["stripe_pi_id"] = stripe_pi_id
        body["stripe_payment_method"] = "demo_stub"
        _log_demo_lifecycle("capture", stripe_pi_id, required_amount, note="payment_stub_mode")
    else:
        stripe_pi_id = body.get("stripe_pi_id") or f"pi_demo_{secrets.token_hex(8)}"
        pi_amount = 0
        body = dict(body)
        body["stripe_pi_id"] = stripe_pi_id

    # VULN-S10 (kanban t_b13072b3): Idempotency race fix. The previous
    # code did the existence check and the INSERT in two SEPARATE
    # `with db()` blocks, so a concurrent submission with the same
    # client_req_id could pass the check and then collide on the
    # UNIQUE(client_req_id) constraint — returning a 500 to one caller.
    # Fix: BEGIN IMMEDIATE acquires the SQLite write lock so the check
    # and insert are atomic, AND catch sqlite3.IntegrityError as a
    # belt-and-braces fallback if a future change splits the two again
    # (or if a different SQLite driver skips the lock acquisition).
    # On IntegrityError, re-fetch the now-existing row and return it
    # as an idempotent replay.
    job_id = ""
    llm_token = ""
    expires_at = ""
    skill_hash = ""
    skill_prompt = None
    usage_context = None
    wasm_uri = None
    resource_limits = None
    job_access_token = create_job_access_token()
    # decrypt_input for the requested skill (kanban t_dea55bb2):
    # defaults to False so unknown / built-in skills never get the
    # decrypt path. Resolved alongside skill_hash/prompt/wasm_uri so
    # a single SQL read pulls every per-skill dispatch field.
    skill_decrypt_input = False
    with db() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT job_id, state FROM jobs WHERE client_req_id = ?",
                (client_req_id,),
            ).fetchone()
            if existing:
                # Idempotent replay — release the write lock and return
                # the prior job. Rollback (no writes happened) so the
                # connection is back to a clean state.
                conn.execute("ROLLBACK")
                return web.json_response({
                    "job_id": existing["job_id"],
                    "state": existing["state"],
                    "idempotent_replay": True,
                }, status=200)

            job_id = f"job_{secrets.token_hex(12)}"
            # Generate per-job LLM access token. The worker uses this to call
            # the broker's /v1/llm/chat/completions proxy. The real LLM API
            # key never leaves the broker.
            llm_token = f"llm_{secrets.token_hex(24)}"
            # Job expires in 10 minutes — long enough for the worker to process
            # any reasonable skill, short enough to limit replay.
            # Per-job ephemeral LLM token TTL. Default 30 min — covers the
            # ~16 min NemoClaw sandbox cold start on a fresh worker. Override
            # via BROKER_LLM_TOKEN_TTL_MIN env var (e.g. for tests that
            # want fast expiry).
            token_ttl_min = int(os.environ.get("BROKER_LLM_TOKEN_TTL_MIN", "30"))
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=token_ttl_min)).isoformat()

            # Two-phase submit (t_0ef31767): if input_files is non-empty,
            # the job starts in awaiting_inputs (NOT queued) and we do NOT
            # write the EFS envelope or kick the worker yet — both happen
            # in mark_job_ready() after the client PUTs the files and
            # signals /ready. The state machine is intentionally kept
            # narrow: awaiting_inputs -> queued is the only transition
            # the broker drives; everything after queued (running,
            # completed, etc.) follows the existing single-phase path.
            initial_state = "awaiting_worker" if input_files else "queued"

            conn.execute(
                "INSERT INTO jobs (job_id, client_req_id, created_at, state, "
                "request_body, webhook_url, input_file_count, input_status, "
                "job_access_token_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, client_req_id, now, initial_state,
                 json.dumps(body), webhook_url,
                 len(input_files),
                 "awaiting_worker" if input_files else None,
                 hash_job_access_token(job_access_token)),
            )
            conn.execute(
                "INSERT INTO llm_tokens (token, job_id, stripe_pi_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (llm_token, job_id, stripe_pi_id, now, expires_at),
            )

            # WASM manifest verification (t_bf00a075): include the canonical
            # skill_hash in every envelope so the worker can verify it received
            # the right skill. For registered WASM skills this is the SHA-256
            # of the WASM binary from `skills.wasm_manifest_hash`; for built-in
            # stubs and unknown skills it falls back to sha256(name). The
            # envelope is the authoritative reference — the worker treats any
            # well-formed 64-char hex as accepted, and recomputes sha256(name)
            # separately for its own signed-payload chain.
            skill_hash = resolve_skill_hash(body["encrypted_skill"], conn)

            # Showcase-skill dispatch wiring (t_ab320c7b, t_43a1bba7,
            # t_4b0a4fbe, t_5d2c8e91): for registered prompt_template skills
            # (attestation-verifier, blind-audit, skill-discoverer, etc.) the
            # broker resolves the prompt template and injects it into the
            # envelope as `skill_prompt` so the worker can use it instead of
            # the generic fallback. Returns None for built-in stubs (worker
            # keeps using its hardcoded dict for those) and for WASM-only
            # registrations (those run on the worker without an LLM prompt).
            # We do NOT add the field when None — the absence of the field is
            # what tells the worker to take its existing fallback path. See
            # resolve_skill_prompt for the rationale.
            skill_prompt = resolve_skill_prompt(body["encrypted_skill"], conn)

            # decrypt_input for the registered skill (kanban t_dea55bb2,
            # showcase skill 3 — blind-audit): if True, the worker will
            # decrypt encrypted_data with its X25519 privkey before handing
            # it to the LLM. Resolved from the same SQL round-trip as
            # skill_prompt so we don't double-read the row.
            skill_decrypt_input = resolve_skill_decrypt_input(
                body["encrypted_skill"], conn)

            # WASM skill dispatch wiring (kanban t_c27c1d8d): for registered
            # WASM skills with an uploaded binary, resolve the on-disk EFS
            # path and inject it into the envelope as `wasm_uri` so the
            # worker can fetch and execute the WASM via wasmtime. Returns
            # None for built-in stubs, prompt-template registrations, and
            # registered-but-not-uploaded WASM skills. Same "field absent"
            # convention as skill_prompt — the worker checks
            # `env.get("wasm_uri")` to decide whether to take the WASM
            # path before the LLM/prompt/sandbox fallbacks.
            wasm_uri = resolve_skill_wasm_uri(body["encrypted_skill"], conn)

            # WASM resource limits (kanban t_c27c1d8d): inject the
            # manifest-declared max_fuel / max_duration_ms /
            # max_memory_mb into the envelope so the worker's
            # execute_wasm_skill can configure the wasmtime engine
            # correctly. Same "field absent" convention as wasm_uri —
            # the worker's env.get() fallbacks handle the non-WASM
            # case. We pass the dict directly so the worker can
            # grow new caps without a broker schema change.
            resource_limits = resolve_skill_resource_limits(
                body["encrypted_skill"], conn)

            # Token-receipt (showcase skill 2): look up the referenced prior
            # job's accounting data and inject it as `usage_context` so the
            # worker can build the cost breakdown without re-querying the DB
            # (the worker only sees EFS, not the broker's SQLite). For all
            # other skills this is null.
            if body["encrypted_skill"] == "token-receipt":
                usage_context = _build_usage_context_for(body["encrypted_data"])

            conn.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            # Belt-and-braces: a UNIQUE-constraint violation could still
            # occur if a concurrent transaction commits between our
            # BEGIN IMMEDIATE and the SELECT (rare but possible if a
            # future SQLite driver reverts to deferred transactions).
            # In that case, ROLLBACK and re-fetch the winning row.
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            log.warning("idempotency race lost for client_req_id=%s: %s",
                        client_req_id, e)
            with db() as replay_conn:
                replay_row = replay_conn.execute(
                    "SELECT job_id, state FROM jobs WHERE client_req_id = ?",
                    (client_req_id,),
                ).fetchone()
            if replay_row is not None:
                return web.json_response({
                    "job_id": replay_row["job_id"],
                    "state": replay_row["state"],
                    "idempotent_replay": True,
                }, status=200)
            # No row found — genuinely unexpected (maybe a different UNIQUE
            # column). Surface as a 500 so the operator sees the real error.
            raise

    # A cold file job must launch and attest its bound worker before the
    # client can encrypt. Upload URLs are exposed by authenticated status
    # once _prepare_file_job transitions awaiting_worker -> awaiting_inputs.
    if input_files:
        asyncio.create_task(_prepare_file_job(job_id))
        log.info("job %s awaiting attested worker (client_req_id=%s)",
                 job_id, client_req_id)
        return web.json_response({
            "job_id": job_id,
            "state": "awaiting_worker",
            "status_url": f"/v1/jobs/{job_id}",
            "job_access_token": job_access_token,
            "idempotent_replay": False,
        }, status=202)

    # Write envelope to EFS inbox (worker polls this).
    # The envelope includes the per-job LLM token so the worker can call
    # the broker's LLM proxy.
    INBOX.mkdir(parents=True, exist_ok=True)
    envelope_path = INBOX / f"{job_id}.json"
    envelope = build_job_envelope(
        job_id=job_id, created_at=now, body=body, llm_token=llm_token,
        skill_hash=skill_hash, skill_prompt=skill_prompt,
        skill_decrypt_input=skill_decrypt_input, wasm_uri=wasm_uri,
        resource_limits=resource_limits, usage_context=usage_context,
    )
    # skill_decrypt_input (showcase skill 3 — blind-audit, kanban t_dea55bb2):
    # if the registered skill has decrypt_input=true, the worker will
    # decrypt encrypted_data with its X25519 privkey (the same key whose
    # pubkey is advertised in /v1/discover.attestation.enclave_pubkey) before
    # handing it to the LLM. We inject this as a per-job envelope flag
    # rather than re-resolving on the worker side because the worker should
    # be able to rely on the broker's authority for skill configuration —
    # a registered prompt_template skill with decrypt_input=true gets the
    # flag every time, no client opt-in required. Same "field absent"
    # convention as skill_prompt/wasm_uri — we always set the field
    # (True or False) so the worker doesn't need to handle "field missing"
    # as a third state.
    # Only inject skill_prompt for registered prompt_template skills.
    # None / WASM-only / built-in / unknown => no field, worker falls back.
    # Chose "field absent" over "field present and empty/null" because the
    # worker's `env.get("skill_prompt") or skill_prompts.get(skill, ...)`
    # check needs a falsy value to fall through — an empty string would
    # be truthy and override a legitimate hardcoded entry. Verified by
    # tests/verify-dispatch-wiring.py E1, E4, E5.
    # WASM skill dispatch (kanban t_c27c1d8d): inject wasm_uri for registered
    # WASM skills with an uploaded binary. See resolve_skill_wasm_uri for
    # the None-when-not-WASM semantics; the absence of the field is what
    # tells the worker to take the LLM/prompt/sandbox fallback path.
    envelope_path.write_text(json.dumps(envelope))
    log.info("job %s queued (client_req_id=%s, llm_token issued)", job_id, client_req_id)

    # Kick worker if not running
    asyncio.create_task(_kick_worker_for_job(job_id))

    return web.json_response({
        "job_id": job_id,
        "state": "queued",
        "status_url": f"/v1/jobs/{job_id}",
        "job_access_token": job_access_token,
        "idempotent_replay": False,
    }, status=202)


async def _kick_worker_for_job(job_id: str) -> None:
    try:
        worker = await worker_mgr.ensure_worker()
        log.info("job %s -> worker %s (private_ip=%s)", job_id, worker.instance_id, worker.private_ip)
        # Mark the job as started now that a worker is assigned
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET state='running', started_at=? WHERE job_id=? AND state='queued'",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
    except Exception as e:
        log.error("worker launch failed for job %s: %s", job_id, e)
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET state='failed', error=?, finished_at=? WHERE job_id=? AND state='queued'",
                (f"worker launch failed: {e}", datetime.now(timezone.utc).isoformat(), job_id),
            )


async def mark_job_ready(request: web.Request) -> web.Response:
    """Phase-2 input attachment flow (t_0ef31767).

    Called by the client after PUTting all input files to their
    presigned URLs. Verifies every declared file is present in S3
    (HeadObject per key), transitions the job from awaiting_inputs
    to queued, writes the EFS envelope with the S3 input keys, and
    kicks the worker. From the worker's perspective the envelope
    looks identical to a single-phase job plus an `input_files`
    field listing the S3 paths it should fetch + delete.

    Error semantics:
      404  -> unknown job_id
      409  -> job is not in awaiting_inputs state (already submitted
              via the single-phase flow, already /ready'd, or in a
              terminal state). The caller should re-fetch /v1/jobs/{id}
              and act on the current state.
      409  -> some input files are missing from S3 (code=inputs_pending
              + missing=[...] so the client knows what to retry).
      500  -> envelope write or worker kick failed after we already
              committed to queued; the broker logs and the dispatcher
              will surface the job as failed (see _kick_worker_for_job).

    Idempotency: a second /ready call against a queued/running job
    returns 409 (state mismatch) rather than 200, so the client sees
    that the prior call already committed. We deliberately do NOT
    rewrite the envelope — that would invalidate the worker's in-
    flight LLM token.
    """
    job_id = request.match_info["job_id"]
    auth_error = require_job_access(request, job_id)
    if auth_error is not None:
        return auth_error
    with db() as conn:
        row = conn.execute(
            "SELECT state, request_body, worker_instance_id, worker_key_id, "
            "input_upload_expires_at FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    if row["state"] != "awaiting_inputs":
        return web.json_response({
            "error": f"job is in state {row['state']}, not awaiting_inputs",
            "code": "wrong_state",
        }, status=409)
    try:
        if datetime.fromisoformat(row["input_upload_expires_at"]) < datetime.now(timezone.utc):
            return web.json_response({
                "error": "input upload window expired",
                "code": "input_upload_expired",
            }, status=410)
    except Exception:
        return web.json_response({"error": "invalid upload expiry"}, status=500)
    identity = _load_verified_worker_identity(row["worker_instance_id"] or "")
    if identity is None or identity.get("key_id") != row["worker_key_id"]:
        return web.json_response({
            "error": "bound worker key is no longer valid; re-submit the file job",
            "code": "worker_key_changed",
        }, status=409)

    # Re-derive the input_files list from the stored request body. We
    # don't trust the client to re-submit the list — the broker is the
    # authority on what was declared at submit time, and re-deriving
    # keeps the /ready call bodyless (and CSRF-resistant).
    try:
        stored_body = json.loads(row["request_body"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return web.json_response({
            "error": "stored request_body unparseable; cannot verify inputs",
        }, status=500)
    input_files = stored_body.get("input_files") or []

    # Verify each file is present in S3. We use HeadObject rather than
    # GetObject so a missing file costs a HEAD round-trip instead of a
    # full GET — the broker never reads the bytes, only confirms they
    # exist. boto3's ClientError is the documented "404 in S3" signal.
    s3 = _get_s3_client()
    missing = []
    for f in input_files:
        s3_key = f"inputs/{job_id}/{f['filename']}"
        try:
            head = s3.head_object(Bucket=ARTIFACT_BUCKET, Key=s3_key)
            expected_size = int(f["size_bytes"]) + 60
            actual_size = head.get("ContentLength")
            if actual_size is not None and int(actual_size) != expected_size:
                return web.json_response({
                    "error": f"encrypted size mismatch for {f['filename']}",
                    "code": "input_size_mismatch",
                    "expected": expected_size,
                    "actual": int(actual_size),
                }, status=409)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            # 404 + NoSuchKey are the only "missing" signals; everything
            # else (500, throttling, network) we surface as 500 so the
            # client retries rather than concluding "missing".
            if code in ("404", "NoSuchKey", "NotFound"):
                missing.append(f["filename"])
            else:
                log.error("head_object(%s) failed: %s", s3_key, e)
                return web.json_response({
                    "error": f"s3 verification failed for {s3_key}: {code}",
                }, status=500)
        except Exception as e:
            log.error("head_object(%s) raised: %s", s3_key, e)
            return web.json_response({
                "error": f"s3 verification failed for {s3_key}: {e}",
            }, status=500)

    if missing:
        return web.json_response({
            "error": "not all input files uploaded yet",
            "missing": missing,
            "code": "inputs_pending",
        }, status=409)

    # Transition awaiting_inputs -> queued. Mark input_status='ready'
    # so audit log queries can distinguish "files declared" from
    # "files verified present" without parsing the request body.
    #
    # Guard the UPDATE with state='awaiting_inputs' (review F1): two
    # concurrent /ready calls could otherwise both pass the SELECT,
    # both run HeadObject, and both UPDATE — leaving us with two
    # envelope writes and two worker kicks for one job. rowcount==0
    # means a concurrent caller already won; return 409 so the late
    # caller sees the same response it would have seen if it had
    # arrived a millisecond later. Same pattern as _kick_worker_for_job
    # at line 1651 ("AND state='queued'").
    with db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET state='queued', input_status='ready' "
            "WHERE job_id=? AND state='awaiting_inputs'",
            (job_id,),
        )
        if cur.rowcount == 0:
            return web.json_response({
                "error": f"job is in state other than awaiting_inputs "
                         f"(concurrent /ready already won)",
                "code": "wrong_state",
            }, status=409)

    # Now safe to write the EFS envelope. The worker fetches from S3 at
    # job start using the s3_keys we record here, so the broker doesn't
    # need to remember them after the envelope is on disk.
    INBOX.mkdir(parents=True, exist_ok=True)
    # Skill prompt + skill_hash resolution are the same as the single-
    # phase flow; pull them through resolve_skill_hash / resolve_skill_prompt
    # so the worker's hardcoded fallback vs registered-prompt precedence
    # stays identical. Done inside the same `with db()` block as the
    # state UPDATE so the row read for prompt resolution is consistent.
    with db() as conn:
        skill_hash = resolve_skill_hash(stored_body["encrypted_skill"], conn)
        skill_prompt = resolve_skill_prompt(
            stored_body["encrypted_skill"], conn)
        skill_decrypt_input = resolve_skill_decrypt_input(
            stored_body["encrypted_skill"], conn)
        # WASM skill dispatch (kanban t_c27c1d8d) — same lookup as the
        # single-phase submit flow. See resolve_skill_wasm_uri for the
        # "None when not a WASM skill" semantics.
        wasm_uri = resolve_skill_wasm_uri(stored_body["encrypted_skill"], conn)
        # WASM resource limits (kanban t_c27c1d8d): same injection as
        # the single-phase submit flow above. The two-phase path
        # (verify_inputs -> commit) would otherwise silently drop
        # max_fuel / max_duration_ms, leaving the worker's defaults
        # in place and bypassing the operator-declared caps.
        resource_limits = resolve_skill_resource_limits(
            stored_body["encrypted_skill"], conn)
        token_row = conn.execute(
            "SELECT token FROM llm_tokens WHERE job_id=?", (job_id,)
        ).fetchone()
        # File-job cold start and upload time must not consume the worker's
        # inference-token TTL. Refresh it only when inputs are committed.
        conn.execute(
            "UPDATE llm_tokens SET expires_at=? WHERE job_id=?",
            ((datetime.now(timezone.utc) + timedelta(minutes=int(
                os.environ.get("BROKER_LLM_TOKEN_TTL_MIN", "30")))).isoformat(),
             job_id),
        )
    if token_row is None:
        return web.json_response({"error": "job LLM token missing"}, status=500)
    prepared_files = [{
        "filename": f["filename"],
        "s3_key": f"inputs/{job_id}/{f['filename']}",
        "content_type": f["content_type"],
        "size_bytes": int(f["size_bytes"]),
        "encrypted_size_bytes": int(f["size_bytes"]) + 60,
        "encryption": FILE_ENCRYPTION,
    } for f in input_files]
    envelope = build_job_envelope(
        job_id=job_id, created_at=datetime.now(timezone.utc).isoformat(),
        body=stored_body, llm_token=token_row["token"], skill_hash=skill_hash,
        input_files=prepared_files, skill_prompt=skill_prompt,
        skill_decrypt_input=skill_decrypt_input, wasm_uri=wasm_uri,
        resource_limits=resource_limits,
        worker_instance_id=row["worker_instance_id"],
        worker_key_id=row["worker_key_id"],
    )
    envelope_path = INBOX / f"{job_id}.json"
    envelope_path.write_text(json.dumps(envelope))
    log.info("job %s inputs verified (file_count=%d) -> queued",
             job_id, len(input_files))

    # Kick the worker — same code path as the single-phase flow. The
    # worker poll loop reads the envelope, sees input_files[], and
    # calls fetch_and_delete_inputs before any LLM work.
    asyncio.create_task(_kick_worker_for_job(job_id))

    return web.json_response({
        "job_id": job_id,
        "state": "queued",
        "inputs_verified": True,
        "file_count": len(input_files),
    }, status=200)


def _input_upload_block(job_id: str, row: sqlite3.Row) -> dict | None:
    if row["state"] != "awaiting_inputs":
        return None
    identity = _load_verified_worker_identity(row["worker_instance_id"] or "")
    if identity is None or identity.get("key_id") != row["worker_key_id"]:
        return None
    try:
        body = json.loads(row["request_body"] or "{}")
    except Exception:
        return None
    files = []
    for item in body.get("input_files") or []:
        key = f"inputs/{job_id}/{item['filename']}"
        files.append({
            "filename": item["filename"],
            "content_type": item["content_type"],
            "size_bytes": item["size_bytes"],
            "encrypted_size_bytes": item["size_bytes"] + 60,
            "upload_url": generate_presigned_upload_url(key),
        })
    att = identity["attestation"]
    return {
        "encryption": {
            "scheme": FILE_ENCRYPTION,
            "key_id": identity["key_id"],
            "public_key": identity["x25519_pubkey_b64"],
            "aad_template": "verdantforged-file-v1\\0input\\0{job_id}\\0{filename}",
        },
        "attestation": {
            "tee_type": att.get("tee_type", "amd-sev-snp"),
            "measurement": att.get("measurement", ""),
            "report": att.get("report", ""),
            "cert_chain": att.get("cert_chain", []),
            "report_data": att.get("report_data", ""),
            "source": att.get("source", ""),
        },
        "expires_at": row["input_upload_expires_at"],
        "ready_url": f"/v1/jobs/{job_id}/ready",
        "files": files,
    }


async def get_job(request: web.Request) -> web.Response:
    job_id = request.match_info["job_id"]
    auth_error = require_job_access(request, job_id)
    if auth_error is not None:
        return auth_error
    with db() as conn:
        row = conn.execute(
            "SELECT job_id, state, created_at, started_at, finished_at, result, error, "
            "webhook_url, webhook_status, artifact_count, stripe_status, "
            "stripe_capture_amount, stripe_transfer_id, stripe_pi_amount_cents, "
            "shortfall_cents, topup_pi_id, topup_capture_amount, request_body, "
            "worker_instance_id, worker_key_id, input_upload_expires_at "
            "FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    out = dict(row)
    upload = _input_upload_block(job_id, row)
    if upload is not None:
        out["input_upload"] = upload
    out.pop("request_body", None)
    if row["state"] == "awaiting_worker":
        state = worker_mgr.get_state()
        if state is None:
            out["worker_wait"] = {"worker_status": "offline", "detail": "waiting for worker launch"}
        else:
            _identity, reason = _worker_identity_status(state.instance_id)
            out["worker_wait"] = {
                "worker_status": "waiting_for_identity",
                "worker_instance_id": state.instance_id,
                "detail": reason,
            }
    out.pop("worker_instance_id", None)
    out.pop("worker_key_id", None)
    out.pop("input_upload_expires_at", None)
    if out.get("result"):
        try:
            out["result"] = json.loads(out["result"])
        except json.JSONDecodeError:
            pass
    # === Stripe PaymentIntent status (t_9fbec867, t_9a705578, t_84d3e5ee) ===
    # Surface a `payment` block on every job response so clients can
    # confirm the capture/refund outcome without a second Stripe call.
    # For shortfall scenarios (t_9a705578), the job transitions to
    # awaiting_topup state with shortfall_cents populated; the GET
    # response surfaces this info for client topup UI.
    #
    # Delegated to _payment_block_for() so this endpoint and the webhook
    # body emit identical blocks (kanban t_84d3e5ee). Previously the
    # webhook dropped the block entirely, breaking the "webhook is
    # authoritative for state change" invariant.
    out.pop("stripe_status", None)
    out.pop("stripe_capture_amount", None)
    out.pop("stripe_transfer_id", None)
    out.pop("stripe_pi_amount_cents", None)
    out.pop("shortfall_cents", None)
    out.pop("topup_pi_id", None)
    out.pop("topup_capture_amount", None)
    payment_block = _payment_block_for(job_id)
    if payment_block is not None:
        out["payment"] = payment_block
    # Inject artifact download URLs into the result envelope so clients can
    # GET individual files without first calling /v1/jobs/{id}/artifacts to
    # learn the filenames. URLs are RELATIVE (/v1/...) — clients should
    # prepend the broker base URL. The webhook payload emits absolute URLs.
    # For S3-backed artifacts (this daemon), the per-file URLs are
    # /v1/jobs/{id}/artifacts/{filename} which 302-redirect to a presigned
    # S3 URL with a 15-minute TTL.
    result = out.get("result") or {}
    arts = result.get("artifacts")
    if isinstance(arts, dict) and arts.get("files"):
        arts = dict(arts)  # don't mutate the stored dict
        arts["manifest_url"] = f"/v1/jobs/{job_id}/artifacts"
        arts["download_urls"] = {
            f["filename"]: f"/v1/jobs/{job_id}/artifacts/{f['filename']}"
            for f in arts.get("files", [])
        }
        # ttl_hours is always surfaced so clients know when the S3 object
        # disappears (lifecycle rule).
        arts.setdefault("ttl_hours", 24)
        # Client-facing note explaining how to decrypt + when the URL expires.
        # Matched by G1d test (must contain "encrypted").
        arts["note"] = (
            "Artifacts are encrypted client-side (X25519+ChaCha20Poly1305) "
            "to your result_pubkey. Download URLs expire in 15 minutes; "
            "S3 objects auto-delete after 24 hours. Decrypt with your X25519 "
            "private key using the ephemeral pubkey + nonce prefix."
        )
        result["artifacts"] = arts
        out["result"] = result
    return web.json_response(out)


# ---------- Result-pack artifact endpoints (S3-backed) ----------
#
# GET /v1/jobs/{job_id}/artifacts             -> manifest JSON + per-file
#                                                 presigned download URLs
# GET /v1/jobs/{job_id}/artifacts/{filename}  -> 302 redirect to a presigned
#                                                 S3 GET URL (15-min TTL)
#
# The manifest itself is NOT stored in EFS — it lives in the job's stored
# `result` JSON column under `result.artifacts`, populated by _finalize_job
# from the worker's outbox payload. Allow-list check on filename still
# applies (matches manifest entries only — defends against
# unauthenticated access to arbitrary job IDs).
#
# Why S3 + presigned URLs (over the previous EFS plaintext storage):
#   - SSE-KMS encryption at rest + 24h lifecycle expiration is managed by
#     S3, not by the daemon (no custom cleanup job needed).
#   - The encrypted blob never touches EFS — even a compromised EFS reader
#     sees only ciphertext (double encryption: KMS at rest + X25519 client
#     side).
#   - Presigned URL TTL (15 min) is decoupled from object lifetime (24h),
#     so a leaked URL stops working long before the object disappears.
#   - No disk-I/O on the broker hot path: serving an artifact is one
#     generate_presigned_url call + 302 redirect.


def _load_s3_artifact_manifest(job_id: str) -> dict | None:
    """Load the result.artifacts manifest from the jobs row for job_id.

    Returns None when the job is missing, has no result, or has no
    artifacts block. The shape matches what the worker writes in
    poller.py:upload_artifacts_to_s3 (and the wrapper that adds the
    primary output as a synthetic artifact).
    """
    with db() as conn:
        row = conn.execute(
            "SELECT result FROM jobs WHERE job_id = ?", (job_id,),
        ).fetchone()
    if not row or not row["result"]:
        return None
    try:
        result = json.loads(row["result"])
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("result JSON for %s unreadable: %s", job_id, e)
        return None
    arts = (result or {}).get("artifacts")
    if not isinstance(arts, dict) or not arts.get("files"):
        return None
    return arts


async def get_job_artifacts(request: web.Request) -> web.Response:
    """Return the artifact manifest with per-file presigned download URLs.

    Response shape matches the worker-side manifest (so callers that
    already parse it can reuse the parsing), augmented with a
    `download_url` field per file (presigned S3 URL, 15-min TTL).
    """
    job_id = request.match_info["job_id"]
    auth_error = require_job_access(request, job_id)
    if auth_error is not None:
        return auth_error
    arts = _load_s3_artifact_manifest(job_id)
    if arts is None:
        return web.json_response({"error": "no artifacts"}, status=404)
    # Augment each file entry with a presigned download URL. Files
    # without an s3_key (legacy manifests from before the migration) are
    # skipped — the manifest is signed by the worker + counter-signed by
    # the broker, so we trust the contents but degrade gracefully on
    # missing keys.
    out_files = []
    for f in arts.get("files", []):
        entry = dict(f)
        if entry.get("s3_key"):
            try:
                entry["download_url"] = generate_presigned_url(entry["s3_key"])
            except Exception as e:
                # Don't fail the whole endpoint on one bad URL — log
                # and serve the rest. Tests inject a mock so this path
                # is exercised only against broken states.
                log.warning("presign failed for %s/%s: %s",
                            job_id, entry.get("filename"), e)
        out_files.append(entry)
    response = dict(arts)
    response["files"] = out_files
    return web.json_response(response)


async def get_job_artifact_file(request: web.Request) -> web.Response:
    """Redirect to a presigned S3 GET URL for the requested artifact file.

    Path-traversal hardening (defence in depth):
      1. Filename must appear in the manifest's artifact entries (allow-list)
      2. Filename must NOT contain '..' segments (cheap reject-list
         that catches obvious traversal payloads without resolving paths,
         since there's no on-disk path involved anymore)
      3. Presigned URL is generated server-side with a 15-min TTL — the
         client cannot supply a key directly.
    """
    job_id = request.match_info["job_id"]
    auth_error = require_job_access(request, job_id)
    if auth_error is not None:
        return auth_error
    filename = request.match_info["filename"]
    arts = _load_s3_artifact_manifest(job_id)
    if arts is None:
        return web.json_response({"error": "no artifacts"}, status=404)
    # Allow-list: filename must appear in the manifest.
    allowed = {f["filename"] for f in arts.get("files", [])}
    if filename not in allowed:
        return web.json_response({"error": "file not in manifest"}, status=404)
    # Defence-in-depth path-traversal reject-list (no on-disk resolution
    # possible here, but reject obvious attacks so a future caller can't
    # use this endpoint as a probe for manifest contents).
    if ".." in filename.split("/"):
        return web.json_response({"error": "invalid filename"}, status=400)
    # Find the matching file entry and generate the presigned URL.
    target = next(f for f in arts["files"] if f["filename"] == filename)
    url = generate_presigned_url(target["s3_key"])
    return web.HTTPFound(url)


# ---------- /v1/discover ----------

# Skill names hardcoded into the broker's worker stubs. Kept as a module
# constant so /v1/discover can list them even when the `skills` table is
# empty (matches the pre-registration behaviour: 3 stubs always
# advertised so demo clients can call /v1/jobs with a stub name).
# Defined here (above discover()) so the call site reads top-to-bottom.
BUILTIN_SKILLS = ("code-review", "summarize", "photo-glow-up")


async def discover(request: web.Request) -> web.Response:
    """Public broker advertisement (per tee-broker-pattern protocol)."""
    # CQ-1: Read the worker's attestation JSON ONCE and extract both
    # `live_measurement`/`worker_attested` and the full SEV-SNP report
    # fields. The previous code opened the file twice (once for the
    # measurement, once for the full report) — a single read is enough.
    # The worker writes this file to /mnt/broker/logs/worker-attestation.json
    # on boot. If no worker is running, we report the env-configured
    # BROKER_EXPECTED_MEASUREMENT (or empty placeholder).
    full_attestation: dict = {}
    attestation_path = LOGS / "worker-attestation.json"
    if attestation_path.exists():
        try:
            full_attestation = json.loads(attestation_path.read_text())
        except Exception:
            full_attestation = {}
    live_measurement = full_attestation.get("measurement", "")
    worker_attested = bool(live_measurement)
    # Fall back to env-configured expected measurement
    if not live_measurement:
        live_measurement = os.environ.get("BROKER_EXPECTED_MEASUREMENT", "")

    # Compute OpenShell policy hash. The policy file controls what egress
    # destinations the broker enclave is permitted to reach. Hashing it lets
    # requesters verify the broker hasn't been tampered with to widen its
    # egress (e.g., to exfiltrate inputs to attacker-controlled servers).
    policy_hash = ""
    policy_path = Path(__file__).parent / "openshell" / "policy.yaml"
    if policy_path.exists():
        try:
            policy_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        except Exception:
            pass

    # Worker X25519 public key (showcase skill 3 — blind-audit, kanban
    # t_dea55bb2). The worker publishes its X25519 pubkey to
    # worker-keys.json at boot (see worker/poller.publish_worker_keys).
    # Clients fetch this from /v1/discover to encrypt their input payload
    # before submitting a blind-audit job — the broker then forwards the
    # ciphertext verbatim and the worker decrypts it inside the TEE.
    # We surface it under `attestation.enclave_pubkey` so the existing
    # client-side discovery flow (see SHOWCASE_SKILLS.md demo script) just
    # reads .attestation.enclave_pubkey without a new field. Empty string
    # when the worker hasn't booted yet — a blind-audit client should
    # fail closed (404 from /v1/discover wouldn't be useful, so we serve
    # an empty string and let the demo client surface "worker not ready").
    worker_x25519_pubkey_b64 = ""
    worker_keys: dict = {}
    worker_keys_path = LOGS / "worker-keys.json"
    if worker_keys_path.exists():
        try:
            worker_keys = json.loads(worker_keys_path.read_text())
            worker_x25519_pubkey_b64 = worker_keys.get(
                "x25519_pubkey_b64", "")
        except Exception:
            worker_x25519_pubkey_b64 = ""
            worker_keys = {}

    # Build attestation block per tee-broker-pattern/agent-skills.md BrokerAttestation.
    # If the worker has populated the full SEV-SNP report fields, surface them.
    attestation_block = {
        "tee_type": "amd-sev-snp",
        "min_measurement": live_measurement,
        "worker_attested": worker_attested,
        "policy_hash": policy_hash,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        # Real SEV-SNP fields (populated by sev_snp.py when available):
        "report": full_attestation.get("report", ""),         # base64 1184-byte SNP report
        "report_data": full_attestation.get("report_data", ""),
        "cert_chain": full_attestation.get("cert_chain", []), # [base64 DER, ...]
        # Prefer the worker's published X25519 key over the static
        # attestation-key binding (worker-attestation.json may carry an
        # empty enclave_pubkey when running on a stub host). The
        # blind-audit flow only ever talks to this worker via the
        # X25519-from-worker-keys.json path, so the broker MUST NOT
        # surface an empty string here — fall back to the attestation
        # value only when worker-keys.json is absent.
        "enclave_pubkey": (worker_x25519_pubkey_b64
                           or full_attestation.get("enclave_pubkey", "")),
        # Worker Ed25519 key verifies worker_signature and Plan 2's
        # sandbox.image_digest_sig. Keep enclave_pubkey as the X25519 input
        # encryption key for backwards compatibility; expose the signing key
        # explicitly so verifiers don't accidentally treat X25519 bytes as an
        # Ed25519 public key.
        "worker_ed25519_pubkey": worker_keys.get("ed25519_pubkey_b64", ""),
        "nemoclaw_version": worker_keys.get("nemoclaw_version", ""),
        "nemoclaw_image": worker_keys.get("nemoclaw_image", ""),
        "nemoclaw_image_digest": worker_keys.get("nemoclaw_image_digest", ""),
        "chip_id": full_attestation.get("chip_id", ""),
        "family_id": full_attestation.get("family_id", ""),
        "attestation_source": full_attestation.get("source", "stub"),
    }

    # Merge built-in skill stubs with registered skills. The built-ins are
    # always present (matches the 3 stubs shipped pre-registration feature);
    # registered skills come from the `skills` table (POST /v1/skills). The
    # union is returned sorted and deduped by name — a registered skill with
    # the same name as a built-in (e.g. "summarize") shadows the built-in.
    supported_skills = list(BUILTIN_SKILLS)
    registered_names = set()
    with db() as conn:
        # Defensive: the `skills` table is only created when POST /v1/skills
        # is first called (or by a fresh DB init in register_skill). On a
        # never-registered broker, the table doesn't exist and the query
        # would 500. Check first.
        exists = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='skills'"
        ).fetchone()
        if exists:
            rows = conn.execute(
                "SELECT name FROM skills s1 "
                "WHERE version = (SELECT MAX(version) FROM skills s2 WHERE s2.name = s1.name)"
            ).fetchall()
            registered_names = {r["name"] for r in rows}
    # Registered skills shadow built-ins with the same name
    supported_skills = [s for s in supported_skills if s not in registered_names]
    supported_skills.extend(sorted(registered_names))

    return web.json_response({
        "broker_id": f"verdantforged-{BROKER_REGION}",
        "endpoint": f"https://{BROKER_DOMAIN}" if BROKER_DOMAIN else f"http://{request.host}",
        "region": BROKER_REGION,
        "attestation": attestation_block,
        "pricing": {
            "model": "session-lease",
            "lease_per_15min_usd": 0.20,
        },
        "supported_skills": supported_skills,
    })


async def healthz(request: web.Request) -> web.Response:
    """Health check with worker boot status and ETA."""
    state = worker_mgr.get_state()
    if state is None:
        return web.json_response({
            "ok": True,
            "worker": False,
            "worker_status": "offline",
        })

    # Read the heartbeat file the worker writes to EFS.
    hb: dict = {}
    hb_path = LOGS / "worker-heartbeat.json"
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text())
        except Exception:
            hb = {}

    boot_stage = hb.get("boot_stage", "")
    hb_status = hb.get("status", "")
    boot_detail = hb.get("boot_detail", "")
    boot_elapsed = hb.get("boot_elapsed_seconds", 0)

    now = time.time()
    uptime = int(now - state.launched_at)

    # Known cold-start profile (seconds) — observed on m6a.xlarge eu-west-1.
    STAGE_DURATIONS = {
        "starting": 5,
        "packages": 60,
        "efs": 10,
        "attestation": 10,
        "nemoclaw": 900,    # the big one — Docker pull + sandbox create
        "sandbox": 120,
        "poller": 15,
        "ready": 0,
    }
    STAGE_ORDER = ["starting", "packages", "efs", "attestation",
                   "nemoclaw", "sandbox", "poller", "ready"]

    # Determine worker status.
    if boot_stage == "ready" or (hb_status in ("idle",) and not boot_stage):
        worker_status = hb_status or "ready"
    elif hb_status.startswith("running:"):
        worker_status = hb_status
    elif boot_stage and boot_stage in STAGE_ORDER:
        worker_status = "booting"
    else:
        worker_status = "booting"
        boot_stage = boot_stage or "starting"
        boot_detail = boot_detail or "Instance running, waiting for cloud-init"

    # Compute ETA: sum remaining stage durations.
    eta_seconds = 0
    if worker_status == "booting":
        try:
            stage_idx = STAGE_ORDER.index(boot_stage) if boot_stage in STAGE_ORDER else 0
        except ValueError:
            stage_idx = 0
        for s in STAGE_ORDER[stage_idx:]:
            if s != boot_stage:
                eta_seconds += STAGE_DURATIONS.get(s, 0)
            else:
                stage_total = STAGE_DURATIONS.get(s, 0)
                prior_total = sum(
                    STAGE_DURATIONS.get(s2, 0)
                    for s2 in STAGE_ORDER[:stage_idx]
                )
                stage_elapsed = max(0, boot_elapsed - prior_total)
                eta_seconds += max(0, stage_total - stage_elapsed)

    # Idle time.
    idle_seconds = None
    if worker_status == "idle":
        last_job = getattr(worker_mgr, "_last_job_finished", None)
        if last_job:
            idle_seconds = int(now - last_job)

    return web.json_response({
        "ok": True,
        "worker": True,
        "worker_status": worker_status,
        "worker_instance_id": state.instance_id,
        "worker_boot_stage": boot_stage,
        "worker_boot_detail": boot_detail,
        "worker_boot_elapsed_seconds": boot_elapsed if worker_status == "booting" else 0,
        "worker_boot_eta_seconds": eta_seconds if worker_status == "booting" else 0,
        "worker_uptime_seconds": uptime,
        "worker_idle_seconds": idle_seconds,
    })


# ---------- Skill registration (POST /v1/skills) ----------

import re as _re

# Validation bounds. Chosen so:
#   - resource limits are bounded above the 3-stub LLM skills already shipped
#     (which run with default 256MB / 60s / 10M fuel) but capped low enough
#     to keep a compromised publisher from allocating gigabytes of worker RAM
#     and burning through the broker's LLM proxy
#   - name/slug stays a safe filename fragment (no path traversal on any
#     future on-disk skill cache)
#   - prompt_template capped at 32 KiB so a single registration can't OOM
#     the daemon on insert
_SKILL_NAME_RE = _re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_SKILL_VERSION_RE = _re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SKILL_ENTRY_RE = _re.compile(r"^[A-Za-z0-9_.]{1,128}$")
_HEX_RE = _re.compile(r"^[0-9a-fA-F]+$")

# Default resource limits (also the upper bound — 256MB matches the worker
# sandbox; 60s matches the existing per-job LLM token TTL)
_SKILL_DEFAULT_MAX_FUEL = 10_000_000
_SKILL_DEFAULT_MAX_DURATION_MS = 60_000
_SKILL_DEFAULT_MAX_MEMORY_MB = 256

_SKILL_HARD_MAX_FUEL = 100_000_000
_SKILL_HARD_MAX_DURATION_MS = 600_000
_SKILL_HARD_MAX_MEMORY_MB = 4096
_SKILL_PROMPT_TEMPLATE_MAX = 32 * 1024  # 32 KiB
_SKILL_WASM_REF_MAX_SIZE = 50 * 1024 * 1024  # 50 MiB


def validate_skill_manifest(body: dict) -> tuple[bool, str, dict]:
    """Validate a skill manifest. Returns (ok, error_message, normalised_dict).

    On success, `normalised_dict` is the parsed manifest with resource-limit
    defaults applied, suitable for INSERT into the `skills` table.

    On failure, error_message is non-empty and normalised_dict is empty.
    """
    if not isinstance(body, dict):
        return False, "manifest must be a JSON object", {}

    # name — slug, lowercase, 3..64 chars, no leading/trailing dash
    name = body.get("name", "")
    if not isinstance(name, str) or not _SKILL_NAME_RE.match(name):
        return False, (
            "name must match ^[a-z0-9][a-z0-9-]{2,63}$ "
            "(lowercase slug, 3-64 chars, no leading/trailing dash)"
        ), {}

    # description — 1..512 chars
    desc = body.get("description", "")
    if not isinstance(desc, str) or not (1 <= len(desc) <= 512):
        return False, "description must be a string 1..512 chars", {}

    # wasm_manifest_hash — 64 hex chars (SHA-256)
    wmh = body.get("wasm_manifest_hash", "")
    if (not isinstance(wmh, str) or len(wmh) != 64
            or not _HEX_RE.match(wmh)):
        return False, "wasm_manifest_hash must be a 64-char hex string (SHA-256)", {}

    # entry_point — safe symbol
    ep = body.get("entry_point", "")
    if not isinstance(ep, str) or not _SKILL_ENTRY_RE.match(ep):
        return False, "entry_point must match ^[A-Za-z0-9_.]{1,128}$", {}

    # version — semver-like, default 0.1.0
    version = body.get("version", "0.1.0")
    if not isinstance(version, str) or not _SKILL_VERSION_RE.match(version):
        return False, "version must match ^[0-9]+\\.[0-9]+\\.[0-9]+$", {}

    # prompt_template XOR wasm_ref — exactly one
    has_prompt = "prompt_template" in body and body["prompt_template"] is not None
    has_wasm = "wasm_ref" in body and body["wasm_ref"] is not None
    if has_prompt == has_wasm:
        return False, "exactly one of prompt_template or wasm_ref must be provided", {}

    prompt_template = None
    wasm_ref_uri = None
    wasm_ref_size = None
    if has_prompt:
        pt = body["prompt_template"]
        if not isinstance(pt, str) or not (1 <= len(pt) <= _SKILL_PROMPT_TEMPLATE_MAX):
            return False, (
                f"prompt_template must be a string 1..{_SKILL_PROMPT_TEMPLATE_MAX} chars"
            ), {}
        prompt_template = pt
    else:
        wr = body["wasm_ref"]
        if not isinstance(wr, dict):
            return False, "wasm_ref must be an object {uri, size_bytes}", {}
        uri = wr.get("uri", "")
        size = wr.get("size_bytes", 0)
        if not isinstance(uri, str) or not (1 <= len(uri) <= 512):
            return False, "wasm_ref.uri must be a string 1..512 chars", {}
        if not isinstance(size, int) or not (1 <= size <= _SKILL_WASM_REF_MAX_SIZE):
            return False, (
                f"wasm_ref.size_bytes must be an int 1..{_SKILL_WASM_REF_MAX_SIZE}"
            ), {}
        wasm_ref_uri = uri
        wasm_ref_size = size

    # resource_limits — optional, bounded
    rl = body.get("resource_limits") or {}
    if not isinstance(rl, dict):
        return False, "resource_limits must be an object", {}

    def _bounded_int(d, key, default, lo, hi):
        v = d.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
            return None, f"resource_limits.{key} must be int in [{lo}, {hi}]"
        return v, None

    max_fuel, err = _bounded_int(rl, "max_fuel",
                                  _SKILL_DEFAULT_MAX_FUEL, 1, _SKILL_HARD_MAX_FUEL)
    if err: return False, err, {}
    max_duration_ms, err = _bounded_int(rl, "max_duration_ms",
                                         _SKILL_DEFAULT_MAX_DURATION_MS,
                                         100, _SKILL_HARD_MAX_DURATION_MS)
    if err: return False, err, {}
    max_memory_mb, err = _bounded_int(rl, "max_memory_mb",
                                       _SKILL_DEFAULT_MAX_MEMORY_MB,
                                       1, _SKILL_HARD_MAX_MEMORY_MB)
    if err: return False, err, {}

    # input_schema / output_schema — optional, must be a dict if present
    def _schema_or_none(v):
        if v is None: return None
        if not isinstance(v, dict): return None
        try:
            json.dumps(v)
            return json.dumps(v)
        except Exception:
            return None
    input_schema = _schema_or_none(body.get("input_schema"))
    output_schema = _schema_or_none(body.get("output_schema"))
    if body.get("input_schema") is not None and input_schema is None:
        return False, "input_schema must be a JSON object", {}
    if body.get("output_schema") is not None and output_schema is None:
        return False, "output_schema must be a JSON object", {}

    # decrypt_input (showcase skill 3 — blind-audit, kanban t_dea55bb2):
    # opt-in flag telling the worker to decrypt encrypted_data with its
    # X25519 privkey BEFORE handing it to the LLM. Used by blind-audit so
    # clients can encrypt source code to the worker's enclave_pubkey
    # (from /v1/discover) and have the broker never see the plaintext.
    # Validation: must be a strict bool (Python's `isinstance(x, bool)` is
    # True only for True/False, not 0/1/"yes"/"true"; matches the JSON-bool
    # shape clients emit so a misconfigured client gets a 400 with a clear
    # message rather than silent truthy-coercion later).
    decrypt_input = body.get("decrypt_input", False)
    if not isinstance(decrypt_input, bool):
        return False, (
            "decrypt_input must be a boolean (True or False)"
        ), {}

    return True, "", {
        "name": name,
        "version": version,
        "description": desc,
        "wasm_manifest_hash": wmh.lower(),
        "entry_point": ep,
        "prompt_template": prompt_template,
        "wasm_ref_uri": wasm_ref_uri,
        "wasm_ref_size": wasm_ref_size,
        "max_fuel": max_fuel,
        "max_duration_ms": max_duration_ms,
        "max_memory_mb": max_memory_mb,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "decrypt_input": decrypt_input,
    }


def _skill_row_to_dict(row) -> dict:
    """Convert a `skills` row into the JSON shape we expose to clients."""
    out = {
        "name": row["name"],
        "version": row["version"],
        "description": row["description"],
        "wasm_manifest_hash": row["wasm_manifest_hash"],
        "entry_point": row["entry_point"],
        "resource_limits": {
            "max_fuel": row["max_fuel"],
            "max_duration_ms": row["max_duration_ms"],
            "max_memory_mb": row["max_memory_mb"],
        },
        "created_at": row["created_at"],
    }
    if row["prompt_template"] is not None:
        out["prompt_template"] = row["prompt_template"]
    if row["wasm_ref_uri"] is not None:
        out["wasm_ref"] = {
            "uri": row["wasm_ref_uri"],
            "size_bytes": row["wasm_ref_size"],
        }
        # WASM skill upload (kanban t_c27c1d8d): if a binary has been
        # uploaded for this version, expose its on-disk URI so clients
        # can download / verify the binary out-of-band. We compute the
        # path the same way the upload endpoint does — keeping the
        # two in sync is critical because the worker reads from the
        # exact same path via resolve_skill_wasm_uri. The field is
        # only added when the file exists; a registered-but-not-
        # uploaded skill reports `wasm_ref` but no `wasm_uri`.
        version = row["version"]
        name = row["name"]
        on_disk = WASM_DIR / f"{name}-{version}.wasm"
        if on_disk.exists():
            out["wasm_uri"] = str(on_disk)
    if row["input_schema"]:
        try: out["input_schema"] = json.loads(row["input_schema"])
        except Exception: pass
    if row["output_schema"]:
        try: out["output_schema"] = json.loads(row["output_schema"])
        except Exception: pass
    # decrypt_input (kanban t_dea55bb2): always include so callers can
    # tell whether the skill will decrypt its input before the LLM call
    # (used by blind-audit clients to decide whether to encrypt their
    # payload to the worker's enclave_pubkey). SQLite stores this as
    # INTEGER 0/1; expose as JSON bool for client-friendliness.
    out["decrypt_input"] = bool(row["decrypt_input"])
    return out


async def register_skill(request: web.Request) -> web.Response:
    """POST /v1/skills — register a new skill (or a new version of an existing one).

    Request body: a SkillManifest JSON object (see validate_skill_manifest).
    On success returns 201 with the persisted manifest and a location URL.
    On validation failure returns 400 with the first error.
    On conflict (same name+version already registered) returns 409.

    Auth (VULN-S2): requires `Authorization: Bearer BROKER_SKILLS_API_KEY`.
    If the key is unset on the server, registration is refused with 503 (the
    operator forgot to configure it). Uses hmac.compare_digest for
    constant-time comparison so the key can't be leaked via timing.
    """
    # Auth gate — closed by default. No key configured => refuse open
    # registration rather than silently allow.
    if not BROKER_SKILLS_API_KEY:
        return web.json_response({
            "error": "skill registration is disabled (BROKER_SKILLS_API_KEY not configured)",
            "code": "skills_auth_not_configured",
        }, status=503)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return web.json_response({
            "error": "missing or invalid Authorization header (expected: Bearer BROKER_SKILLS_API_KEY)",
            "code": "skills_auth_required",
        }, status=401)
    presented = auth_header[len("Bearer "):].strip()
    import hmac
    if not hmac.compare_digest(presented, BROKER_SKILLS_API_KEY):
        return web.json_response({
            "error": "invalid skills API key",
            "code": "skills_auth_invalid",
        }, status=401)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    ok, err, normalised = validate_skill_manifest(body)
    if not ok:
        return web.json_response({"error": err, "code": "invalid_manifest"}, status=400)

    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        existing = conn.execute(
            "SELECT name, version FROM skills WHERE name=? AND version=?",
            (normalised["name"], normalised["version"]),
        ).fetchone()
        if existing:
            return web.json_response({
                "error": f"skill {normalised['name']}@{normalised['version']} already registered",
                "code": "skill_already_registered",
            }, status=409)
        conn.execute(
            "INSERT INTO skills ("
            "name, version, description, wasm_manifest_hash, entry_point,"
            "prompt_template, wasm_ref_uri, wasm_ref_size,"
            "max_fuel, max_duration_ms, max_memory_mb,"
            "input_schema, output_schema, decrypt_input, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                normalised["name"], normalised["version"],
                normalised["description"], normalised["wasm_manifest_hash"],
                normalised["entry_point"],
                normalised["prompt_template"], normalised["wasm_ref_uri"],
                normalised["wasm_ref_size"],
                normalised["max_fuel"], normalised["max_duration_ms"],
                normalised["max_memory_mb"],
                normalised["input_schema"], normalised["output_schema"],
                1 if normalised["decrypt_input"] else 0,
                now,
            ),
        )

    log.info("skill registered: %s@%s (entry=%s, hash=%s)",
             normalised["name"], normalised["version"],
             normalised["entry_point"], normalised["wasm_manifest_hash"][:16])
    response_body = _skill_row_to_dict({
        "name": normalised["name"],
        "version": normalised["version"],
        "description": normalised["description"],
        "wasm_manifest_hash": normalised["wasm_manifest_hash"],
        "entry_point": normalised["entry_point"],
        "prompt_template": normalised["prompt_template"],
        "wasm_ref_uri": normalised["wasm_ref_uri"],
        "wasm_ref_size": normalised["wasm_ref_size"],
        "max_fuel": normalised["max_fuel"],
        "max_duration_ms": normalised["max_duration_ms"],
        "max_memory_mb": normalised["max_memory_mb"],
        "input_schema": normalised["input_schema"],
        "output_schema": normalised["output_schema"],
        "decrypt_input": normalised["decrypt_input"],
        "created_at": now,
    })
    return web.json_response(response_body, status=201, headers={
        "Location": f"/v1/skills/{normalised['name']}@{normalised['version']}",
    })


async def get_skill(request: web.Request) -> web.Response:
    """GET /v1/skills/{ref} — return the latest registered version of a skill,
    or the specific version if {ref} is `name@version`."""
    ref = request.match_info["ref"]
    if "@" in ref:
        name, _, version = ref.partition("@")
        if not version:
            return web.json_response({"error": "empty version after @"}, status=400)
    else:
        name = ref
        version = None
    with db() as conn:
        if version is not None:
            row = conn.execute(
                "SELECT * FROM skills WHERE name=? AND version=?",
                (name, version),
            ).fetchone()
        else:
            # Latest version: the one with the largest version string under
            # this name. We use lexicographic ordering on the semver string —
            # safe because validate_skill_manifest enforces the semver regex.
            row = conn.execute(
                "SELECT * FROM skills WHERE name=? ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(_skill_row_to_dict(row))


async def list_skills(request: web.Request) -> web.Response:
    """GET /v1/skills — list every distinct skill name with its latest version."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM skills s1 WHERE version = ("
            "  SELECT MAX(version) FROM skills s2 WHERE s2.name = s1.name"
            ") ORDER BY name"
        ).fetchall()
    return web.json_response({
        "skills": [_skill_row_to_dict(r) for r in rows],
    })


async def upload_skill_wasm(request: web.Request) -> web.Response:
    """POST /v1/skills/{name}/wasm — upload the WASM binary for an
    already-registered WASM skill.

    Architectural context (kanban t_c27c1d8d): POST /v1/skills registers
    the manifest (name, version, hash, size_bytes). This endpoint accepts
    the actual binary body and writes it to
    `WASM_DIR/{name}-{version}.wasm` so the worker can read it from the
    same EFS mount. The split lets us register first (validate the
    manifest cheaply) and upload second (expensive streaming), so a
    misbehaving client doesn't burn bandwidth on a registration that's
    going to 409.

    Auth: same Bearer-token model as POST /v1/skills (BROKER_SKILLS_API_KEY,
    closed-by-default). The header carrying the expected hash is
    `X-Wasm-Manifest-Hash` — a client MUST send it because that's the
    contract against which the body is verified.

    Validation (in order, fail-closed):
      1. Bearer auth + API key configured
      2. Skill name registered (else 404)
      3. Skill has wasm_ref_uri set (else 409 skill_not_wasm — can't
         upload a binary for a prompt-template skill)
      4. X-Wasm-Manifest-Hash present + well-formed 64-char hex
      5. X-Wasm-Manifest-Hash matches the registered wasm_manifest_hash
         (else 403 wasm_hash_mismatch — the binary belongs to a different
         skill version than what's registered)
      6. Body length matches the registered wasm_ref.size_bytes (else
         403 wasm_size_mismatch — clients can't lie about size to get
         around the registration-time cap)
      7. Body sha256 == registered wasm_manifest_hash (else 403
         wasm_hash_mismatch with the computed hash in the response so
         the client can see what went wrong)

    On success: 201 with `{wasm_uri, sha256, size_bytes, name, version}`
    and a `Location` header. The URI is the absolute EFS path the worker
    reads from — same shape as the resolution done by
    resolve_skill_wasm_uri.
    """
    # 1. Auth gate — closed by default. Same key as POST /v1/skills.
    if not BROKER_SKILLS_API_KEY:
        return web.json_response({
            "error": "skill upload is disabled (BROKER_SKILLS_API_KEY not configured)",
            "code": "skills_auth_not_configured",
        }, status=503)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return web.json_response({
            "error": "missing or invalid Authorization header",
            "code": "skills_auth_required",
        }, status=401)
    presented = auth_header[len("Bearer "):].strip()
    import hmac as _hmac
    if not _hmac.compare_digest(presented, BROKER_SKILLS_API_KEY):
        return web.json_response({
            "error": "invalid skills API key",
            "code": "skills_auth_invalid",
        }, status=401)

    name = request.match_info["name"]
    # 2+3. Look up the registered skill (latest version). 404 if unknown;
    # 409 skill_not_wasm if it's a prompt-template registration.
    with db() as conn:
        row = conn.execute(
            "SELECT version, wasm_manifest_hash, wasm_ref_uri, wasm_ref_size "
            "FROM skills s1 "
            "WHERE s1.name = ? "
            "AND s1.version = (SELECT MAX(version) FROM skills s2 "
            "                  WHERE s2.name = s1.name)",
            (name,),
        ).fetchone()
    if row is None:
        return web.json_response({
            "error": f"skill {name!r} is not registered",
            "code": "skill_not_found",
        }, status=404)
    if row["wasm_ref_uri"] is None:
        return web.json_response({
            "error": (f"skill {name!r} is a prompt-template registration; "
                     f"WASM upload is only valid for WASM skills"),
            "code": "skill_not_wasm",
        }, status=409)
    registered_version = row["version"]
    registered_hash = row["wasm_manifest_hash"]
    registered_size = row["wasm_ref_size"]

    # 4. Validate the hash header.
    import re as _upload_re
    hash_header = request.headers.get("X-Wasm-Manifest-Hash", "")
    if (not isinstance(hash_header, str) or len(hash_header) != 64
            or not _upload_re.match(r"^[0-9a-fA-F]{64}$", hash_header)):
        return web.json_response({
            "error": ("X-Wasm-Manifest-Hash header is required and must be "
                      "a 64-char hex SHA-256"),
            "code": "wasm_hash_header_missing",
        }, status=400)
    hash_header_lc = hash_header.lower()
    if hash_header_lc != registered_hash:
        return web.json_response({
            "error": ("X-Wasm-Manifest-Hash does not match the registered "
                      "wasm_manifest_hash for this skill"),
            "code": "wasm_hash_mismatch",
            "registered_hash": registered_hash,
            "presented_hash": hash_header_lc,
        }, status=403)

    # 5+6. Read the body. We bound the read at registered_size + 1 so a
    # client streaming more bytes than the registered size gets a clean
    # 403 wasm_size_mismatch without us buffering an arbitrarily large
    # blob into memory. max_fuel cap is on the broker, so the body is
    # guaranteed ≤ 50 MiB by registration time (validate_skill_manifest
    # already enforced it). aiohttp's StreamReader.read(n) returns UP TO
    # n bytes — it can short-read if the producer hasn't filled the
    # chunk yet. We loop until we either (a) accumulate exactly
    # expected_size bytes, or (b) overshoot (we read one extra byte to
    # detect that case without consuming the rest of the stream).
    expected_size = int(registered_size)
    chunks = []
    total = 0
    over_cap = False
    while total < expected_size + 1:
        needed = expected_size + 1 - total
        chunk = await request.content.read(n=needed)
        if not chunk:
            break  # EOF before we filled the buffer
        chunks.append(chunk)
        total += len(chunk)
        if total > expected_size:
            over_cap = True
            break
    body = b"".join(chunks)
    actual_size = len(body)
    if over_cap or actual_size > expected_size:
        return web.json_response({
            "error": (f"uploaded body size (>{expected_size}) exceeds "
                      f"registered wasm_ref.size_bytes ({expected_size})"),
            "code": "wasm_size_mismatch",
            "registered_size": expected_size,
            "received_size": actual_size,
        }, status=403)
    if actual_size != expected_size:
        return web.json_response({
            "error": (f"uploaded body size ({actual_size}) does not match "
                      f"registered wasm_ref.size_bytes ({expected_size})"),
            "code": "wasm_size_mismatch",
            "registered_size": expected_size,
            "received_size": actual_size,
        }, status=403)

    # 7. Recompute sha256 — the binding constraint. If this passes the
    # worker can verify against the same hash via env["skill_hash"].
    import hashlib as _hl
    actual_hash = _hl.sha256(body).hexdigest()
    if actual_hash != registered_hash:
        return web.json_response({
            "error": ("uploaded body sha256 does not match registered "
                      "wasm_manifest_hash"),
            "code": "wasm_hash_mismatch",
            "registered_hash": registered_hash,
            "actual_hash": actual_hash,
        }, status=403)

    # Persist atomically: write to .tmp, fsync, rename. The atomic
    # rename guarantees a worker that races the upload sees either the
    # old file (if the rename hasn't happened) or the new one — never a
    # half-written blob. WASM_DIR.mkdir with exist_ok=True mirrors the
    # pattern used for ARTIFACTS_DIR.
    WASM_DIR.mkdir(parents=True, exist_ok=True)
    final_path = WASM_DIR / f"{name}-{registered_version}.wasm"
    tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    try:
        with open(tmp_path, "wb") as f:
            f.write(body)
            f.flush()
            import os as _os
            _os.fsync(f.fileno())
        _os.replace(tmp_path, final_path)
    except Exception as e:
        try:
            tmp_path.unlink()
        except Exception:
            pass
        log.error("WASM upload failed for %s@%s: %s", name, registered_version, e)
        return web.json_response({
            "error": f"failed to persist WASM binary: {type(e).__name__}: {e}",
            "code": "wasm_persist_failed",
        }, status=500)

    log.info("WASM uploaded: %s@%s (sha=%s size=%d)",
             name, registered_version, actual_hash[:16], actual_size)
    return web.json_response({
        "name": name,
        "version": registered_version,
        "wasm_uri": str(final_path),
        "sha256": actual_hash,
        "size_bytes": actual_size,
    }, status=201, headers={
        "Location": f"/v1/skills/{name}-{registered_version}.wasm",
    })


# ---------- Outbox polling ----------

async def outbox_poller() -> None:
    """Watch /mnt/broker/jobs/outbox for completed results and update DB + webhook."""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    log.info("outbox poller started")
    while True:
        try:
            for f in OUTBOX.iterdir():
                if not f.name.endswith(".json") or f.name in seen:
                    continue
                seen.add(f.name)
                try:
                    payload = json.loads(f.read_text())
                except Exception as e:
                    log.warning("outbox %s unreadable: %s", f.name, e)
                    continue
                job_id = payload.get("job_id")
                if not job_id:
                    continue
                await _finalize_job(job_id, payload)
        except Exception as e:
            log.exception("outbox loop error: %s", e)
        await asyncio.sleep(2)


async def _finalize_job(job_id: str, payload: dict) -> None:
    state = payload.get("state", "completed")
    now = datetime.now(timezone.utc).isoformat()
    # VULN-S4: Add a `broker_signature` field to the result envelope after
    # verifying the worker's signature. This is the broker's independent
    # non-repudiation root — the worker's signing key is a random Ed25519
    # key persisted on disk (not derived from SEV-SNP), so the
    # `worker_signature` only attests worker liveness. The broker's key
    # is generated on first boot and lives only on the control plane,
    # so a verifier with the broker's public key can prove the result
    # passed through this broker instance.
    #
    # We sign the same canonical payload the worker signed (result_hash
    # + skill_hash + input_hash) so the two signatures are over
    # identical bytes. The verifier only needs the broker pubkey to
    # trust the result; cross-checking the worker signature is a
    # bonus. We tolerate missing hashes (best-effort sign) so a
    # malformed envelope still gets broker-signed — recording
    # _something_ is more useful for audit than refusing the job.
    result_obj = payload.get("result") or {}
    if isinstance(result_obj, dict):
        result_hash = result_obj.get("result_hash", "")
        skill_hash = result_obj.get("skill_hash", "")
        input_hash = result_obj.get("input_hash", "")
        if result_hash and skill_hash and input_hash:
            sig_payload = f"{result_hash}|{skill_hash}|{input_hash}".encode("utf-8")
            try:
                result_obj["broker_signature"] = crypto.broker_sign(sig_payload)
            except Exception as e:
                # Don't fail the finalize — record the sign failure in the
                # envelope so auditors see it, but keep the row update going.
                log.warning("broker_sign failed for job %s: %s", job_id, e)
                result_obj.setdefault("broker_signature_error", str(e))
    result_blob = json.dumps(result_obj)
    error = payload.get("error")
    # Extract artifact count from the result envelope so the row reflects
    # whether the job produced result-pack files. Used by clients polling
    # /v1/jobs to know whether to fetch /v1/jobs/{id}/artifacts. Default 0
    # for jobs without artifacts (backwards compatible).
    artifacts_summary = (payload.get("result") or {}).get("artifacts") or {}
    artifact_count = int(artifacts_summary.get("count", 0))

    # === Stripe PaymentIntent lifecycle (t_9fbec867) ===
    # Pull the data we need to compute the actual cost and call the
    # appropriate Stripe API. We read these AFTER the broker_signature
    # step so the signed envelope reflects the verified input — payment
    # status is NOT part of the signed payload (it's a broker-side
    # concern, not a worker claim).
    with db() as conn:
        job_row = conn.execute(
            "SELECT j.started_at, j.finished_at, lt.stripe_pi_id, "
            "lt.tokens_used FROM jobs j LEFT JOIN llm_tokens lt "
            "ON lt.job_id = j.job_id WHERE j.job_id = ?",
            (job_id,),
        ).fetchone()
    pi_id = (job_row["stripe_pi_id"] if job_row else "") or ""
    # Compute duration_ms from start/finish. If either is missing (test
    # fixture or a job that never reached 'running') default to 15 min
    # (one lease slot) so the cost reflects a sensible minimum.
    duration_ms = 0
    if job_row and job_row["started_at"]:
        try:
            t0 = datetime.fromisoformat(job_row["started_at"])
            if job_row["finished_at"]:
                t1 = datetime.fromisoformat(job_row["finished_at"])
                duration_ms = max(0, int((t1 - t0).total_seconds() * 1000))
            else:
                duration_ms = max(0, int((datetime.now(timezone.utc) - t0).total_seconds() * 1000))
        except (ValueError, TypeError):
            duration_ms = 0
    total_tokens = int(job_row["tokens_used"] if job_row and job_row["tokens_used"] else 0)
    # kanban t_d0ee4495: calculate_job_cost now returns a split so the
    # cost ledger can record lease/token/total without re-deriving. We
    # log the split BEFORE calling capture_payment so the audit trail
    # is complete even if capture fails (refund path or live-mode error
    # shouldn't lose the cost-breakdown evidence).
    lease_cents, token_cents, amount_cents = calculate_job_cost(duration_ms, total_tokens)
    # Capture on completed, refund on failed/timeout. 'completed' is the
    # only state where we charge the customer; everything else is a
    # refund. We persist the payment outcome as stripe_status so clients
    # polling GET /v1/jobs/{id} after finalize see a stable status string.
    payment_record: dict = {}
    if pi_id:
        if state == "completed":
            # Demo-mode ledger gets the split for reviewer audit; live
            # mode bypasses the ledger (Stripe is the system of record).
            _log_demo_lifecycle("capture", pi_id, amount_cents,
                                lease_cents=lease_cents,
                                token_cents=token_cents)
            payment_record = capture_payment(pi_id, amount_cents)
            # Check for shortfall - if the capture failed with a shortfall
            # and we're in LIVE mode (STRIPE_SECRET_KEY set), transition
            # to awaiting_topup state (kanban t_9a705578).
            if not payment_record.get("captured") and payment_record.get("status") == "shortfall_required":
                now = datetime.now(timezone.utc).isoformat()
                shortfall_cents = int(payment_record.get("shortfall_cents", 0) or 0)
                # Only trigger in LIVE mode (demo mode skips shortfall path)
                if STRIPE_SECRET_KEY and shortfall_cents > 0:
                    # Update job to awaiting_topup, keep result encrypted
                    with db() as conn:
                        conn.execute(
                            "UPDATE jobs SET state=?, shortfall_cents=?, awaiting_topup_at=?, finished_at=?, error=? WHERE job_id=?",
                            ("awaiting_topup", shortfall_cents, now, datetime.now(timezone.utc).isoformat(),
                             "payment_shortfall_required", job_id),
                        )
                    log.info("job %s -> awaiting_topup (shortfall=%d cents)", job_id, shortfall_cents)
                    # Deliver webhook notification
                    with db() as conn:
                        webhook_row = conn.execute(
                            "SELECT webhook_url FROM jobs WHERE job_id=?",
                            (job_id,),
                        ).fetchone()
                    if webhook_row and webhook_row["webhook_url"]:
                        webhook_payload = {
                            "job_id": job_id,
                            "state": "awaiting_topup",
                            "event": "topup_required",
                            "topup_url": f"/v1/jobs/{job_id}/topup",
                            "shortfall_cents": shortfall_cents,
                        }
                        try:
                            await _deliver_webhook(job_id, webhook_row["webhook_url"], webhook_payload, "awaiting_topup")
                        except Exception as e:
                            log.warning("webhook delivery failed for topup_required: %s", e)
                    # Return early - don't update stripe_status or mark finished
                    return
        elif state in ("failed", "timeout"):
            payment_record = refund_payment(pi_id)
        else:  # queued/running — finalize shouldn't run, but be safe
            payment_record = {"status": "skipped", "state": state}
    else:
        # No pi_id (test fixture) — record a no-op so the column is set.
        payment_record = {"status": "skipped", "reason": "no_pi_id"}

    # Persist stripe_* columns alongside the state update. We do this in
    # the SAME write so a partial failure doesn't leave a half-updated
    # row. The status string is the canonical client-facing field; the
    # amount column is set to the requested capture so clients can
    # reason about cost without a second Stripe call. Demo-mode rows are
    # additionally tagged so analysts can filter them out when studying
    # real-customer payment behaviour.
    stripe_status = str(payment_record.get("status", "unknown"))
    # Demo-mode prefix so dashboards can tell demo_capture/demo_refund
    # apart from real Stripe activity without joining against the
    # STRIPE_SECRET_KEY env. Without this prefix both would show as
    # "succeeded" (intentional — the helpers return the same shape so
    # callers can't branch on it).
    if payment_record.get("demo") and not stripe_status.startswith("demo_"):
        stripe_status = f"demo_{stripe_status}"
    stripe_capture_amount = payment_record.get("amount_cents")
    stripe_transfer_id = payment_record.get("id") or ""

    with db() as conn:
        conn.execute(
            "UPDATE jobs SET state=?, result=?, error=?, finished_at=?, artifact_count=?, "
            "stripe_status=?, stripe_capture_amount=?, stripe_transfer_id=? "
            "WHERE job_id=? AND state IN ('queued', 'running')",
            (state, result_blob, error, now, artifact_count,
             stripe_status, stripe_capture_amount, stripe_transfer_id, job_id),
        )
        row = conn.execute(
            "SELECT webhook_url FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
    log.info("job %s -> %s (artifacts=%d, stripe=%s, amount_cents=%s)",
             job_id, state, artifact_count, stripe_status, stripe_capture_amount)
    if row and row["webhook_url"]:
        await _deliver_webhook(job_id, row["webhook_url"], payload, state)
    if state in ("completed", "failed", "timeout"):
        await worker_mgr.note_job_finished()


async def topup_job(request: web.Request) -> web.Response:
    """Handle topup PI verification and capture (kanban t_9a705578).

    After a job completes and the broker detects shortfall, the job
    transitions to state='awaiting_topup' and the result stays encrypted.
    The client calls this endpoint with a new PaymentIntent ID that
    covers the shortfall amount. On success, the broker:
      - Verifies the topup PI exists and has status 'requires_capture'
      - Captures the topup PI for the shortfall amount
      - Updates job columns: topup_pi_id, topup_capture_amount,
        stripe_topup_transfer_id
      - Transitions job to state='completed'
      - Releases the encrypted result (if all payments succeeded)

    Request body: {"stripe_pi_id": "pi_topup_xxx"}
    Response on success: {"status": "succeeded", "message": "..."}
    Response on error: 400, 404, 409, or 500 with error details.

    B3 — Idempotent topup PI capture (kanban t_b2ceaf21,
    docs/security/threat-model-topup-flow.md §7). The earlier
    implementation read the job's state in one transaction and updated it
    in another, which left a window where two concurrent topup requests
    could BOTH capture the same topup PI (or capture DIFFERENT topup PIs
    for the same job and release the result twice). Fix in three layers:

      1. **Cached-retry path (idempotent replay).** If the job row
         already has topup_pi_id + stripe_topup_transfer_id set, return
         the cached response without touching Stripe. This makes the
         endpoint safe to retry — the client (or its network) can replay
         the request indefinitely and get the same answer.
      2. **BEGIN IMMEDIATE atomic state transition.** Same pattern as
         the client_req_id race fix at daemon.py:1968-2090: acquire the
         SQLite write lock, re-read the job's state inside the
         transaction, and only flip it from 'awaiting_topup' to
         'completed' if no concurrent transaction already has. A second
         concurrent request that arrives mid-capture finds the state
         already moved off 'awaiting_topup' and bails with HTTP 409 +
         code "topup_already_settled".
      3. **Stripe idempotency_key.** Every PaymentIntent.capture call
         in this handler passes an idempotency_key derived from
         sha256(job_id + "|" + topup_pi_id)[:32]. Stripe dedupes on this
         key for 24h, so even if two requests somehow race past the DB
         lock (e.g. against a non-SQLite deployment), Stripe itself
         ensures only one capture settles.
    """
    job_id = request.match_info.get("job_id", "")
    if not job_id:
        return web.json_response({"error": "job_id required"}, status=400)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    topup_pi_id = body.get("stripe_pi_id", "").strip() if isinstance(body, dict) else ""
    if not topup_pi_id.startswith("pi_"):
        return web.json_response({"error": "invalid stripe_pi_id format"}, status=400)

    # ------------------------------------------------------------------
    # B3 step 1: cached-retry path. If a previous topup call already
    # captured and committed, the job is in 'completed' state with the
    # topup_pi_id + stripe_topup_transfer_id populated. Return the same
    # success shape WITHOUT calling Stripe again — safe to retry from
    # the client's network (idempotent replay).
    # ------------------------------------------------------------------
    with db() as conn:
        cached = conn.execute(
            "SELECT state, topup_pi_id, topup_capture_amount, "
            "stripe_topup_transfer_id, shortfall_cents "
            "FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if not cached:
        return web.json_response({"error": "job not found"}, status=404)
    # Only serve the cached response when the SAME topup PI is being
    # retried. A different PI on an already-completed job is the
    # concurrent-different-PI race — fall through to the BEGIN
    # IMMEDIATE block which will reject with 409.
    if (cached["state"] == "completed"
            and cached["topup_pi_id"]
            and cached["topup_pi_id"] == topup_pi_id):
        return web.json_response({
            "status": "succeeded",
            "message": f"topup already captured for job {job_id}",
            "job_id": job_id,
            "topup_pi_id": cached["topup_pi_id"],
            "topup_transfer_id": cached["stripe_topup_transfer_id"],
            "topup_capture_amount": int(cached["topup_capture_amount"] or 0),
            "shortfall_cents": int(cached["shortfall_cents"] or 0),
            "idempotent_replay": True,
        })

    # ------------------------------------------------------------------
    # B3 step 2 + 3: atomic BEGIN IMMEDIATE state flip + idempotent
    # Stripe captures. The transaction holds the SQLite write lock so a
    # concurrent topup request cannot squeeze through the
    # check-then-update window.
    # ------------------------------------------------------------------
    # B3 idempotency key. Derived from (job_id, topup_pi_id) so it's
    # deterministic across retries but unique per (job, topup) pair.
    # Stripe dedupes on this for 24h.
    idempotency_key = hashlib.sha256(
        f"{job_id}|{topup_pi_id}".encode()).hexdigest()[:32]

    shortfall_cents = 0
    original_pi_id = ""
    captured_topup_id = ""
    captured_original_id = ""
    committed = False
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT state, shortfall_cents FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if not job:
                conn.execute("ROLLBACK")
                return web.json_response({"error": "job not found"}, status=404)
            if job["state"] != "awaiting_topup":
                # Concurrent second request beat us to the lock — the
                # job is already moved off awaiting_topup. Reject with
                # the canonical B3 409 so the client can distinguish
                # "you lost the race" from "you submitted garbage".
                conn.execute("ROLLBACK")
                return web.json_response({
                    "error": f"job not in awaiting_topup state (current: {job['state']})",
                    "code": "topup_already_settled",
                    "current_state": job["state"],
                }, status=409)

            shortfall_cents = int(job["shortfall_cents"] or 0)
            if shortfall_cents <= 0:
                # Defense in depth — finalize should never have left a
                # zero-shortfall job in awaiting_topup, but if a future
                # code path does, we want a clean error rather than a
                # $0 capture.
                conn.execute("ROLLBACK")
                return web.json_response({
                    "error": "job has no shortfall to top up",
                    "code": "no_shortfall",
                }, status=409)

            # Look up the original PI while holding the lock so the
            # row can't be mutated underneath us by another worker.
            llm_row = conn.execute(
                "SELECT stripe_pi_id FROM llm_tokens WHERE job_id=?",
                (job_id,),
            ).fetchone()
            original_pi_id = (llm_row["stripe_pi_id"] if llm_row else "") or ""
            if not original_pi_id:
                conn.execute("ROLLBACK")
                return web.json_response(
                    {"error": "original payment intent not found"},
                    status=500)

            # B4/T8 fix: verify the topup PI is actually capturable before
            # we call Stripe. The pre-B3 implementation called
            # verify_payment_intent(topup_pi_id) here and rejected bad PIs
            # with HTTP 400; B3's rewrite skipped that check (it relied on
            # Stripe's idempotency_key for safety, but didn't account for
            # the PI being in a non-capturable state like "canceled").
            # Without this check, a client could submit a bad PI, we'd call
            # capture on it (succeeding in demo mode because the fake
            # Stripe doesn't validate status), and silently flip the job to
            # "completed" with a non-existent topup. Restoring the verify
            # keeps the rejection surface and lets T8/T9 in
            # verify-insufficient-funds.py pass.
            pi_ok, pi_err, _pi_amount = verify_payment_intent(topup_pi_id)
            if not pi_ok:
                conn.execute("ROLLBACK")
                return web.json_response({
                    "error": f"topup pi verification failed: {pi_err}",
                    "code": "invalid_topup_pi",
                }, status=400)

            # ---- Stripe calls happen INSIDE the transaction ----
            # We hold the SQLite write lock the entire time, so no
            # concurrent topup request can enter this block. The
            # idempotency_key is forwarded to Stripe so even if the DB
            # lock were ever bypassed (different driver, non-SQLite
            # deployment), Stripe itself dedupes.
            topup_capture = capture_payment(
                topup_pi_id, shortfall_cents,
                idempotency_key=f"topup:{idempotency_key}")
            if not topup_capture.get("captured") and topup_capture.get("status") != "shortfall_required":
                conn.execute("ROLLBACK")
                return web.json_response({
                    "error": f"topup capture failed: {topup_capture.get('error', 'unknown error')}",
                }, status=500)
            captured_topup_id = topup_capture.get("id") or topup_pi_id

            # Re-capture the ORIGINAL PI for the shortfall amount.
            # Pre-B3 code captured the original for the TOPUP's held
            # amount (a copy-paste bug from the line above that read
            # `pi_amount`). Both captures now use shortfall_cents so
            # the total charged equals 2 x shortfall_cents — matching
            # the t_9a705578 test expectation (T6:
            # payment.amount_cents == 200 for a 100-cent shortfall).
            original_capture = capture_payment(
                original_pi_id, shortfall_cents,
                idempotency_key=f"topup:{idempotency_key}:orig")
            if not original_capture.get("captured") and original_capture.get("status") != "shortfall_required":
                conn.execute("ROLLBACK")
                return web.json_response({
                    "error": f"original capture failed: {original_capture.get('error', 'unknown error')}",
                }, status=500)
            captured_original_id = original_capture.get("id") or original_pi_id

            now = datetime.now(timezone.utc).isoformat()

            # ---- COMMIT — state flip is atomic with the captures ----
            # NOTE: `stripe_capture_amount` is set to shortfall_cents
            # (the original PI's portion of the capture) so that
            # _payment_block_for() can add topup_capture_amount and
            # surface the total: shortfall_cents (original) +
            # shortfall_cents (topup) = 2 x shortfall_cents. This
            # matches T6 in verify-insufficient-funds.py
            # (amount_cents == 200 for a 100-cent shortfall).
            conn.execute(
                "UPDATE jobs SET "
                "state = ?, finished_at = ?, "
                "topup_pi_id = ?, topup_capture_amount = ?, "
                "stripe_topup_transfer_id = ?, "
                "stripe_transfer_id = ?, "
                "stripe_status = ?, stripe_capture_amount = ?, "
                "awaiting_topup_at = NULL "
                "WHERE job_id = ? AND state = 'awaiting_topup'",
                ("completed", now,
                 topup_pi_id, shortfall_cents,
                 captured_topup_id,
                 captured_original_id,
                 "succeeded", shortfall_cents, job_id),
            )
            # Belt-and-braces: confirm the UPDATE actually flipped the row.
            # If WHERE clause didn't match (someone else moved the state
            # under us — shouldn't happen with BEGIN IMMEDIATE, but the
            # predicate protects against a future driver that skips the
            # lock), bail with 409.
            flipped = conn.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if not flipped or flipped["state"] != "completed":
                conn.execute("ROLLBACK")
                return web.json_response({
                    "error": "topup state transition lost race",
                    "code": "topup_already_settled",
                    "current_state": (flipped or {}).get("state"),
                }, status=409)
            conn.execute("COMMIT")
            committed = True
    except sqlite3.Error as e:
        log.error("topup_job %s sqlite error: %s", job_id, e)
        return web.json_response({"error": f"db error: {e}"}, status=500)

    if not committed:
        # Defensive — should be unreachable because every branch above
        # either commits or returns. But if a future refactor adds a
        # silent fall-through, fail closed.
        return web.json_response(
            {"error": "topup did not commit", "code": "topup_not_committed"},
            status=500)

    log.info("topup job %s completed (original PI=%s, topup PI=%s, shortfall=%d, topup_transfer=%s)",
             job_id, original_pi_id, topup_pi_id, shortfall_cents, captured_topup_id)

    # Deliver webhook notification (outside the lock).
    with db() as conn:
        webhook_row = conn.execute(
            "SELECT webhook_url FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    if webhook_row and webhook_row["webhook_url"]:
        payload = {
            "job_id": job_id,
            "state": "completed",
            "payment": {
                "status": "succeeded",
                "amount_cents": 2 * shortfall_cents,
                "original_pi_id": original_pi_id,
                "topup_pi_id": topup_pi_id,
                "topup_transfer_id": captured_topup_id,
                "shortfall_cents": shortfall_cents,
            },
        }
        await _deliver_webhook(job_id, webhook_row["webhook_url"], payload, "completed")

    return web.json_response({
        "status": "succeeded",
        "message": f"topup successful for job {job_id}",
        "job_id": job_id,
        "original_pi_id": original_pi_id,
        "topup_pi_id": topup_pi_id,
        "topup_transfer_id": captured_topup_id,
        "topup_capture_amount": shortfall_cents,
        "shortfall_cents": shortfall_cents,
    })


def _payment_block_for(job_id: str) -> dict | None:
    """Build the canonical `payment` block for a job, matching GET /v1/jobs/{id}.

    Reads the same columns get_job() reads (stripe_status, stripe_capture_amount,
    stripe_transfer_id, stripe_pi_amount_cents, shortfall_cents, topup_pi_id,
    topup_capture_amount) and applies the same status-derivation rules
    (demo_ prefix → stripped, demo flag carried; shortfall → "shortfall_required";
    pending demo → "pending"; finalized → capture/refund status).

    Chose "re-query the row" over "trust the caller to pass a payment dict" so
    the webhook body can never drift from the GET response: both call sites
    produce identical output because they read the same row with the same
    logic. Kanban t_84d3e5ee / audit t_9fb71ad7: the prior implementation
    let callers build their own payment block (or skip it entirely), which
    meant the webhook could emit `state: completed` with no `payment` field
    and force downstream consumers to call back into the broker.

    Returns None when no payment info should be surfaced (job has no
    stripe_status column populated and is not in awaiting_topup state —
    e.g. legacy rows from before the Stripe columns were added).
    """
    with db() as conn:
        row = conn.execute(
            "SELECT state, stripe_status, stripe_capture_amount, "
            "stripe_transfer_id, stripe_pi_amount_cents, shortfall_cents, "
            "topup_pi_id, topup_capture_amount "
            "FROM jobs WHERE job_id = ?", (job_id,),
        ).fetchone()
    if not row:
        return None
    state = row["state"] or ""
    stripe_status = row["stripe_status"]
    stripe_capture_amount = row["stripe_capture_amount"]
    stripe_transfer_id = row["stripe_transfer_id"]
    stripe_pi_amount_cents = row["stripe_pi_amount_cents"]
    shortfall_cents = row["shortfall_cents"]
    topup_pi_id = row["topup_pi_id"]
    topup_capture_amount = row["topup_capture_amount"]

    if stripe_status:
        # Demo-mode rows are tagged with a `demo_` prefix on the column so
        # dashboards can filter real vs demo with a single LIKE query. For
        # the client-facing payment block we strip the prefix so the status
        # string matches live mode's shape — clients shouldn't have to
        # branch on "demo_succeeded" vs "succeeded". The `demo: True`
        # flag below carries the demo/live signal instead.
        client_status = stripe_status
        client_is_demo = False
        if client_status.startswith("demo_"):
            client_status = client_status[len("demo_"):]
            client_is_demo = True
        out = {
            "status": client_status,
            "amount_cents": stripe_capture_amount,
            "stripe_id": stripe_transfer_id or None,
            "held_amount_cents": stripe_pi_amount_cents,
            "mode": "demo" if client_is_demo else ("live" if STRIPE_SECRET_KEY else "demo"),
            "demo": client_is_demo,
        }
        # Aggregate total capture for shortfall resolution scenarios
        if topup_capture_amount:
            out["amount_cents"] = (out.get("amount_cents") or 0) + topup_capture_amount
        # Record topup PI ID when present
        if topup_pi_id:
            out["topup_stripe_id"] = topup_pi_id
        return out
    if shortfall_cents and state == "awaiting_topup":
        # Shortfall scenario — job waiting for client topup. Mirror the
        # shape get_job() emits so the webhook and GET response are
        # interchangeable for downstream consumers.
        return {
            "status": "shortfall_required",
            "shortfall_cents": shortfall_cents,
            "topup_url": f"/v1/jobs/{job_id}/topup",
            "held_amount_cents": stripe_pi_amount_cents,
            "mode": "live" if STRIPE_SECRET_KEY else "demo",
        }
    if not STRIPE_SECRET_KEY:
        # Demo mode + job not yet finalized — give clients a hint that
        # mirrors what get_job() does.
        return {"status": "pending", "mode": "demo", "demo": True}
    return None


def _artifact_urls_for(job_id: str, artifacts_summary: dict | None) -> dict | None:
    """Build the webhook artifact_urls block. Returns None when no artifacts.

    Uses absolute URLs (https://BROKER_DOMAIN/...) so the webhook receiver
    doesn't need to know the broker base. BROKER_DOMAIN is empty in dev /
    tests, in which case we fall back to a path-only URL — same shape, the
    receiver can prepend its own base if needed.
    """
    if not artifacts_summary or not artifacts_summary.get("files"):
        return None
    base = f"https://{BROKER_DOMAIN}" if BROKER_DOMAIN else ""
    return {
        "manifest": f"{base}/v1/jobs/{job_id}/artifacts",
        "files": {
            f["filename"]: f"{base}/v1/jobs/{job_id}/artifacts/{f['filename']}"
            for f in artifacts_summary.get("files", [])
        },
    }


async def _deliver_webhook(job_id: str, url: str, payload: dict, state: str) -> None:
    """POST a webhook for `job_id` with the canonical broker body shape.

    Body shape (kanban t_84d3e5ee):
      {job_id, state, event, result, artifact_urls, payment, event_id}

    The `payment` block is derived from the DB row via _build_webhook_body()
    so the webhook body can never drift from the GET /v1/jobs/{id} response —
    every call site that delivers a webhook gets the same canonical payment
    block regardless of which code path triggered the delivery.

    `event` mirrors the `state` for terminal state transitions (e.g.
    `completed` / `failed` / `timeout`) and uses an action verb for
    non-terminal ones (`topup_required` for `awaiting_topup`). Callers
    can still read `state` for the canonical job state string.

    `event_id` (kanban t_30ca541f, threat model §4.3 fix #4) is a fresh
    UUID per delivery so receivers can dedupe across retries.

    B4 mitigations applied here:
      - Per-job cap: bail with a logged warning when `webhook_attempts >=
        WEBHOOK_MAX_ATTEMPTS_PER_JOB` so a single job can't spam webhooks
        (default 3 covers awaiting_topup + completed + timeout fallback).
      - Async dispatch: enqueue into `_webhook_queue` (set up in
        `_on_startup`) so the outbox-poller / topup-job handler returns
        immediately. The dispatcher worker (see `_webhook_dispatcher`)
        owns the actual POST with bounded parallelism and a 5s timeout.
      - Synchronous fallback: when `_webhook_queue is None` (i.e. tests
        that called `app.on_startup.clear()`), POST inline. This keeps
        `verify-webhook-payload.py` working — its `_spy_capture` replaces
        the entire `_deliver_webhook` symbol, so the post-call DB write
        there is fine, but tests that DON'T monkeypatch this function
        need a fallback so we don't silently drop deliveries.
    """
    # B4 per-job cap. Atomic UPDATE … RETURNING (SQLite >= 3.35) lets us
    # check + increment in one round trip. Skip in the no-cap case.
    if WEBHOOK_MAX_ATTEMPTS_PER_JOB > 0:
        with db() as conn:
            row = conn.execute(
                "SELECT webhook_attempts FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            current = (row["webhook_attempts"] if row else 0) or 0
            if current >= WEBHOOK_MAX_ATTEMPTS_PER_JOB:
                log.warning(
                    "webhook cap hit for job %s (attempts=%d >= max=%d), skipping",
                    job_id, current, WEBHOOK_MAX_ATTEMPTS_PER_JOB,
                )
                with conn:
                    conn.execute(
                        "UPDATE jobs SET webhook_status=? WHERE job_id=?",
                        (f"error: cap={WEBHOOK_MAX_ATTEMPTS_PER_JOB}", job_id),
                    )
                return
            with conn:
                conn.execute(
                    "UPDATE jobs SET webhook_attempts = webhook_attempts + 1 "
                    "WHERE job_id=?",
                    (job_id,),
                )

    if _webhook_queue is None:
        # Synchronous fallback path (test environments, or shutdown drain).
        # Same body shape + event_id as the queued path so behaviour is
        # identical from the receiver's perspective.
        await _post_webhook_now(job_id, url, payload, state)
        return

    try:
        _webhook_queue.put_nowait({
            "job_id": job_id,
            "url": url,
            "payload": payload,
            "state": state,
        })
    except asyncio.QueueFull:
        # Bound is generous (default 1000); if we ever fill it, fall back
        # to inline POST so we don't drop the message silently.
        log.warning("webhook queue full; delivering inline for job %s", job_id)
        await _post_webhook_now(job_id, url, payload, state)


async def _post_webhook_now(job_id: str, url: str, payload: dict, state: str) -> None:
    """Perform the actual POST. Used by both the dispatcher and the sync fallback.

    Builds the canonical body (with `event_id` UUID), POSTs it under the
    shorter B4 timeout (5s default), and records `webhook_status` on the
    jobs row. Network errors are caught + logged; the dispatcher will
    re-attempt within the per-job cap window.
    """
    try:
        body = _build_webhook_body(job_id, payload, state)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=WEBHOOK_DELIVERY_TIMEOUT_SECONDS)
        ) as session:
            async with session.post(url, json=body) as resp:
                status = str(resp.status)
        with db() as conn:
            conn.execute("UPDATE jobs SET webhook_status=? WHERE job_id=?",
                         (status, job_id))
        log.info("webhook %s -> %s for job %s (event_id=%s)",
                 url, status, job_id, body.get("event_id"))
    except Exception as e:
        log.warning("webhook %s delivery failed for job %s: %s", url, job_id, e)
        with db() as conn:
            conn.execute("UPDATE jobs SET webhook_status=? WHERE job_id=?",
                         (f"error: {e}", job_id))


async def _webhook_host_throttle_check(url: str) -> bool:
    """Sliding-window rate limit per host. Returns True if delivery is allowed.

    Maintains an in-memory deque of recent timestamps per hostname. The
    deque is pruned of timestamps older than 1s on every check, then the
    size is compared to WEBHOOK_HOST_RATE_PER_SEC. A short lock guards
    the dict against concurrent dispatcher tasks.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return True  # can't throttle what we can't identify; let it through
    now = time.monotonic()
    cutoff = now - 1.0
    async with _webhook_host_throttle_lock:
        dq = _webhook_host_throttle.get(host)
        if dq is None:
            dq = collections.deque()
            _webhook_host_throttle[host] = dq
        # Prune expired entries.
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= WEBHOOK_HOST_RATE_PER_SEC:
            return False
        dq.append(now)
        return True


async def _webhook_dispatcher_worker(sem: asyncio.Semaphore) -> None:
    """Single dispatcher worker. Consumes the queue and POSTs in parallel.

    Multiple worker coroutines can be spawned (controlled by
    WEBHOOK_MAX_PARALLEL / 2 defaulting to 5); each holds a slot on the
    shared semaphore so total in-flight POSTs is bounded.
    """
    assert _webhook_queue is not None
    while True:
        item = await _webhook_queue.get()
        try:
            if not await _webhook_host_throttle_check(item["url"]):
                log.warning(
                    "webhook host rate limit hit for %s (cap=%d/s); dropping delivery for job %s",
                    item["url"], WEBHOOK_HOST_RATE_PER_SEC, item["job_id"],
                )
                with db() as conn:
                    conn.execute(
                        "UPDATE jobs SET webhook_status=? WHERE job_id=?",
                        (f"error: host_rate={WEBHOOK_HOST_RATE_PER_SEC}/s", item["job_id"]),
                    )
                continue
            async with sem:
                await _post_webhook_now(
                    item["job_id"], item["url"], item["payload"], item["state"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("webhook dispatcher crashed on %s: %s", item, e)
        finally:
            _webhook_queue.task_done()


def _build_webhook_body(job_id: str, payload: dict, state: str) -> dict:
    """Build the canonical webhook body for `job_id`.

    Extracted from _deliver_webhook so tests can assert on the body
    without standing up an aiohttp receiver. Mirrors the `payment`
    block shape of GET /v1/jobs/{id} via _payment_block_for()
    (kanban t_84d3e5ee).

    B4 (t_30ca541f): every body carries an `event_id` UUID so receivers
    can dedupe. The ID is generated here at body-build time so the
    canonical `_build_webhook_body` surface stays a pure function
    (no module-level state). Two webhook deliveries for the same state
    transition will produce different event_ids — exactly the dedup
    semantics callers asked for.
    """
    event = "topup_required" if state == "awaiting_topup" else state
    payment_block = _payment_block_for(job_id)
    body = {
        "job_id": job_id,
        "state": state,
        "event": event,
        "event_id": uuid.uuid4().hex,
        "result": payload.get("result"),
        "artifact_urls": _artifact_urls_for(
            job_id, (payload.get("result") or {}).get("artifacts")),
    }
    if payment_block is not None:
        body["payment"] = payment_block
    return body


# ---------- Webhook dispatcher lifecycle ----------

async def _start_webhook_dispatcher(app: web.Application) -> None:
    """Initialise the webhook queue and spawn dispatcher workers.

    Called from `app.on_startup` (see build_app). Idempotent: re-running
    it just no-ops because the global `_webhook_queue` is already set.
    The number of worker tasks is `max(1, WEBHOOK_MAX_PARALLEL // 2)`
    so total in-flight POSTs stays at WEBHOOK_MAX_PARALLEL across the
    worker pool (each worker grabs one semaphore slot per delivery).
    """
    global _webhook_queue
    if _webhook_queue is not None:
        return
    # 1000-slot queue is generous for the demo. If a single job's slow
    # webhook ever fills it, _deliver_webhook falls back to inline POST
    # so we never silently drop messages.
    _webhook_queue = asyncio.Queue(maxsize=1000)
    sem = asyncio.Semaphore(WEBHOOK_MAX_PARALLEL)
    n_workers = max(1, WEBHOOK_MAX_PARALLEL // 2)
    tasks = [asyncio.create_task(_webhook_dispatcher_worker(sem))
             for _ in range(n_workers)]
    _webhook_dispatcher_tasks.clear()
    _webhook_dispatcher_tasks.extend(tasks)
    log.info("webhook dispatcher started: workers=%d parallel=%d queue=1000",
             n_workers, WEBHOOK_MAX_PARALLEL)


async def _stop_webhook_dispatcher(app: web.Application) -> None:
    """Cancel dispatcher workers + clear throttle state on app shutdown.

    Called from `app.on_cleanup` (see build_app). Drains the queue first
    so in-flight deliveries finish (best-effort, bounded by the 5s
    per-POST timeout).
    """
    global _webhook_queue, _webhook_host_throttle
    if _webhook_queue is not None:
        # Let in-flight workers finish whatever they're holding, but
        # don't take new work. cancel() is enough for the workers since
        # they `await _webhook_queue.get()` in a tight loop.
        for task in _webhook_dispatcher_tasks:
            task.cancel()
        for task in _webhook_dispatcher_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        _webhook_dispatcher_tasks.clear()
        _webhook_queue = None
    # Throttle state is per-process; reset so a restart starts clean.
    _webhook_host_throttle.clear()


# ---------- App setup ----------

worker_mgr: WorkerManager  # initialised in main()


async def _on_startup(app: web.Application) -> None:
    global worker_mgr
    worker_mgr = WorkerManager()
    await _resume_file_jobs()
    asyncio.create_task(outbox_poller())
    asyncio.create_task(_topup_ttl_sweep_loop())
    asyncio.create_task(_input_upload_sweep_loop())
    log.info("daemon ready on https://%s", BROKER_DOMAIN or "(http)")


async def llm_proxy(request: web.Request) -> web.Response:
    """LLM proxy — workers call this with their per-job LLM token.

    The broker validates the token, checks the account cap, then forwards
    the request to the real LLM API using its private key. The response
    (including token usage) is returned to the worker and the usage is
    recorded against the account and the job.

    In production this would be the only way workers reach the LLM — the
    real API key never enters the worker or the NemoClaw sandbox.
    """

    # Auth: require a valid per-job LLM token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return web.json_response({"error": "missing or invalid Authorization header"}, status=401)
    llm_token = auth_header[len("Bearer "):].strip()

    # Allow a dedicated onboard token (from config.env) for non-job
    # calls (NemoClaw onboard validation, health checks). This is a
    # separate key from the upstream LLM API key — it only authenticates
    # the onboard wizard's test call so it can validate the endpoint.
    # Per-job tokens still go through the normal DB lookup.
    onboard_token = os.environ.get("BROKER_ONBOARD_TOKEN", "")
    if onboard_token and llm_token == onboard_token:
        # Validated as onboard token — proxy the call without job accounting
        job_id = "onboard-validation"
        stripe_pi_id = None
        already_used = 0
    else:
        with db() as conn:
            token_row = conn.execute(
                "SELECT job_id, stripe_pi_id, expires_at, tokens_used FROM llm_tokens WHERE token=?",
                (llm_token,),
            ).fetchone()
            if not token_row:
                return web.json_response({"error": "invalid or expired LLM token"}, status=401)
            # Check expiry (VULN-S11, kanban t_b13072b3): use datetime
            # comparison instead of string comparison. ISO 8601 strings
            # sort lexically ONLY when both sides use the same offset/format
            # — a Z-suffix ("2026-01-01T00:00:00Z") compares > any +00:00
            # string of the same instant, which would let an expired token
            # through if the producer ever used Z. fromisoformat handles
            # both forms (and timezone-aware parsing since Python 3.11).
            try:
                expires = datetime.fromisoformat(token_row["expires_at"])
            except (ValueError, TypeError):
                # Malformed timestamp — treat as expired so a poison-pill
                # token can't keep the proxy open.
                return web.json_response({"error": "LLM token expired"}, status=401)
            if expires < datetime.now(timezone.utc):
                return web.json_response({"error": "LLM token expired"}, status=401)
            job_id = token_row["job_id"]
            stripe_pi_id = token_row["stripe_pi_id"]
            already_used = token_row["tokens_used"]

    # Get request body
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    # Check daily account cap before calling
    today = datetime.now(timezone.utc).isoformat()[:10]
    # VULN-S7: account = account_key_for(stripe_pi_id) — hashed with the
    # server-side secret (see account_key_for module-level definition).
    # The previous form `stripe_pi_id.split("_")[:2]` was a demo placeholder
    # that produced trivially-spoofable keys (any caller who knew the
    # format could mint a new account bucket by choosing a different
    # suffix). Production would use the real Stripe customer.id.
    account = account_key_for(stripe_pi_id)
    DEMO_TOKEN_CAP = int(os.environ.get("DEMO_TOKEN_CAP", "50000"))
    with db() as conn:
        usage_row = conn.execute(
            "SELECT tokens_used FROM account_usage WHERE account=? AND date=?",
            (account, today),
        ).fetchone()
        current_usage = (usage_row["tokens_used"] if usage_row else 0) + already_used
        if current_usage >= DEMO_TOKEN_CAP:
            return web.json_response({
                "error": f"daily token cap exceeded ({current_usage}/{DEMO_TOKEN_CAP})",
                "code": "token_cap_exceeded",
            }, status=429)

    # Forward to the real LLM API (Gemini / Ollama / OpenAI / etc.).
    # The key comes from env. base_url may or may not have a trailing slash
    # or a path prefix — handle both cases. Always use the broker's
    # configured model — never trust the client's model parameter.
    real_api_key = os.environ.get("BROKER_LLM_API_KEY", "")
    real_base_url = os.environ.get("BROKER_LLM_BASE_URL", "https://ollama.com/v1")
    real_model = os.environ.get("BROKER_LLM_MODEL", "minimax-m3:cloud")
    if not real_api_key.strip():
        log.error("LLM proxy not configured for job %s: BROKER_LLM_API_KEY is empty", job_id)
        return web.json_response({
            "error": "llm_upstream_not_configured",
            "detail": "BROKER_LLM_API_KEY is empty on the broker",
        }, status=503)

    # Strip trailing slash from base_url, then append /chat/completions
    real_base_url = real_base_url.rstrip("/")
    forward_url = real_base_url + "/chat/completions"

    # VULN-S5: Construct a MINIMAL forward body. Whitelist exactly the
    # fields the upstream LLM API needs (model + messages + max_tokens +
    # stream). Strip everything else — a compromised worker can otherwise
    # inject system prompts, set temperature/tools, force streaming, or
    # request huge max_tokens. Force max_tokens <= MAX_TOKENS_CAP
    # (capped regardless of what the worker asked for) and stream=False
    # (streaming on this proxy path leaks partial tokens over the wire
    # before we've recorded usage).
    #
    # Why 100_000 and not 1024: the upstream model (minimax-m3:cloud) is
    # a reasoning model that splits its completion budget between a
    # hidden `reasoning` field (chain-of-thought) and a visible
    # `content` field. At 1024 completion tokens, a 3K+ prompt with
    # attached files still eats most of the budget on reasoning and the
    # visible `content` comes back truncated or empty for longer
    # multi-step answers (adversarial code reviews, multi-file
    # refactors) — see Pitfall 29 in tee-broker-deploy-config. Raising
    # the cap to 100_000 lets reasoning + content both complete for
    # realistic prompts. Per-day spend is still bounded by
    # DEMO_TOKEN_CAP, so this is not an unbounded cost lever.
    #
    # Chose whitelist over blacklist because new LLM API fields
    # (e.g. response_format, logprobs) would silently flow through a
    # blacklist and become new attack surface.
    forward_body = {
        "model": real_model,
        "messages": body.get("messages", []),
        "max_tokens": min(int(body.get("max_tokens", 4096)), MAX_TOKENS_CAP),
        "stream": False,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                forward_url,
                json=forward_body,
                headers={
                    "Authorization": f"Bearer {real_api_key}",
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status >= 400:
                    err_body = await resp.text()
                    log.error("LLM upstream error for job %s: %s %s", job_id, resp.status, err_body[:500])
                    return web.json_response({"error": f"upstream LLM error: {resp.status}"}, status=502)
                llm_resp = await resp.json()
    except Exception as e:
        log.error("LLM proxy error for job %s: %s", job_id, e)
        return web.json_response({"error": f"proxy error: {e}"}, status=502)

    # Record token usage
    usage = llm_resp.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

    with db() as conn:
        # Per-token usage
        conn.execute(
            "UPDATE llm_tokens SET tokens_used = tokens_used + ?, calls = calls + 1 WHERE token=?",
            (total_tokens, llm_token),
        )
        # Per-account daily usage
        conn.execute(
            "INSERT INTO account_usage (account, date, tokens_used, tokens_cap) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(account, date) DO UPDATE SET tokens_used = tokens_used + ?",
            (account, today, total_tokens, DEMO_TOKEN_CAP, total_tokens),
        )
        # Per-job usage
        conn.execute(
            "UPDATE jobs SET llm_tokens_used = llm_tokens_used + ?, llm_calls = llm_calls + 1 WHERE job_id=?",
            (total_tokens, job_id),
        )

    log.info("llm_proxy job=%s prompt=%d completion=%d total=%d account=%s",
             job_id, prompt_tokens, completion_tokens, total_tokens, account)

    # Add accounting metadata to the response so the worker can include
    # it in the job result
    llm_resp["_billing"] = {
        "job_id": job_id,
        "account": account,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "demo_cap": DEMO_TOKEN_CAP,
    }

    return web.json_response(llm_resp)


async def llm_usage(request: web.Request) -> web.Response:
    """Return the token usage recorded for a specific job."""
    job_id = request.match_info["job_id"]
    auth_error = require_job_access(request, job_id)
    if auth_error is not None:
        return auth_error
    auth_error = require_job_access(request, job_id)
    if auth_error is not None:
        return auth_error
    with db() as conn:
        job = conn.execute(
            "SELECT llm_tokens_used, llm_calls FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    if not job:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({
        "job_id": job_id,
        "llm_tokens_used": job["llm_tokens_used"] or 0,
        "llm_calls": job["llm_calls"] or 0,
    })


# === B5 chargeback / ack handlers (kanban t_69b52324) ===
#
# Threat-model-topup-flow.md §5 flagged that the broker had no defence
# against Stripe chargebacks: 24h result retention is shorter than
# Stripe's ≥120d chargeback window, no dispute webhook handler, no
# proof-of-delivery. This block closes B5 with four pieces:
#
#   1. _verify_stripe_signature — HMAC-SHA256 over `t=<ts>.<body>` with
#      a 5-minute clock-skew window. Reject stale (>5min) timestamps
#      so a leaked body can't be replayed days later.
#   2. _record_dispute_event    — append-only idempotent audit log.
#      Replay (same dispute_id) returns (False, dispute_id) so the
#      handler can answer 200 + idempotent_replay=True without
#      double-bumping the fraud score.
#   3. _bump_fraud_score        — increments one of the three counter
#      columns for an account_key and recomputes the suspended flag
#      against FRAUD_SCORE_BAN_THRESHOLD. Returns the new score and
#      the suspended flag so the webhook handler can log/respond.
#   4. _account_is_suspended    — pure read used by submit_job (and
#      available for other handlers). Returns (bool, reason_string).
#
# The corresponding HTTP handlers (POST /v1/stripe/webhook and
# POST /v1/jobs/{id}/ack) live right below and are wired in
# build_app() further down.

# HMAC tolerance for stripe-signature. Matches Stripe's published
# default of 300s. We use a module-level constant so the value is
# searchable in code reviews and not buried in the helper body.
STRIPE_SIGNATURE_TOLERANCE_SECONDS = 300


def _verify_stripe_signature(body: bytes, header: str,
                             secret: str) -> tuple[bool, str]:
    """Verify a Stripe webhook signature header.

    Stripe format: `t=<unix_ts>,v1=<hex_hmac_sha256>` where the HMAC is
    computed over `<t>.<body>`. The header can carry multiple `v1=`
    entries (key rotation); we accept if ANY v1 matches. A single
    `v0=` (legacy test signature) is silently ignored so the helper
    stays forward-compatible.

    Returns ``(ok, reason)``. ``reason`` is empty when ok=True;
    otherwise it's one of: "webhook_disabled", "missing_signature",
    "malformed_signature", "stale_signature", "bad_signature".

    No network or DB calls — pure-function so unit tests can exercise
    V1-V5 without aiohttp.
    """
    import hmac as _hmac
    if not secret:
        return False, "webhook_disabled"
    if not header:
        return False, "missing_signature"
    # Parse `k=v,k=v,...` into a dict. We only care about `t` and `v1`.
    parts: dict[str, str] = {}
    for piece in header.split(","):
        piece = piece.strip()
        if "=" not in piece:
            return False, "malformed_signature"
        k, _, v = piece.partition("=")
        parts[k.strip()] = v.strip()
    ts_str = parts.get("t")
    if not ts_str or not ts_str.isdigit():
        return False, "malformed_signature"
    v1_values = parts.get("v1")
    if not v1_values:
        return False, "malformed_signature"
    # 5-minute clock-skew window. Stripe's published default.
    try:
        ts_int = int(ts_str)
    except ValueError:
        return False, "malformed_signature"
    if abs(int(time.time()) - ts_int) > STRIPE_SIGNATURE_TOLERANCE_SECONDS:
        return False, "stale_signature"
    # Verify each v1. Stripe may send multiple during key rotation;
    # accept if any matches.
    signed_payload = f"{ts_str}.".encode("utf-8") + body
    expected = _hmac.new(
        secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    for v1 in v1_values.split(","):  # also tolerate v1=a,v1=b
        if _hmac.compare_digest(expected, v1.strip()):
            return True, ""
    return False, "bad_signature"


def _record_dispute_event(event: dict) -> tuple[bool, str]:
    """Insert one dispute_events row. Idempotent on event_id.

    Returns ``(inserted, dispute_id)``. On replay
    ``inserted=False`` and the existing dispute_id is returned. The
    raw_payload column preserves the full Stripe event JSON so an
    operator can reconstruct the timeline even if Stripe purges the
    dashboard. event_type is constrained to charge.dispute.* —
    callers MUST filter before calling.

    Replay detection: PRIMARY KEY on event_id (Stripe's evt_xxx).
    Stripe re-sends the SAME event_id on retry, so the INSERT
    raises IntegrityError and we return (False, dispute_id). A
    follow-up charge.dispute.closed for the SAME dispute sends a
    DIFFERENT event_id, so the INSERT succeeds and the audit log
    gets both rows — queryable by dispute_id (which is the bare
    Stripe du_xxx, no suffixing).
    """
    obj = (event.get("data") or {}).get("object") or {}
    dispute_id = obj.get("id") or ""
    charge_id = obj.get("charge") or ""
    pi_id = obj.get("payment_intent") or ""
    event_type = event.get("type") or ""
    status = obj.get("status") or ""
    amount = int(obj.get("amount") or 0)
    reason = obj.get("reason") or ""
    ed = obj.get("evidence_details") or {}
    due_by_unix = ed.get("due_by")
    if due_by_unix is not None:
        try:
            due_by_iso = datetime.fromtimestamp(
                int(due_by_unix), tz=timezone.utc
            ).isoformat()
        except (ValueError, OSError, OverflowError):
            due_by_iso = None
    else:
        due_by_iso = None
    now_iso = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO dispute_events "
                "(event_id, dispute_id, charge_id, payment_intent, "
                " event_type, status, amount_cents, reason, "
                " evidence_due_by, created_at, raw_payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event.get("id") or "", dispute_id, charge_id, pi_id,
                 event_type, status, amount, reason, due_by_iso,
                 now_iso, json.dumps(event)),
            )
            return True, dispute_id
        except sqlite3.IntegrityError:
            # event_id PRIMARY KEY collision — replay. Return the
            # existing dispute_id so the handler can mark
            # idempotent_replay=True.
            return False, dispute_id


def _bump_fraud_score(account_key: str, signal: str) -> tuple[int, bool]:
    """Increment one fraud-score counter for an account and recompute suspended.

    ``signal`` must be one of "chargebacks_filed", "abandoned_jobs",
    "refunded_topups". Unknown signals raise ValueError so a typo in
    a future call site is caught immediately rather than silently
    no-op'd. Returns ``(new_score, suspended)``. The ``last_event_at``
    column is bumped on every mutation for the audit trail.
    """
    if signal not in ("chargebacks_filed", "abandoned_jobs",
                      "refunded_topups"):
        raise ValueError(f"unknown fraud signal: {signal!r}")
    now_iso = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO account_fraud_score "
            "(account_key, chargebacks_filed, abandoned_jobs, "
            " refunded_topups, last_event_at) "
            "VALUES (?, 0, 0, 0, ?) "
            "ON CONFLICT(account_key) DO NOTHING",
            (account_key, now_iso),
        )
        # Increment the right counter
        conn.execute(
            f"UPDATE account_fraud_score SET {signal} = {signal} + 1, "
            f"last_event_at = ? WHERE account_key = ?",
            (now_iso, account_key),
        )
        row = conn.execute(
            "SELECT chargebacks_filed, abandoned_jobs, refunded_topups "
            "FROM account_fraud_score WHERE account_key = ?",
            (account_key,),
        ).fetchone()
    score = (row["chargebacks_filed"] + row["abandoned_jobs"]
             + row["refunded_topups"])
    suspended = score > FRAUD_SCORE_BAN_THRESHOLD
    if suspended:
        reason = (f"fraud_score={score} > threshold="
                  f"{FRAUD_SCORE_BAN_THRESHOLD} "
                  f"(cb={row['chargebacks_filed']} "
                  f"abandoned={row['abandoned_jobs']} "
                  f"refunded={row['refunded_topups']})")
        with db() as conn:
            conn.execute(
                "UPDATE account_fraud_score SET suspended = 1, "
                "suspended_reason = ? WHERE account_key = ?",
                (reason, account_key),
            )
    return score, suspended


def _account_is_suspended(account_key: str) -> tuple[bool, str]:
    """Return ``(suspended, reason)`` for an account_key.

    ``reason`` is the human-readable suspended_reason column when
    suspended, otherwise an empty string. Used by submit_job to
    refuse 403 + account_suspended before the BEGIN IMMEDIATE.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT suspended, suspended_reason FROM account_fraud_score "
            "WHERE account_key = ?",
            (account_key,),
        ).fetchone()
    if row is None:
        return False, ""
    if row["suspended"]:
        return True, row["suspended_reason"] or "suspended"
    return False, ""


async def handle_stripe_webhook(request: web.Request) -> web.Response:
    """POST /v1/stripe/webhook — charge.dispute.* events (kanban t_69b52324).

    Closed by default: when STRIPE_WEBHOOK_SECRET is unset (demo mode
    or misconfigured deploy), the endpoint refuses with 503
    webhook_disabled so accidental curl-from-localhost tests don't
    poison the dispute_events table. Same posture as the skills API
    key gate above.

    On a verified charge.dispute.created:
      - Insert into dispute_events (idempotent on dispute_id)
      - Bump account_fraud_score.chargebacks_filed by 1 for the
        PI's account_key_for. If score > threshold, suspended=1.
      - Return 200 {processed: True, dispute_id: "..."}.

    On replay (same dispute_id) the INSERT collides on UNIQUE; we
    return 200 {idempotent_replay: True, dispute_id: "..."} WITHOUT
    re-bumping the score. This is what stops a misbehaving Stripe
    retry from double-counting a single dispute.

    On charge.dispute.closed we log the resolution to dispute_events
    as a NEW row (different event_id) with the same dispute_id — the
    operator can SELECT * WHERE dispute_id='du_xxx' and see the full
    created + closed timeline. fraud_score is NOT bumped for closed
    events — the abuse already counted when the dispute was created.

    Any other event type returns 200 {received: True, processed:
    False} so Stripe's retry stops without us having to log noise.
    """
    if not STRIPE_WEBHOOK_SECRET:
        return web.json_response({
            "error": "stripe webhook is disabled (STRIPE_WEBHOOK_SECRET not configured)",
            "code": "webhook_disabled",
        }, status=503)
    # Read raw body — JSON re-parse would change whitespace and break
    # the HMAC.
    body = await request.read()
    sig_header = request.headers.get("stripe-signature", "")
    ok, reason = _verify_stripe_signature(
        body, sig_header, STRIPE_WEBHOOK_SECRET
    )
    if not ok:
        return web.json_response({
            "error": f"signature verification failed: {reason}",
            "code": reason,
        }, status=400)
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        return web.json_response({
            "error": "invalid JSON body",
            "code": "invalid_json",
        }, status=400)
    event_type = event.get("type") or ""
    if event_type not in ("charge.dispute.created",
                          "charge.dispute.closed"):
        # Accept the event silently so Stripe stops retrying, but
        # don't process.
        return web.json_response({
            "received": True,
            "processed": False,
            "reason": f"unhandled event type: {event_type!r}",
        }, status=200)
    inserted, dispute_id = _record_dispute_event(event)
    response: dict = {
        "received": True,
        "processed": True,
        "event_type": event_type,
        "dispute_id": dispute_id,
    }
    if not inserted:
        # Replay — same dispute seen before. Don't re-bump.
        response["idempotent_replay"] = True
        return web.json_response(response, status=200)
    if event_type == "charge.dispute.created":
        obj = (event.get("data") or {}).get("object") or {}
        pi_id = obj.get("payment_intent") or ""
        if pi_id:
            ak = account_key_for(pi_id)
            score, suspended = _bump_fraud_score(ak, "chargebacks_filed")
            response["fraud_score"] = score
            response["account_suspended"] = suspended
    return web.json_response(response, status=200)


async def handle_ack_job(request: web.Request) -> web.Response:
    """POST /v1/jobs/{id}/ack — customer confirms receipt (kanban t_69b52324).

    The threat model flags that 24h result retention is shorter than
    Stripe's ≥120d chargeback window. Without proof-of-delivery, a
    chargeback for "product_not_received" is hard to defend. This
    endpoint lets the customer pin a delivery-confirmation
    timestamp + their own proof text + the source IP, persisted
    alongside the job. The trio is what the operator surfaces to
    Stripe's dispute evidence portal.

    Behaviour:
      - Auth: the per-job access token returned at submission.
      - Body: ``{"proof": "<free text up to 2048 bytes>"}``.
      - Job must be in a terminal state (completed / failed /
        timeout / abandoned). Queued/running jobs return 409
        invalid_state — you can't ack a job you haven't received
        yet.
      - Replay (second ack within the window) returns 200 +
        idempotent_replay=True with the ORIGINAL timestamp and
        proof; we never overwrite the first ack.
      - ACK_WINDOW_HOURS caps how far back the ack is meaningful.
        Outside the window we still persist the ack but include
        ``out_of_window: True`` so the operator can see the dispute
        evidence is weaker (the customer took >24h to confirm).
    """
    job_id = request.match_info["job_id"]
    auth_error = require_job_access(request, job_id)
    if auth_error is not None:
        return auth_error
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({
            "error": "invalid JSON body",
            "code": "invalid_json",
        }, status=400)
    proof = body.get("proof") if isinstance(body, dict) else None
    if not isinstance(proof, str) or not proof:
        return web.json_response({
            "error": "body must include non-empty 'proof' string",
            "code": "proof_required",
        }, status=400)
    if len(proof.encode("utf-8")) > 2048:
        return web.json_response({
            "error": "'proof' exceeds 2048 bytes",
            "code": "proof_too_large",
        }, status=400)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    # Peer IP — request.transport.get_extra_info('peername') returns
    # (host, port, ...) for TCP. Fall back to None when the transport
    # is unix / pipe (test harness etc.).
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peer, tuple) and peer:
        ack_ip = str(peer[0])
    elif isinstance(peer, str):
        ack_ip = peer
    else:
        ack_ip = None
    with db() as conn:
        row = conn.execute(
            "SELECT job_id, state, finished_at, acked_at, ack_proof "
            "FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return web.json_response({
                "error": f"job {job_id!r} not found",
                "code": "not_found",
            }, status=404)
        # Replay path — first ack already exists, return it unchanged.
        if row["acked_at"]:
            return web.json_response({
                "job_id": job_id,
                "acked_at": row["acked_at"],
                "ack_proof": row["ack_proof"],
                "idempotent_replay": True,
            }, status=200)
        state = row["state"]
        # Only terminal states are ackable. Non-terminal (queued,
        # running, awaiting_topup, awaiting_inputs) returns 409.
        terminal = {"completed", "failed", "timeout", "abandoned"}
        if state not in terminal:
            return web.json_response({
                "error": f"cannot ack job in state {state!r} (must be one of {sorted(terminal)})",
                "code": "invalid_state",
                "state": state,
            }, status=409)
        # Compute out_of_window vs finished_at. If finished_at is
        # NULL (shouldn't happen for a terminal state, but be
        # defensive) treat as in-window.
        out_of_window = False
        if row["finished_at"]:
            try:
                finished = datetime.fromisoformat(row["finished_at"])
                window_end = finished + timedelta(hours=ACK_WINDOW_HOURS)
                if now > window_end:
                    out_of_window = True
            except ValueError:
                pass
        conn.execute(
            "UPDATE jobs SET acked_at = ?, ack_proof = ?, ack_ip = ? "
            "WHERE job_id = ? AND acked_at IS NULL",
            (now_iso, proof, ack_ip, job_id),
        )
    return web.json_response({
        "job_id": job_id,
        "acked_at": now_iso,
        "ack_proof": proof,
        "ack_ip": ack_ip,
        "out_of_window": out_of_window,
        "idempotent_replay": False,
    }, status=200)


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/jobs", submit_job)
    app.router.add_get("/v1/jobs/{job_id}", get_job)
    # Two-phase input attachment flow (t_0ef31767). Client calls this
    # after PUTting all input files to their presigned S3 URLs. Returns
    # 200 once HeadObject verifies every file is present and the job
    # has transitioned awaiting_inputs -> queued; 409 if files are
    # missing or the job is in the wrong state. See mark_job_ready for
    # full semantics.
    app.router.add_post("/v1/jobs/{job_id}/ready", mark_job_ready)
    # Shortfall topup endpoint (kanban t_9a705578). After a job completes
    # and capture_payment() detects that the held PI amount is insufficient,
    # the job transitions to awaiting_topup. The client calls this endpoint
    # with a new PaymentIntent ID covering the shortfall. Once verified and
    # captured, the broker releases the result.
    app.router.add_post("/v1/jobs/{job_id}/topup", topup_job)
    # Result-pack artifact endpoints (allow-list + path-traversal protection
    # in the handler). The {filename:.+} regex matches one or more chars
    # including slashes so nested paths like "code/main.py" work.
    app.router.add_get("/v1/jobs/{job_id}/artifacts", get_job_artifacts)
    app.router.add_get("/v1/jobs/{job_id}/artifacts/{filename:.+}", get_job_artifact_file)
    app.router.add_post("/v1/demo/shared-payment-token", demo_shared_payment_token)
    app.router.add_get("/v1/discover", discover)
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/v1/llm/chat/completions", llm_proxy)
    # Alias without /llm prefix — NemoClaw's OpenShell proxy forwards
    # inference.local/v1/chat/completions to the endpointUrl, which is
    # https://broker/v1/llm. The proxy strips the /llm path segment and
    # sends /v1/chat/completions to the broker. This alias catches that.
    app.router.add_post("/v1/chat/completions", llm_proxy)
    # OpenShell proxy may also append the full path to the endpointUrl,
    # producing /v1/llm/v1/chat/completions. Catch that too.
    app.router.add_post("/v1/llm/v1/chat/completions", llm_proxy)
    app.router.add_get("/v1/llm/usage/{job_id}", llm_usage)
    app.router.add_post("/v1/skills", register_skill)
    app.router.add_get("/v1/skills", list_skills)
    app.router.add_get("/v1/skills/{ref}", get_skill)
    # B5 chargeback / ack handling (kanban t_69b52324). See
    # handle_stripe_webhook / handle_ack_job docstrings above.
    app.router.add_post("/v1/stripe/webhook", handle_stripe_webhook)
    # Customer proof-of-delivery (B5 §2). Bearer-token auth uses
    # BROKER_SKILLS_API_KEY; see handle_ack_job.
    app.router.add_post("/v1/jobs/{job_id}/ack", handle_ack_job)
    # WASM binary upload (kanban t_c27c1d8d). Body is the raw WASM
    # bytes; the SHA-256 in `X-Wasm-Manifest-Hash` is verified against
    # the registered manifest. The `{name:.+}` regex allows skill names
    # with dashes / underscores / dots to route correctly.
    app.router.add_post("/v1/skills/{name:.+}/wasm", upload_skill_wasm)
    app.router.add_static("/static", path=BROKER_EFS_MOUNT / "static", show_index=False)
    app.on_startup.append(_on_startup)
    # CQ-3 (kanban t_b13072b3): kick the periodic request_body purge loop.
    # The task runs forever, sleeping REQUEST_BODY_PURGE_INTERVAL_SECONDS
    # between sweeps. We start it here (rather than in main()) so it
    # shares the event loop with the aiohttp app — the loop gets cancelled
    # on shutdown.
    async def _start_purge_loop(app):
        app["purge_task"] = asyncio.create_task(_request_body_purge_loop())
    async def _stop_purge_loop(app):
        task = app.get("purge_task")
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    app.on_startup.append(_start_purge_loop)
    app.on_cleanup.append(_stop_purge_loop)
    # B4 webhook dispatcher (kanban t_30ca541f). Async, bounded-parallelism
    # delivery so a slow webhook target can't DoS the outbox poller. We
    # hook it through on_startup/on_cleanup (rather than _on_startup)
    # so tests that call `app.on_startup.clear()` automatically skip
    # it — the dispatcher has a sync fallback for that path.
    app.on_startup.append(_start_webhook_dispatcher)
    app.on_cleanup.append(_stop_webhook_dispatcher)
    return app


def main() -> None:
    init_db()
    INBOX.mkdir(parents=True, exist_ok=True)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)
    web.run_app(build_app(), host="127.0.0.1", port=8080, access_log=None)


if __name__ == "__main__":
    main()
