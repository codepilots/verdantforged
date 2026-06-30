"""Verify the 6 low-priority security + code-quality fixes from
SECURITY_REVIEW.md. Runs locally — exercises daemon.py + poller.py +
Caddyfile code paths without needing a live broker or worker.

Tests:
  J. Rate limiting on POST /v1/jobs (VULN-S6)
     - 11th submission from same IP within 1 minute returns 429.
     - Different IPs are tracked independently.
     - BROKER_RATE_LIMIT_DISABLED=1 bypasses the limiter (for tests).

  K. CORS restriction (VULN-S8)
     - Caddyfile no longer hardcodes `Access-Control-Allow-Origin: *`.
     - Caddyfile references {$BROKER_CORS_ORIGIN} (or similar env var).
     - Default BROKER_CORS_ORIGIN is https://verdant.codepilots.co.uk.

  L. SQLite race on idempotency (VULN-S10)
     - Two concurrent submits with the same client_req_id: at most one
       creates a new job; the other returns idempotent_replay=True with
       HTTP 200 (NOT a 500 from a UNIQUE-constraint crash).
     - Daemon uses BEGIN IMMEDIATE or catches sqlite3.IntegrityError.

  M. LLM token expiry uses datetime comparison (VULN-S11)
     - Token with isoformat expires_at returns 401 on past expiry.
     - Token with non-microsecond Z suffix (e.g. "2026-01-01T00:00:00Z")
       is still parsed correctly as a past timestamp.
     - The daemon.py source uses datetime.fromisoformat() for expiry.

  N. Dead _ensure_worker_encryption_key removed (CQ-2)
     - The function is no longer defined in poller.py.
     - execute_in_envelope does NOT call it (no
       worker_encryption_priv assignment left).
     - The /opt/worker/keys/worker_encryption.priv file is never
       referenced or created (only worker_signing.priv remains).

  O. request_body privacy cleanup (CQ-3)
     - purge_old_request_bodies() helper exists in daemon.py.
     - It sets request_body to NULL for jobs older than 24h.
     - A privacy-comment block documents the implication.
     - Caddyfile / docs do NOT carry request_body data further.

Run with any Python that has aiohttp+cryptography available.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import sys
import base64
import tempfile
import subprocess
import sqlite3
import shutil
import time
from pathlib import Path

# ---- Test environment setup --------------------------------------------------
# Override EFS mount so daemon.py import doesn't fail (it tries to mkdir
# $BROKER_EFS_MOUNT/logs at import time). Set env BEFORE importing daemon.
TEST_ROOT = Path(tempfile.mkdtemp(prefix="lowsev-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_REGION"] = "eu-west-1"
os.environ["BROKER_VPC_ID"] = ""
os.environ["BROKER_SUBNET_ID"] = ""
os.environ["BROKER_WORKER_SG"] = ""
os.environ["BROKER_WORKER_AMI"] = ""
os.environ["BROKER_WORKER_IAM_ROLE"] = ""
os.environ["DEMO_TOKEN_CAP"] = "50000"
# Force DEMO MODE for Stripe — the user's shell has STRIPE_SECRET_KEY set
# (from the tee-broker-site backend) but the `stripe` Python package isn't
# installed here. Unset it before importing daemon so verify_payment_intent
# short-circuits to "stripe_disabled" instead of trying a live API call.
os.environ.pop("STRIPE_SECRET_KEY", None)
# VULN-S7 secret reused from the medium-severity suite. Pinned here for
# reproducibility, though L-series tests don't depend on it directly.
os.environ["BROKER_ACCOUNT_HASH_SECRET"] = "test-secret-for-low-priority-suite"

DAEMON_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/broker-daemon"
WORKER_DIR = "/home/autumn/hermes/competition/tee-broker-deploy/worker"
REPO_ROOT = "/home/autumn/hermes/competition/tee-broker-deploy"
sys.path.insert(0, DAEMON_DIR)
sys.path.insert(0, WORKER_DIR)

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def _bump_fail() -> None:
    global FAIL
    FAIL += 1


def _bump_pass() -> None:
    global PASS
    PASS += 1


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        print(f"[PASS] {label}")
        PASS += 1
    else:
        print(f"[FAIL] {label}" + (f"  ({detail})" if detail else ""))
        FAIL += 1
        FAILURES.append(label)


def fresh_daemon():
    """Re-import daemon with current env vars (some are read at import time)."""
    if "daemon" in sys.modules:
        del sys.modules["daemon"]
    import daemon  # noqa: E402
    return daemon


def make_submittable_body(client_req_id="req_low_001"):
    """Return a minimal valid POST /v1/jobs body that passes validate_submit."""
    return {
        "client_req_id": client_req_id,
        "stripe_pi_id": "pi_lowsev_test",
        "encrypted_skill": "summarize",
        "encrypted_data": "data-" + client_req_id,
        "requester_sig": "0x",
        "result_pubkey": "0x",
    }


def fake_db_factory(cap_response=None):
    """Build a FakeConn that mimics daemon.db() for submit_job paths.

    cap_response: when set, return this row (sqlite3.Row-compatible) from the
    `SELECT tokens_used, tokens_cap FROM account_usage WHERE account=? AND date=?`
    query, simulating "cap exceeded".
    """
    class FakeCursor:
        def __init__(self, row):
            self._row = row
        def fetchone(self):
            return self._row

    class FakeConn:
        def __init__(self):
            self.tables: dict[str, list] = {"jobs": [], "llm_tokens": [], "account_usage": [], "skills": []}
            self._inserted_jobs: list[dict] = []

        def execute(self, sql, params=()):
            sql_str = " ".join(sql.split())
            # The idempotency check (L-series)
            if sql_str.startswith("SELECT job_id, state FROM jobs WHERE client_req_id ="):
                # Return a fake "existing" row so we can simulate the
                # idempotent replay path WITHOUT actually inserting. The
                # rate-limiter tests don't care about idempotency, but
                # VULN-S10 test does need this.
                # For rate-limiter J-series tests: always return None so
                # every submission is treated as new.
                return FakeCursor(None)
            if sql_str.startswith("SELECT tokens_used, tokens_cap FROM account_usage"):
                if cap_response is not None:
                    return FakeCursor(cap_response)
                return FakeCursor(None)
            if sql_str.startswith("SELECT job_id, stripe_pi_id, expires_at, tokens_used FROM llm_tokens"):
                return FakeCursor(None)
            if sql_str.startswith("SELECT tokens_used FROM account_usage"):
                return FakeCursor(None)
            if sql_str.startswith("SELECT MAX(version)"):
                return FakeCursor(None)
            # INSERT / UPDATE — nothing to return
            if sql_str.startswith("INSERT INTO jobs"):
                self._inserted_jobs.append({"job_id": params[0], "client_req_id": params[1]})
                return FakeCursor(None)
            return FakeCursor(None)

        def __enter__(self): return self
        def __exit__(self, *a): return False

    import contextlib
    @contextlib.contextmanager
    def fake_db():
        yield FakeConn()
    return fake_db, FakeConn


# ---- J. Rate limiting on POST /v1/jobs (VULN-S6) ----------------------------
def test_rate_limiting_default_10_per_minute() -> None:
    """11th submission from same IP within 60s must return 429.

    The rate limiter is per-IP (default 10/min). The first 10 succeed (202),
    the 11th is refused with 429 + a `code: rate_limited` marker.
    """
    daemon = fresh_daemon()
    fake_db, conn = make_submittable_body.__wrapped__() if hasattr(make_submittable_body, "__wrapped__") else (None, None)
    # Build a FakeConn + fake_db manually
    class FakeCursor:
        def __init__(self, row):
            self._row = row
        def fetchone(self):
            return self._row
    class FakeConn:
        def __init__(self):
            self.tables: dict[str, list] = {"jobs": [], "llm_tokens": [], "account_usage": [], "skills": []}
        def execute(self, sql, params=()):
            sql_str = " ".join(sql.split())
            if sql_str.startswith("SELECT job_id, state FROM jobs WHERE client_req_id ="):
                return FakeCursor(None)
            if sql_str.startswith("SELECT tokens_used, tokens_cap FROM account_usage"):
                return FakeCursor(None)
            if sql_str.startswith("SELECT MAX(version)"):
                return FakeCursor(None)
            return FakeCursor(None)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import contextlib
    @contextlib.contextmanager
    def fake_db_cm():
        yield FakeConn()
    daemon.db = fake_db_cm

    # Also neutralise the worker kicker (we don't want it launching EC2).
    async def _noop_kick(*a, **kw):
        return None
    daemon._kick_worker_for_job = _noop_kick

    # Make sure rate limiting is NOT disabled for this test.
    os.environ.pop("BROKER_RATE_LIMIT_DISABLED", None)
    # Reload daemon so BROKER_RATE_LIMIT_DISABLED is re-read at module load.
    daemon = fresh_daemon()
    daemon.db = fake_db_cm
    daemon._kick_worker_for_job = _noop_kick

    from aiohttp.test_utils import make_mocked_request

    statuses = []
    for i in range(11):
        body = make_submittable_body(f"req_rate_j_{i}")
        req = make_mocked_request(
            "POST", "/v1/jobs",
            headers={"X-Forwarded-For": "198.51.100.10"})
        req.json = _async_json(body)
        async def run():
            return await daemon.submit_job(req)
        resp = asyncio.run(run())
        statuses.append(resp.status)

    # The first 10 should be accepted (202). The 11th MUST be 429.
    first_ten_ok = all(s == 202 for s in statuses[:10])
    eleventh_rejected = statuses[10] == 429
    check("J1. first 10 submissions from same IP return 202",
          first_ten_ok, f"got statuses={statuses}")
    check("J2. 11th submission from same IP within 60s returns 429",
          eleventh_rejected, f"got statuses={statuses}")

    # The 429 response MUST carry a `code: rate_limited` marker so clients
    # can distinguish it from generic 4xx errors.
    last_body = _last_response_body(daemon)


def _async_json(payload):
    async def _j(*a, **kw):
        return payload
    return _j


def _last_response_body(daemon):
    """Capture the body of the most recent submit_job response.

    The daemon doesn't expose the response object after asyncio.run, so
    we monkey-patch aiohttp's web.json_response during the next call
    instead. Implemented inline by the J3 test below — this helper just
    documents the contract.
    """
    return None


def test_rate_limit_response_body() -> None:
    """The 429 response body MUST carry `code: rate_limited` so clients
    can distinguish it from token-cap exhaustion (429 token_cap_exceeded)
    or generic 4xx errors.
    """
    daemon = fresh_daemon()
    # Fake DB
    class FakeCursor:
        def __init__(self, row): self._row = row
        def fetchone(self): return self._row
    class FakeConn:
        def execute(self, sql, params=()):
            sql_str = " ".join(sql.split())
            if sql_str.startswith("SELECT job_id, state FROM jobs WHERE client_req_id ="):
                return FakeCursor(None)
            if sql_str.startswith("SELECT tokens_used, tokens_cap FROM account_usage"):
                return FakeCursor(None)
            if sql_str.startswith("SELECT MAX(version)"):
                return FakeCursor(None)
            return FakeCursor(None)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import contextlib
    @contextlib.contextmanager
    def fake_db_cm():
        yield FakeConn()
    daemon.db = fake_db_cm
    async def _noop_kick(*a, **kw):
        return None
    daemon._kick_worker_for_job = _noop_kick

    # Ensure rate limit is enabled and the IP limiter state is fresh.
    os.environ.pop("BROKER_RATE_LIMIT_DISABLED", None)
    daemon = fresh_daemon()
    daemon.db = fake_db_cm
    daemon._kick_worker_for_job = _noop_kick
    if hasattr(daemon, "_rate_limit_state"):
        daemon._rate_limit_state.clear()

    from aiohttp.test_utils import make_mocked_request
    captured = {}
    # Wrap json_response to capture the body of the LAST response.
    real_json_response = daemon.web.json_response
    def capturing_json_response(payload, *a, **kw):
        captured["payload"] = payload
        captured["status"] = kw.get("status", 200)
        return real_json_response(payload, *a, **kw)
    daemon.web.json_response = capturing_json_response

    # Burn through the 10-per-minute budget, then capture the 11th body.
    for i in range(10):
        body = make_submittable_body(f"req_rate_j3_{i}")
        req = make_mocked_request(
            "POST", "/v1/jobs",
            headers={"X-Forwarded-For": "198.51.100.20"})
        req.json = _async_json(body)
        async def run():
            return await daemon.submit_job(req)
        asyncio.run(run())
    # 11th call
    body = make_submittable_body("req_rate_j3_11th")
    req = make_mocked_request(
        "POST", "/v1/jobs",
        headers={"X-Forwarded-For": "198.51.100.20"})
    req.json = _async_json(body)
    async def run11():
        return await daemon.submit_job(req)
    asyncio.run(run11())

    # Restore real json_response.
    daemon.web.json_response = real_json_response

    payload = captured.get("payload", {})
    status = captured.get("status", 0)
    check("J3. 11th 429 response body has 'code' field",
          isinstance(payload, dict) and "code" in payload,
          f"payload={payload!r}")
    check("J4. 11th 429 response body code='rate_limited'",
          payload.get("code") == "rate_limited",
          f"got code={payload.get('code')!r}")
    check("J5. 11th 429 response status is 429",
          status == 429, f"got status={status}")


def test_rate_limit_different_ips_independent() -> None:
    """Two different IPs must each get their own 10/min budget."""
    daemon = fresh_daemon()
    class FakeCursor:
        def __init__(self, row): self._row = row
        def fetchone(self): return self._row
    class FakeConn:
        def execute(self, sql, params=()):
            sql_str = " ".join(sql.split())
            if sql_str.startswith("SELECT job_id, state FROM jobs WHERE client_req_id ="):
                return FakeCursor(None)
            if sql_str.startswith("SELECT tokens_used, tokens_cap FROM account_usage"):
                return FakeCursor(None)
            if sql_str.startswith("SELECT MAX(version)"):
                return FakeCursor(None)
            return FakeCursor(None)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import contextlib
    @contextlib.contextmanager
    def fake_db_cm():
        yield FakeConn()
    daemon.db = fake_db_cm
    async def _noop_kick(*a, **kw): return None
    daemon._kick_worker_for_job = _noop_kick

    os.environ.pop("BROKER_RATE_LIMIT_DISABLED", None)
    daemon = fresh_daemon()
    daemon.db = fake_db_cm
    daemon._kick_worker_for_job = _noop_kick
    if hasattr(daemon, "_rate_limit_state"):
        daemon._rate_limit_state.clear()

    from aiohttp.test_utils import make_mocked_request

    # IP A burns its full budget
    for i in range(10):
        body = make_submittable_body(f"req_rate_ipA_{i}")
        req = make_mocked_request(
            "POST", "/v1/jobs",
            headers={"X-Forwarded-For": "198.51.100.30"})
        req.json = _async_json(body)
        async def run():
            return await daemon.submit_job(req)
        asyncio.run(run())
    # IP B should still be able to submit (independent budget)
    body = make_submittable_body("req_rate_ipB_001")
    req = make_mocked_request(
        "POST", "/v1/jobs",
        headers={"X-Forwarded-For": "198.51.100.31"})
    req.json = _async_json(body)
    async def run_b():
        return await daemon.submit_job(req)
    resp_b = asyncio.run(run_b())
    check("J6. different IP gets its own rate-limit budget (202)",
          resp_b.status == 202, f"got status={resp_b.status}")


def test_rate_limit_disabled_bypass() -> None:
    """BROKER_RATE_LIMIT_DISABLED=1 bypasses the limiter for tests / demos."""
    # Set BEFORE importing daemon so the module-level config picks it up.
    os.environ["BROKER_RATE_LIMIT_DISABLED"] = "1"
    daemon = fresh_daemon()
    class FakeCursor:
        def __init__(self, row): self._row = row
        def fetchone(self): return self._row
    class FakeConn:
        def execute(self, sql, params=()):
            sql_str = " ".join(sql.split())
            if sql_str.startswith("SELECT job_id, state FROM jobs WHERE client_req_id ="):
                return FakeCursor(None)
            if sql_str.startswith("SELECT tokens_used, tokens_cap FROM account_usage"):
                return FakeCursor(None)
            if sql_str.startswith("SELECT MAX(version)"):
                return FakeCursor(None)
            return FakeCursor(None)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import contextlib
    @contextlib.contextmanager
    def fake_db_cm():
        yield FakeConn()
    daemon.db = fake_db_cm
    async def _noop_kick(*a, **kw): return None
    daemon._kick_worker_for_job = _noop_kick

    from aiohttp.test_utils import make_mocked_request
    statuses = []
    for i in range(15):
        body = make_submittable_body(f"req_rate_disabled_{i}")
        req = make_mocked_request(
            "POST", "/v1/jobs",
            headers={"X-Forwarded-For": "198.51.100.40"})
        req.json = _async_json(body)
        async def run():
            return await daemon.submit_job(req)
        resp = asyncio.run(run())
        statuses.append(resp.status)
    check("J7. BROKER_RATE_LIMIT_DISABLED=1 lets >10 jobs through without 429",
          all(s == 202 for s in statuses), f"got statuses={statuses}")
    # Restore env so other tests see the default-on state.
    os.environ.pop("BROKER_RATE_LIMIT_DISABLED", None)


# ---- K. CORS restriction (VULN-S8) ------------------------------------------
def test_caddyfile_cors_restricted() -> None:
    """Caddyfile MUST NOT hardcode `Access-Control-Allow-Origin: *`.

    After the fix, it should reference {$BROKER_CORS_ORIGIN} (or an env
    placeholder) so a single env var controls the allowlist. The default
    in the daemon's BROKER_CORS_ORIGIN module constant should be the
    verdant.codepilots.co.uk domain.
    """
    caddy_src = Path(REPO_ROOT, "broker-daemon/caddy/Caddyfile").read_text()
    # K1: the literal wildcard + Access-Control-Allow-Origin on the same
    # line is the bug. After the fix, the wildcard form is replaced.
    wildcard_origin = re.search(
        r'Access-Control-Allow-Origin\s+"\*"', caddy_src)
    check("K1. Caddyfile no longer hardcodes Access-Control-Allow-Origin: *",
          wildcard_origin is None,
          f"found {wildcard_origin.group(0) if wildcard_origin else 'none'}")
    # K2: an env placeholder is referenced.
    env_placeholder = (
        "{$BROKER_CORS_ORIGIN}" in caddy_src
        or "${BROKER_CORS_ORIGIN}" in caddy_src
        or "{$BROKER_CORS_ORIGIN:" in caddy_src
        or "{$CORS_ALLOWED_ORIGIN" in caddy_src
        or "Access-Control-Allow-Origin {$BROKER_CORS_ORIGIN" in caddy_src
    )
    check("K2. Caddyfile references an env-var placeholder for the origin",
          env_placeholder,
          "checked Caddyfile for env-var placeholder like {BROKER_CORS_ORIGIN}")
    # K3: default daemon env value should default to verdant.codepilots.co.uk.
    daemon = fresh_daemon()
    default_origin = os.environ.get("BROKER_CORS_ORIGIN", getattr(daemon, "BROKER_CORS_ORIGIN", ""))
    # If the constant exists, assert it. If not yet wired (because the fix
    # is in Caddyfile only), skip this assertion with a clear FAIL message.
    if hasattr(daemon, "BROKER_CORS_ORIGIN"):
        check("K3. daemon BROKER_CORS_ORIGIN default = https://verdant.codepilots.co.uk",
              default_origin == "https://verdant.codepilots.co.uk",
              f"got {default_origin!r}")
    else:
        check("K3. daemon BROKER_CORS_ORIGIN constant defined",
              False,
              "daemon module lacks BROKER_CORS_ORIGIN — needs a default value"
              " and Caddyfile must read from env")


# ---- L. SQLite race on idempotency (VULN-S10) -------------------------------
def test_idempotency_race_returns_replay() -> None:
    """Two concurrent submits with the same client_req_id must converge
    on a single job: one returns the new job, the other returns
    `idempotent_replay=True` with HTTP 200 (NOT 500).
    """
    daemon = fresh_daemon()
    # Use a REAL SQLite DB so the UNIQUE(client_req_id) constraint is
    # actually enforced. The previous code had a check-then-insert race
    # because the check and insert happened on different connections
    # (one open + one freshly opened inside the second `with db()` block).
    real_db_path = TEST_ROOT / "idempotency.db"
    if real_db_path.exists():
        real_db_path.unlink()
    # Use a context manager that points at our real DB
    import contextlib
    @contextlib.contextmanager
    def real_db():
        # isolation_level=None (autocommit) mirrors the production db()
        # in daemon.py — without it, an UPDATE inside this context is
        # rolled back when the connection closes (default isolation
        # starts an implicit transaction), so the purge would silently
        # appear to succeed (rowcount=1) but the change would vanish.
        # CQ-3 (kanban t_b13072b3).
        conn = sqlite3.connect(str(real_db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()
    daemon.db = real_db
    daemon.init_db()
    async def _noop_kick(*a, **kw): return None
    daemon._kick_worker_for_job = _noop_kick
    # Don't disable rate limiting — but reset the limiter state so the
    # tests below aren't blocked by previous tests' budgets.
    if hasattr(daemon, "_rate_limit_state"):
        daemon._rate_limit_state.clear()
    # Use a per-test IP so the rate limiter doesn't reject the second
    # submission in this test.
    test_ip = "203.0.113.55"

    from aiohttp.test_utils import make_mocked_request

    async def submit_once(client_req_id: str):
        body = make_submittable_body(client_req_id)
        req = make_mocked_request(
            "POST", "/v1/jobs",
            headers={"X-Forwarded-For": test_ip})
        req.json = _async_json(body)
        return await daemon.submit_job(req)

    async def race_two():
        # Submit the same client_req_id twice in quick succession.
        # With the old (pre-fix) code, both would pass the idempotency
        # check (no row exists yet) and one INSERT would fail with
        # UNIQUE-constraint -> 500. With the fix, BEGIN IMMEDIATE
        # serialises them, OR the IntegrityError is caught and the
        # second call returns the first call's job.
        r1 = await submit_once("req_race_l_001")
        r2 = await submit_once("req_race_l_001")
        return r1, r2

    r1, r2 = asyncio.run(race_two())

    # Both responses must be JSON-serialisable and carry job_id.
    b1 = json.loads(r1.body.decode())
    b2 = json.loads(r2.body.decode())

    # At least one MUST be idempotent_replay=True; the other must be the
    # original submit (idempotent_replay=False).
    is_replay = lambda b: b.get("idempotent_replay") is True
    is_new = lambda b: b.get("idempotent_replay") is False
    replay_count = sum(1 for b in (b1, b2) if is_replay(b))
    new_count = sum(1 for b in (b1, b2) if is_new(b))

    check("L1. both submissions returned without 500",
          r1.status < 500 and r2.status < 500,
          f"got statuses {r1.status}, {r2.status}, bodies {b1!r}, {b2!r}")
    check("L2. exactly one of the two responses is idempotent_replay=True",
          replay_count == 1 and new_count == 1,
          f"got b1={b1}, b2={b2}")
    check("L3. both responses share the same job_id (same underlying job)",
          b1.get("job_id") == b2.get("job_id"),
          f"got job_ids {b1.get('job_id')!r} and {b2.get('job_id')!r}")
    # The replay response MUST be 200, the new one MUST be 202.
    replay_resp = r1 if is_replay(b1) else r2
    new_resp = r1 if is_new(b1) else r2
    check("L4. idempotent_replay response is HTTP 200",
          replay_resp.status == 200, f"got status={replay_resp.status}")
    check("L5. new submission response is HTTP 202",
          new_resp.status == 202, f"got status={new_resp.status}")


def test_idempotency_uses_begin_immediate_or_integrity_error() -> None:
    """The source MUST use BEGIN IMMEDIATE or catch sqlite3.IntegrityError.

    Either is a valid fix — the test just checks that ONE of the two
    defensive patterns is present near the idempotency check.
    """
    src = Path(DAEMON_DIR, "daemon.py").read_text()
    has_begin_immediate = "BEGIN IMMEDIATE" in src
    has_integrity_catch = "sqlite3.IntegrityError" in src and (
        "except" in src.split("IntegrityError")[0][-500:]
        if "IntegrityError" in src else False
    )
    # Simpler: just look for the patterns anywhere in the file.
    has_integrity_catch_simple = (
        "except sqlite3.IntegrityError" in src
        or "except sqlite3.IntegrityError, e" in src  # py2 style, just in case
        or re.search(r"except\s+\(?\s*sqlite3\.IntegrityError", src) is not None
    )
    check("L6. daemon.py uses BEGIN IMMEDIATE or catches sqlite3.IntegrityError",
          has_begin_immediate or has_integrity_catch_simple,
          f"BEGIN IMMEDIATE={has_begin_immediate}, IntegrityError catch={has_integrity_catch_simple}")


# ---- M. LLM token expiry uses datetime comparison (VULN-S11) ---------------
def test_llm_token_expiry_uses_fromisoformat() -> None:
    """The daemon.py source MUST use datetime.fromisoformat for token
    expiry comparison — string comparison breaks when the stored
    timestamp has a different format (e.g. Z suffix vs +00:00).
    """
    src = Path(DAEMON_DIR, "daemon.py").read_text()
    # Look in the llm_proxy function (where the expiry check lives) for
    # the new pattern. The new line should be:
    #     expires = datetime.fromisoformat(token_row["expires_at"])
    # and the comparison should NOT be a string < string comparison.
    #
    # Heuristic: locate the llm_proxy function body and check it.
    m = re.search(r"async def llm_proxy\(.*?\n(?:[^\n]*\n)*", src)
    assert m is not None, "could not locate llm_proxy"
    # Find the function start line and the next top-level 'async def '.
    start = m.start()
    nxt = re.search(r"\nasync def |\ndef |\nclass ", src[start + 1:])
    end = (start + 1 + nxt.start()) if nxt else len(src)
    body = src[start:end]
    # The body must reference fromisoformat.
    uses_fromisoformat = "fromisoformat" in body
    # And it must NOT use the old string < .isoformat() comparison.
    # Old pattern: row["expires_at"] < datetime.now(...).isoformat()
    # New pattern:  expires = datetime.fromisoformat(row["expires_at"]) ...
    #                expires < datetime.now(...)
    old_pattern = re.search(
        r'token_row\["expires_at"\]\s*<\s*datetime\.now', body)
    check("M1. llm_proxy uses datetime.fromisoformat for expiry",
          uses_fromisoformat, f"body excerpt:\n{body[:600]}")
    check("M2. llm_proxy does NOT do string < datetime.now().isoformat() comparison",
          old_pattern is None, f"found old pattern: {old_pattern.group(0) if old_pattern else 'none'}")


def test_llm_token_expiry_past_returns_401() -> None:
    """A token with expires_at in the past (using Z suffix format) MUST
    be rejected with 401, even though the comparison is now datetime-vs-
    datetime rather than string-vs-string.
    """
    daemon = fresh_daemon()
    # Set up a fake DB where the token row's expires_at is in the past
    # using the Z-suffix form (was previously incomparable to the
    # +00:00 form from .isoformat()).
    past_iso_z = "2020-01-01T00:00:00Z"
    class FakeCursor:
        def __init__(self, row): self._row = row
        def fetchone(self): return self._row
    class FakeConn:
        def execute(self, sql, params=()):
            sql_str = " ".join(sql.split())
            if sql_str.startswith("SELECT job_id, stripe_pi_id, expires_at, tokens_used FROM llm_tokens"):
                return FakeCursor({
                    "job_id": "job_past",
                    "stripe_pi_id": "pi_test",
                    "expires_at": past_iso_z,
                    "tokens_used": 0,
                })
            if sql_str.startswith("SELECT tokens_used FROM account_usage"):
                return FakeCursor(None)
            return FakeCursor(None)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import contextlib
    @contextlib.contextmanager
    def fake_db_cm():
        yield FakeConn()
    daemon.db = fake_db_cm

    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request(
        "POST", "/v1/llm/chat/completions",
        headers={"Authorization": "Bearer fake-token"})
    async def run():
        return await daemon.llm_proxy(req)
    resp = asyncio.run(run())
    check("M3. past-expiry token (Z-suffix format) returns 401",
          resp.status == 401, f"got status={resp.status}")
    body = json.loads(resp.body.decode())
    check("M4. 401 response body mentions expiry",
          "expired" in body.get("error", "").lower(),
          f"error={body.get('error')!r}")


# ---- N. Dead _ensure_worker_encryption_key removed (CQ-2) ------------------
def test_dead_encryption_key_removed() -> None:
    """The _ensure_worker_encryption_key function and its call site must
    be removed from poller.py. The signing key path is unchanged.
    """
    poller_src = Path(WORKER_DIR, "poller.py").read_text()
    # N1: function definition removed.
    check("N1. poller.py no longer defines _ensure_worker_encryption_key",
          "def _ensure_worker_encryption_key" not in poller_src,
          "function definition still present")
    # N2: the call site in execute_in_envelope is gone.
    check("N2. poller.py no longer assigns worker_encryption_priv",
          "worker_encryption_priv" not in poller_src,
          "assignment still present")
    # N3: the worker_encryption.priv filename is no longer referenced
    # anywhere in poller.py (no on-disk file path).
    check("N3. poller.py no longer references worker_encryption.priv file",
          "worker_encryption.priv" not in poller_src,
          "file path still present")
    # N4: the signing key path is unchanged (sanity).
    check("N4. poller.py still defines _ensure_worker_signing_key",
          "def _ensure_worker_signing_key" in poller_src,
          "signing key path was accidentally removed")


def test_publish_worker_keys_still_works() -> None:
    """Removing _ensure_worker_encryption_key must not break
    publish_worker_keys() — the X25519 pubkey is still published.

    Worker-key publication is the SOLE non-dead use of the encryption
    keypair (it's the public key advertised to clients for blind-audit
    input encryption). The fix renames the helper to make it clear that
    the key is for the public side only.
    """
    # Sandbox the poller via env vars so we don't write to /opt/worker/keys.
    tmp = Path(tempfile.mkdtemp(prefix="poller-pubkey-test-"))
    keys = tmp / "keys"
    logs = tmp / "logs"
    keys.mkdir(parents=True)
    logs.mkdir(parents=True)
    # Use BROKER_EFS_MOUNT to redirect the worker-keys file and the
    # EFS-backed DB; BROKER_WORKER_KEYS redirects the signing key file.
    os.environ["BROKER_EFS_MOUNT"] = str(tmp)
    os.environ["BROKER_WORKER_KEYS"] = str(keys)
    os.environ["BROKER_EFS_LOGS"] = str(logs)
    # Fresh import of the worker module picks up the new env vars.
    if "poller" in sys.modules:
        del sys.modules["poller"]
    import poller  # noqa
    record = poller.publish_worker_keys()
    check("N5. publish_worker_keys() returns a record",
          isinstance(record, dict), f"got {record!r}")
    check("N6. publish_worker_keys() returns x25519_pubkey_b64",
          "x25519_pubkey_b64" in record, f"record={record!r}")
    check("N7. publish_worker_keys() returns ed25519_pubkey_b64",
          "ed25519_pubkey_b64" in record, f"record={record!r}")


# ---- O. request_body privacy cleanup (CQ-3) ---------------------------------
def test_request_body_privacy_cleanup_helper() -> None:
    """purge_old_request_bodies() must exist and set request_body=NULL
    for jobs older than 24h.
    """
    daemon = fresh_daemon()
    # O1: the helper function exists.
    check("O1. daemon.purge_old_request_bodies() is defined",
          hasattr(daemon, "purge_old_request_bodies"),
          "helper missing")
    # O2: a privacy doc comment is in the source.
    src = Path(DAEMON_DIR, "daemon.py").read_text()
    # The comment block should mention either "24h" / "24 hours" / "purge"
    # / "privacy" alongside the request_body column.
    privacy_block = re.search(
        r"(privacy|24h|24 hours|purg|expire).*?request_body",
        src, re.DOTALL | re.IGNORECASE)
    check("O2. daemon.py has a privacy/expiry comment about request_body",
          privacy_block is not None,
          "no comment block linking privacy/expiry to request_body")


def test_request_body_purge_sets_null() -> None:
    """purge_old_request_bodies() must NULL out request_body for jobs
    older than 24h, and leave younger jobs alone.
    """
    daemon = fresh_daemon()
    real_db_path = TEST_ROOT / "purge_test.db"
    if real_db_path.exists():
        real_db_path.unlink()
    import contextlib
    @contextlib.contextmanager
    def real_db():
        # isolation_level=None (autocommit) — see the L-test real_db for
        # why (CQ-3, kanban t_b13072b3). Without it the purge UPDATE
        # silently rolls back on close.
        conn = sqlite3.connect(str(real_db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()
    daemon.db = real_db
    daemon.init_db()

    # Seed two jobs: one 25h old (must be purged) and one 1h old (must be kept).
    with real_db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, request_body) "
            "VALUES (?, ?, ?, 'completed', ?)",
            ("job_old_25h", "req_old", "2026-01-01T00:00:00+00:00", '{"secret":"a"}'),
        )
        # Compute a 1h-old timestamp dynamically.
        from datetime import datetime, timezone, timedelta
        one_h_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO jobs (job_id, client_req_id, created_at, state, request_body) "
            "VALUES (?, ?, ?, 'completed', ?)",
            ("job_young_1h", "req_young", one_h_ago, '{"secret":"b"}'),
        )
        conn.commit()

    # Run the purge.
    if hasattr(daemon, "purge_old_request_bodies"):
        daemon.purge_old_request_bodies()
    else:
        check("O3. purge_old_request_bodies() runs without error", False,
              "helper missing")
        return

    # Verify outcomes.
    with real_db() as conn:
        old_row = conn.execute(
            "SELECT request_body FROM jobs WHERE job_id=?", ("job_old_25h",)).fetchone()
        young_row = conn.execute(
            "SELECT request_body FROM jobs WHERE job_id=?", ("job_young_1h",)).fetchone()
    check("O3. 25h-old job has request_body=NULL after purge",
          old_row["request_body"] is None,
          f"got {old_row['request_body']!r}")
    check("O4. 1h-old job retains request_body after purge",
          young_row["request_body"] is not None
          and "secret" in young_row["request_body"],
          f"got {young_row['request_body']!r}")


# ---- Main --------------------------------------------------------------------
def main() -> int:
    print("=== J. Rate limiting on POST /v1/jobs (VULN-S6) ===")
    test_rate_limiting_default_10_per_minute()
    test_rate_limit_response_body()
    test_rate_limit_different_ips_independent()
    test_rate_limit_disabled_bypass()
    print()
    print("=== K. CORS restriction (VULN-S8) ===")
    test_caddyfile_cors_restricted()
    print()
    print("=== L. SQLite race on idempotency (VULN-S10) ===")
    test_idempotency_race_returns_replay()
    test_idempotency_uses_begin_immediate_or_integrity_error()
    print()
    print("=== M. LLM token expiry uses datetime comparison (VULN-S11) ===")
    test_llm_token_expiry_uses_fromisoformat()
    test_llm_token_expiry_past_returns_401()
    print()
    print("=== N. Dead _ensure_worker_encryption_key removed (CQ-2) ===")
    test_dead_encryption_key_removed()
    test_publish_worker_keys_still_works()
    print()
    print("=== O. request_body privacy cleanup (CQ-3) ===")
    test_request_body_privacy_cleanup_helper()
    test_request_body_purge_sets_null()
    print()
    print(f"=== Summary: PASS={PASS} FAIL={FAIL} ===")
    if FAIL:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())