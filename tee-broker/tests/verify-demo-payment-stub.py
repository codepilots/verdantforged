#!/usr/bin/env python3
"""Verify the broker's demo payment stub route works without Stripe Link.

This is the regional-lockout / demo fallback for users who cannot authenticate
with Stripe Link in their country. The test proves:
  D1. /v1/demo/shared-payment-token returns a synthetic spt_demo_ token
  D2. the payload echoes the expected amount / currency / challenge shape
  D3. the stub route can run even when STRIPE_SECRET_KEY is set, as long as
      BROKER_PAYMENT_STUB_MODE=1 is enabled
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="demo-payment-stub-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_LLM_BASE_URL"] = "http://127.0.0.1:1"
os.environ["BROKER_LLM_API_KEY"] = "stub"
os.environ["BROKER_LLM_MODEL"] = "stub-model"
os.environ["BROKER_SKILLS_API_KEY"] = "test-skills-key-deadbeef"
os.environ["BROKER_PAYMENT_STUB_MODE"] = "1"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_stubbed_demo_only"
os.environ["STRIPE_NETWORK_ID"] = "profile_test_stubbed_demo"
os.environ["STRIPE_CURRENCY"] = "usd"

BROKER_DIR = Path(__file__).resolve().parents[1] / "broker-daemon"
import sys
sys.path.insert(0, str(BROKER_DIR))

import daemon  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"[PASS] {label}")
    else:
        raise AssertionError(f"{label}: {detail}")



async def main() -> int:
    daemon.init_db()
    (daemon.BROKER_EFS_MOUNT / "static").mkdir(parents=True, exist_ok=True)
    app = daemon.build_app()
    app.on_startup.clear()
    server = TestServer(app)
    async with TestClient(server) as client:
        resp = await client.post(
            "/v1/demo/shared-payment-token",
            json={"amount_cents": 130, "currency": "usd"},
        )
        check("D1. stub route responds 200", resp.status == 200, f"status={resp.status}")
        payload = await resp.json()
        token = payload.get("shared_payment_token") or payload.get("spt")
        check("D1b. returned token is spt_demo_...", isinstance(token, str) and token.startswith("spt_demo_"), f"payload={payload}")
        check("D2. amount echoes requested value", payload.get("amount") == 130, f"payload={payload}")
        check("D2b. currency echoes requested value", payload.get("currency") == "usd", f"payload={payload}")
        check("D2c. challenge is present", "Payment amount=\"130\"" in payload.get("challenge", ""), f"payload={payload}")
        check("D3. mode is demo", payload.get("mode") == "demo", f"payload={payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
