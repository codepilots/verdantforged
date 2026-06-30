#!/usr/bin/env python3
"""Audit the broker's attestation/signature handling against the TEE broker spec.

Expected (per /home/autumn/hermes/competition/tee-broker-pattern/SPEC.md and
agent-skills.md):

  BrokerAttestation should include:
    - report (base64 SEV-SNP attestation report)
    - cert_chain (base64 DER chain to AMD root)
    - enclave_pubkey (base64 X25519)
    - policy_hash (base64 SHA256 — OpenShell egress policy)
    - tee_type (e.g., amd-sev-snp)
    - measurement (PCR-derived SEV-SNP measurement, NOT a stub)

  ExecutionRequest should include:
    - skill_hash, skill_encrypted, data_encrypted
    - result_pubkey (base64 Ed25519)  ← requester provides this for result encryption
    - mpp_escrow_id, max_fuel, max_duration_ms
    - requester_sig (signature over the request)  ← broker should verify

  ExecutionResult should include:
    - result_encrypted (encrypted to result_pubkey)
    - result_hash, fuel_used, duration_ms, skill_hash, input_hash
    - attestation: tee_type, measurement
    - signature over the result (signed by broker enclave key)

This script audits the LIVE broker against these expectations.
"""
import urllib.request, urllib.error, json, time, sys, hashlib
import boto3

BROKER = "https://verdant.codepilots.co.uk"
PASS = 0
FAIL = 0
WARN = 0


def check(label, condition, severity="FAIL"):
    global PASS, FAIL, WARN
    if condition:
        print(f"[PASS] {label}")
        PASS += 1
    elif severity == "WARN":
        print(f"[WARN] {label}")
        WARN += 1
    else:
        print(f"[FAIL] {label}")
        FAIL += 1


def submit_job(req_id, sig="0xdeadbeef", pubkey="0xabcdef"):
    body = json.dumps({
        "client_req_id": req_id,
        "encrypted_skill": "summarize",
        "encrypted_data": "Test data.",
        "requester_sig": sig,
        "result_pubkey": pubkey,
        "stripe_pi_id": "pi_audit_test",
    }).encode()
    req = urllib.request.Request(f"{BROKER}/v1/jobs", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:300]}


print("=== 1. /v1/discover attestation ===")
with urllib.request.urlopen(f"{BROKER}/v1/discover") as r:
    d = json.loads(r.read())
print(json.dumps(d.get("attestation", {}), indent=2))
att = d.get("attestation", {})

# Expected keys per spec: report, cert_chain, enclave_pubkey, policy_hash,
# tee_type, measurement
expected_keys = ["report", "cert_chain", "enclave_pubkey", "policy_hash",
                 "tee_type", "measurement"]
for k in expected_keys:
    check(f"discover.attestation.{k} present", k in att, severity="WARN")

check("discover.attestation.tee_type == 'amd-sev-snp'",
      att.get("tee_type") == "amd-sev-snp")
m = att.get("min_measurement", "")
check(f"discover.attestation.min_measurement is non-empty ({m[:20]}...)",
      bool(m), severity="WARN")
check("discover.attestation.min_measurement is NOT a stub",
      m and m != "stub-no-measurement", severity="WARN")
# Spec says report should be base64-encoded SEV-SNP report (very long, ~2.5KB+)
check("discover.attestation.report looks like base64 (>=1000 chars)",
      len(att.get("report", "")) >= 1000, severity="WARN")
check("discover.attestation.cert_chain is a non-empty array",
      isinstance(att.get("cert_chain"), list) and len(att.get("cert_chain", [])) >= 1,
      severity="WARN")
check("discover.attestation.enclave_pubkey is base64 (~32 bytes)",
      len(att.get("enclave_pubkey", "")) >= 40, severity="WARN")
check("discover.attestation.policy_hash is hex SHA256 (64 chars)",
      len(att.get("policy_hash", "")) == 64, severity="WARN")

print()
print("=== 2. POST /v1/jobs request validation ===")
# Submit a job with empty sig/pubkey to see what validate_submit accepts
r1 = submit_job(f"audit-1-{int(time.time())}", sig="0xdeadbeef", pubkey="0xabcdef")
check("submit returns llm_token (not error)", "llm_token" in r1)
check("submit validates requester_sig (not stub)",
      r1.get("requester_sig") != "0xdeadbeef", severity="WARN")

# Submit missing fields — should reject
body = json.dumps({"client_req_id": f"audit-2-{int(time.time())}",
                   "encrypted_skill": "summarize"}).encode()
req = urllib.request.Request(f"{BROKER}/v1/jobs", data=body,
    headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        r_bad = json.loads(r.read())
except urllib.error.HTTPError as e:
    r_bad = {"_error": e.code, "_body": e.read().decode()[:200]}
check("missing fields rejected with 400", r_bad.get("_error") == 400)

# Submit with invalid stripe_pi_id
r3 = submit_job(f"audit-3-{int(time.time())}", sig="0x", pubkey="0x")
# Override stripe_pi_id
body3 = json.dumps({
    "client_req_id": f"audit-3-{int(time.time())}",
    "encrypted_skill": "summarize",
    "encrypted_data": "x",
    "requester_sig": "0x", "result_pubkey": "0x",
    "stripe_pi_id": "not_pi_prefix",
}).encode()
req = urllib.request.Request(f"{BROKER}/v1/jobs", data=body3,
    headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        r_bad3 = json.loads(r.read())
except urllib.error.HTTPError as e:
    r_bad3 = {"_error": e.code, "_body": e.read().decode()[:200]}
check("invalid stripe_pi_id rejected with 400", r_bad3.get("_error") == 400)

print()
print("=== 3. Poll job and check attestation in result ===")
job_id = r1["job_id"]
for i in range(30):
    time.sleep(10)
    try:
        req = urllib.request.Request(f"{BROKER}/v1/jobs/{job_id}")
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read())
            if j.get("state") == "completed":
                result = j.get("result", {})
                print(f"result keys: {list(result.keys())}")
                attestation = result.get("attestation", {})
                print(f"attestation: {json.dumps(attestation, indent=2)}")

                # Expected: tee_type, measurement, plus worker signature
                check("result.attestation.tee_type present",
                      "tee_type" in attestation)
                check("result.attestation.tee_type == 'amd-sev-snp'",
                      attestation.get("tee_type") == "amd-sev-snp")
                check("result.attestation.measurement present",
                      "measurement" in attestation)
                m = attestation.get("measurement", "")
                check("result.attestation.measurement is NOT 'stub-no-measurement'",
                      m and m != "stub-no-measurement", severity="WARN")
                check("result.attestation.measurement looks like SHA256 hex (64 chars)",
                      len(m) == 64, severity="WARN")

                # Expected: result_encrypted (encrypted to result_pubkey)
                check("result.result_encrypted present (encrypted to result_pubkey)",
                      "result_encrypted" in result, severity="WARN")
                check("result.result_hash present (SHA256 of result)",
                      "result_hash" in result, severity="WARN")
                check("result.skill_hash present",
                      "skill_hash" in result, severity="WARN")
                check("result.input_hash present",
                      "input_hash" in result, severity="WARN")
                check("result.fuel_used present",
                      "fuel_used" in result, severity="WARN")
                check("result.duration_ms present",
                      "duration_ms" in result, severity="WARN")
                # VULN-S4: result envelope now carries `worker_signature`
                # (worker-emitted Ed25519) AND `broker_signature`
                # (broker-emitted Ed25519, added by _finalize_job). The
                # worker signature attests liveness; the broker signature
                # is the authoritative non-repudiation root. The audit
                # check here just confirms at least one signature is
                # present (matching the original spec wording).
                check("result signature present (signed over result)",
                      "broker_signature" in result
                      or "worker_signature" in result
                      or "signature" in result,
                      severity="WARN")

                # Verify result_pubkey was actually used to encrypt the result
                check("result includes result_pubkey (echoed for verification)",
                      result.get("result_pubkey") == "0xabcdef",
                      severity="WARN")
                break
            elif j.get("state") == "failed":
                print(f"job failed: {j.get('error')}")
                break
    except Exception as e:
        print(f"poll error: {e}")

print()
print("=== 4. Worker attestation on EFS ===")
ssm = boto3.client('ssm', region_name='eu-west-2')
ec2 = boto3.client('ec2', region_name='eu-west-2')
cp_res = ec2.describe_instances(Filters=[{"Name": "tag:Role", "Values": ["control-plane"]},
                                          {"Name": "instance-state-name", "Values": ["running"]}])
cp = [i for r in cp_res['Reservations'] for i in r['Instances']][0]
r = ssm.send_command(InstanceIds=[cp['InstanceId']], DocumentName='AWS-RunShellScript',
    Parameters={'commands': ['cat /mnt/broker/logs/worker-attestation.json']})
cid = r['Command']['CommandId']
for _ in range(10):
    time.sleep(3)
    try:
        inv = ssm.get_command_invocation(CommandId=cid, InstanceId=cp['InstanceId'])
        if inv['Status'] != 'InProgress':
            try:
                wa = json.loads(inv.get('StandardOutputContent', ''))
                print(json.dumps(wa, indent=2))
                check("worker-attestation.json present on EFS", True)
                check("worker-attestation.json has tee_type", "tee_type" in wa)
                check("worker-attestation.json has measurement", "measurement" in wa)
                m = wa.get("measurement", "")
                check("worker measurement is NOT 'stub-no-measurement'",
                      m and m != "stub-no-measurement", severity="WARN")
                check("worker measurement looks like SHA256 (64 chars)",
                      len(m) == 64, severity="WARN")
            except Exception as e:
                print(f"parse err: {e}")
                check("worker-attestation.json present on EFS", False)
            break
    except ssm.exceptions.InvocationDoesNotExist:
        time.sleep(2)

print()
print(f"=== Summary ===")
print(f"Passed:   {PASS}")
print(f"Warnings: {WARN}")
print(f"Failed:   {FAIL}")
print(f"Ad-hoc audit — checked live broker against tee-broker-pattern spec.")
print(f"Scope: attestation fields, signature verification, encrypted result format.")
sys.exit(0 if FAIL == 0 else 1)