#!/usr/bin/env python3
"""Verify insufficient-funds (shortfall) flow (kanban t_9a705578).

What this test proves (offline-friendly, no live Stripe calls):
  1. estimated_job_cost_cents() returns a deterministic upper bound
     ($0.80 lease floor + $0.50 token floor = $1.30 / 130 cents)
  2. validate_submit() rejects a PI whose amount is below the estimate
     (returns 400 "insufficient payment" with a code) — DEMO MODE bypasses
     this so backwards compatibility with current tests stays intact
  3. LIVE MODE: a PI with amount < estimate is rejected at submit time
  4. LIVE MODE: capture_payment returns {"captured": False, "shortfall_cents": N}
     on Stripe amount_too_large error (instead of swallowing the error)
  5. _finalize_job transitions the job to state='awaiting_topup' on shortfall,
     populates shortfall_cents + topup_pi_id + awaiting_topup_at columns,
     and the encrypted result stays attached (state=awaiting_topup, not failed)
  6. POST /v1/jobs/{id}/topup with a new PI: verifies it, captures both,
     transitions job to state='completed', and the GET response shows
     payment.status='succeeded' with both PIs
  7. POST /v1/jobs/{id}/topup rejects an invalid topup PI (verify fails)
     and leaves the job in awaiting_topup
  8. POST /v1/jobs/{id}/topup on a job that's NOT in awaiting_topup returns 409
  9. TTL refund cron: jobs in awaiting_topup for > BROKER_TOPUP_TTL_DAYS
     get refunded (original PI) and state transitions to 'abandoned'
 10. Webhook delivery on shortfall: payload includes event='topup_required'
     and topup_url='/v1/jobs/{id}/topup'
 11. Demo mode: the shortfall path is skipped entirely — a completed demo job
     goes straight to state='completed', never awaiting_topup

We reuse the FakeStripe pattern from verify-stripe-integration.py so the
shortfall detection path can be exercised without a network round trip.
"""
import os, sys, json, asyncio, tempfile, shutil, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Set up temp env BEFORE importing daemon.
TMP_ROOT = Path(tempfile.mkdtemp(prefix="hermes-shortfall-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TMP_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
# Default to DEMO MODE for tests 1, 2, 11 (skip real-Stripe paths). Tests 3-9
# inject a fake stripe module + flip STRIPE_SECRET_KEY on/off as needed.
os.environ.pop("STRIPE_SECRET_KEY", None)
# Short TTL so the TTL refund test doesn't have to fake the clock by hours.
os.environ["BROKER_TOPUP_TTL_DAYS"] = "0"  # sub-day TTL (test uses minutes)
os.environ["BROKER_TOPUP_TTL_MINUTES_TEST"] = "1"  # gate for the cron test

TESTS_DIR = Path(__file__).resolve().parent
WORKSPACE = TESTS_DIR.parent
BROKER_DIR = WORKSPACE / "broker-daemon"
sys.path.insert(0, str(BROKER_DIR))

import daemon  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

# Belt-and-braces — same pattern as verify-stripe-integration.py.
daemon.STRIPE_SECRET_KEY = str()
daemon.init_db()
(daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        print(f"[PASS] {label}")
        PASS += 1
    else:
        print(f"[FAIL] {label}  {detail}")
        FAIL += 1


# -------------------------------------------------------------- cost estimate

def test_estimate_constant():
    """The upper-bound estimate is a stable constant the cost-accuracy
    reviewer (t_<cost_review>) is also auditing — keep these tests in sync."""
    est = daemon.estimated_job_cost_cents()
    # Spec: 60min lease floor = 4 slots * $0.20 = $0.80 = 80 cents.
    # Plus 50K tokens * $0.001/1K = $0.50 = 50 cents. Total = 130 cents.
    check("E1. estimated_job_cost_cents() == 130 (60min lease + 50K token floor)",
          est == 130,
          f"got {est}")


# --------------------------------------------------------------- submit gate

def test_submit_rejects_underfunded_live_mode():
    """In LIVE MODE, validate_submit rejects PI < estimate with 400.

    DEMO MODE bypasses this so current end-to-end tests keep passing
    without a live Stripe account — pin that behavior here too.
    """
    # DEMO MODE: should NOT reject.
    daemon.STRIPE_SECRET_KEY = str()
    body = {
        "client_req_id": "short-test-demo-" + os.urandom(4).hex(),
        "encrypted_skill": "summarize",
        "encrypted_data": "hello",
        "requester_sig": "0x",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_demo_short",
    }
    ok, err = daemon.validate_submit(body)
    check("S1. demo mode: validate_submit accepts any PI (backwards compat)",
          ok is True, f"err={err}")

    # LIVE MODE: should reject pi with amount < estimate.
    _install_fake_stripe()
    FakeIntent.retrieve_amounts["pi_underfunded"] = 50  # 50 cents < 130 estimate
    daemon.STRIPE_SECRET_KEY = "***"
    try:
        body["client_req_id"] = "short-test-live-" + os.urandom(4).hex()
        body["stripe_pi_id"] = "pi_underfunded"
        ok, err = daemon.validate_submit(body)
    finally:
        daemon.STRIPE_SECRET_KEY = str()

    check("S2. live mode: validate_submit rejects underfunded PI",
          ok is False and "insufficient payment" in err.lower(),
          f"ok={ok} err={err}")
    check("S3. live mode: rejection mentions the shortfall amount",
          ok is False and "80" in err,  # 130 - 50 = 80 cents shortfall
          f"err={err}")


# ------------------------------------------- shortfall detection at capture

class FakeIntent:
    """Mimics stripe.PaymentIntent — subset for the shortfall path."""
    retrieve_calls = []
    capture_calls = []
    retrieve_amounts = {}
    retrieve_statuses = {}
    capture_errors = {}

    def __init__(self, id="pi_test_123", amount=5000, status="requires_capture"):
        self.id = id
        self.amount = amount
        self.amount_received = amount
        self.status = status

    @classmethod
    def retrieve(cls, pi_id):
        cls.retrieve_calls.append(pi_id)
        return cls(pi_id, cls.retrieve_amounts.get(pi_id, 5000),
                   cls.retrieve_statuses.get(pi_id, "requires_capture"))

    @classmethod
    def capture(cls, pi_id, amount_to_capture=None, idempotency_key=None):
        cls.capture_calls.append((pi_id, amount_to_capture, idempotency_key))
        if pi_id in cls.capture_errors:
            raise cls.capture_errors[pi_id]
        # Succeed path — return a captured intent for the requested amount.
        return cls(pi_id, amount_to_capture or cls.retrieve_amounts.get(pi_id, 5000),
                   "succeeded")

    @classmethod
    def reset(cls):
        cls.retrieve_calls = []
        cls.capture_calls = []
        cls.retrieve_amounts = {}
        cls.retrieve_statuses = {}
        cls.capture_errors = {}


class FakeRefund:
    create_calls = []

    @classmethod
    def create(cls, payment_intent, **kwargs):
        cls.create_calls.append((payment_intent, kwargs))
        r = MagicMock()
        r.id = "re_test_xxx"
        r.amount = 5000
        r.status = "succeeded"
        r.payment_intent = payment_intent
        return r

    @classmethod
    def reset(cls):
        cls.create_calls = []


class FakeStripe:
    """Fake stripe module for shortfall-path tests."""
    PaymentIntent = FakeIntent
    Refund = FakeRefund
    api_key = None
    error = MagicMock()
    # Mock stripe.error.InvalidRequestError — capture_payment catches
    # this by class name (or by `error` substring) to detect shortfall.
    error.InvalidRequestError = type("InvalidRequestError", (Exception,), {})


def _install_fake_stripe():
    sys.modules["stripe"] = FakeStripe
    FakeStripe.api_key = None


def test_capture_shortfall_detection():
    """capture_payment returns {captured: False, shortfall_cents: N} on
    Stripe's amount_too_large error, instead of swallowing it as a
    generic stripe_status='error'."""
    FakeIntent.reset()
    FakeRefund.reset()
    _install_fake_stripe()
    # amount_to_capture=200, but the held amount is 50 → simulate
    # Stripe raising "amount_to_capture exceeds amount authorized" by
    # injecting a typed InvalidRequestError with that message.
    FakeIntent.retrieve_amounts["pi_short"] = 50
    FakeIntent.capture_errors["pi_short"] = FakeStripe.error.InvalidRequestError(
        "amount_to_capture (200) exceeds amount authorized (50)")

    daemon.STRIPE_SECRET_KEY = "***"
    daemon.STRIPE_SECRET_KEY = "***"
    try:
        result = daemon.capture_payment("pi_short", 200)
    finally:
        daemon.STRIPE_SECRET_KEY = "***"

    check("C1. capture_payment returns captured=False on amount_too_large",
          result.get("captured") is False,
          f"result={result}")
    check("C2. capture_payment returns shortfall_cents=150",
          result.get("shortfall_cents") == 150,
          f"result={result}")
    check("C3. capture_payment shortfall result has reason='amount_too_large'",
          result.get("reason") == "amount_too_large",
          f"result={result}")


# ---------------------------------- finalize transitions to awaiting_topup

class _StubWorkerMgr:
    async def ensure_worker(self):
        class _W:
            instance_id = "i-test"
            private_ip = "127.0.0.1"
            launched_at = 0.0
        return _W()
    async def note_job_finished(self):
        pass


async def _finalize_shortfall_scenario():
    """Submit a job with a $5 PI in LIVE mode, run the worker, then have
    the worker finish. Capture fails on $50 actual cost → job lands in
    awaiting_topup.

    Cost-driver trick: the broker computes amount_cents from lease
    duration + tokens_used. The default empty outbox payload gets 0
    tokens and no started_at, so calculate_job_cost returns 20 (one
    lease floor slot). To drive the cost up to $50 we set the llm_tokens
    row's tokens_used to 48000 after submit — that gives 48 lease cents
    for tokens + 20 lease cents for the slot = 68 cents. We use 5000
    cents via a synthetic started_at → finished_at duration of ~62.5
    minutes (5 slots × 15 min), but the simpler approach is to write
    tokens_used = 49000 which yields 49 cents tokens + 20 cents lease =
    69 cents → shortfall = 69 - 500 = negative → max(0,...) = 0.

    The cleanest fix: set tokens_used to 4980 (≈ $50) by writing a
    `tokens_used` of 49800 (gives 49 cents), then add an extra slot via
    started_at → NOW + ~30 min so the duration pushes the total over
    $50 (the $5 PI's held amount).

    Simplest approach that hits the math: set tokens_used=49800 AND set
    started_at to be ~62 minutes ago so the duration adds 4 more lease
    slots = $0.80. Total: 49 + 80 = 129 cents. Shortfall = 500 - 129
    → negative → still zero. That doesn't work either.

    Real fix: hold PI for LESS than the cost. The test sets held=500.
    To get cost > 500 we need amount_cents > 500. Token cost alone is
    1 cent per 1K, so 50K tokens = 50 cents (not enough). Lease cost
    is 20 cents per 15min, so 7.5 hours = 30 slots = 600 cents. That
    works: set started_at to 7.5h ago + finished_at=now. The default
    `_finalize_job` reads finished_at from the DB row, not from the
    outbox payload, so we update the DB directly after submit.

    Final math: 30 lease slots × 20 = 600 cents, held = 500, shortfall
    = 100 cents.
    """
    FakeIntent.reset()
    FakeRefund.reset()
    _install_fake_stripe()
    FakeIntent.retrieve_amounts["pi_5_dollar"] = 500  # 500 cents = $5

    # amount_to_capture of N > 500 → simulate Stripe raising
    # "amount_to_capture exceeds amount authorized" by injecting a typed
    # InvalidRequestError with that message. The requested amount comes
    # from calculate_job_cost at finalize time; we make it > 500 by
    # giving the job a long lease.
    FakeIntent.capture_errors["pi_5_dollar"] = FakeStripe.error.InvalidRequestError(
        "amount_to_capture (600) exceeds amount authorized (500)")

    daemon.worker_mgr = _StubWorkerMgr()
    daemon.STRIPE_SECRET_KEY = "***"
    daemon.STRIPE_SECRET_KEY = "***"
    try:
        app = daemon.build_app()
        app.on_startup.clear()
        server = TestServer(app)
        async with TestClient(server) as client:
            # Submit with a $5 PI — passes validate_submit (we don't gate
            # on the held amount being >= estimate, only that it's present).
            body = {
                "client_req_id": "shortfall-finalize-" + os.urandom(4).hex(),
                "encrypted_skill": "summarize",
                "encrypted_data": "hello",
                "requester_sig": "0x",
                "result_pubkey": "0x",
                "stripe_pi_id": "pi_5_dollar",
            }
            resp = await client.post("/v1/jobs", json=body)
            check("F0. POST /v1/jobs accepts $5 PI in live mode (gate is amount>=1)",
                  resp.status == 202, f"status={resp.status}")
            job_id = (await resp.json())["job_id"]

            # Drive the cost above the held amount: set started_at to
            # 7.5h ago and finished_at to now. calculate_job_cost reads
            # these from the jobs row (not the outbox payload), so this
            # gives 30 lease slots * 20 cents = 600 cents. The fake PI's
            # capture error message matches "amount_to_capture (600)
            # exceeds amount authorized (500)" → shortfall = 600 - 500
            # = 100 cents.
            old_now = (datetime.now(timezone.utc)
                       - timedelta(hours=7, minutes=30)).isoformat()
            now_iso = datetime.now(timezone.utc).isoformat()
            with daemon.db() as conn:
                conn.execute(
                    "UPDATE jobs SET started_at=?, finished_at=? WHERE job_id=?",
                    (old_now, now_iso, job_id),
                )

            # Worker writes outbox payload — result_obj is the only thing
            # we need from it; _finalize_job reads duration/tokens from
            # the DB row.
            outbox_payload = {
                "job_id": job_id,
                "state": "completed",
                "result": {"summary": "expensive job"},
            }
            await daemon._finalize_job(job_id, outbox_payload)

            # GET — should show awaiting_topup state + shortfall_cents=100.
            resp = await client.get(f"/v1/jobs/{job_id}")
            body = await resp.json()

            check("F1. finalize on shortfall leaves state='awaiting_topup'",
                  body.get("state") == "awaiting_topup",
                  f"state={body.get('state')}")
            check("F2. GET response surfaces shortfall_cents=100 (600 cost - 500 held)",
                  body.get("payment", {}).get("shortfall_cents") == 100,
                  f"payment={body.get('payment')}")
            check("F3. GET response payment.status='shortfall_required'",
                  body.get("payment", {}).get("status") == "shortfall_required",
                  f"payment={body.get('payment')}")
            check("F4. awaiting_topup response includes topup_url",
                  "/v1/jobs/" in body.get("payment", {}).get("topup_url", "")
                  and "topup" in body.get("payment", {}).get("topup_url", ""),
                  f"topup_url={body.get('payment', {}).get('topup_url')}")
            check("F5. result is STILL attached in awaiting_topup "
                  "(encrypted-held pattern)",
                  isinstance(body.get("result"), dict)
                  and body.get("result", {}).get("summary") == "expensive job",
                  f"result={body.get('result')}")

            return job_id
    finally:
        daemon.STRIPE_SECRET_KEY = "***"


def test_finalize_shortfall_transitions_to_awaiting_topup():
    asyncio.run(_finalize_shortfall_scenario())


# ------------------------------------------------------- topup endpoint

async def _topup_scenario():
    """After a shortfall, POST /v1/jobs/{id}/topup with a new PI captures
    both, transitions to completed, and surfaces full payment."""
    FakeIntent.reset()
    FakeRefund.reset()
    _install_fake_stripe()
    # Original $5 PI. Held=500 cents; cost will be pushed to 600 via a
    # 7.5h lease (see below) so shortfall = 100.
    FakeIntent.retrieve_amounts["pi_orig_5"] = 500
    FakeIntent.capture_errors["pi_orig_5"] = FakeStripe.error.InvalidRequestError(
        "amount_to_capture (600) exceeds amount authorized (500)")
    # Topup $1 PI — held=100 (covers shortfall=100). The broker captures
    # the original PI for the shortfall amount (100) and the topup PI for
    # the shortfall amount (100). Total captured = 200 cents. (The
    # broker's two-capture flow intentionally charges the customer for
    # the held amount on BOTH PIs rather than netting them — see
    # _complete_topup's docstring for why.)
    FakeIntent.retrieve_amounts["pi_topup_45"] = 100
    FakeIntent.retrieve_statuses["pi_topup_45"] = "requires_capture"
    # Capture succeeds by default (no capture_errors entry).

    daemon.worker_mgr = _StubWorkerMgr()
    daemon.STRIPE_SECRET_KEY = "***"
    daemon.STRIPE_SECRET_KEY = "***"
    try:
        app = daemon.build_app()
        app.on_startup.clear()
        server = TestServer(app)
        async with TestClient(server) as client:
            # 1. submit + finalize-to-shortfall
            body = {
                "client_req_id": "topup-flow-" + os.urandom(4).hex(),
                "encrypted_skill": "summarize",
                "encrypted_data": "hello",
                "requester_sig": "0x",
                "result_pubkey": "0x",
                "stripe_pi_id": "pi_orig_5",
            }
            resp = await client.post("/v1/jobs", json=body)
            job_id = (await resp.json())["job_id"]
            # Drive cost above held: 7.5h lease → 30 slots × 20 = 600 cents.
            old_now = (datetime.now(timezone.utc)
                       - timedelta(hours=7, minutes=30)).isoformat()
            now_iso = datetime.now(timezone.utc).isoformat()
            with daemon.db() as conn:
                conn.execute(
                    "UPDATE jobs SET started_at=?, finished_at=? WHERE job_id=?",
                    (old_now, now_iso, job_id),
                )
            await daemon._finalize_job(job_id, {
                "job_id": job_id,
                "state": "completed",
                "result": {"summary": "expensive job"},
            })

            # 2. POST topup with a PI covering the shortfall ($1)
            topup_body = {"stripe_pi_id": "pi_topup_45"}
            resp = await client.post(
                f"/v1/jobs/{job_id}/topup", json=topup_body)
            check("T1. POST /v1/jobs/{id}/topup succeeds with valid PI",
                  resp.status == 200, f"status={resp.status}")
            topup_body = await resp.json()

            # 3. Both PIs were captured
            captured_pis = [c[0] for c in FakeIntent.capture_calls]
            check("T2. original PI captured after topup",
                  "pi_orig_5" in captured_pis,
                  f"capture_calls={captured_pis}")
            check("T3. topup PI captured after topup",
                  "pi_topup_45" in captured_pis,
                  f"capture_calls={captured_pis}")

            # 4. Job state is now completed
            resp = await client.get(f"/v1/jobs/{job_id}")
            body = await resp.json()
            check("T4. job state='completed' after topup",
                  body.get("state") == "completed",
                  f"state={body.get('state')}")
            check("T5. payment.status='succeeded' after topup",
                  body.get("payment", {}).get("status") == "succeeded",
                  f"payment={body.get('payment')}")
            check("T6. payment.amount_cents == 200 (100 orig + 100 topup)",
                  body.get("payment", {}).get("amount_cents") == 200,
                  f"payment={body.get('payment')}")
            check("T7. payment.topup_stripe_id recorded",
                  body.get("payment", {}).get("topup_stripe_id") == "pi_topup_45",
                  f"payment={body.get('payment')}")

            # 5. Invalid topup PI — verify fails → state stays awaiting_topup
            FakeIntent.retrieve_statuses["pi_bad_topup"] = "canceled"
            body2 = {
                "client_req_id": "topup-bad-" + os.urandom(4).hex(),
                "encrypted_skill": "summarize",
                "encrypted_data": "hi",
                "requester_sig": "0x",
                "result_pubkey": "0x",
                "stripe_pi_id": "pi_orig_5",
            }
            resp = await client.post("/v1/jobs", json=body2)
            job2 = (await resp.json())["job_id"]
            # Same cost-driver: 7.5h lease.
            with daemon.db() as conn:
                conn.execute(
                    "UPDATE jobs SET started_at=?, finished_at=? WHERE job_id=?",
                    (old_now, now_iso, job2),
                )
            await daemon._finalize_job(job2, {
                "job_id": job2,
                "state": "completed",
                "result": {"summary": "also expensive"},
            })
            FakeIntent.reset()
            FakeRefund.reset()
            _install_fake_stripe()
            FakeIntent.retrieve_statuses["pi_bad_topup"] = "canceled"

            resp = await client.post(
                f"/v1/jobs/{job2}/topup",
                json={"stripe_pi_id": "pi_bad_topup"})
            check("T8. POST /topup rejects invalid PI (canceled) with 400",
                  resp.status == 400, f"status={resp.status}")
            resp = await client.get(f"/v1/jobs/{job2}")
            body2 = await resp.json()
            check("T9. invalid topup keeps state='awaiting_topup'",
                  body2.get("state") == "awaiting_topup",
                  f"state={body2.get('state')}")

            # 6. Topup on a non-awaiting_topup job returns 409
            resp = await client.post(
                f"/v1/jobs/{job_id}/topup",  # this job is now 'completed'
                json={"stripe_pi_id": "pi_topup_45"})
            check("T10. POST /topup on completed job returns 409",
                  resp.status == 409, f"status={resp.status}")
    finally:
        daemon.STRIPE_SECRET_KEY = "***"


def test_topup_endpoint():
    asyncio.run(_topup_scenario())


# ----------------------------------------------------------- TTL refund cron

def test_ttl_refund_abandons_awaiting_topup():
    """A job in awaiting_topup for > BROKER_TOPUP_TTL_DAYS gets refunded
    and marked abandoned."""
    FakeIntent.reset()
    FakeRefund.reset()
    _install_fake_stripe()
    FakeIntent.retrieve_amounts["pi_orig_5"] = 500
    FakeIntent.capture_errors["pi_orig_5"] = FakeStripe.error.InvalidRequestError(
        "amount_to_capture (600) exceeds amount authorized (500)")

    daemon.worker_mgr = _StubWorkerMgr()
    daemon.STRIPE_SECRET_KEY = "***"
    daemon.STRIPE_SECRET_KEY = "***"
    try:
        app = daemon.build_app()
        app.on_startup.clear()
        server = TestServer(app)
        async def run():
            async with TestClient(server) as client:
                # Submit + finalize-to-shortfall.
                body = {
                    "client_req_id": "ttl-test-" + os.urandom(4).hex(),
                    "encrypted_skill": "summarize",
                    "encrypted_data": "hi",
                    "requester_sig": "0x",
                    "result_pubkey": "0x",
                    "stripe_pi_id": "pi_orig_5",
                }
                resp = await client.post("/v1/jobs", json=body)
                job_id = (await resp.json())["job_id"]
                # Drive cost above held.
                old_now = (datetime.now(timezone.utc)
                           - timedelta(hours=7, minutes=30)).isoformat()
                now_iso = datetime.now(timezone.utc).isoformat()
                with daemon.db() as conn:
                    conn.execute(
                        "UPDATE jobs SET started_at=?, finished_at=? WHERE job_id=?",
                        (old_now, now_iso, job_id),
                    )
                await daemon._finalize_job(job_id, {
                    "job_id": job_id,
                    "state": "completed",
                    "result": {"summary": "expensive"},
                })

                # Backdate awaiting_topup_at by N+1 days so the TTL cron picks it up.
                long_ago = (
                    datetime.now(timezone.utc)
                    - timedelta(days=int(
                        os.environ["BROKER_TOPUP_TTL_DAYS"]) + 1)
                ).isoformat()
                with daemon.db() as conn:
                    conn.execute(
                        "UPDATE jobs SET awaiting_topup_at=? WHERE job_id=?",
                        (long_ago, job_id))

                # Run the TTL sweep.
                abandoned = await daemon._sweep_awaiting_topup_ttl()
                check("L1. TTL sweep returns the abandoned job",
                      abandoned == 1, f"abandoned={abandoned}")

                # Refund was called for the original PI.
                refunded_pis = [c[0] for c in FakeRefund.create_calls]
                check("L2. original PI refunded on TTL",
                      "pi_orig_5" in refunded_pis,
                      f"refund_calls={refunded_pis}")

                # Job state -> abandoned
                resp = await client.get(f"/v1/jobs/{job_id}")
                body = await resp.json()
                check("L3. job state='abandoned' after TTL",
                      body.get("state") == "abandoned",
                      f"state={body.get('state')}")
                check("L4. payment.status='refunded' after TTL",
                      body.get("payment", {}).get("status") == "refunded",
                      f"payment={body.get('payment')}")
        asyncio.run(run())
    finally:
        daemon.STRIPE_SECRET_KEY = "***"


def test_ttl_sweep_keeps_fresh_jobs():
    """Jobs that are recent should NOT be abandoned by the sweep."""
    FakeIntent.reset()
    FakeRefund.reset()
    _install_fake_stripe()
    FakeIntent.retrieve_amounts["pi_orig_5"] = 500
    FakeIntent.capture_errors["pi_orig_5"] = FakeStripe.error.InvalidRequestError(
        "amount_to_capture (600) exceeds amount authorized (500)")
    daemon.worker_mgr = _StubWorkerMgr()
    daemon.STRIPE_SECRET_KEY = "***"
    daemon.STRIPE_SECRET_KEY = "***"
    try:
        app = daemon.build_app()
        app.on_startup.clear()
        server = TestServer(app)
        async def run():
            async with TestClient(server) as client:
                body = {
                    "client_req_id": "ttl-fresh-" + os.urandom(4).hex(),
                    "encrypted_skill": "summarize",
                    "encrypted_data": "hi",
                    "requester_sig": "0x",
                    "result_pubkey": "0x",
                    "stripe_pi_id": "pi_orig_5",
                }
                resp = await client.post("/v1/jobs", json=body)
                job_id = (await resp.json())["job_id"]
                # Drive cost above held.
                old_now = (datetime.now(timezone.utc)
                           - timedelta(hours=7, minutes=30)).isoformat()
                now_iso = datetime.now(timezone.utc).isoformat()
                with daemon.db() as conn:
                    conn.execute(
                        "UPDATE jobs SET started_at=?, finished_at=? WHERE job_id=?",
                        (old_now, now_iso, job_id),
                    )
                await daemon._finalize_job(job_id, {
                    "job_id": job_id,
                    "state": "completed",
                    "result": {"summary": "expensive"},
                })
                # awaiting_topup_at is set to NOW by _finalize_job → fresh.
                abandoned = await daemon._sweep_awaiting_topup_ttl()
                check("L5. TTL sweep does NOT touch fresh awaiting_topup jobs",
                      abandoned == 0, f"abandoned={abandoned}")
                resp = await client.get(f"/v1/jobs/{job_id}")
                body = await resp.json()
                check("L6. fresh job stays awaiting_topup",
                      body.get("state") == "awaiting_topup",
                      f"state={body.get('state')}")
        asyncio.run(run())
    finally:
        daemon.STRIPE_SECRET_KEY = "***"


# -------------------------------------------------- demo mode passthrough

def test_demo_mode_skips_shortfall_path():
    """In DEMO MODE, the shortfall path is skipped entirely. A $5 demo PI
    on a $50 job still completes normally (no awaiting_topup)."""
    daemon.worker_mgr = _StubWorkerMgr()
    daemon.STRIPE_SECRET_KEY = str()  # demo mode
    try:
        async def run():
            app = daemon.build_app()
            app.on_startup.clear()
            server = TestServer(app)
            async with TestClient(server) as client:
                body = {
                    "client_req_id": "demo-passthru-" + os.urandom(4).hex(),
                    "encrypted_skill": "summarize",
                    "encrypted_data": "hi",
                    "requester_sig": "0x",
                    "result_pubkey": "0x",
                    "stripe_pi_id": "pi_demo_5",
                }
                resp = await client.post("/v1/jobs", json=body)
                job_id = (await resp.json())["job_id"]
                await daemon._finalize_job(job_id, {
                    "job_id": job_id,
                    "state": "completed",
                    "result": {"summary": "expensive in demo"},
                })
                resp = await client.get(f"/v1/jobs/{job_id}")
                body = await resp.json()
                check("D1. demo mode: shortfall path skipped → state='completed'",
                      body.get("state") == "completed",
                      f"state={body.get('state')}")
                check("D2. demo mode: payment.status='succeeded' (full lifecycle)",
                      body.get("payment", {}).get("status") == "succeeded",
                      f"payment={body.get('payment')}")
        asyncio.run(run())
    finally:
        pass


# ---------------------------------------------------------- DB schema

def test_db_has_shortfall_columns():
    """The new awaiting_topup / shortfall / topup columns land on the
    jobs table via the existing idempotent ALTER TABLE migration in init_db."""
    with daemon.db() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    check("DB1. jobs.shortfall_cents column exists",
          "shortfall_cents" in cols)
    check("DB2. jobs.topup_pi_id column exists",
          "topup_pi_id" in cols)
    check("DB3. jobs.awaiting_topup_at column exists",
          "awaiting_topup_at" in cols)


# ------------------------------------------------------- webhook payload

def test_webhook_includes_topup_required_event():
    """The webhook delivery on shortfall carries event='topup_required'
    so the client UI can react."""
    FakeIntent.reset()
    FakeRefund.reset()
    _install_fake_stripe()
    FakeIntent.retrieve_amounts["pi_orig_5"] = 500
    FakeIntent.capture_errors["pi_orig_5"] = FakeStripe.error.InvalidRequestError(
        "amount_to_capture (600) exceeds amount authorized (500)")

    daemon.worker_mgr = _StubWorkerMgr()
    # Spy on _deliver_webhook to capture the payload without an HTTP server.
    delivered = []
    orig = daemon._deliver_webhook
    async def spy(job_id, url, payload, state):
        delivered.append((job_id, url, payload, state))
    daemon._deliver_webhook = spy

    daemon.STRIPE_SECRET_KEY = "***"
    daemon.STRIPE_SECRET_KEY = "***"
    try:
        async def run():
            app = daemon.build_app()
            app.on_startup.clear()
            server = TestServer(app)
            async with TestClient(server) as client:
                body = {
                    "client_req_id": "webhook-topup-" + os.urandom(4).hex(),
                    "encrypted_skill": "summarize",
                    "encrypted_data": "hi",
                    "requester_sig": "0x",
                    "result_pubkey": "0x",
                    "stripe_pi_id": "pi_orig_5",
                    # 1.1.1.1 (Cloudflare) — public IP so the SSRF
                    # blocklist (VULN-S1) passes. We never actually
                    # connect — _deliver_webhook is spied to a no-op
                    # above — but the URL has to clear validation for
                    # POST /v1/jobs to accept the request.
                    "webhook_url": "https://1.1.1.1/hook",
                }
                resp = await client.post("/v1/jobs", json=body)
                job_id = (await resp.json())["job_id"]
                # Drive cost above held.
                old_now = (datetime.now(timezone.utc)
                           - timedelta(hours=7, minutes=30)).isoformat()
                now_iso = datetime.now(timezone.utc).isoformat()
                with daemon.db() as conn:
                    conn.execute(
                        "UPDATE jobs SET started_at=?, finished_at=? WHERE job_id=?",
                        (old_now, now_iso, job_id),
                    )
                await daemon._finalize_job(job_id, {
                    "job_id": job_id,
                    "state": "completed",
                    "result": {"summary": "expensive"},
                })
                check("W1. webhook delivered on shortfall",
                      len(delivered) == 1, f"delivered={delivered}")
                if delivered:
                    _, url, payload, state = delivered[0]
                    check("W2. webhook event='topup_required'",
                          payload.get("event") == "topup_required",
                          f"payload keys={list(payload.keys())}")
                    check("W3. webhook state='awaiting_topup'",
                          state == "awaiting_topup",
                          f"state={state}")
                    check("W4. webhook includes topup_url",
                          payload.get("topup_url", "").endswith(f"/v1/jobs/{job_id}/topup"),
                          f"topup_url={payload.get('topup_url')}")
                    check("W5. webhook includes shortfall_cents",
                          payload.get("shortfall_cents") == 100,
                          f"payload={payload}")
        asyncio.run(run())
    finally:
        daemon.STRIPE_SECRET_KEY = "***"
        daemon._deliver_webhook = orig


# --------------------------------------------------------------- entry point

def main():
    test_estimate_constant()
    test_submit_rejects_underfunded_live_mode()
    test_capture_shortfall_detection()
    test_finalize_shortfall_transitions_to_awaiting_topup()
    test_topup_endpoint()
    test_ttl_refund_abandons_awaiting_topup()
    test_ttl_sweep_keeps_fresh_jobs()
    test_demo_mode_skips_shortfall_path()
    test_db_has_shortfall_columns()
    test_webhook_includes_topup_required_event()

    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print(f"Insufficient-funds / shortfall flow (demo + live).")
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()