import os
import subprocess
import sys

def check_text_in_file(path, pattern):
    try:
        with open(path, 'r') as f:
            content = f.read()
            return pattern in content
    except Exception as e:
        print(f"Error reading {path}: {e}")
        return False

def main():
    assertions = [
        # Payment Flow Doc
        ("/home/autumn/hermes/competition/tee-broker-deploy/docs/payment-flow.md", "Webhook Payload Shape"),
        ("/home/autumn/hermes/competition/tee-broker-deploy/docs/payment-flow.md", "job_id"),
        ("/home/autumn/hermes/competition/tee-broker-deploy/docs/payment-flow.md", "payment"),
        ("/home/autumn/hermes/competition/tee-broker-deploy/docs/payment-flow.md", "topup_pi_id"),
        
        # Terms of Service
        ("/home/autumn/hermes/competition/tee-broker-site/src/pages/terms.astro", "charge.dispute.created"),
        ("/home/autumn/hermes/competition/tee-broker-site/src/pages/terms.astro", "Dispute resolution follows Stripe's standard process"),
    ]

    failures = 0
    for path, pattern in assertions:
        if not check_text_in_file(path, pattern):
            print(f"FAIL: {path} missing pattern '{pattern}'")
            failures += 1
        else:
            print(f"PASS: {path} contains '{pattern}'")

    if failures > 0:
        sys.exit(1)
    print("All documentation assertions passed.")

if __name__ == "__main__":
    main()
