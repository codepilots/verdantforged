#!/usr/bin/env python3
"""Post-edit verification for the VerdantForged TEE broker daemon.

Run this AFTER every commit touching broker-daemon/daemon.py or the
test files under tests/. It re-runs the two offline test suites that
exercise the changed code paths and reports explicit PASS/FAIL counts.

Companion to skill: tee-broker-deploy-config
Last verified green: 2026-06-28 against commit 8cf3685 (HEAD at the time)
  - verify-stripe-integration.py : exit=0 PASS=32 FAIL=0
  - verify-security-fixes.py     : exit=0 PASS=29 FAIL=0

Usage:
  python3 ~/.hermes/skills/tee-broker-deploy-config/scripts/post-edit-check.py
  # or from the broker workspace:
  ./scripts/post-edit-check.py

Exits 0 on full green, 1 if either suite exits non-zero, 2 if either
suite exits 0 but reports FAIL > 0 (some test runners print PASS counts
even when individual assertions fail).
"""
import re
import subprocess
import sys
from pathlib import Path

# Resolve workspace relative to this script: <skills>/tee-broker-deploy-config/scripts/post-edit-check.py
# -> workspace is ../../../competition/tee-broker-deploy
DEFAULT_WORKSPACE = Path(__file__).resolve().parents[3] / "competition" / "tee-broker-deploy"
WORKSPACE = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_WORKSPACE


def run(label, script):
    print(f"=== {label} ===")
    proc = subprocess.run(
        [sys.executable, str(WORKSPACE / script)],
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout
    if out:
        for line in out.splitlines()[-25:]:
            print(line)
    if proc.returncode != 0 and proc.stderr:
        print("--- stderr (tail) ---")
        for line in proc.stderr.splitlines()[-15:]:
            print(line)
    print(f"[exit {proc.returncode}]")
    print()
    return proc.returncode, out


def parse_summary(out):
    summary = {}
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Passed:"):
            summary["passed"] = int(s.split(":", 1)[1].strip())
        if s.startswith("Failed:"):
            summary["failed"] = int(s.split(":", 1)[1].strip())
        if "Summary" in s and "PASS=" in s:
            m_p = re.search(r"PASS\s*=\s*(\d+)", s)
            m_f = re.search(r"FAIL\s*=\s*(\d+)", s)
            if m_p:
                summary["passed"] = int(m_p.group(1))
            if m_f:
                summary["failed"] = int(m_f.group(1))
    return summary


results = {}
rc1, out1 = run("verify-stripe-integration.py", "tests/verify-stripe-integration.py")
results["stripe"] = rc1
rc2, out2 = run("verify-security-fixes.py", "tests/verify-security-fixes.py")
results["security"] = rc2

s1 = parse_summary(out1)
s2 = parse_summary(out2)

print("=== AD-HOC VERIFICATION SUMMARY ===")
print(f"  verify-stripe-integration.py : exit={rc1} passed={s1.get('passed', '?')} failed={s1.get('failed', '?')}")
print(f"  verify-security-fixes.py    : exit={rc2} passed={s2.get('passed', '?')} failed={s2.get('failed', '?')}")
print()
print("Changed code paths covered by these suites:")
print("  - broker-daemon/daemon.py         (Stripe helpers, lifecycle, log helpers)")
print("  - tests/verify-stripe-integration.py (B-series deploy wiring + lifecycle assertions)")
print()
print("NOT covered (require live AWS or sibling-task workspace):")
print("  - verify-crypto-e2e.py             (live broker + AWS creds)")
print("  - verify-llm-proxy-security.py     (live AWS creds)")
print("  - verify-input-attachments.py      (sibling task t_0ef31767)")

if rc1 != 0 or rc2 != 0:
    sys.exit(1)
if s1.get("failed", 1) != 0 or s2.get("failed", 1) != 0:
    sys.exit(2)
sys.exit(0)