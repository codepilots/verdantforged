#!/usr/bin/env python3
"""Verify the LLM proxy security model.

Tests:
  1. Job submission issues a unique per-job token
  2. Different jobs get DIFFERENT tokens
  3. Invalid token rejected with 401
  4. No-auth request rejected with 401
  5. Valid token works and gets billed
  6. Client cannot override the upstream model (broker forces it)
  7. Worker has no direct path to Ollama (no llm-api-key on EFS)
"""
import urllib.request, urllib.error, json, time, sys
import boto3

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


def submit_job(req_id):
    body = json.dumps({
        "client_req_id": req_id,
        "encrypted_skill": "summarize",
        "encrypted_data": "Security test.",
        "requester_sig": "0x", "result_pubkey": "0x", "stripe_pi_id": "pi_sec_test"
    }).encode()
    req = urllib.request.Request(f"{BROKER}/v1/jobs", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def call_proxy(token, model="minimax-m3", content="say hi"):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                       "max_tokens": 10}).encode()
    req = urllib.request.Request(f"{BROKER}/v1/llm/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:200]}


# 1. Submit job gets per-job token
r1 = submit_job(f"sec-test-1-{int(time.time())}")
check("1. job submission returns llm_token", bool(r1.get("llm_token")))
check("1b. llm_token length is 52 chars (secrets.token_hex(24))", len(r1.get("llm_token", "")) == 52)
token1 = r1["llm_token"]

# 2. Different job gets different token
r2 = submit_job(f"sec-test-2-{int(time.time())}")
token2 = r2["llm_token"]
check("2. different jobs get different tokens", token1 != token2)

# 3. Invalid token rejected
result = call_proxy("llm_fake_invalid_token_123")
check("3. invalid token returns 401", result.get("_error") == 401)

# 4. No-auth request rejected
body = json.dumps({"model": "minimax-m3", "messages": [{"role": "user", "content": "hi"}]}).encode()
req = urllib.request.Request(f"{BROKER}/v1/llm/chat/completions", data=body,
    headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        no_auth_result = {"code": 200, "body": r.read().decode()[:200]}
except urllib.error.HTTPError as e:
    no_auth_result = {"code": e.code, "body": e.read().decode()[:200]}
check("4. no-auth request returns 401", no_auth_result["code"] == 401)

# 5. Valid token works
result = call_proxy(token1, content="hello in 3 words")
check("5. valid token returns chat completion",
      "choices" in result and len(result.get("choices", [])) > 0)
check("5b. response includes billing metadata", "_billing" in result)
check("5c. billing includes demo_cap", result.get("_billing", {}).get("demo_cap") == 50000)

# 6. Client cannot override the model
result = call_proxy(token1, model="gpt-999-jailbreak")
check("6. client cannot override model (broker forces minimax-m3)",
      result.get("model") == "minimax-m3")

# 7. Worker has no direct path to Ollama (no llm-api-key on EFS)
ssm = boto3.client('ssm', region_name='eu-west-2')
ec2 = boto3.client('ec2', region_name='eu-west-2')
cp_res = ec2.describe_instances(Filters=[{"Name": "tag:Role", "Values": ["control-plane"]},
                                          {"Name": "instance-state-name", "Values": ["running"]}])
cp = [i for r in cp_res['Reservations'] for i in r['Instances']][0]
r = ssm.send_command(InstanceIds=[cp['InstanceId']], DocumentName='AWS-RunShellScript',
    Parameters={'commands': ['if [ -f /mnt/broker/logs/llm-api-key ]; then echo PRESENT; else echo ABSENT; fi']})
cid = r['Command']['CommandId']
key_state = "?"
for _ in range(10):
    time.sleep(3)
    try:
        inv = ssm.get_command_invocation(CommandId=cid, InstanceId=cp['InstanceId'])
        if inv['Status'] != 'InProgress':
            key_state = inv.get('StandardOutputContent', '').strip()
            break
    except ssm.exceptions.InvocationDoesNotExist:
        time.sleep(2)
check(f"7. llm-api-key is ABSENT from EFS (got: {key_state})", key_state == "ABSENT")

# 8. Verify the poller doesn't have direct-llm code
r = ssm.send_command(InstanceIds=[cp['InstanceId']], DocumentName='AWS-RunShellScript',
    Parameters={'commands': ['grep -c "direct-llm\\|llm-api-key" /mnt/broker/logs/worker-poller.py']})
cid = r['Command']['CommandId']
poller_check = "?"
for _ in range(10):
    time.sleep(3)
    try:
        inv = ssm.get_command_invocation(CommandId=cid, InstanceId=cp['InstanceId'])
        if inv['Status'] != 'InProgress':
            poller_check = inv.get('StandardOutputContent', '').strip()
            break
    except ssm.exceptions.InvocationDoesNotExist:
        time.sleep(2)
check(f"8. poller has 0 direct-llm references (got: {poller_check})", poller_check == "0")

print()
print(f"=== Summary ===")
print(f"Passed: {PASS}")
print(f"Failed: {FAIL}")
print(f"Ad-hoc verification — static + behavioral tests against live broker.")
print(f"Scope: LLM proxy security (per-job tokens, no worker-side key, model override protection)")
sys.exit(0 if FAIL == 0 else 1)