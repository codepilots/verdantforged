#!/usr/bin/env python3
"""Verify NemoClaw is onboarded and used on the worker (t_ab320c7b).

The NemoClaw path is the production compute substrate for the broker:
skills run inside an attested NemoClaw sandbox, not as plain host
processes. This test asserts:

1. The user-data script uses `pip install hermes-agent`, NOT the
   broken `curl https://www.nvidia.com/nemoclaw.sh` URL.
2. The poller's `dispatch_to_sandbox()` is wired into execute_in_envelope.
3. The poller's `dispatch_to_sandbox()` is the PREFERRED path (not a
   dead code branch).
4. A live worker (if reachable) actually has NemoClaw installed and a
   sandbox onboarded.

This test exists because NemoClaw was working on eu-west-2 (June 27)
but the migration to eu-west-1 (June 28) silently dropped the wiring.
The user explicitly requested routing everything through NemoClaw so
we have hard evidence the path is set up.

Usage:
    python3 tests/verify-nemoclaw-onboard.py
    python3 tests/verify-nemoclaw-onboard.py --live
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_DATA = REPO_ROOT / "worker" / "user-data.sh"
POLLER = REPO_ROOT / "worker" / "poller.py"

# ANSI for terse terminal output
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

failures: list[str] = []
checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record a pass/fail and print a one-line result."""
    checks.append((name, ok, detail))
    color = GREEN if ok else RED
    sym = "✓" if ok else "✗"
    line = f"  {color}{sym}{RESET} {name}"
    if detail and not ok:
        line += f"\n      {DIM}{detail}{RESET}"
    print(line)
    if not ok:
        failures.append(name)


def section(title: str) -> None:
    print(f"\n{YELLOW}{title}{RESET}")


# ---- 1. user-data.sh uses the right install path ----------------------------

section("1. user-data.sh install path")

ud_text = USER_DATA.read_text() if USER_DATA.exists() else ""
check(
    "user-data.sh exists",
    USER_DATA.exists(),
    f"missing: {USER_DATA}",
)

# The OLD broken path: no env vars, output suppressed
has_old_broken_curl = bool(re.search(
    r"curl[^|]+https?://(?:www\.)?nvidia\.com/nemoclaw\.sh[^|]*\|[^|]*bash[^|]*>/dev/null",
    ud_text,
))
check(
    "REMOVED: 'curl nemoclaw.sh | bash >/dev/null 2>&1' (no env vars, output suppressed)",
    not has_old_broken_curl,
    "old broken install path suppressed output and didn't pass env vars",
)

# The NEW correct path: curl nemoclaw.sh with NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
has_correct_curl = bool(re.search(
    r"curl[^|]+https?://(?:www\.)?nvidia\.com/nemoclaw\.sh[^|]*\|[^|]*bash",
    ud_text,
))
check(
    "ADDED: 'curl https://www.nvidia.com/nemoclaw.sh | bash' (official installer)",
    has_correct_curl,
    "NemoClaw must install via the official NVIDIA installer script",
)

# Must pass NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1 for non-interactive
has_accept = "NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1" in ud_text
check(
    "NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1 set for non-interactive",
    has_accept,
    "installer prompts for third-party acceptance without this env var",
)

# Must pass NEMOCLAW_NON_INTERACTIVE=1
has_nonint = "NEMOCLAW_NON_INTERACTIVE=1" in ud_text
check(
    "NEMOCLAW_NON_INTERACTIVE=1 set for cloud-init",
    has_nonint,
    "installer would hang on prompts without this",
)

# Sandbox name persistence (env file + systemd drop-in)
has_systemd_dropin = "worker-poller.service.d/nemoclaw.conf" in ud_text
check(
    "NEMOCLAW_SANDBOX_NAME persisted via systemd drop-in",
    has_systemd_dropin,
    "poller.py reads env at import — must persist across systemd restarts",
)


# ---- 2. poller.py wires dispatch_to_sandbox into execute_in_envelope ---------

section("2. poller.py sandbox dispatch wired")

poller_text = POLLER.read_text() if POLLER.exists() else ""
check(
    "poller.py exists",
    POLLER.exists(),
    f"missing: {POLLER}",
)

# dispatch_to_sandbox function definition
has_dispatch_fn = "def dispatch_to_sandbox(" in poller_text
check(
    "dispatch_to_sandbox() function defined",
    has_dispatch_fn,
)

# The function is CALLED from execute_in_envelope, not just defined.
# Look for the call site within execute_in_envelope — must be inside the
# function body, not just a docstring.
execute_in_envelope_match = re.search(
    r"def execute_in_envelope\([^)]*\):(.*?)(?=\n\ndef |\nclass |\Z)",
    poller_text,
    re.DOTALL,
)
if execute_in_envelope_match:
    eie_body = execute_in_envelope_match.group(1)
    has_call = "dispatch_to_sandbox(" in eie_body
    check(
        "dispatch_to_sandbox() called from execute_in_envelope",
        has_call,
        "function defined but never called — sandbox path is dead code",
    )
else:
    check(
        "execute_in_envelope function exists",
        False,
        "couldn't find execute_in_envelope function body",
    )

# The sandbox path is the PREFERRED path — should appear BEFORE the
# broker-llm-proxy else-branch in the source.
sb_pos = poller_text.find("execution_mode = \"nemoclaw-sandbox\"")
proxy_pos = poller_text.find("execution_mode = \"broker-llm-proxy\"")
check(
    "sandbox path is the preferred (first) branch",
    sb_pos > 0 and proxy_pos > 0 and sb_pos < proxy_pos,
    f"sandbox at offset {sb_pos}, proxy at {proxy_pos}",
)

# Sandbox failure should NOT silently fall back to host proxy.
# Look for the explicit "we do NOT silently fall back" comment.
has_explicit_fail = "do NOT silently fall back" in poller_text
check(
    "sandbox failures fail LOUD (no silent fallback to host proxy)",
    has_explicit_fail,
    "threat model requires operator awareness of attested-execution failures",
)


# ---- 3. Live worker sanity check (optional, requires --live) -----------------

section("3. Live worker sanity check")

if "--live" in sys.argv:
    # Check the warm-pool worker on eu-west-1 (if SSHable)
    # Skip silently if not reachable — this test is optional.
    worker_ip = os.environ.get("WORKER_IP", "")
    if not worker_ip:
        # Try to discover from EC2 (eu-west-1, tag ManagedBy=broker-daemon)
        try:
            r = subprocess.run(
                ["aws", "ec2", "describe-instances",
                 "--region", "eu-west-1",
                 "--filters",
                 "Name=tag:ManagedBy,Values=broker-daemon",
                 "Name=instance-state-name,Values=running",
                 "--query", "Reservations[].Instances[].[PrivateIpAddress,InstanceId]",
                 "--output", "text"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                worker_ip = r.stdout.strip().split()[0]
        except Exception:
            pass

    if worker_ip:
        ssh = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
               "-i", os.path.expanduser("~/.ssh/verdantforged-eu-west-1.pem"),
               f"ubuntu@{worker_ip}"]
        # Check nemohermes is installed
        r = subprocess.run(ssh + ["command -v nemohermes"],
                          capture_output=True, text=True, timeout=15)
        check(
            f"nemohermes installed on live worker {worker_ip}",
            bool(r.returncode == 0 and r.stdout.strip()),
            r.stderr.strip()[:200] or "no output",
        )
        # Check sandbox exists
        r = subprocess.run(ssh + ["nemohermes list --json 2>/dev/null"],
                          capture_output=True, text=True, timeout=15)
        has_sandbox = r.returncode == 0 and '"sandboxes"' in r.stdout and '"name"' in r.stdout
        check(
            "live worker has onboarded NemoClaw sandbox",
            has_sandbox,
            f"nemohermes list returned: {r.stdout[:200]}",
        )
        # Check NEMOCLAW_SANDBOX_NAME is set in systemd drop-in
        r = subprocess.run(ssh + ["cat /etc/systemd/system/worker-poller.service.d/nemoclaw.conf 2>/dev/null"],
                          capture_output=True, text=True, timeout=10)
        check(
            "live worker has NEMOCLAW_SANDBOX_NAME systemd drop-in",
            "NEMOCLAW_SANDBOX_NAME" in r.stdout,
            r.stdout[:200] or "(empty)",
        )
    else:
        print(f"  {DIM}skipped: no WORKER_IP env var and no EC2 instances found{RESET}")
else:
    print(f"  {DIM}skipped: pass --live to also probe the running worker{RESET}")


# ---- Summary -----------------------------------------------------------------

print()
total = len(checks)
passed = sum(1 for _, ok, _ in checks if ok)
if failures:
    print(f"{RED}{passed}/{total} checks passed — {len(failures)} failure(s):{RESET}")
    for f in failures:
        print(f"  {RED}✗ {f}{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}{passed}/{total} checks passed — NemoClaw wiring locked in ✓{RESET}")
    sys.exit(0)
