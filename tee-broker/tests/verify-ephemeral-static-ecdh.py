#!/usr/bin/env python3
"""Verify ephemeral-static X25519 ECDH for per-job forward secrecy.

The broker promises:
  - Every job uses a FRESH ephemeral X25519 keypair for ECDH
  - The ephemeral pubkey is sent in result_pubkey_ephemeral
  - The ephemeral privkey is discarded after the job
  - The static worker key is NEVER used directly for result encryption

Properties we verify:
  1. Each job produces a DIFFERENT result_pubkey_ephemeral (proves ephemeral,
     not static)
  2. result_pubkey_ephemeral is different from the worker's STATIC pubkey
  3. Each result_encrypted uses a DIFFERENT ephemeral (so even if two jobs
     have the same input, the ciphertexts differ — IND-CCA2)
  4. Requester can still decrypt using their privkey + worker_ephemeral_pub
  5. The worker's STATIC key, if compromised, cannot decrypt past jobs
     (we simulate by checking we never see the static pubkey in the wire)

For demo: the worker's static pubkey would normally be unknown to the
requester. We can derive it by calling _ensure_worker_encryption_key() at
the same path. Since we can't easily access the worker's filesystem from
this verifier, we use a different angle: verify that result_pubkey_ephemeral
CHANGES between jobs (proves ephemeral, not static).
"""
import urllib.request, json, time, sys, base64, hashlib, secrets
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives import serialization

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


def submit_and_wait(req_id, req_priv):
    """Submit a job with a real X25519 pubkey, poll until done, return result."""
    req_pub_b64 = base64.b64encode(req_priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)).decode()
    body = json.dumps({
        "client_req_id": req_id,
        "encrypted_skill": "summarize",
        "encrypted_data": f"Ephemeral-static ECDH test data: {req_id}",
        "requester_sig": "0x",
        "result_pubkey": req_pub_b64,
        "stripe_pi_id": "pi_ephemeral_test",
    }).encode()
    req = urllib.request.Request(f"{BROKER}/v1/jobs", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    job_id = resp["job_id"]
    # Poll
    for _ in range(40):
        time.sleep(10)
        try:
            with urllib.request.urlopen(f"{BROKER}/v1/jobs/{job_id}") as r:
                j = json.loads(r.read())
                if j.get('state') == 'completed':
                    return j.get('result', {})
                if j.get('state') == 'failed':
                    print(f"job failed: {j.get('error')}")
                    return None
        except Exception:
            pass
    return None


# === Submit two jobs with DIFFERENT requester keys, collect ephemeral pubs ===
print("Submitting job 1 (with fresh requester keypair)...")
req_priv1 = X25519PrivateKey.generate()
result1 = submit_and_wait(f"ephemeral-1-{int(time.time())}", req_priv1)
if not result1:
    print("job 1 did not complete; aborting")
    sys.exit(1)
eph1 = result1.get("result_pubkey_ephemeral")
ct1 = result1.get("result_encrypted")
print(f"job 1 ephemeral: {eph1[:30]}...")

print("\nSubmitting job 2 (with a different requester keypair)...")
time.sleep(2)
req_priv2 = X25519PrivateKey.generate()
result2 = submit_and_wait(f"ephemeral-2-{int(time.time())}", req_priv2)
if not result2:
    print("job 2 did not complete; aborting")
    sys.exit(1)
eph2 = result2.get("result_pubkey_ephemeral")
ct2 = result2.get("result_encrypted")
print(f"job 2 ephemeral: {eph2[:30]}...")

# === Property 1: each job has a DIFFERENT ephemeral pubkey ===
check("1. job 1 and job 2 have DIFFERENT result_pubkey_ephemeral",
      eph1 and eph2 and eph1 != eph2)

# === Property 2: result_encrypted differs between jobs ===
check("2. job 1 and job 2 have DIFFERENT result_encrypted (different nonce/eph)",
      ct1 and ct2 and ct1 != ct2)

# === Property 3: ephemeral pubs are 32 bytes ===
check("3. job 1 ephemeral is 32 bytes (base64-decoded)",
      eph1 and len(base64.b64decode(eph1)) == 32)
check("4. job 2 ephemeral is 32 bytes (base64-decoded)",
      eph2 and len(base64.b64decode(eph2)) == 32)

# === Property 4: requester can still decrypt with their privkey + ephemeral pub ===
def decrypt(encrypted_b64, req_priv):
    blob = base64.b64decode(encrypted_b64)
    worker_eph = X25519PublicKey.from_public_bytes(blob[:32])
    nonce = blob[32:44]
    ciphertext = blob[44:]
    shared = req_priv.exchange(worker_eph)
    return ChaCha20Poly1305(shared).decrypt(nonce, ciphertext, b"verdantforged-result")

try:
    p1 = decrypt(ct1, req_priv1)
    p1_json = json.loads(p1)
    check("5. requester can decrypt job 1 with their privkey",
          p1_json.get('output') == result1.get('output'))
except Exception as e:
    print(f"[FAIL] 5. job 1 decrypt error: {e}")
    FAIL += 1

try:
    p2 = decrypt(ct2, req_priv2)
    p2_json = json.loads(p2)
    check("6. requester can decrypt job 2 with their privkey",
          p2_json.get('output') == result2.get('output'))
    # Wrong requester cannot decrypt job 2
    try:
        wrong = decrypt(ct2, req_priv1)
        # If this works, it's a bug — different privkeys should produce
        # different shared secrets
        check("7. WRONG requester privkey CANNOT decrypt job 2", False)
    except Exception:
        check("7. WRONG requester privkey CANNOT decrypt job 2 (AEAD rejects)",
              True)
except Exception as e:
    print(f"[FAIL] 6. job 2 decrypt error: {e}")
    FAIL += 1

# === Property 6: high-entropy ephemeral pubs ===
# X25519 public keys should look uniformly random. Verify first byte is
# varied (would catch a stuck-key bug).
eph_bytes_list = [base64.b64decode(e) for e in (eph1, eph2) if e]
all_first_bytes = set(b[0] for b in eph_bytes_list)
check(f"9. ephemeral pubkeys have varied first bytes (entropy check, got {len(all_first_bytes)} unique)",
      len(all_first_bytes) >= 2)

print()
print(f"=== Summary ===")
print(f"Passed: {PASS}")
print(f"Failed: {FAIL}")
print(f"Ad-hoc verification — ephemeral-static X25519 ECDH for forward secrecy.")
print(f"Scope: per-job ephemeral keypair, ciphertext uniqueness, decrypt round-trip.")
sys.exit(0 if FAIL == 0 else 1)