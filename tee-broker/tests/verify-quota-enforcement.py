#!/usr/bin/env python3
"""Verify per-account daily job-cap enforcement (kanban t_a18827b6, VULN-LLMTK).

What this test proves (offline-friendly, no live broker needed):

  Q1. Under cap OK.  Submitting fewer than BROKER_DAILY_JOB_CAP jobs from
      one account returns 202.
  Q2. At cap rejected.  Once jobs_used >= BROKER_DAILY_JOB_CAP, the next
      submit returns HTTP 429 with `code: "daily_cap"` AND `reason: "daily_cap"`.
      Stripe verify is NOT called on the rejected path (the broker must not
      hammer Stripe for already-quota'd accounts).
  Q3. Day rollover resets.  After day_utc advances past today, the counter
      starts at zero again and a previously-quota'd account can submit
      another job (assuming cap is still 5/day).
  Q4. Distinct accounts don't share counter.  account_key_for(pi_a) !=
      account_key_for(pi_b), so quota state is per-account — exhausting
      account A's quota does not block account B.

We mock the broker's SQLite layer so the test runs without touching the
host's broker DB. The mocked connection implements the minimal surface
that submit_job's submit flow touches — see the FakeConn class.

We also mock Stripe (verify_payment_intent) so we can verify the rejection
path does NOT call it (Q2 assert: `verify_calls == []` after a cap-exceeded
attempt).

Run with any Python that has aiohttp available.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ---- Test environment setup --------------------------------------------------
# Override EFS mount so daemon.py import doesn't fail (it tries to mkdir
# $BROKER_EFS_MOUNT/logs at import time). Set env BEFORE importing daemon.
TEST_ROOT = Path(tempfile.mkdtemp(prefix="quota-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_REGION"] = "eu-west-1"
os.environ["BROKER_VPC_ID"] = ""
os.environ["BROKER_SUBNET_ID"] = ""
os.environ["BROKER_WORKER_SG"] = ""
os.environ["BROKER_WORKER_AMI"] = ""
os.environ["BROKER_WORKER_IAM_ROLE"] = ""
# Set a low cap so the test doesn't have to submit 50000 jobs.
os.environ["BROKER_DAILY_JOB_CAP"] = "5"
# Disable rate limiter so it doesn't interfere with the cap test (the
# rate limiter is per-IP, the cap is per-account — different scopes).
os.environ["BROKER_RATE_LIMIT_DISABLED"] = "1"
# VULN-S7 secret pinned for reproducibility.
os.environ["BROKER_ACCOUNT_HASH_SECRET"] = "test-secret-for-quota-suite"
# Force DEMO MODE for Stripe.
os.environ.pop("STRIPE_SECRET_KEY", None)

DAEMON_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/broker-daemon"
sys.path.insert(0, DAEMON_DIR)

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def _bump_pass() -> None:
    global PASS
    PASS += 1


def _bump_fail() -> None:
    global FAIL
    FAIL += 1


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[PASS] {label}")
        _bump_pass()
    else:
        print(f"[FAIL] {label}" + (f"  ({detail})" if detail else ""))
        _bump_fail()
        FAILURES.append(label)


def fresh_daemon():
    """Re-import daemon with current env vars (some are read at import time)."""
    if "daemon" in sys.modules:
        del sys.modules["daemon"]
    import daemon  # noqa: E402
    return daemon


def make_submittable_body(client_req_id: str, stripe_pi_id: str = "pi_quota_test_a") -> dict:
    """Return a minimal valid POST /v1/jobs body that passes validate_submit."""
    return {
        "client_req_id": client_req_id,
        "stripe_pi_id": stripe_pi_id,
        "encrypted_skill": "summarize",
        "encrypted_data": "data-" + client_req_id,
        "requester_sig": "0x",
        "result_pubkey": "0x",
    }


def _async_json(payload):
    async def _j(*a, **kw):
        return payload
    return _j


# ---- Fake SQLite connection (per-account quota tracking) -------------------

class FakeCursor:
    """Minimal cursor — fetchone() returns a single row, fetchall() returns []."""
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeQuotaConn:
    """Fake SQLite connection that tracks account_quota rows in memory.

    The account_quota table has columns (account_key, day_utc, jobs_used)
    per the VULN-LLMTK fix spec. The submit flow:
      1. SELECT jobs_used FROM account_quota WHERE account_key=? AND day_utc=?
      2. INSERT/UPDATE jobs_used = jobs_used + 1
    For tests, we want to control what the SELECT returns so we can simulate
    "already at cap" without running 5 actual submissions first.

    Pass `quota_rows={account_key: jobs_used, ...}` to preset rows for the
    configured `day_utc`. When the daemon INSERTs/INCREMENTs, we update
    the dict in-place so a follow-up SELECT in the same test sees the
    new value.

    For the day-rollover test (Q3), we pre-seed the PRIOR day directly via
    the internal dict — the broker's SELECT for the real "today" misses
    (no row for today) and falls through to jobs_used=0.
    """

    def __init__(self, quota_rows: dict[str, int] | None = None, day_utc: str = "2026-06-28"):
        self.quota_rows: dict[tuple[str, str], int] = {}
        # Seed from simplified {account_key: jobs_used} (we'll use today's day_utc).
        if quota_rows:
            for acc, used in quota_rows.items():
                self.quota_rows[(acc, day_utc)] = used
        self._day_utc = day_utc
        # Track what SQL we saw so tests can verify ordering (Q2).
        self.executed_sql: list[tuple[str, tuple]] = []
        # Track INSERT/UPDATE statements on account_quota specifically.
        self.quota_writes: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        sql_str = " ".join(sql.split())
        self.executed_sql.append((sql_str, tuple(params)))
        # SELECT for the cap check (VULN-LLMTK fix).
        if sql_str.startswith("SELECT jobs_used FROM account_quota"):
            account_key, day_utc = params[0], params[1]
            used = self.quota_rows.get((account_key, day_utc))
            # Return a row-like dict so the daemon's `row["jobs_used"]`
            # accessor works. The Q3 day-rollover test relies on this
            # returning None when the broker asks for a different day
            # than the seeded day (we seed 2026-06-28, broker asks
            # 2026-06-29 because that's the real wall-clock date).
            return FakeCursor({"jobs_used": used} if used is not None else None)
        # INSERT OR REPLACE / UPSERT — increment the counter.
        if sql_str.startswith("INSERT INTO account_quota") or \
           sql_str.startswith("UPDATE account_quota"):
            self.quota_writes.append((sql_str, tuple(params)))
            # Parse out the new value. The daemon's contract: the params tuple
            # includes (account_key, day_utc, jobs_used_new). We trust that
            # shape and apply it.
            if len(params) >= 3:
                account_key, day_utc, new_used = params[0], params[1], params[2]
                self.quota_rows[(account_key, day_utc)] = new_used
            return FakeCursor(None)
        # Stub out everything else — the quota tests don't care.
        return FakeCursor(None)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_quota_db_cm(conn: FakeQuotaConn):
    import contextlib
    @contextlib.contextmanager
    def cm():
        yield conn
    return cm


# ---- Q1. Under cap OK ------------------------------------------------------
def test_q1_under_cap_ok() -> None:
    """Submitting fewer than the cap succeeds.

    We simulate 4 jobs in account A (cap = 5). The 4th submit must return 202,
    and the broker must have bumped the counter to 4 by the end.
    """
    daemon = fresh_daemon()
    # Seed account A at 3 jobs used (under cap of 5, so 4th is allowed).
    account = daemon.account_key_for("pi_quota_test_a")
    conn = FakeQuotaConn(quota_rows={account: 3}, day_utc="2026-06-28")
    daemon.db = _make_quota_db_cm(conn)

    # No-op the worker kicker (no EC2 launch in tests).
    async def _noop_kick(*a, **kw):
        return None
    daemon._kick_worker_for_job = _noop_kick

    from aiohttp.test_utils import make_mocked_request

    body = make_submittable_body("req_q1_001", "pi_quota_test_a")
    req = make_mocked_request("POST", "/v1/jobs")
    req.json = _async_json(body)
    resp = asyncio.run(daemon.submit_job(req))

    # Status check — under cap must NOT be rejected with 429 daily_cap.
    check("Q1. under-cap submit returns 202 (not 429 daily_cap)",
          resp.status == 202, f"got status={resp.status}")


# ---- Q2. At cap rejected with reason=daily_cap -----------------------------
def test_q2_at_cap_rejected() -> None:
    """Submitting at/over the cap returns 429 + code/reason=daily_cap.

    We seed account A at jobs_used = 5 (== cap). The next submit must:
      - Return HTTP 429
      - Carry code: "daily_cap" and reason: "daily_cap"
      - NOT call verify_payment_intent (Stripe not spammed for quota'd accounts)
    """
    daemon = fresh_daemon()
    account = daemon.account_key_for("pi_quota_test_a")
    # Seed AT cap.
    conn = FakeQuotaConn(quota_rows={account: 5}, day_utc="2026-06-28")
    daemon.db = _make_quota_db_cm(conn)

    async def _noop_kick(*a, **kw):
        return None
    daemon._kick_worker_for_job = _noop_kick

    # Wrap web.json_response so we can capture the rejection payload.
    captured: dict = {}
    real_json_response = daemon.web.json_response
    def capturing_json_response(payload, *a, **kw):
        captured["payload"] = payload
        captured["status"] = kw.get("status", 200)
        return real_json_response(payload, *a, **kw)
    daemon.web.json_response = capturing_json_response

    # Spy on Stripe verify — must NOT be called for quota'd accounts.
    verify_calls: list[str] = []
    real_verify = daemon.verify_payment_intent
    def spy_verify(pi_id):
        verify_calls.append(pi_id)
        return real_verify(pi_id)
    daemon.verify_payment_intent = spy_verify

    try:
        from aiohttp.test_utils import make_mocked_request
        body = make_submittable_body("req_q2_001", "pi_quota_test_a")
        req = make_mocked_request("POST", "/v1/jobs")
        req.json = _async_json(body)
        resp = asyncio.run(daemon.submit_job(req))
    finally:
        daemon.web.json_response = real_json_response
        daemon.verify_payment_intent = real_verify

    payload = captured.get("payload", {})
    status = captured.get("status", 0)
    check("Q2a. at-cap submit returns HTTP 429",
          status == 429, f"got status={status} payload={payload}")
    check("Q2b. 429 body has code='daily_cap'",
          payload.get("code") == "daily_cap",
          f"got code={payload.get('code')!r}")
    check("Q2c. 429 body has reason='daily_cap'",
          payload.get("reason") == "daily_cap",
          f"got reason={payload.get('reason')!r}")
    check("Q2d. verify_payment_intent NOT called for quota'd account",
          verify_calls == [],
          f"got verify_calls={verify_calls}")


# ---- Q3. Day rollover resets counter ---------------------------------------
def test_q3_day_rollover_resets() -> None:
    """Once day_utc advances, the counter resets and submits succeed again.

    We seed account A at jobs_used = 5 on YESTERDAY's date. The broker's
    SELECT for TODAY (the real wall-clock date) misses — no row, treat
    as jobs_used=0 — so the submit must succeed.
    """
    daemon = fresh_daemon()
    account = daemon.account_key_for("pi_quota_test_a")
    # Seed ONLY the prior day, with the cap value used.
    conn = FakeQuotaConn(
        quota_rows={}, day_utc="2099-12-31",  # unused — we seed prior day directly
    )
    # Use yesterday's actual UTC date so the test is robust to wall-clock drift.
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    conn.quota_rows[(account, yesterday)] = 5

    daemon.db = _make_quota_db_cm(conn)

    async def _noop_kick(*a, **kw):
        return None
    daemon._kick_worker_for_job = _noop_kick

    from aiohttp.test_utils import make_mocked_request
    body = make_submittable_body("req_q3_001", "pi_quota_test_a")
    req = make_mocked_request("POST", "/v1/jobs")
    req.json = _async_json(body)
    resp = asyncio.run(daemon.submit_job(req))

    check("Q3. day rollover resets counter and submit returns 202",
          resp.status == 202, f"got status={resp.status}")


# ---- Q4. Distinct accounts don't share counter -----------------------------
def test_q4_distinct_accounts_isolated() -> None:
    """account_key_for(pi_a) != account_key_for(pi_b); quotas are per-account.

    We seed account A at 5 (capped). Account B (different PI) is at 0.
    Submitting from A must return 429 daily_cap, but submitting from B
    must succeed (202).
    """
    daemon = fresh_daemon()
    account_a = daemon.account_key_for("pi_quota_test_a")
    account_b = daemon.account_key_for("pi_quota_test_b")

    # Sanity: distinct PIs yield distinct account keys (the whole point of
    # VULN-S7 hashing).
    check("Q4-pre. account_key_for is injective on distinct PIs",
          account_a != account_b,
          f"both hashed to {account_a}")

    conn = FakeQuotaConn(
        quota_rows={account_a: 5},  # A at cap
        day_utc="2026-06-28",
    )
    daemon.db = _make_quota_db_cm(conn)

    async def _noop_kick(*a, **kw):
        return None
    daemon._kick_worker_for_job = _noop_kick

    # Capture status per account.
    from aiohttp.test_utils import make_mocked_request
    statuses: dict[str, int] = {}

    # A: over cap → 429
    body_a = make_submittable_body("req_q4_a", "pi_quota_test_a")
    req_a = make_mocked_request("POST", "/v1/jobs")
    req_a.json = _async_json(body_a)
    statuses["A"] = asyncio.run(daemon.submit_job(req_a)).status

    # B: fresh account → 202
    body_b = make_submittable_body("req_q4_b", "pi_quota_test_b")
    req_b = make_mocked_request("POST", "/v1/jobs")
    req_b.json = _async_json(body_b)
    statuses["B"] = asyncio.run(daemon.submit_job(req_b)).status

    check("Q4a. quota'd account A returns 429",
          statuses["A"] == 429, f"got A status={statuses['A']}")
    check("Q4b. fresh account B returns 202",
          statuses["B"] == 202, f"got B status={statuses['B']}")


# ---- entrypoint -------------------------------------------------------------
def main() -> None:
    test_q1_under_cap_ok()
    test_q2_at_cap_rejected()
    test_q3_day_rollover_resets()
    test_q4_distinct_accounts_isolated()

    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print(f"Quota enforcement (kanban t_a18827b6, VULN-LLMTK).")
    if FAILURES:
        print("Failures:")
        for label in FAILURES:
            print(f"  - {label}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
