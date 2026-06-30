#!/usr/bin/env python3
"""End-to-end NemoClaw sandbox test.

Submits a real job to the live broker, polls until completion, prints the
full request/result, and prints the Stripe billing. Run from the host that
has /tmp/stripe_key.txt and /tmp/last_pi.json (PI is created on demand).

Usage:
  python3 tests/verify-nemoclaw-e2e.py [--stripe-key-file PATH] [--poll-seconds N]

Exit codes:
  0 = job completed with execution_mode=nemoclaw-sandbox
  1 = job failed or completed with wrong mode
  2 = polling timed out
"""
import subprocess
import json
import time
import sys
import argparse
import requests


def fetch_args():
    p = argparse.ArgumentParser()
    p.add_argument("--stripe-key-file", default="/tmp/stripe_key.txt")
    p.add_argument("--poll-seconds", type=int, default=900,
                   help="max seconds to poll for job completion")
    p.add_argument("--skill", default="summarize")
    p.add_argument("--input", default=None)
    p.add_argument("--PI", default=None,
                   help="reuse an existing PI (skips PI creation)")
    p.add_argument("--no-billing", action="store_true",
                   help="skip the Stripe PI-status check")
    return p.parse_args()


def hr(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def create_payment_intent(key, amount_cents=5000):
    """Create + confirm a manual-capture PI for $50 USD."""
    r = subprocess.run([
        'curl', '-sS', 'https://api.stripe.com/v1/payment_intents',
        '-u', f'{key}:',
        '-d', 'amount={a}'.format(a=amount_cents),
        '-d', 'currency=usd',
        '-d', 'confirm=true',
        '-d', 'automatic_payment_methods[enabled]=true',
        '-d', 'automatic_payment_methods[allow_redirects]=never',
        '-d', 'payment_method=pm_card_visa',
        '-d', 'capture_method=manual',
        '-d', 'return_url=https://verdant.codepilots.co.uk',
    ], capture_output=True, text=True, timeout=15)
    pi = json.loads(r.stdout)
    if pi.get("error"):
        raise SystemExit(f"Stripe error: {pi['error']}")
    return pi


def submit_job(pi_id, skill, input_data):
    payload = {
        "client_req_id":   f"e2e-nemoclaw-{int(time.time())}",
        "encrypted_skill": skill,
        "encrypted_data":  input_data,
        "requester_sig":   "0xdeadbeef",
        "result_pubkey":   "0x04abcdef",
        "stripe_pi_id":    pi_id,
    }
    r = requests.post(
        "https://verdant.codepilots.co.uk/v1/jobs",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    return payload, r.json()


def poll_for_completion(job_id, poll_seconds=900):
    t0 = time.time()
    last = None
    deadline = t0 + poll_seconds
    while time.time() < deadline:
        time.sleep(5)
        elapsed = time.time() - t0
        try:
            j = requests.get(f"https://verdant.codepilots.co.uk/v1/jobs/{job_id}",
                             timeout=10).json()
        except Exception as e:
            print(f"    [{elapsed:.0f}s] poll error: {e}")
            continue
        last = j
        state = j.get("state", "?")
        if int(elapsed) % 12 == 0:
            print(f"    [{elapsed:.0f}s] state={state}")
        if state in ("completed", "failed", "timeout"):
            print(f"    [{elapsed:.0f}s] FINAL state={state}")
            return state, j
    return "timeout", last


def fetch_broker_log(job_id, region="eu-west-1", since_seconds=900):
    """Grab the broker log lines for this job_id via SSM."""
    import boto3
    ssm = boto3.client('ssm', region_name=region)
    ssm_resolved = ssm.send_command(
        InstanceIds=['i-05117b9649db5b343'],
        DocumentName='AWS-RunShellScript',
        Comment=f'billing log for {job_id}',
        Parameters={'commands': [
            f'journalctl --no-pager -u verdantforged-broker-daemon --since '
            f'"{since_seconds//60} min ago" 2>&1 | grep -E "{job_id}|stripe" | tail -15',
            'echo SEP1',
        ]},
    )
    cid = ssm_resolved['Command']['CommandId']
    # Wait up to 30s
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(3)
        try:
            inv = ssm.get_command_invocation(CommandId=cid, InstanceId='i-05117b9649db5b343')
        except Exception:
            continue
        if inv['Status'] != 'InProgress':
            return inv.get('StandardOutputContent', '')
    return ''


def fetch_pi_status(pi_id, key):
    r = subprocess.run([
        'curl', '-sS', f'https://api.stripe.com/v1/payment_intents/{pi_id}',
        '-u', f'{key}:',
    ], capture_output=True, text=True, timeout=15)
    return json.loads(r.stdout)


def main():
    args = fetch_args()
    SK = open(args.stripe_key_file).read().strip()

    hr("END-TO-END NemoClaw Sandbox Test")

    # 1. PI
    hr("[1] Creating PaymentIntent")
    pi = create_payment_intent(SK) if not args.PI else {"id": args.PI, "amount": 5000, "status": "reused"}
    print(f"    PI ID:          {pi['id']}")
    print(f"    Amount:         ${pi['amount']/100:.2f} USD")
    print(f"    Status:         {pi['status']}")
    print(f"    Created:        unix={pi.get('created')}")
    with open('/tmp/last_pi.json', 'w') as f:
        json.dump(pi, f, indent=2)

    # 2. Build payload
    input_data = args.input or (
        "The VerdantForged TEE broker is a pay-per-compute marketplace that "
        "ships jobs to SEV-SNP attested workers running inside NemoClaw sandboxes. "
        "Each worker is an ephemeral EC2 instance with a verified measurement; "
        "each sandbox is a Docker container managed by OpenShell. The broker "
        "settles payment via Stripe auth-on-submit / capture-on-completion, "
        "refunding the customer if the job fails."
    )
    job_payload, ack = submit_job(pi['id'], args.skill, input_data)

    # 3. Submit
    hr("[2] Job submission payload")
    print(json.dumps(job_payload, indent=4))
    hr(f"[3] POST https://verdant.codepilots.co.uk/v1/jobs -> {ack.get('job_id')}")
    print(f"    Status: {ack.get('status', 'queued')}")
    print(f"    Body:   {json.dumps(ack, indent=4)}")
    job_id = ack["job_id"]

    # 4. Poll
    hr(f"[4] Polling for {job_id}")
    state, j = poll_for_completion(job_id, poll_seconds=args.poll_seconds)

    # 5. Result
    hr(f"JOB RESULT — {state}")
    result = (j or {}).get("result")
    if result:
        print(json.dumps(result, indent=2))
    else:
        print("(no result field; full job record below)")
        if j:
            print(json.dumps(j, indent=2)[:3000])

    # 6. Broker log + billing
    hr("BROKER LOG (filtered for this job + stripe)")
    log = fetch_broker_log(job_id)
    print(log[-3000:] if log else "(no log fetched)")

    hr("STRIPE BILLING")
    if args.no_billing:
        print("(billing check skipped)")
    else:
        pi_now = fetch_pi_status(pi['id'], SK)
        print(f"  PI ID:                {pi_now['id']}")
        print(f"  Amount authorized:    ${pi_now['amount']/100:.2f}")
        print(f"  Amount captured:      ${pi_now.get('amount_received', 0)/100:.2f}")
        print(f"  Status:               {pi_now['status']}")
        if pi_now.get('latest_charge'):
            print(f"  Latest charge:        {pi_now['latest_charge']}")
        print(f"  Created:              {pi_now.get('created')}")
        charges = (pi_now.get('charges') or {}).get('data') or []
        if charges:
            print(f"  Charge data:")
            print(json.dumps(charges[0], indent=2)[:800])

    # Exit code
    if state == "completed" and result and result.get("execution_mode") == "nemoclaw-sandbox":
        return 0
    if state in ("completed", "failed", "timeout"):
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
