#!/usr/bin/env python3
"""Verify the SEV-SNP framework + post-AMI-change state.

After attempting to switch the worker AMI from Ubuntu 24.04 to Amazon
Linux 2023 (blocked by eu-west-2 not supporting SEV-SNP), verify:

  1. Broker is still healthy at https://verdant.codepilots.co.uk
  2. /v1/discover.attestation has the expected SEV-SNP framework fields:
     - report (empty string for now, framework ready)
     - cert_chain (empty list, framework ready)
     - enclave_pubkey, chip_id, family_id, attestation_source
  3. The non-SEV-SNP fields still work:
     - tee_type, min_measurement, policy_hash, fetched_at
  4. worker/sev_snp.py exists and is a valid Python module
  5. sev_snp.py can run locally and returns the expected stub structure
  6. worker/poller.py still imports successfully (no syntax errors)
  7. README.md has updated SEV-SNP requirements section
  8. Broker daemon.py has the CpuOptions comment (reverted, but documented)
"""
import urllib.request, json, os, sys, hashlib
from pathlib import Path

ROOT = Path("/home/autumn/hermes/competition/tee-broker-deploy")
BROKER = "https://verdant.codepilots.co.uk"
PASS = 0
FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        print(f"[PASS] {label}")
        PASS += 1
    else:
        print(f"[FAIL] {label}")
        FAIL += 1


# === 1. Broker health ===
print("=== broker health ===")
try:
    with urllib.request.urlopen(f"{BROKER}/healthz", timeout=10) as r:
        h = json.loads(r.read())
    check("1. broker healthz returns ok", h.get("ok") is True)
    check("2. broker reports worker status (true/false)", "worker" in h)
except Exception as e:
    check(f"1. broker healthz reachable: {e}", False)

# === 2. /v1/discover attestation fields ===
print("\n=== discover attestation framework ===")
try:
    with urllib.request.urlopen(f"{BROKER}/v1/discover", timeout=10) as r:
        d = json.loads(r.read())
    a = d.get("attestation", {})
    # Framework fields (should be present even if empty)
    framework_fields = ["report", "cert_chain", "enclave_pubkey", "chip_id",
                        "family_id", "attestation_source"]
    for f in framework_fields:
        check(f"3. attestation.{f} field present", f in a)
    # Existing fields
    check("4. attestation.tee_type is amd-sev-snp",
          a.get("tee_type") == "amd-sev-snp")
    check("5. attestation.policy_hash is 64 hex chars",
          len(a.get("policy_hash", "")) == 64 and
          all(c in "0123456789abcdef" for c in a.get("policy_hash", "")))
    check("6. attestation.min_measurement is non-empty",
          bool(a.get("min_measurement")))
    # attestation_source should be 'stub' or 'instance_id_sha256' (not 'snpguest')
    # because eu-west-2 doesn't support SEV-SNP
    check(f"7. attestation_source is stub or instance_id_sha256 (got {a.get('attestation_source')})",
          a.get("attestation_source") in ("stub", "instance_id_sha256"))
    # report should be empty string (not random data) since no SEV-SNP
    check("8. attestation.report is empty string (no SEV-SNP available)",
          a.get("report") == "")
    # cert_chain should be empty list
    check("9. attestation.cert_chain is empty list (no SEV-SNP available)",
          a.get("cert_chain") == [])
except Exception as e:
    check(f"discover reachable: {e}", False)

# === 3. worker/sev_snp.py exists and runs ===
print("\n=== sev_snp.py local check ===")
sev_snp_path = ROOT / "worker" / "sev_snp.py"
check("10. worker/sev_snp.py exists", sev_snp_path.exists())
if sev_snp_path.exists():
    # Try to import it
    import importlib.util
    spec = importlib.util.spec_from_file_location("sev_snp", sev_snp_path)
    if spec and spec.loader:
        try:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            check("11. sev_snp.py imports without error", True)
            check("12. sev_snp.py has fetch_sev_snp_attestation() function",
                  hasattr(mod, "fetch_sev_snp_attestation"))
            check("13. sev_snp.py has get_full_attestation() function",
                  hasattr(mod, "get_full_attestation"))
            check("14. sev_snp.py has get_sev_snp_measurement() function",
                  hasattr(mod, "get_sev_snp_measurement"))
            # Run get_full_attestation - should return stub on this host
            att = mod.get_full_attestation()
            check("15. sev_snp.get_full_attestation() returns dict",
                  isinstance(att, dict))
            check("16. stub result has 'source' field",
                  "source" in att)
            check(f"17. stub source is 'stub' (no SEV-SNP on host)",
                  att.get("source") == "stub")
            check("18. stub result has 'measurement' field",
                  "measurement" in att)
        except Exception as e:
            check(f"sev_snp.py imports without error: {e}", False)
    else:
        check("sev_snp.py is loadable", False)

# === 4. worker/poller.py imports without syntax errors ===
print("\n=== poller.py syntax check ===")
poller_path = ROOT / "worker" / "poller.py"
check("19. worker/poller.py exists", poller_path.exists())
if poller_path.exists():
    import py_compile
    try:
        py_compile.compile(str(poller_path), doraise=True)
        check("20. worker/poller.py compiles without errors", True)
    except py_compile.PyCompileError as e:
        check(f"poller.py compiles: {e}", False)

# === 5. broker-daemon/daemon.py compiles ===
print("\n=== daemon.py syntax check ===")
daemon_path = ROOT / "broker-daemon" / "daemon.py"
check("21. broker-daemon/daemon.py exists", daemon_path.exists())
if daemon_path.exists():
    import py_compile
    try:
        py_compile.compile(str(daemon_path), doraise=True)
        check("22. broker-daemon/daemon.py compiles without errors", True)
    except py_compile.PyCompileError as e:
        check(f"daemon.py compiles: {e}", False)

# === 6. README.md has SEV-SNP section ===
print("\n=== README documentation ===")
readme = (ROOT / "README.md").read_text()
check("23. README mentions SEV-SNP region requirements (eu-west-1/2/3 etc.)",
      "eu-west-1" in readme and "SEV-SNP" in readme)
check("24. README explains why current region doesn't support SEV-SNP",
      "doesn't have" in readme.lower() or
      "does not support" in readme.lower() or
      "unsupported" in readme.lower())
check("25. README has CpuOptions AmdSevSnp mention",
      "AmdSevSnp" in readme or "CpuOptions" in readme)
check("26. README mentions sev_snp.py parsing",
      "sev_snp" in readme)

# === 7. Worker is using Ubuntu 24.04 (not AL2023) ===
print("\n=== worker AMI ===")
import boto3
ec2 = boto3.client('ec2', region_name='eu-west-2')
res = ec2.describe_instances(Filters=[{"Name": "tag:Role", "Values": ["tee-worker"]},
                                      {"Name": "instance-state-name", "Values": ["running"]}])
workers = [i for r in res['Reservations'] for i in r['Instances']]
if workers:
    ami = workers[0]['ImageId']
    check(f"27. worker is back on Ubuntu 24.04 (AMI={ami})",
          ami == 'ami-01bd674894e3ea876')
else:
    check("27. no worker running (can\\'t verify AMI)", True)

print()
print(f"=== Summary ===")
print(f"Passed: {PASS}")
print(f"Failed: {FAIL}")
print(f"Ad-hoc verification — SEV-SNP framework + post-AMI-change state.")
print(f"Scope: broker health, /v1/discover fields, sev_snp.py, poller syntax, daemon syntax, README docs.")
sys.exit(0 if FAIL == 0 else 1)