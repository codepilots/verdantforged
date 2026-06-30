#!/usr/bin/env python3
"""Verify B5 chargeback / ack handling (kanban t_69b52324).

What this test proves (offline-friendly, no live Stripe network):

Signature verification
  V1. Valid HMAC-SHA256 sig + fresh timestamp passes
  V2. Tampered body fails (bad_signature)
  V3. Wrong secret fails (bad_signature)
  V4. Stale timestamp (>5 min old) fails (stale_signature)
  V5. Missing/malformed stripe-signature header fails (missing_signature /
      malformed_signature)

Webhook handler (POST /v1/stripe/webhook)
  W1. STRIPE_WEBHOOK_SECRET unset → 503 webhook_disabled
  W2. Invalid signature → 400 bad_signature (no DB row inserted)
  W3. Valid signature + charge.dispute.created → 200, dispute_events row
      inserted, fraud score for the disputed PI's account bumped by 1
  W4. Replay (same dispute_id twice) → 200 idempotent_replay=True,
      fraud score NOT double-bumped
  W5. charge.dispute.closed → 200, dispute_events row inserted, no
      fraud-score bump
  W6. Unhandled event type (e.g. payment_intent.succeeded) → 200
      received=True processed=False

Ack endpoint (POST /v1/jobs/{id}/ack)
  A1. BROKER_SKILLS_API_KEY unset → 503 ack_disabled
  A2. Missing Authorization → 401
  A3. Wrong Bearer token → 401
  A4. Body missing 'proof' → 400 proof_required
  A5. Ack on a non-terminal job (queued/running) → 409 invalid_state
  A6. Ack on a completed job with valid proof → 200 + acked_at +
      ack_proof + ack_ip persisted
  A7. Second ack within window → 200 + idempotent_replay=True with the
      ORIGINAL timestamp (no overwrite)
  A8. Ack on a missing job → 404
  A9. Proof > 2048 bytes → 400 proof_too_large

Fraud scoring
  F1. 1 chargebacks_filed → score=1, NOT suspended
  F2. 2 chargebacks_filed → score=2, NOT suspended (threshold=2 means
      score>2 = banned, so 2 is still under)
  F3. 1 abandoned_jobs → score=3, NOW suspended (>2)
  F4. Subsequent submit_job for the same account → 403 account_suspended
  F5. fraud_score increments DO persist across helper calls
      (no per-day reset; matches threat model §5 spec)

We use a fake `stripe` module only when STRIPE_SECRET_KEY is set, since
the dispute webhook handler does not call stripe.* APIs (it only
parses the inbound payload).
"""
import os, sys, json, asyncio, tempfile, shutil, hmac, hashlib, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Set up temp env BEFORE importing daemon.
TMP_ROOT = Path(tempfile.mkdtemp(prefix="hermes-chargeback-test-"))
(TMP_ROOT / "static").mkdir()
os.environ["BROKER_EFS_MOUNT"] = str(TMP_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_SKILLS_API_KEY"] = "test-broker-key-deadbeef"
os.environ["BROKER_ACCOUNT_HASH_SECRET"] = "test-chargeback-secret"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_chargeback_test_12345"
os.environ.pop("STRIPE_SECRET_KEY", None)
# Tighten thresholds so the test exercises the threshold transitions
# without needing 3 real signals — we use the default (2) but the test
# asserts the spec explicitly so a future config change is caught.
os.environ["BROKER_FRAUD_BAN_THRESHOLD"] = "2"
os.environ["BROKER_ACK_WINDOW_HOURS"] = "24"

TESTS_DIR = Path(__file__).resolve().parent
WORKSPACE = TESTS_DIR.parent
BROKER_DIR = WORKSPACE / "broker-daemon"
sys.path.insert(0, str(BROKER_DIR))

import daemon  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

daemon.STRIPE_SECRET_KEY = str()
daemon.init_db()

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


# ============================================================ signature

def make_sig(secret, body, ts=None):
    """Build a Stripe-style `t=<ts>,v1=<hmac>` header."""
    if ts is None:
        ts = str(int(time.time()))
    signed = f"{ts}.".encode("utf-8") + body
    v1 = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1}", ts


def test_signature_helpers():
    """V1-V5: pure-Python signature verification (no HTTP)."""
    body = b'{"id":"evt_test","type":"charge.dispute.created"}'
    sig, ts = make_sig("whsec_chargeback_test_12345", body)
    # V1. valid
    ok, reason = daemon._verify_stripe_signature(body, sig, "whsec_chargeback_test_12345")
    check("V1. valid HMAC passes", ok and reason == "",
          f"ok={ok} reason={reason!r}")
    # V2. tampered body
    ok, reason = daemon._verify_stripe_signature(body + b"x", sig, "whsec_chargeback_test_12345")
    check("V2. tampered body fails", not ok and reason == "bad_signature",
          f"ok={ok} reason={reason!r}")
    # V3. wrong secret
    ok, reason = daemon._verify_stripe_signature(body, sig, "whsec_wrong")
    check("V3. wrong secret fails", not ok and reason == "bad_signature",
          f"ok={ok} reason={reason!r}")
    # V4. stale timestamp
    stale_ts = str(int(time.time()) - 600)  # 10 min old
    stale_sig = f"t={stale_ts},v1={hmac.new(b'whsec_chargeback_test_12345', f'{stale_ts}.'.encode() + body, hashlib.sha256).hexdigest()}"
    ok, reason = daemon._verify_stripe_signature(body, stale_sig, "whsec_chargeback_test_12345")
    check("V4. stale timestamp fails", not ok and reason == "stale_signature",
          f"ok={ok} reason={reason!r}")
    # V5. missing/malformed header
    ok, reason = daemon._verify_stripe_signature(body, "", "whsec_chargeback_test_12345")
    check("V5a. missing header fails", not ok and reason == "missing_signature",
          f"reason={reason!r}")
    ok, reason = daemon._verify_stripe_signature(body, "garbage_no_equals", "whsec_chargeback_test_12345")
    check("V5b. malformed header fails", not ok and reason == "malformed_signature",
          f"reason={reason!r}")
    # Bonus: secret unset (closed-by-default)
    ok, reason = daemon._verify_stripe_signature(body, sig, "")
    check("V5c. secret unset fails", not ok and reason == "webhook_disabled",
          f"reason={reason!r}")


# ======================================================= dispute_events DB

def test_record_dispute_event():
    """_record_dispute_event helper is idempotent on dispute_id."""
    event = {
        "id": "evt_test_1",
        "type": "charge.dispute.created",
        "data": {"object": {
            "id": "du_test_1",
            "charge": "ch_test_1",
            "payment_intent": "pi_test_disputed",
            "amount": 2500,
            "reason": "fraudulent",
            "status": "needs_response",
            "evidence_details": {"due_by": 1735689600},
        }},
    }
    inserted, did = daemon._record_dispute_event(event)
    check("DE1. first dispute inserts", inserted is True and did == "du_test_1",
          f"inserted={inserted} did={did}")
    # Replay
    inserted2, did2 = daemon._record_dispute_event(event)
    check("DE2. replay returns inserted=False", inserted2 is False and did2 == "du_test_1",
          f"inserted={inserted2} did={did2}")
    # Closed event for a different dispute
    closed_event = {
        "id": "evt_test_2",
        "type": "charge.dispute.closed",
        "data": {"object": {
            "id": "du_test_2",
            "charge": "ch_test_2",
            "payment_intent": "pi_test_2",
            "amount": 2500,
            "status": "won",
        }},
    }
    inserted3, did3 = daemon._record_dispute_event(closed_event)
    check("DE3. closed event inserts as separate row", inserted3 is True and did3 == "du_test_2",
          f"inserted={inserted3} did={did3}")
    # Verify both rows are present
    with daemon.db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM dispute_events").fetchone()["c"]
    check(f"DE4. 2 rows in dispute_events (got {n})", n == 2, f"got {n}")


# =============================================== fraud-score unit helpers

def test_fraud_score_helpers():
    """F1-F5: _bump_fraud_score and _account_is_suspended unit checks."""
    # Wipe any pre-existing row so this test is order-independent
    with daemon.db() as conn:
        conn.execute("DELETE FROM account_fraud_score WHERE account_key LIKE 'test_fraud_%'")
    ak = "test_fraud_acct_001"
    s, suspended = daemon._bump_fraud_score(ak, "chargebacks_filed")
    check(f"F1. 1 chargeback → score=1 suspended=False (got {s}/{suspended})",
          s == 1 and suspended is False)
    s, suspended = daemon._bump_fraud_score(ak, "chargebacks_filed")
    check(f"F2. 2 chargebacks → score=2 suspended=False (got {s}/{suspended})",
          s == 2 and suspended is False)
    s, suspended = daemon._bump_fraud_score(ak, "abandoned_jobs")
    check(f"F3. +1 abandoned → score=3 suspended=True (got {s}/{suspended})",
          s == 3 and suspended is True)
    # Look up via _account_is_suspended
    susp, reason = daemon._account_is_suspended(ak)
    check(f"F4a. _account_is_suspended True with reason (reason={reason[:40]!r})",
          susp is True and "score=" in reason, f"reason={reason!r}")
    # Verify the row persists (no per-day reset)
    with daemon.db() as conn:
        row = conn.execute(
            "SELECT chargebacks_filed, abandoned_jobs, refunded_topups, suspended "
            "FROM account_fraud_score WHERE account_key=?", (ak,),
        ).fetchone()
    check(f"F5. counters persist (cf={row['chargebacks_filed']} aj={row['abandoned_jobs']} "
          f"suspended={row['suspended']})",
          row["chargebacks_filed"] == 2 and row["abandoned_jobs"] == 1
          and row["refunded_topups"] == 0 and row["suspended"] == 1,
          f"row={dict(row)}")
    # Clean up
    with daemon.db() as conn:
        conn.execute("DELETE FROM account_fraud_score WHERE account_key=?", (ak,))


# ============================================= webhook endpoint (HTTP)

def make_dispute_event(dispute_id="du_http_1", event_id="evt_http_1",
                       pi_id="pi_http_disputed", amount=2500,
                       reason="product_not_received",
                       event_type="charge.dispute.created"):
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": {
            "id": dispute_id,
            "charge": f"ch_{dispute_id}",
            "payment_intent": pi_id,
            "amount": amount,
            "reason": reason,
            "status": "needs_response" if event_type == "charge.dispute.created" else "lost",
            "evidence_details": {"due_by": 1735689600},
        }},
    }


async def _run_webhook_tests():
    """W1-W6: webhook endpoint via aiohttp TestServer."""
    app = daemon.build_app()
    app.on_startup.clear()
    server = TestServer(app)
    async with TestClient(server) as client:
        # W1: STRIPE_WEBHOOK_SECRET unset → 503
        saved_secret = daemon.STRIPE_WEBHOOK_SECRET
        daemon.STRIPE_WEBHOOK_SECRET = ""
        try:
            resp = await client.post("/v1/stripe/webhook", json={})
            check(f"W1. webhook disabled → 503 (got {resp.status})",
                  resp.status == 503)
            body = await resp.json()
            check(f"W1b. code=webhook_disabled (got {body.get('code')!r})",
                  body.get("code") == "webhook_disabled")
        finally:
            daemon.STRIPE_WEBHOOK_SECRET = saved_secret
        # W2: invalid signature → 400, no DB row
        body = json.dumps(make_dispute_event()).encode("utf-8")
        resp = await client.post(
            "/v1/stripe/webhook",
            data=body,
            headers={"stripe-signature": "t=1,v1=deadbeef",
                     "content-type": "application/json"})
        check(f"W2. invalid sig → 400 (got {resp.status})", resp.status == 400)
        with daemon.db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM dispute_events WHERE dispute_id=?",
                ("du_http_1",)).fetchone()["c"]
        check(f"W2b. invalid sig inserted 0 rows (got {n})", n == 0)
        # W3: valid sig + dispute.created → 200, DB row + fraud bump
        sig, _ = make_sig("whsec_chargeback_test_12345", body)
        resp = await client.post(
            "/v1/stripe/webhook",
            data=body,
            headers={"stripe-signature": sig,
                     "content-type": "application/json"})
        check(f"W3. valid dispute.created → 200 (got {resp.status})",
              resp.status == 200)
        rbody = await resp.json()
        check(f"W3b. processed=True dispute_id=du_http_1 (got {rbody})",
              rbody.get("processed") is True and rbody.get("dispute_id") == "du_http_1")
        with daemon.db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM dispute_events WHERE dispute_id=?",
                ("du_http_1",)).fetchone()["c"]
        check(f"W3c. 1 row in dispute_events (got {n})", n == 1)
        # Fraud score for pi_http_disputed's account
        ak = daemon.account_key_for("pi_http_disputed")
        score_row = daemon.db().__enter__()
        # Hmm, db() is a generator — use the with form instead:
        # Actually let's use a fresh conn
        pass
        with daemon.db() as conn:
            row = conn.execute(
                "SELECT chargebacks_filed, suspended FROM account_fraud_score "
                "WHERE account_key=?", (ak,)).fetchone()
        check(f"W3d. fraud score bumped for account (got {dict(row) if row else None})",
              row is not None and row["chargebacks_filed"] == 1)
        # W4: replay (same body) → idempotent_replay=True, no double bump
        resp2 = await client.post(
            "/v1/stripe/webhook",
            data=body,
            headers={"stripe-signature": sig,
                     "content-type": "application/json"})
        check(f"W4. replay → 200 (got {resp2.status})", resp2.status == 200)
        rb2 = await resp2.json()
        check(f"W4b. idempotent_replay=True (got {rb2})",
              rb2.get("idempotent_replay") is True)
        with daemon.db() as conn:
            cf = conn.execute(
                "SELECT chargebacks_filed FROM account_fraud_score "
                "WHERE account_key=?", (ak,)).fetchone()["chargebacks_filed"]
        check(f"W4c. fraud score NOT double-bumped (cf={cf})", cf == 1)
        # W5: charge.dispute.closed → 200, new row, no bump
        closed_body = json.dumps(make_dispute_event(
            dispute_id="du_http_closed", event_id="evt_http_closed",
            pi_id="pi_http_disputed_closed",
            event_type="charge.dispute.closed")).encode("utf-8")
        sig2, _ = make_sig("whsec_chargeback_test_12345", closed_body)
        resp3 = await client.post(
            "/v1/stripe/webhook",
            data=closed_body,
            headers={"stripe-signature": sig2,
                     "content-type": "application/json"})
        check(f"W5. dispute.closed → 200 (got {resp3.status})",
              resp3.status == 200)
        with daemon.db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM dispute_events WHERE dispute_id=?",
                ("du_http_closed",)).fetchone()["c"]
        check(f"W5b. closed row inserted (got {n})", n == 1)
        ak2 = daemon.account_key_for("pi_http_disputed_closed")
        with daemon.db() as conn:
            r2 = conn.execute(
                "SELECT * FROM account_fraud_score WHERE account_key=?",
                (ak2,)).fetchone()
        check(f"W5c. closed event does NOT bump fraud score (row={dict(r2) if r2 else None})",
              r2 is None)
        # W6: unhandled event type → 200 received=True processed=False
        unhandled = {"id": "evt_other", "type": "payment_intent.succeeded",
                     "data": {"object": {}}}
        ub = json.dumps(unhandled).encode("utf-8")
        sig3, _ = make_sig("whsec_chargeback_test_12345", ub)
        resp4 = await client.post(
            "/v1/stripe/webhook", data=ub,
            headers={"stripe-signature": sig3,
                     "content-type": "application/json"})
        check(f"W6. unhandled event → 200 (got {resp4.status})",
              resp4.status == 200)
        rb4 = await resp4.json()
        check(f"W6b. processed=False (got {rb4})",
              rb4.get("processed") is False)


# ================================================ ack endpoint (HTTP)

def _insert_completed_job(job_id="job_ack_test_1", pi_id="pi_ack_test_1",
                          state="completed", finished_minutes_ago=5):
    """Insert a minimal jobs row + llm_tokens row so ack_job can find it."""
    finished = (datetime.now(timezone.utc)
                - timedelta(minutes=finished_minutes_ago)).isoformat()
    created = (datetime.now(timezone.utc)
               - timedelta(minutes=finished_minutes_ago + 10)).isoformat()
    with daemon.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO jobs "
            "(job_id, client_req_id, created_at, started_at, finished_at, state, "
            " result, error, stripe_status, stripe_pi_amount_cents, acked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, f"creq_{job_id}", created, created, finished, state,
             '{"summary":"ack test"}', None, "succeeded", 2500, None),
        )
        conn.execute(
            "INSERT OR REPLACE INTO llm_tokens "
            "(token, job_id, stripe_pi_id, created_at, expires_at, "
            " tokens_used, calls) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"llm_{job_id}", job_id, pi_id, created,
             (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
             100, 1),
        )


async def _run_ack_tests():
    """A1-A9: ack endpoint via aiohttp TestServer."""
    app = daemon.build_app()
    app.on_startup.clear()
    server = TestServer(app)
    async with TestClient(server) as client:
        # A1: BROKER_SKILLS_API_KEY unset → 503
        saved_key = daemon.BROKER_SKILLS_API_KEY
        daemon.BROKER_SKILLS_API_KEY = ""
        try:
            resp = await client.post("/v1/jobs/job_x/ack", json={"proof": "x"})
            check(f"A1. ack disabled → 503 (got {resp.status})", resp.status == 503)
        finally:
            daemon.BROKER_SKILLS_API_KEY = saved_key
        # A2: missing Authorization
        resp = await client.post("/v1/jobs/job_x/ack", json={"proof": "x"})
        check(f"A2. missing auth → 401 (got {resp.status})", resp.status == 401)
        # A3: wrong bearer
        resp = await client.post(
            "/v1/jobs/job_x/ack", json={"proof": "x"},
            headers={"Authorization": "Bearer wrong-key"})
        check(f"A3. wrong token → 401 (got {resp.status})", resp.status == 401)
        # A4: body missing 'proof'
        _insert_completed_job("job_ack_a4", state="completed")
        resp = await client.post(
            "/v1/jobs/job_ack_a4/ack", json={"noproof": True},
            headers={"Authorization": "Bearer test-broker-key-deadbeef"})
        check(f"A4. missing proof → 400 (got {resp.status})", resp.status == 400)
        rb = await resp.json()
        check(f"A4b. code=proof_required (got {rb.get('code')!r})",
              rb.get("code") == "proof_required")
        # A5: ack on non-terminal job
        _insert_completed_job("job_ack_a5", state="queued")
        resp = await client.post(
            "/v1/jobs/job_ack_a5/ack", json={"proof": "hello"},
            headers={"Authorization": "Bearer test-broker-key-deadbeef"})
        check(f"A5. queued state → 409 (got {resp.status})", resp.status == 409)
        rb = await resp.json()
        check(f"A5b. code=invalid_state (got {rb.get('code')!r})",
              rb.get("code") == "invalid_state")
        # A6: valid ack on completed job
        _insert_completed_job("job_ack_a6", state="completed")
        before = datetime.now(timezone.utc)
        resp = await client.post(
            "/v1/jobs/job_ack_a6/ack",
            json={"proof": "I confirm receipt of the result at 2026-06-28T17:00:00Z"},
            headers={"Authorization": "Bearer test-broker-key-deadbeef"})
        check(f"A6. valid ack → 200 (got {resp.status})", resp.status == 200)
        rb = await resp.json()
        check(f"A6b. acked_at present + idempotent_replay=False",
              bool(rb.get("acked_at")) and rb.get("idempotent_replay") is False,
              f"got {rb}")
        # Verify DB row was populated
        with daemon.db() as conn:
            row = conn.execute(
                "SELECT acked_at, ack_proof, ack_ip FROM jobs WHERE job_id=?",
                ("job_ack_a6",)).fetchone()
        check(f"A6c. ack persisted (acked_at={'set' if row['acked_at'] else 'NULL'}, "
              f"proof={'set' if row['ack_proof'] else 'NULL'})",
              bool(row["acked_at"]) and bool(row["ack_proof"]),
              f"row={dict(row)}")
        # A7: second ack → idempotent_replay with original timestamp
        original_ts = row["acked_at"]
        resp = await client.post(
            "/v1/jobs/job_ack_a6/ack",
            json={"proof": "I confirm again"},
            headers={"Authorization": "Bearer test-broker-key-deadbeef"})
        check(f"A7. second ack → 200 (got {resp.status})", resp.status == 200)
        rb = await resp.json()
        check(f"A7b. idempotent_replay=True with original timestamp "
              f"(original={original_ts}, got={rb.get('acked_at')!r})",
              rb.get("idempotent_replay") is True and rb.get("acked_at") == original_ts)
        # Verify ack_proof was NOT overwritten
        with daemon.db() as conn:
            proof_now = conn.execute(
                "SELECT ack_proof FROM jobs WHERE job_id=?",
                ("job_ack_a6",)).fetchone()["ack_proof"]
        check(f"A7c. ack_proof NOT overwritten (got {proof_now[:40]!r}...)",
              proof_now.startswith("I confirm receipt"))
        # A8: ack on missing job → 404
        resp = await client.post(
            "/v1/jobs/job_does_not_exist/ack",
            json={"proof": "ghost"},
            headers={"Authorization": "Bearer test-broker-key-deadbeef"})
        check(f"A8. missing job → 404 (got {resp.status})", resp.status == 404)
        # A9: proof > 2048 bytes → 400
        _insert_completed_job("job_ack_a9", state="completed")
        big_proof = "x" * 2049
        resp = await client.post(
            "/v1/jobs/job_ack_a9/ack", json={"proof": big_proof},
            headers={"Authorization": "Bearer test-broker-key-deadbeef"})
        check(f"A9. oversized proof → 400 (got {resp.status})", resp.status == 400)
        rb = await resp.json()
        check(f"A9b. code=proof_too_large (got {rb.get('code')!r})",
              rb.get("code") == "proof_too_large")


# ========================================== suspension blocks submit_job

async def _run_suspension_test():
    """F4 (HTTP): submit_job is blocked for suspended accounts."""
    app = daemon.build_app()
    app.on_startup.clear()
    server = TestServer(app)
    async with TestClient(server) as client:
        body = {
            "client_req_id": "creq_suspension_test_" + os.urandom(4).hex(),
            "encrypted_skill": "summarize",
            "encrypted_data": "hello",
            "requester_sig": "0x",
            "result_pubkey": "0x",
            "stripe_pi_id": "pi_for_suspended_acct",
        }
        # Compute the expected account_key and insert the suspension row
        # under that key directly (no manual UPDATE dance).
        expected_ak = daemon.account_key_for("pi_for_suspended_acct")
        with daemon.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO account_fraud_score "
                "(account_key, chargebacks_filed, abandoned_jobs, refunded_topups, "
                " suspended, suspended_reason, last_event_at) "
                "VALUES (?, 3, 0, 0, 1, 'test-suspended', ?)",
                (expected_ak, datetime.now(timezone.utc).isoformat()),
            )
        # Sanity-check the row exists
        with daemon.db() as conn:
            r = conn.execute(
                "SELECT suspended FROM account_fraud_score WHERE account_key=?",
                (expected_ak,)).fetchone()
        assert r is not None and r["suspended"] == 1, \
            f"setup failed: row={dict(r) if r else None} ak={expected_ak}"
        resp = await client.post("/v1/jobs", json=body)
        check(f"F4. submit for suspended account → 403 (got {resp.status})",
              resp.status == 403)
        rb = await resp.json()
        check(f"F4b. code=account_suspended (got {rb.get('code')!r})",
              rb.get("code") == "account_suspended")
        # Cleanup
        with daemon.db() as conn:
            conn.execute("DELETE FROM account_fraud_score WHERE account_key=?",
                         (expected_ak,))


# ============================================================ entrypoint

def main():
    test_signature_helpers()
    test_record_dispute_event()
    test_fraud_score_helpers()
    asyncio.run(_run_webhook_tests())
    asyncio.run(_run_ack_tests())
    asyncio.run(_run_suspension_test())

    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print(f"B5 chargeback/ack (signatures + webhook + ack + fraud score).")
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
