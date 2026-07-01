#!/usr/bin/env python3
"""Regression test for EC2 RunInstances user-data size.

AWS rejects RunInstances when UserData exceeds 16,384 bytes. The full worker
bootstrap is intentionally larger than that and must stay EFS-resident; the
daemon should render only a tiny loader into RunInstances.UserData.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile

REPO = Path(__file__).resolve().parent.parent
DAEMON = REPO / "broker-daemon" / "daemon.py"


def load_daemon(efs_mount: str):
    os.environ["BROKER_EFS_MOUNT"] = efs_mount
    sys.path.insert(0, str((REPO / "broker-daemon").resolve()))
    spec = importlib.util.spec_from_file_location("daemon_under_test", DAEMON)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="vf-userdata-") as tmp:
        mod = load_daemon(tmp)
        mod.LOGS.mkdir(parents=True, exist_ok=True)
        full_bootstrap = "\n".join([
            "set -u",
            "log 'NemoClaw install via official installer'",
            "echo __EFS_DNS__ __ARTIFACT_BUCKET__ __ARTIFACT_REGION__",
            "echo ${BROKER_ONBOARD_TOKEN:-onboard-placeholder} __NEMOCLAW_STUB_MODE__",
        ])
        (mod.LOGS / "worker-bootstrap.sh").write_text(full_bootstrap)
        setattr(mod, "ARTIFACT_BUCKET", "artifact-bucket-test")
        setattr(mod, "BROKER_REGION", "eu-west-1")
        os.environ["BROKER_EFS_DNS"] = "fs-test.efs.eu-west-1.amazonaws.com"
        os.environ["BROKER_ONBOARD_TOKEN"] = "tok_test_not_secret"
        os.environ.pop("BROKER_NEMOCLAW_STUB_MODE", None)

        user_data = mod.WorkerManager()._render_worker_user_data()
        size = len(user_data.encode("utf-8"))
        print(f"rendered_user_data_bytes={size}")
        assert size < 12_000, "loader must leave margin under AWS 16,384-byte limit"
        assert size < 16_384, "RunInstances hard limit"
        assert "worker-bootstrap.sh" in user_data
        assert "worker-bootstrap.rendered.sh" in user_data
        assert "NemoClaw install via official installer" not in user_data, (
            "full bootstrap leaked into RunInstances UserData")
        assert "@@EFS_DNS@@" not in user_data
        assert "EFS_DNS=fs-test.efs.eu-west-1.amazonaws.com" in user_data
        assert "'__EFS_DNS__': os.environ.get('EFS_DNS', '')" in user_data, (
            "runtime renderer must keep the bootstrap placeholder literal intact")
        assert "tok_test_not_secret" in user_data
    print("[PASS] worker RunInstances user-data stays slim")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
