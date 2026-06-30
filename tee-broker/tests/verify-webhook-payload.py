#!/usr/bin/env python3
"""Verify the broker webhook payload includes the `payment` block (kanban t_84d3e5ee).

What this test proves (offline-friendly, demo mode, no live Stripe calls):
  W1. webhook payload for state=completed includes `payment` block
  W2. webhook payload for state=failed    includes `payment` block (refund path)
  W3. webhook payload for state=awaiting_topup (shortfall) includes `payment` block
  W4. webhook `payment` block equals GET /v1/jobs/{id}`payment` for state=completed
      (asserted as key-set + value equality; ignores ephemeral held_amount_cents
       default-0 for the empty outbox payload)
  W5. webhook `payment` block equals GET /v1/jobs/{id}`payment` for state=failed
  W6. webhook `payment` block equals GET /v1/jobs/{id}`payment` for state=awaiting_topup

This test is the regression guard for the audit finding from t_9fb71ad7:
"The Stripe webhook handler omits the payment block from the emitted payload,
breaking the 'webhook is authoritative for state change' invariant that
t_9fbec867 assumes."

Why we spy on _deliver_webhook: the production code actually POSTs to the
caller-supplied URL. Spying lets us capture the payload that *would* be sent
without needing a real HTTP receiver, and it lets us compare the captured
payload to the GET /v1/jobs/{id} response shape directly.
"""
import os, sys, json, asyncio, tempfile, shutil
from pathlib import Path
from unittest.mock import MagicMock

# Set up temp env BEFORE importing daemon.
TMP_ROOT = Path(tempfile.mkdtemp(prefix="hermes-webhook-payload-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TMP_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
# Force DEMO MODE (no live Stripe calls).
os.environ.pop("STRIPE_SECRET_KEY", None)

TESTS_DIR = Path(__file__).resolve().parent
WORKSPACE = TESTS_DIR.parent
BROKER_DIR = WORKSPACE / "broker-daemon"
sys.path.insert(0, str(BROKER_DIR))

import daemon  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

# Belt-and-braces force demo mode at module level too.
daemon.STRIPE_SECRET_KEY = str()

daemon.init_db()
(daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)

PASS = 0
FAIL = 0
CAPTURED: list[dict] = []  # [(job_id, state, payload), ...]


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        print(f"[PASS] {label}")
        PASS += 1
    else:
        print(f"[FAIL] {label}  {detail}")
        FAIL += 1


def _stub_validate_webhook_url(url, *args, **kwargs):
    """Bypass SSRF validation in tests — we never actually POST.

    The webhook receiver URL must be a real public HTTPS endpoint in
    production (VULN-S1), but for this test we spy on _deliver_webhook
    so the URL is never actually contacted. Patching the validator here
    keeps the test offline and dependency-free.
    """
    return True, ""


def _stub_worker_mgr():
    class _StubWorkerMgr:
        async def ensure_worker(self):
            class _W:
                instance_id = "i-test"
                private_ip = "127.0.0.1"
                launched_at = 0.0
            return _W()

        async def note_job_finished(self):
            pass

    return _StubWorkerMgr()


def _submit_body(pi_id="pi_test_webhook_payload_001"):
    return {
        "client_req_id": "webhook-payload-" + os.urandom(4).hex(),
        "encrypted_skill": "summarize",
        "encrypted_data": "hello",
        "requester_sig": "0x",
        "result_pubkey": "0x",
        "stripe_pi_id": pi_id,
    }


async def _spy_capture(job_id, url, payload, state):
    """Replacement for daemon._deliver_webhook that records the body
    that would have been POSTed (built via _build_webhook_body).
    """
    body = daemon._build_webhook_body(job_id, payload, state)
    CAPTURED.append({"job_id": job_id, "url": url, "state": state, "body": dict(body)})
    # Pretend the POST succeeded so finalize updates webhook_status normally.
    with daemon.db() as conn:
        conn.execute("UPDATE jobs SET webhook_status=? WHERE job_id=?", ("200", job_id))


def _payment_subset(payment: dict) -> dict:
    """Project to the keys the test cares about, dropping ephemeral/demo noise.

    Comparison strategy: pin the keys a downstream consumer cares about
    (status / amount_cents / stripe_id / mode / demo). Excludes
    `held_amount_cents` which can legitimately be None at submit time and is
    not surfaced in webhook payloads in some paths.
    """
    if payment is None:
        return {}
    keep = {"status", "amount_cents", "stripe_id", "mode", "demo"}
    return {k: payment.get(k) for k in keep if k in payment}


async def _run_scenario(state_name: str, finalize_payload: dict, shortfall_cents: int = 0):
    """Submit a job, finalize it, capture the webhook payload, return (webhook, get_resp)."""
    CAPTURED.clear()
    saved_hook = daemon._deliver_webhook
    saved_validate = daemon._validate_webhook_url
    daemon._deliver_webhook = _spy_capture
    daemon._validate_webhook_url = _stub_validate_webhook_url
    try:
        app = daemon.build_app()
        app.on_startup.clear()
        server = TestServer(app)
        async with TestClient(server) as client:
            # Submit a job with a webhook URL so delivery runs.
            body = _submit_body(pi_id=f"pi_test_webhook_{state_name}")
            body["webhook_url"] = "https://example.invalid/webhook-receiver"
            resp = await client.post("/v1/jobs", json=body)
            assert resp.status == 202, f"submit failed: {resp.status}"
            job_id = (await resp.json())["job_id"]

            # For awaiting_topup we have to drive a shortfall path. The
            # simplest way is to set shortfall_cents on the job row after
            # submit but before finalize — _finalize_job's shortfall branch
            # will then flip the state to awaiting_topup.
            if state_name == "awaiting_topup":
                with daemon.db() as conn:
                    conn.execute(
                        "UPDATE jobs SET shortfall_cents=? WHERE job_id=?",
                        (shortfall_cents, job_id),
                    )
                # Now run the finalize — with shortfall_cents set and state=completed,
                # the current code path will detect shortfall and emit awaiting_topup
                # webhook. (See _finalize_job's shortfall branch.)
                await daemon._finalize_job(job_id, {"job_id": job_id, "state": "completed",
                                                     "result": {"summary": "ok"}})
            else:
                await daemon._finalize_job(job_id, finalize_payload)

            # GET /v1/jobs/{id} for comparison.
            resp = await client.get(f"/v1/jobs/{job_id}")
            get_body = await resp.json()
    finally:
        daemon._deliver_webhook = saved_hook
        daemon._validate_webhook_url = saved_validate

    # Find the captured body for THIS job_id (most recent call wins;
    # for awaiting_topup there may be multiple finalize-side writes).
    matching = [c for c in CAPTURED if c["job_id"] == job_id]
    assert matching, f"webhook never delivered for job {job_id}"
    captured = matching[-1]
    return captured["body"], get_body


async def test_completed_payload_includes_payment():
    """W1 + W4: state=completed webhook must include payment block matching GET."""
    CAPTURED.clear()
    body, get_body = await _run_scenario(
        "completed",
        {"job_id": "_placeholder", "state": "completed", "result": {"summary": "ok"}},
    )
    payment = body.get("payment")
    check("W1. webhook payload for state=completed has `payment` block",
          payment is not None and isinstance(payment, dict),
          f"body keys={list(body)}")
    if payment is None:
        return
    # Compare to GET response payment block.
    get_payment = get_body.get("payment")
    check("W4. webhook payment == GET /v1/jobs/{id}`payment` (state=completed)",
          _payment_subset(payment) == _payment_subset(get_payment),
          f"webhook={payment} vs get={get_payment}")
    # Sanity: status reflects capture (demo mode → succeeded).
    check("W4b. webhook payment.status == 'succeeded' for completed job (demo mode)",
          payment.get("status") == "succeeded",
          f"payment={payment}")
    check("W4c. webhook payment.amount_cents is set (not None)",
          payment.get("amount_cents") is not None,
          f"payment={payment}")


async def test_failed_payload_includes_payment():
    """W2 + W5: state=failed webhook must include payment block matching GET."""
    body, get_body = await _run_scenario(
        "failed",
        {"job_id": "_placeholder", "state": "failed", "error": "boom"},
    )
    payment = body.get("payment")
    check("W2. webhook payload for state=failed has `payment` block",
          payment is not None and isinstance(payment, dict),
          f"body keys={list(body)}")
    if payment is None:
        return
    get_payment = get_body.get("payment")
    check("W5. webhook payment == GET /v1/jobs/{id}`payment` (state=failed)",
          _payment_subset(payment) == _payment_subset(get_payment),
          f"webhook={payment} vs get={get_payment}")
    # Failed path runs refund_payment in demo mode → stripe_id starts with re_demo_
    check("W5b. webhook payment.stripe_id is set (refund id in demo mode)",
          payment.get("stripe_id") and str(payment.get("stripe_id")).startswith("re_"),
          f"payment={payment}")


async def test_awaiting_topup_payload_includes_payment():
    """W3 + W6: state=awaiting_topup webhook must include payment block matching GET.

    The shortfall branch in _finalize_job only triggers in LIVE mode (when
    STRIPE_SECRET_KEY is set); in demo mode capture always succeeds and the
    job transitions straight to completed. To exercise the awaiting_topup
    shape deterministically we set the DB row to state='awaiting_topup' +
    shortfall_cents=42 directly, then drive the webhook delivery.
    """
    CAPTURED.clear()
    saved_hook = daemon._deliver_webhook
    saved_validate = daemon._validate_webhook_url
    daemon._deliver_webhook = _spy_capture
    daemon._validate_webhook_url = _stub_validate_webhook_url
    try:
        app = daemon.build_app()
        app.on_startup.clear()
        server = TestServer(app)
        async with TestClient(server) as client:
            body = _submit_body(pi_id="pi_test_webhook_awaiting_topup")
            body["webhook_url"] = "https://example.invalid/webhook-receiver"
            resp = await client.post("/v1/jobs", json=body)
            assert resp.status == 202
            job_id = (await resp.json())["job_id"]

            # Force the row into awaiting_topup state for this scenario.
            with daemon.db() as conn:
                conn.execute(
                    "UPDATE jobs SET state='awaiting_topup', shortfall_cents=42, "
                    "stripe_status=NULL WHERE job_id=?",
                    (job_id,),
                )

            # Drive the webhook directly with the awaiting_topup state.
            # The captured body must include the canonical payment block.
            await daemon._deliver_webhook(
                job_id, "https://example.invalid/webhook-receiver",
                {"result": None}, "awaiting_topup",
            )

            resp = await client.get(f"/v1/jobs/{job_id}")
            get_body = await resp.json()
    finally:
        daemon._deliver_webhook = saved_hook
        daemon._validate_webhook_url = saved_validate

    matching = [c for c in CAPTURED if c["job_id"] == job_id]
    assert matching, f"webhook never delivered for job {job_id}"
    captured_body = matching[-1]["body"]

    payment = captured_body.get("payment")
    check("W3. webhook payload for state=awaiting_topup has `payment` block",
          payment is not None and isinstance(payment, dict),
          f"body keys={list(captured_body)}")
    if payment is None:
        return
    get_payment = get_body.get("payment")
    keep = {"status", "shortfall_cents", "mode"}
    web_subset = {k: payment.get(k) for k in keep if k in payment}
    get_subset = {k: get_payment.get(k) for k in keep if k in get_payment} if get_payment else {}
    check("W6. webhook payment == GET /v1/jobs/{id}`payment` (state=awaiting_topup)",
          web_subset == get_subset,
          f"webhook={payment} vs get={get_payment}")
    check("W6b. webhook payment.status == 'shortfall_required'",
          payment.get("status") == "shortfall_required",
          f"payment={payment}")
    check("W6c. webhook payment.shortfall_cents == 42",
          payment.get("shortfall_cents") == 42,
          f"payment={payment}")


# ---------------------------------------------------------------- entry point

def main():
    # Stub the worker manager so submit_job can complete without AWS creds.
    daemon.worker_mgr = _stub_worker_mgr()

    asyncio.run(test_completed_payload_includes_payment())
    asyncio.run(test_failed_payload_includes_payment())
    asyncio.run(test_awaiting_topup_payload_includes_payment())

    print()
    print(f"=== Summary ===")
    print(f"Passed: {PASS}")
    print(f"Failed: {FAIL}")
    print(f"Webhook payload shape (kanban t_84d3e5ee).")
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
