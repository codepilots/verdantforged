# NemoClaw image trust chain

Companion reference to the **NemoClaw image trust chain** section in `SKILL.md`. This file captures the full analysis: why the gap exists, the three mitigation levels, the worker-side signature payload format, the reviewer-side verification code, and the long-term path to hardware-attested NemoClaw versions.

Captured 2026-06-30 from a working session on the VerdantForged site (`~/hermes/competition/tee-broker-site/`) and the live broker (`~/hermes/competition/tee-broker-deploy/`). Generalizes to any TEE broker that uses NemoClaw as its execution sandbox — not project-specific.

---

## The gap

The SEV-SNP attestation report's `measurement` field is the SHA-384 of the **EC2 AMI's initial memory contents at launch** (per `worker/sev_snp.py:131`: `measurement = report_bytes[144:192]`). That covers the kernel, the initramfs, the systemd services that were loaded at boot, and any binary the AMI ships with.

It does NOT cover the **NemoClaw Docker image**. `nemohermes` downloads that image at runtime from NVIDIA's CDN, AFTER the launch measurement is taken. The image lands in `/var/lib/docker/...` and becomes the root filesystem of a container started later. By that time, the SEV-SNP measurement is already locked.

**The attack:** an operator (or a MITM on the nemohermes download) ships a custom NemoClaw build. The chip's measurement is unchanged because the AMI is unchanged. A requester pinning `min_measurement` and verifying Step 5 of `/verify-attestation` sees "all good" — but the NemoClaw image is not what they thought it was.

---

## The three mitigations

### Mitigation 1 — Operator publishes a measurement table

The simplest possible thing: the operator publishes a table mapping (NemoClaw version, EC2 AMI id) → published `min_measurement` value. The requester pins the value for the version they expect.

```yaml
# Operator-published table (requesters pin this)
nemoclaw_v0.7.2_ami_06b9219be654efe2b: abc123...  # 96 hex
nemoclaw_v0.7.1_ami_09f8c0d2...:        def456...
```

**Catches:** operator shipping an unapproved AMI.
**Does not catch:** operator shipping a custom NemoClaw build and publishing a measurement for it. The requester pins the value the operator published; if the operator is the liar, the requester pins the wrong value.

**Operator cost:** zero — just publish the table.

### Mitigation 2 — Worker signs the NemoClaw image digest (recommended)

After `nemohermes onboard`, the worker captures three fields and signs them with its Ed25519 key:

```python
# worker/user-data.sh captures these after nemohermes onboard
nemoclaw_version   = subprocess.check_output(["nemohermes", "--version"]).strip()
nemoclaw_image     = json.loads(subprocess.check_output(["nemohermes", "list", "--json"]))["sandboxes"][0]["image"]
nemoclaw_image_digest = subprocess.check_output(
    ["docker", "images", "--digests", nemoclaw_image, "--format", "{{.Digest}}"]
).strip()
```

The worker writes them to `/opt/worker/.nemoclaw_metadata`, and `publish_worker_keys()` includes them in the published `worker-keys.json`. At result-envelope time, the worker signs the bundle:

```python
# worker/poller.py — extend the existing sandbox_attestation block
sig_payload = (
    f"{nemoclaw_version}|{nemoclaw_image_digest}|"
    f"{sb_name}|{x25519_pubkey_b64}|{report_data[:64]}"
).encode()
sig = ed25519_sign(worker_privkey, sig_payload)

sandbox_attestation.update({
    "nemoclaw_version":      nemoclaw_version,
    "nemoclaw_image":        nemoclaw_image,
    "nemoclaw_image_digest": nemoclaw_image_digest,
    "image_digest_sig":      sig.hex(),
})
```

**The signature chains to the SEV-SNP report:**

```
image_digest_sig
  → worker Ed25519 key  (published in worker-keys.json)
    → worker X25519 pubkey  (also in worker-keys.json)
      → worker_binding HMAC  (in report_data[:64], signed by AMD chip)
        → SEV-SNP report  (signed by AMD chip)
          → VLEK/VCEK cert chain
            → AMD ARK root  (pinned in verifier environment)
```

**Catches:** operator pulling a different NemoClaw image than the one the requester expected; MITM on the nemohermes download.
**Does not catch:** same as level 1 — operator controlling the tag and the reference.

**Operator cost:** ~25 lines in `worker/user-data.sh`, ~40 lines in `worker/poller.py`, one new test in `tests/verify-sandbox-execution.py`. The Ed25519 keypair is the same one the worker already uses for result envelopes; no new keys.

**Reviewer-side verification (Python, using the `cryptography` library):**

```python
import requests, base64, subprocess
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# 1. Pull the same NemoClaw image locally
subprocess.check_call(["docker", "pull", "nemoclaw/nemoclaw:0.7.2"])
local_digest = subprocess.check_output(
    ["docker", "images", "--digests", "nemoclaw/nemoclaw:0.7.2",
     "--format", "{{.Digest}}"]
).strip().decode()

# 2. Read the worker's signed claim from a completed job
sb = job_result["sandbox"]
print(f"worker says: version={sb['nemoclaw_version']} digest={sb['nemoclaw_image_digest']}")

# 3. Fetch the worker's pubkey from /v1/discover
att = requests.get(f"{brokerBase}/v1/discover").json()["attestation"]
worker_pub = base64.b64decode(att["enclave_pubkey"])

# 4. Verify the signature
sig_payload = (
    f"{sb['nemoclaw_version']}|{sb['nemoclaw_image_digest']}|"
    f"{sb['name']}|{att['enclave_pubkey']}|{att['report_data'][:64]}"
).encode()
Ed25519PublicKey.from_public_bytes(worker_pub).verify(
    bytes.fromhex(sb["image_digest_sig"]), sig_payload)
print("OK: signature valid; image_digest is bound to the worker's attestation")

# 5. Compare to your local pull
assert local_digest == sb["nemoclaw_image_digest"], \
    f"image mismatch: local={local_digest} worker={sb['nemoclaw_image_digest']}"
print("OK: worker pulled the same NemoClaw image you did")
```

### Mitigation 3 — Bake NemoClaw into the EC2 AMI

The chip measures whatever is in initial memory at launch. If the NemoClaw Docker image is part of the AMI (extracted into `/var/lib/docker/` at build time), the chip measures it too.

**Implementation:**

1. Modify the AMI build pipeline (Packer):
   - Download the NemoClaw Docker image at AMI build time
   - Extract it into `/var/lib/docker` (so it's part of the AMI's filesystem, not a runtime fetch)
   - Run `nemohermes onboard` once at build time to produce a working `/var/lib/docker` overlay
2. The NemoClaw CLI no longer needs to download anything at boot — the image is already there
3. Publish a `min_measurement` table per (NemoClaw version, AMI id) the same way the existing `min_measurement` is published
4. The chip's report IS the signature on the AMI, including the embedded NemoClaw image

**Catches:** everything level 2 catches, plus: operator claiming "v0.7.2" with a custom build (because the chip measured the actual bytes).
**Does not catch:** operator building a malicious NemoClaw from source and publishing a measurement for it. Same residual trust as the other levels.

**Operator cost:** modify Packer, AMI grows by ~1–2 GB, AMI rebuilds on every NemoClaw version bump. Also: reproducible AMI builds become hard (NemoClaw is a moving target; getting the same measurement on rebuild requires deterministic Packer + Amazon Linux + NemoClaw + Docker layer).

**Trade-off worth highlighting:** the ~16 min cold-start time (NemoClaw downloaded from CDN on fresh workers) goes away, since the image is already local.

### The honest limit (all three mitigations)

Every check above assumes the requester is comparing against a *trusted reference* — a measurement table, a NemoClaw Docker image, a version string — that came from somewhere the requester already trusts.

**The only thing that closes that loop is a signed manifest from the image vendor (NVIDIA)** — the equivalent of AMD's KDS for VLEK/VCEK. NemoClaw does not currently ship one. The protocol would look like:

```json
{
  "version": "0.7.2",
  "image_sha256": "abc123...",
  "released_at": "2026-...",
  "signature": "<Ed25519 sig over the above, by NVIDIA's release key>"
}
```

A reviewer fetches this from `nemo.nvidia.com/v0.7.2/manifest.json`, verifies the signature against NVIDIA's pinned release key, and pins the `image_sha256` to compare against the worker's signed claim. Without it, the reviewer is comparing against a measurement the operator published — and if the operator is the liar, the reviewer pins the wrong value.

**This is out of scope for the current deploy** (it requires NVIDIA to ship the manifest service). The right thing to do today is: pick mitigation level 2, document the residual trust clearly, and surface the gap to the NemoClaw team so it gets closed at the source.

---

## Two-image architecture diagram

The mental model that fixes the conflation trap:

```
┌─────────────────────────────────────────────────┐
│ EC2 instance (m6a.xlarge, AMD SEV-SNP)          │  ← SEV-SNP measures THIS
│  - Ubuntu 24.04 AMI                              │
│  - kernel + initramfs (in initial memory)        │
│  - docker.io                                     │
│  - worker/poller.py                              │
│  - NemoClaw CLI (nemohermes)                     │
│                                                 │
│  ┌─────────────────────────────────────────┐    │
│  │ NemoClaw Docker container               │    │  ← NOT measured by SEV-SNP
│  │  - nemohermes sandbox runtime            │    │     (child process, not in
│  │  - worker-agent.py                       │    │      initial memory)
│  │  - inference.local (OpenShell)           │    │
│  │  - any skills installed                  │    │
│  └─────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
```

**Things to remember:**
- The SEV-SNP measurement is of the AMI's initial memory. It covers everything loaded before launch.
- The NemoClaw Docker image is downloaded at runtime, AFTER the measurement. It is NOT in the chip's measurement.
- Mitigation 2 (worker signs the image digest) chains the image to the chip's measurement via the worker's Ed25519 key, which is bound to `report_data` via the `worker_binding` HMAC. The chip measures the AMI; the worker key is generated inside the AMI; the worker key signs the image digest; the chain is complete.
- The worker Ed25519 key is generated at boot (`worker/poller.py:publish_worker_keys`) and published in `worker-keys.json`. The same key is used for result-envelope signing. It rotates per worker.
- The NemoClaw CLI tool (`nemohermes`) is part of the AMI (installed in `step 4` of `worker/user-data.sh`). It is in initial memory only as far as its binary and CLI surface go; the NemoClaw *runtime* (the Docker image it manages) is a separate artifact.

---

## What "NemoClaw instance" actually means

The marketing site used to conflate three different things under the name "NemoClaw":

1. **The NemoClaw control-plane sandbox** — where the broker daemon itself runs in some deployments. (Some operator setups put the daemon in a NemoClaw sandbox; the reference deployment at `tee-broker-deploy/` runs it as a bare systemd service on t3.small, not in NemoClaw.)
2. **The NemoClaw execution sandbox** — a per-LLM-job sandbox spawned inside the worker EC2 by `nemohermes exec`. This is what "the NemoClaw instance" usually means in the current design.
3. **The NemoClaw Docker image** — the runtime that backs (2). It is downloaded at runtime, AFTER the SEV-SNP launch measurement.

**Rule of thumb:** when someone says "the NemoClaw instance" in the context of a TEE broker, ask: "is this the deployment surface, the execution sandbox, or the Docker image?" The trust model and the attestation story differ for each.

---

## See Also

- `SKILL.md` § "The NemoClaw image trust chain" — the three-mitigation table and the honest limit
- `SKILL.md` pitfall #26 — the trap and the fix pattern
- `tee-broker-pattern` — protocol/architecture skill; cross-references this gap in the Day 3 "planner inside enclave" section
- `tee-broker-deploy-config` — operational/deployment skill; documents the actual `worker/poller.py` lines that implement the binding
- `/verify-attestation` (VerdantForged marketing site) — Step 6 is the reviewer-side check for this mitigation
- `/topology` (VerdantForged marketing site) — the architecture section that disambiguates the three "NemoClaw instance" meanings
