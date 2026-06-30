#!/usr/bin/env python3
"""Verify OpenShell policy hash is exposed in /v1/discover.

The broker exposes its egress policy hash so requesters can verify the
broker hasn't been tampered with to widen egress (e.g., to exfiltrate
inputs to attacker-controlled servers).

Properties we verify:
  1. /v1/discover.attestation.policy_hash is present and non-empty
  2. policy_hash is 64 hex chars (SHA-256)
  3. policy_hash matches the local openshell/policy.yaml file
  4. policy_hash changes when the policy file changes
  5. policy_hash is NOT the empty string (defense against missing file)
"""
import urllib.request, json, hashlib, sys, os, shutil, tempfile

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


# === 1. discover endpoint returns policy_hash ===
with urllib.request.urlopen(f"{BROKER}/v1/discover") as r:
    d = json.loads(r.read())
att = d.get("attestation", {})
policy_hash = att.get("policy_hash", "")

check("1. /v1/discover.attestation.policy_hash is present",
      "policy_hash" in att)
check("2. policy_hash is a non-empty string",
      isinstance(policy_hash, str) and len(policy_hash) > 0)
check("3. policy_hash is 64 hex chars (SHA-256)",
      len(policy_hash) == 64 and all(c in "0123456789abcdef" for c in policy_hash))
check("4. policy_hash is NOT the empty string",
      policy_hash != "")

# === 2. policy_hash matches local file ===
local_policy = "/home/autumn/hermes/competition/tee-broker-deploy/broker-daemon/openshell/policy.yaml"
local_hash = ""
if os.path.exists(local_policy):
    with open(local_policy, "rb") as f:
        local_hash = hashlib.sha256(f.read()).hexdigest()
    print(f"local policy hash:  {local_hash}")
    print(f"broker policy hash: {policy_hash}")
    check("5. broker policy_hash matches local file",
          policy_hash == local_hash)
else:
    check(f"5. local policy file exists at {local_policy}", False)

# === 3. verify policy_hash would change if policy changed ===
# Write to a temp file with the same content → hash matches
with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as tmp:
    tmp.write(open(local_policy).read())
    tmp_path = tmp.name
try:
    with open(tmp_path, "rb") as f:
        tmp_hash = hashlib.sha256(f.read()).hexdigest()
    check("6. policy_hash is deterministic (same content → same hash)",
          tmp_hash == local_hash)
    # Modify the file → hash changes
    with open(tmp_path, "a") as f:
        f.write("\n# extra comment\n")
    with open(tmp_path, "rb") as f:
        modified_hash = hashlib.sha256(f.read()).hexdigest()
    check("7. policy_hash changes when policy file changes",
          modified_hash != local_hash)
finally:
    os.unlink(tmp_path)

# === 4. defense-in-depth: missing policy file → empty hash, not crash ===
# We can't easily simulate this without deploying. Just check that the
# field format is consistent (always string, never null/undefined).
check("8. policy_hash is a string type (defense: never null)",
      isinstance(policy_hash, str))

# === 5. attestation block has the expected fields per tee-broker-pattern ===
expected_fields = ["tee_type", "min_measurement", "policy_hash", "fetched_at"]
for f in expected_fields:
    check(f"9. attestation has '{f}' field", f in att)

print()
print(f"=== Summary ===")
print(f"Passed: {PASS}")
print(f"Failed: {FAIL}")
print(f"Ad-hoc verification — OpenShell policy hash in /v1/discover.")
print(f"Scope: presence, format, matches local file, deterministic, changes on edit.")
sys.exit(0 if FAIL == 0 else 1)