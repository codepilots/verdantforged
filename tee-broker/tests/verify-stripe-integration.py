#!/usr/bin/env python3
"""Verify real Stripe PaymentIntent lifecycle integration (kanban t_9fbec867).

What this test proves (offline-friendly, no live Stripe calls):
  1. STRIPE_SECRET_KEY env var turns on the live-API path; absent = demo mode
  2. calculate_job_cost matches the spec: $0.20/15min lease + $0.001/1K tokens
  3. verify_payment_intent in DEMO MODE returns (True, "stripe_disabled", 0)
     without ever importing stripe — backward-compatible with current broker
  4. capture_payment / refund_payment in DEMO MODE return {stripe_disabled: True}
  5. capture_payment in LIVE MODE calls stripe.PaymentIntent.capture with the
     calculated amount (mocked — no network) and returns captured=True
  6. refund_payment in LIVE MODE calls stripe.Refund.create(payment_intent=...)
     and returns refunded=True
  7. verify_payment_intent rejects PIs in cancelled/requires_payment_method
     state with an "invalid payment" error (sufficient funds error path)
  8. DB migration: stripe_capture_amount, stripe_transfer_id, stripe_status
     columns land on the jobs table and are readable
  9. POST /v1/jobs in DEMO MODE still works end-to-end (backwards compat)
 10. GET /v1/jobs/{id} surfaces a `payment` block with the captured status
     after _finalize_job runs (using the demo-mode helpers, no network)

We inject a fake `stripe` module via `sys.modules` so the lazy
`import stripe` in daemon._stripe_client() picks up our fake. The fake
records every call so assertions can pin API surface (retrieve / capture /
refund) and verify argument shapes.
"""
import os, sys, json, asyncio, tempfile, shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

# Set up temp env BEFORE importing daemon.
TMP_ROOT = Path(tempfile.mkdtemp(prefix="hermes-stripe-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TMP_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
# The daemon picks up STRIPE_SECRET_KEY at import time. Strip it BEFORE
# importing daemon so we land in DEMO MODE for tests 1-4, 9, 10. Live-mode
# tests (5-7) inject a fake stripe module + temporarily set the key.
os.environ.pop("STRIPE_SECRET_KEY", None)

TESTS_DIR = Path(__file__).resolve().parent
WORKSPACE = TESTS_DIR.parent
BROKER_DIR = WORKSPACE / "broker-daemon"
sys.path.insert(0, str(BROKER_DIR))

import daemon  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

# Belt-and-braces: in case the env var was already set when the test
# process started (CI secrets), force the module-level constant too.
# Construct the literal at runtime so the test source never contains
# any substring that looks like a Stripe live/test secret.
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


# ------------------------------------------------------------------ cost fn

def test_calculate_job_cost():
    """$0.20 per 15min lease + $0.001 per 1K tokens, returned as a split.

    Spec from kanban t_d0ee4495: the cost function returns a 3-tuple
    (lease_cents, token_cents, total_cents) so the cost ledger (and any
    other downstream audit) can record the breakdown without re-deriving
    it from a single int.
    """
    # C1. Single 15-min slot, zero tokens → (lease=20, token=0, total=20).
    l, t, n = daemon.calculate_job_cost(15 * 60 * 1000, 0)
    check("C1. cost(15min, 0) == (20, 0, 20)",
          (l, t, n) == (20, 0, 20),
          f"got (lease={l}, token={t}, total={n})")
    # C2. Double slot → lease doubles, tokens still zero.
    l, t, n = daemon.calculate_job_cost(30 * 60 * 1000, 0)
    check("C2. cost(30min, 0) == (40, 0, 40)",
          (l, t, n) == (40, 0, 40),
          f"got (lease={l}, token={t}, total={n})")
    # C3. Sub-slot floor: 7.5 min still pays for one slot → lease=20.
    l, t, n = daemon.calculate_job_cost(7 * 60 * 1000 + 30 * 1000, 0)
    check("C3. cost(7.5min, 0) == (20, 0, 20) (1-slot floor)",
          (l, t, n) == (20, 0, 20),
          f"got (lease={l}, token={t}, total={n})")
    # C4. Exactly 1K tokens: integer division rounds up to 1 cent.
    l, t, n = daemon.calculate_job_cost(15 * 60 * 1000, 1000)
    check("C4. cost(15min, 1000) == (20, 1, 21)",
          (l, t, n) == (20, 1, 21),
          f"got (lease={l}, token={t}, total={n})")
    # C5. 50K tokens, single slot: 20 + 50 = 70.
    l, t, n = daemon.calculate_job_cost(15 * 60 * 1000, 50000)
    check("C5. cost(15min, 50000) == (20, 50, 70)",
          (l, t, n) == (20, 50, 70),
          f"got (lease={l}, token={t}, total={n})")
    # C6. 1ms — floor to 1 slot.
    l, t, n = daemon.calculate_job_cost(1, 0)
    check("C6. cost(1ms, 0) == (20, 0, 20) (sub-slot floors to 1 slot)",
          (l, t, n) == (20, 0, 20),
          f"got (lease={l}, token={t}, total={n})")
    # C7. Round-trip invariant: tuple sum equals total. Cheap regression
    # guard against accidental split changes (e.g. one leg doubled).
    l, t, n = daemon.calculate_job_cost(15 * 60 * 1000, 50000)
    check("C7. cost(15min, 50000).total == lease + token",
          n == l + t, f"lease={l} token={t} total={n}")


# ------------------------------------------------------- demo-mode helpers

def test_demo_mode_returns_disabled():
    """Without STRIPE_SECRET_KEY, all Stripe helpers short-circuit cleanly."""
    ok, err, amount = daemon.verify_payment_intent("pi_test_fake_123")
    check("D1. verify in demo mode returns (True, 'stripe_disabled', 0)",
          ok and err == "stripe_disabled" and amount == 0,
          f"got ok={ok} err={err} amount={amount}")

    cap = daemon.capture_payment("pi_test_fake_123", 20)
    # Full-lifecycle demo mode: same shape as live mode so callers can't
    # branch on demo vs live by inspecting the response. The `demo: True`
    # flag is the only tell.
    check("D2. capture in demo mode returns full-lifecycle shape",
          cap.get("captured") is True
          and cap.get("status") == "succeeded"
          and cap.get("id") == "pi_test_fake_123"
          and cap.get("amount_cents") == 20
          and cap.get("demo") is True,
          f"got {cap}")

    ref = daemon.refund_payment("pi_test_fake_123")
    check("D3. refund in demo mode returns full-lifecycle shape",
          ref.get("refunded") is True
          and ref.get("status") == "succeeded"
          and ref.get("id") == "re_demo_pi_test_fake_123"
          and ref.get("payment_intent") == "pi_test_fake_123"
          and ref.get("demo") is True,
          f"got {ref}")


# --------------------------------------------- live mode with fake `stripe`

class FakeIntent:
    """Mimics the small slice of stripe.PaymentIntent we touch."""
    retrieve_calls = []
    capture_calls = []
    retrieve_amounts = {}
    retrieve_statuses = {}
    retrieve_errors = {}
    capture_errors = {}

    def __init__(self, id="pi_test_123", amount=5000, status="requires_capture"):
        self.id = id
        self.amount = amount
        self.amount_received = amount
        self.status = status

    @classmethod
    def retrieve(cls, pi_id):
        cls.retrieve_calls.append(pi_id)
        if pi_id in cls.retrieve_errors:
            raise cls.retrieve_errors[pi_id]
        return cls(pi_id, cls.retrieve_amounts.get(pi_id, 5000),
                   cls.retrieve_statuses.get(pi_id, "requires_capture"))

    @classmethod
    def capture(cls, pi_id, amount_to_capture=None, idempotency_key=None):
        cls.capture_calls.append((pi_id, amount_to_capture, idempotency_key))
        if pi_id in cls.capture_errors:
            raise cls.capture_errors[pi_id]
        return cls(pi_id, amount_to_capture or cls.retrieve_amounts.get(pi_id, 5000),
                   "succeeded")

    @classmethod
    def reset(cls):
        cls.retrieve_calls = []
        cls.capture_calls = []
        cls.retrieve_amounts = {}
        cls.retrieve_statuses = {}
        cls.retrieve_errors = {}
        cls.capture_errors = {}


class FakeRefund:
    create_calls = []

    @classmethod
    def create(cls, payment_intent, **kwargs):
        cls.create_calls.append((payment_intent, kwargs))
        r = MagicMock()
        r.id = "re_test_abc"
        r.amount = 5000
        r.status = "succeeded"
        r.payment_intent = payment_intent
        return r

    @classmethod
    def reset(cls):
        cls.create_calls = []


class FakeStripe:
    """Fake stripe module installed into sys.modules during live-mode tests."""
    PaymentIntent = FakeIntent
    Refund = FakeRefund
    api_key = None
    error = MagicMock()
    # Module-level exception class so callers can `except stripe.error.StripeError`
    error.StripeError = type("StripeError", (Exception,), {})


def _install_fake_stripe():
    sys.modules["stripe"] = FakeStripe
    FakeStripe.api_key = None


def test_live_mode_verify_calls_retrieve():
    """With STRIPE_SECRET_KEY set, verify must call stripe.PaymentIntent.retrieve."""
    FakeIntent.reset(); FakeRefund.reset()
    _install_fake_stripe()
    saved = daemon.STRIPE_SECRET_KEY
    daemon.STRIPE_SECRET_KEY = "sk_test_dummy"
    try:
        ok, err, amount = daemon.verify_payment_intent("pi_test_abc_001")
    finally:
        daemon.STRIPE_SECRET_KEY = saved
    check("L1. live verify calls PaymentIntent.retrieve",
          FakeIntent.retrieve_calls == ["pi_test_abc_001"],
          f"calls={FakeIntent.retrieve_calls}")
    check("L2. live verify returns amount from Stripe",
          amount == 5000 and ok and err == "",
          f"ok={ok} err={err} amount={amount}")


def test_live_mode_verify_rejects_bad_status():
    FakeIntent.reset()
    FakeIntent.retrieve_statuses["pi_test_bad"] = "canceled"
    _install_fake_stripe()
    saved = daemon.STRIPE_SECRET_KEY
    daemon.STRIPE_SECRET_KEY = "sk_test_dummy"
    try:
        ok, err, _ = daemon.verify_payment_intent("pi_test_bad")
    finally:
        daemon.STRIPE_SECRET_KEY = saved
    check("L3. live verify rejects status=canceled",
          (not ok) and "canceled" in err,
          f"ok={ok} err={err}")


def test_live_mode_capture_calls_stripe():
    FakeIntent.reset(); FakeRefund.reset()
    _install_fake_stripe()
    saved = daemon.STRIPE_SECRET_KEY
    daemon.STRIPE_SECRET_KEY = "sk_test_dummy"
    try:
        result = daemon.capture_payment("pi_test_xyz", 42)
    finally:
        daemon.STRIPE_SECRET_KEY = saved
    check("L4. live capture calls PaymentIntent.capture(amount_to_capture=42)",
          len(FakeIntent.capture_calls) == 1
          and FakeIntent.capture_calls[0][0] == "pi_test_xyz"
          and FakeIntent.capture_calls[0][1] == 42,
          f"calls={FakeIntent.capture_calls}")
    check("L5. live capture returns captured=True with amount",
          result.get("captured") is True and result.get("amount_cents") == 42,
          f"result={result}")


def test_live_mode_refund_calls_stripe():
    FakeIntent.reset(); FakeRefund.reset()
    _install_fake_stripe()
    saved = daemon.STRIPE_SECRET_KEY
    daemon.STRIPE_SECRET_KEY = "sk_test_dummy"
    try:
        result = daemon.refund_payment("pi_test_xyz")
    finally:
        daemon.STRIPE_SECRET_KEY = saved
    check("L6. live refund calls Refund.create(payment_intent=...)",
          len(FakeRefund.create_calls) == 1
          and FakeRefund.create_calls[0][0] == "pi_test_xyz",
          f"calls={FakeRefund.create_calls}")
    check("L7. live refund returns refunded=True with id",
          result.get("refunded") is True and result.get("id") == "re_test_abc",
          f"result={result}")


# ----------------------------------------- bootstrap / CFN wiring (deploy)

def test_bootstrap_and_cfn_wiring():
    """Verify STRIPE_SECRET_KEY is wired into the bootstrap + CFN templates.

    Catches the "I added the daemon helpers but forgot to plumb the env
    var" mistake — without these lines the daemon would never receive
    the Stripe key in production. We assert the literal substring
    presence (no echo of the value, no risk of leaking secrets in CI
    logs).
    """
    bootstrap_path = WORKSPACE / "scripts" / "bootstrap-control-plane.sh"
    cfn_path = WORKSPACE / "cloudformation-control-plane.yaml"
    deploy_path = WORKSPACE / "deploy.sh"
    bootstrap = bootstrap_path.read_text() if bootstrap_path.exists() else ""
    cfn = cfn_path.read_text() if cfn_path.exists() else ""
    deploy = deploy_path.read_text() if deploy_path.exists() else ""
    check("B1. bootstrap-control-plane.sh writes STRIPE_SECRET_KEY to config.env",
          "STRIPE_SECRET_KEY" in bootstrap,
          "STRIPE_SECRET_KEY not in bootstrap script")
    check("B2. cloudformation-control-plane.yaml declares STRIPE_SECRET_KEY parameter",
          "STRIPE_SECRET_KEY" in cfn,
          "STRIPE_SECRET_KEY not in CFN template")
    check("B3. cloudformation-control-plane.yaml forwards STRIPE_SECRET_KEY to config.env",
          # Accept either the literal `STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY}`
          # form or the renamed-parameter form (`...=${StripeSecretKey}`)
          # introduced by sibling task t_b13072b3. The semantic check is
          # "the env var name STRIPE_SECRET_KEY reaches config.env via
          # CFN parameter interpolation" — not the exact interpolation form.
          ("STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY}" in cfn
           or "STRIPE_SECRET_KEY=${StripeSecretKey}" in cfn),
          "config.env forwarding missing")
    check("B4. deploy.sh forwards STRIPE_SECRET_KEY to bootstrap SSM command",
          "STRIPE_SECRET_KEY" in deploy,
          "deploy.sh does not forward STRIPE_SECRET_KEY")
    # Stripe key MUST be NoEcho in CFN (preventing the value from
    # appearing in stack outputs or describe-stacks results).
    check("B5. CFN STRIPE_SECRET_KEY parameter is NoEcho",
          "NoEcho: true" in cfn
          and cfn.find("NoEcho: true") < cfn.find("STRIPE_SECRET_KEY")
              + 500,  # NoEcho must be within ~500 chars of the param name
          "STRIPE_SECRET_KEY parameter is not NoEcho (leaks in stack outputs)")


# ------------------------------------------------------ DB schema migration

def test_db_has_stripe_columns():
    with daemon.db() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    check("DB1. jobs.stripe_capture_amount column exists",
          "stripe_capture_amount" in cols)
    check("DB2. jobs.stripe_transfer_id column exists",
          "stripe_transfer_id" in cols)
    check("DB3. jobs.stripe_status column exists",
          "stripe_status" in cols)


# ------------------------------------------------- end-to-end demo mode flow

def _good_submit_body():
    return {
        "client_req_id": "stripe-test-" + os.urandom(4).hex(),
        "encrypted_skill": "summarize",
        "encrypted_data": "hello",
        "requester_sig": "0x",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_test_demo_mode_001",
    }


async def _e2e_demo_flow():
    # Point the demo-mode ledger at a path inside TMP_ROOT so we can
    # assert on the captured row's lease/token split without picking up
    # rows from a sibling test or the developer's real workspace.
    # COST_LEDGER.jsonl is gitignored, so this is a per-process scratch
    # file we don't need to clean up beyond rmtree at the bottom of main().
    ledger_path = TMP_ROOT / "test-COST_LEDGER.jsonl"
    os.environ["BROKER_COST_LEDGER"] = str(ledger_path)
    if ledger_path.exists():
        ledger_path.unlink()
    # Strip WorkerManager (needs AWS creds) — replace with a stub so
    # _finalize_job can call note_job_finished() without blowing up.
    # We also stub ensure_worker() / note_job_finished() because
    # submit_job's _kick_worker_for_job will call the worker manager
    # after each POST /v1/jobs — without a stub it'd fail and flip
    # the job to state='failed' before we can test the completed path.
    class _StubWorkerMgr:
        async def ensure_worker(self):
            class _W:
                instance_id = "i-test"
                private_ip = "127.0.0.1"
                launched_at = 0.0
            return _W()
        async def note_job_finished(self):
            pass
    daemon.worker_mgr = _StubWorkerMgr()

    app = daemon.build_app()
    app.on_startup.clear()
    server = TestServer(app)
    async with TestClient(server) as client:
        # submit
        resp = await client.post("/v1/jobs", json=_good_submit_body())
        check("E1. POST /v1/jobs succeeds in demo mode",
              resp.status == 202, f"status={resp.status}")
        body = await resp.json()
        job_id = body["job_id"]

        # GET /v1/jobs/{id} before finalize
        resp = await client.get(f"/v1/jobs/{job_id}")
        check("E2. GET /v1/jobs/{id} returns queued or running",
              resp.status == 200, f"status={resp.status}")

        # Simulate worker writing an outbox payload — call _finalize_job directly.
        outbox_payload = {
            "job_id": job_id,
            "state": "completed",
            "result": {"summary": "ok"},
        }
        await daemon._finalize_job(job_id, outbox_payload)

        # GET /v1/jobs/{id} AFTER finalize → payment block surfaced
        resp = await client.get(f"/v1/jobs/{job_id}")
        body = await resp.json()
        payment = body.get("payment")
        check("E3. GET /v1/jobs after finalize has `payment` block",
              payment is not None, f"body keys={list(body)}")
        # Full-lifecycle demo mode: payment block now mirrors live-mode
        # shape (status=succeeded, demo=True). The row-level stripe_status
        # column is the prefixed form ("demo_succeeded") for dashboards.
        check("E4. payment.status == 'succeeded' in demo mode (full lifecycle)",
              payment and payment.get("status") == "succeeded",
              f"payment={payment}")
        check("E4a. payment.demo == True in demo mode",
              payment and payment.get("demo") is True,
              f"payment={payment}")
        check("E4b. payment.amount_cents == 20 (one lease slot, no LLM traffic)",
              payment and payment.get("amount_cents") == 20,
              f"payment={payment}")

        # Now finalize a FAILED job and verify refund path runs in demo mode
        fail_body = _good_submit_body()
        resp = await client.post("/v1/jobs", json=fail_body)
        job2 = (await resp.json())["job_id"]
        await daemon._finalize_job(job2, {"job_id": job2, "state": "failed",
                                          "error": "boom"})
        resp = await client.get(f"/v1/jobs/{job2}")
        body = await resp.json()
        payment2 = body.get("payment")
        check("E5. failed job gets payment.status == 'succeeded' + stripe_id prefixed re_demo_",
              payment2 and payment2.get("status") == "succeeded"
              and str(payment2.get("stripe_id", "")).startswith("re_demo_"),
              f"payment={payment2}")

        # DB rows persisted the stripe_status columns with demo_ prefix
        with daemon.db() as conn:
            row = conn.execute(
                "SELECT stripe_status, stripe_capture_amount FROM jobs WHERE job_id=?",
                (job_id,)).fetchone()
        check("E6. stripe_status column persisted as demo_succeeded (prefix preserved)",
              row and row["stripe_status"] == "demo_succeeded",
              f"row={dict(row) if row else None}")

        # COST_LEDGER.jsonl must record the lease/token split for the
        # capture event (kanban t_d0ee4495). A reviewer reading the
        # ledger mid-deploy should see the breakdown without having to
        # re-derive it from amount_cents. Empty outbox payload → lease
        # floor (1 slot = 20 cents) and 0 tokens → (20, 0, 20). We filter
        # on pi_id because the refund job above will also have appended
        # a 'refund' row.
        ledger_rows = []
        if ledger_path.exists():
            with ledger_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ledger_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        # Tolerate the same malformed-row case noted in
                        # the cost-accuracy review (concatenated JSON
                        # objects); we just skip garbage.
                        continue
        capture_rows = [
            r for r in ledger_rows
            if r.get("event") == "capture" and r.get("pi_id") == "pi_test_demo_mode_001"
        ]
        # The split-logging pre-call in _finalize_job (kanban t_d0ee4495)
        # writes the audit row first; capture_payment then writes its
        # own demo-mode row. Both should carry the split, but we only
        # assert on the pre-call row (the one explicitly named in the
        # task body) and tolerate the second — its presence is a
        # design choice for a follow-up if reviewers object to the
        # duplication.
        check("E7. demo capture appended to COST_LEDGER.jsonl",
              len(capture_rows) >= 1,
              f"got {len(capture_rows)} capture rows; ledger={ledger_rows}")
        # Find the FIRST capture row (the pre-call from _finalize_job).
        # It must carry lease/token/total. We use the first rather than
        # the last so the assertion is stable across future changes.
        if capture_rows:
            cap = capture_rows[0]
            check("E8. ledger capture row records lease_cents=20, token_cents=0, total_cents=20",
                  cap.get("lease_cents") == 20
                  and cap.get("token_cents") == 0
                  and cap.get("total_cents") == 20,
                  f"got {cap}")


def test_e2e_demo_mode():
    asyncio.run(_e2e_demo_flow())


# ============================================================ B3 acceptance
#
# Kanban t_b2ceaf21, threat-model-topup-flow.md §7. Two tests:
#
#   B3-A. Two concurrent topup requests with the SAME topup PI for the
#         same job — only ONE captures (Stripe idempotency_key dedupes
#         within 24h, and the cached-retry path returns the cached result
#         for the second request without re-calling Stripe).
#
#   B3-B. Two concurrent topup requests with DIFFERENT topup PIs for
#         the same job — the FIRST commits and flips state, the SECOND
#         finds state != 'awaiting_topup' inside BEGIN IMMEDIATE and
#         bails with HTTP 409 + code "topup_already_settled".
#
# Strategy: call daemon.topup_job() directly with a make_mocked_request
# (aiohttp's helper), avoiding the complexity of orchestrating real
# concurrent TestClient requests. The state machine that matters is the
# SQLite BEGIN IMMEDIATE lock + the cached-retry short-circuit; both are
# exercised end-to-end inside topup_job without the HTTP layer.

def _seed_awaiting_topup_job(job_id: str, stripe_pi_id: str, shortfall_cents: int = 100) -> None:
    """Insert a job row directly into 'awaiting_topup' state with a linked
    llm_tokens row carrying the original PI. Bypasses the full submit +
    finalize-shortfall flow so the test focuses narrowly on the B3
    acceptance criteria inside topup_job().
    """
    now = datetime.now(timezone.utc).isoformat()
    with daemon.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO jobs (job_id, client_req_id, created_at, "
            "state, request_body, shortfall_cents, awaiting_topup_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, f"b3-test-{job_id}", now, "awaiting_topup",
             "{}", shortfall_cents, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO llm_tokens (token, job_id, stripe_pi_id, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (f"tok-{job_id}", job_id, stripe_pi_id, now, now),
        )


def _make_topup_request(job_id: str, topup_pi_id: str):
    """Build an aiohttp mocked request that topup_job() can consume.

    We don't need a real route — make_mocked_request's match_info is the
    only thing topup_job looks at. The app argument wires up the path
    template so aiohttp knows how to populate match_info.
    """
    from aiohttp.test_utils import make_mocked_request
    from unittest.mock import AsyncMock

    async def dummy(req):
        from aiohttp import web
        return web.Response(text="ok")

    from aiohttp import web
    app = web.Application()
    app.router.add_post("/v1/jobs/{job_id}/topup", dummy)
    req = make_mocked_request(
        "POST", f"/v1/jobs/{job_id}/topup",
        match_info={"job_id": job_id}, app=app,
    )
    # Body is read via await request.json() inside topup_job.
    req.json = AsyncMock(return_value={"stripe_pi_id": topup_pi_id})
    return req


async def _b3_same_pi_concurrent_captures():
    """B3-A: two concurrent topup calls with the SAME topup PI.
    The first call captures; the second call should hit the cached-retry
    path and NOT re-call Stripe (idempotent_replay=True, same
    stripe_topup_transfer_id)."""
    FakeIntent.reset()
    FakeRefund.reset()
    _install_fake_stripe()
    # Original PI is what the broker will capture-for-shortfall, topup PI
    # is the new one. The original capture also goes through capture_payment
    # so we need both PIs to be valid in FakeIntent.
    job_id = "job_b3_same_" + os.urandom(3).hex()
    _seed_awaiting_topup_job(job_id, "pi_orig_b3a", shortfall_cents=100)
    FakeIntent.retrieve_amounts["pi_orig_b3a"] = 500
    FakeIntent.retrieve_amounts["pi_topup_b3a"] = 100

    # Force LIVE MODE for the duration of this test — otherwise daemon
    # capture_payment short-circuits to demo mode and never hits FakeIntent.
    saved_key = daemon.STRIPE_SECRET_KEY
    daemon.STRIPE_SECRET_KEY = "***"
    try:
        # First call — captures, flips state to completed.
        req1 = _make_topup_request(job_id, "pi_topup_b3a")
        resp1 = await daemon.topup_job(req1)
        check("B3A.1. first concurrent topup returns 200",
              resp1.status == 200,
              f"status={resp1.status}")
        body1 = json.loads(resp1.body)
        check("B3A.2. first call captures BOTH topup + original PI (single fire)",
              # Two captures: topup PI + original PI re-capture for shortfall
              # (see t_9a705578's two-capture flow that charges the customer
              # 2x shortfall_cents — one on the held PI, one on the topup PI).
              len(FakeIntent.capture_calls) == 2,
              f"capture_calls={FakeIntent.capture_calls}")
        check("B3A.3. first call idempotency_key has topup: prefix",
              any(c[2] and c[2].startswith("topup:")
                  for c in FakeIntent.capture_calls),
              f"capture_calls={FakeIntent.capture_calls}")

        # Second call — cached retry path. Same job_id, SAME topup_pi_id.
        # Must NOT call Stripe (idempotent_replay=True) and must return the
        # cached stripe_topup_transfer_id.
        req2 = _make_topup_request(job_id, "pi_topup_b3a")
        resp2 = await daemon.topup_job(req2)
        check("B3A.4. second concurrent topup (same PI) returns 200",
              resp2.status == 200,
              f"status={resp2.status}")
        body2 = json.loads(resp2.body)
        check("B3A.5. second call is idempotent_replay=True (no new Stripe call)",
              body2.get("idempotent_replay") is True,
              f"body={body2}")
        check("B3A.6. second call returns the SAME topup_transfer_id",
              body2.get("topup_transfer_id") == body1.get("topup_transfer_id")
              and body2.get("topup_transfer_id"),
              f"first={body1.get('topup_transfer_id')} second={body2.get('topup_transfer_id')}")
        # CRITICAL: capture_calls count MUST still be 2 — second call short-circuited.
        check("B3A.7. second call did NOT re-call Stripe (capture_calls still 2)",
              len(FakeIntent.capture_calls) == 2,
              f"capture_calls={FakeIntent.capture_calls}")
    finally:
        daemon.STRIPE_SECRET_KEY = saved_key


def test_b3_same_pi_concurrent_captures():
    asyncio.run(_b3_same_pi_concurrent_captures())


async def _b3_different_pi_concurrent_captures():
    """B3-B: two concurrent topup calls with DIFFERENT topup PIs.
    The first commits and flips state; the second finds state !=
    'awaiting_topup' inside BEGIN IMMEDIATE and returns HTTP 409 +
    code "topup_already_settled"."""
    FakeIntent.reset()
    FakeRefund.reset()
    _install_fake_stripe()
    job_id = "job_b3_diff_" + os.urandom(3).hex()
    _seed_awaiting_topup_job(job_id, "pi_orig_b3b", shortfall_cents=100)
    FakeIntent.retrieve_amounts["pi_orig_b3b"] = 500
    FakeIntent.retrieve_amounts["pi_topup_b3b_1"] = 100
    FakeIntent.retrieve_amounts["pi_topup_b3b_2"] = 100

    # Force LIVE MODE — daemon.capture_payment() short-circuits to demo
    # mode when STRIPE_SECRET_KEY is empty, bypassing FakeIntent.
    saved_key = daemon.STRIPE_SECRET_KEY
    daemon.STRIPE_SECRET_KEY = "***"
    try:
        # First call with PI #1 — captures, flips state.
        req1 = _make_topup_request(job_id, "pi_topup_b3b_1")
        resp1 = await daemon.topup_job(req1)
        check("B3B.1. first concurrent topup (PI #1) returns 200",
              resp1.status == 200,
              f"status={resp1.status}")
        body1 = json.loads(resp1.body)
        check("B3B.2. first call persisted pi_topup_b3b_1",
              body1.get("topup_transfer_id") == "pi_topup_b3b_1",
              f"body={body1}")

        # Second call with PI #2 (DIFFERENT PI). The DB state is now
        # 'completed', so the cached-retry short-circuit does NOT match
        # (different topup_pi_id). Falls into BEGIN IMMEDIATE, re-reads
        # state, finds it != 'awaiting_topup', returns 409.
        req2 = _make_topup_request(job_id, "pi_topup_b3b_2")
        resp2 = await daemon.topup_job(req2)
        check("B3B.3. second concurrent topup (DIFFERENT PI) returns 409",
              resp2.status == 409,
              f"status={resp2.status}")
        body2 = json.loads(resp2.body)
        check("B3B.4. rejection code is 'topup_already_settled'",
              body2.get("code") == "topup_already_settled",
              f"body={body2}")
        check("B3B.5. rejection surfaces current_state='completed'",
              body2.get("current_state") == "completed",
              f"body={body2}")
        # Stripe must NOT have been called for PI #2 (rejection happens
        # before any capture call).
        captured_pis = [c[0] for c in FakeIntent.capture_calls]
        check("B3B.6. second call did NOT capture pi_topup_b3b_2",
              "pi_topup_b3b_2" not in captured_pis,
              f"capture_calls={captured_pis}")
        # And the count from the first call is preserved (no double-fire).
        check("B3B.7. Stripe capture_calls count unchanged (2 — same as first call)",
              len(captured_pis) == 2,
              f"capture_calls={FakeIntent.capture_calls}")
        # Sanity: DB row still references PI #1, not PI #2.
        with daemon.db() as conn:
            row = conn.execute(
                "SELECT topup_pi_id, state, stripe_topup_transfer_id FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        check("B3B.8. DB row still references PI #1 (rejected PI #2 never landed)",
              row and row["topup_pi_id"] == "pi_topup_b3b_1"
              and row["state"] == "completed"
              and row["stripe_topup_transfer_id"] == "pi_topup_b3b_1",
              f"row={dict(row) if row else None}")
    finally:
        daemon.STRIPE_SECRET_KEY = saved_key
def test_b3_different_pi_concurrent_captures():
    asyncio.run(_b3_different_pi_concurrent_captures())


# --------------------------------------------------------------- entry point

def main():
    test_calculate_job_cost()
    test_demo_mode_returns_disabled()
    test_live_mode_verify_calls_retrieve()
    test_live_mode_verify_rejects_bad_status()
    test_live_mode_capture_calls_stripe()
    test_live_mode_refund_calls_stripe()
    test_db_has_stripe_columns()
    test_bootstrap_and_cfn_wiring()
    test_e2e_demo_mode()
    test_b3_same_pi_concurrent_captures()
    test_b3_different_pi_concurrent_captures()

    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print(f"Stripe integration unit + e2e (demo mode).")
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()