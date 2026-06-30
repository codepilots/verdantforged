# Attestation trust model — what the SEV-SNP chip can and cannot prove

This reference exists because the broker's `/v1/discover` endpoint
publishes a real SEV-SNP attestation report, but the deeper question
"can the operator lie about what they're running?" has a non-obvious
answer that depends on **which layer of the deployment** you're asking
about.

Captured from a 2026-06-30 conversation where the user pushed back on
plausible-sounding copy ("the broker attests the NemoClaw version")
and forced a careful read of `tee-broker-deploy/worker/sev_snp.py` +
`worker/poller.py` + `worker/user-data.sh`. The two-layer distinction
that follows is verified against those files.

## The two-image architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Worker EC2 (m6a.xlarge, AMD SEV-SNP)                        │
│                                                              │
│  Initial VM memory at launch:                               │
│    - kernel                                                  │
│    - initramfs                                               │
│    - systemd services (worker-poller.service, ...)           │
│    - docker.io (CLI + daemon)                                │
│    - nemohermes CLI (Node.js)                                │
│    - worker/poller.py (Python)                               │
│                                                              │
│  Downloaded AFTER SEV-SNP launch measurement:               │
│    - NemoClaw Docker image (from NVIDIA CDN)                 │
│    - Any sandbox runtime installed via `nemohermes onboard`  │
│                                                              │
│  Per-LLM-job process (NOT measured by SEV-SNP):             │
│    - nemohermes exec spawns a sandbox container              │
│    - the container runs worker-agent.py + the request skill  │
└──────────────────────────────────────────────────────────────┘
```

The SEV-SNP report's `launch_measurement` field
(`report_bytes[144:192]`, SHA-384) hashes the **initial VM memory
contents** — i.e. the kernel + initramfs + systemd state. It does NOT
include anything downloaded or spawned after the launch measurement.

## What each attestation level proves

| Claim                                          | Hardware root?                              | Who can lie?                                                                |
|-----------------------------------------------|---------------------------------------------|-----------------------------------------------------------------------------|
| Worker booted the published AMI                | **Yes** (SEV-SNP `launch_measurement`)      | Only the AMD chip + whoever has the AMI build key                           |
| Worker booted the same kernel as before        | **Yes**                                     | Only the AMD chip                                                           |
| Worker pulls NemoClaw Docker image at runtime | **No** — chip already measured at launch    | The worker (operator controls the image-pull step)                          |
| Worker reports "NemoClaw v0.7.2"               | **No** — that's a string, no hardware root  | The worker (writes to `worker-keys.json` from `nemohermes --version`)       |
| Worker reports `image_digest: sha256:abc...`  | **No** if unsigned; **self-verifiable** if signed by worker's Ed25519 (which IS bound to the SEV-SNP report via `worker_binding`) | The worker can lie about what it ran; the signature just proves the worker's *claim* about what it ran |
| Worker honestly ran some NemoClaw sandbox      | **Partial** — yes if the sandbox shares the worker's attestation (it does, as a child process) | NemoClaw itself doesn't sign the sandbox; only the OpenShell policy constraint is hardware-enforced via the SEV-SNP report |

## What mitigations exist, with exact cost

### Mitigation 1 — Pin the published `min_measurement` per (NemoClaw version, AMI id)

Cost: zero lines of code. Just maintain a table:

```json
{
  "v0.7.2": {
    "ec2_ami": "ami-06b9219be654efe2b",
    "min_measurement": "<96-hex SHA-384 from the broker's /v1/discover>"
  },
  "v0.7.1": {
    "ec2_ami": "ami-08...different_ami",
    "min_measurement": "<different hex>"
  }
}
```

A requester with this table can verify: live broker shows
`min_measurement = M`, claims v0.7.2 → consistent. Claims v0.7.2 but
`min_measurement = M'` → lie exposed.

Catches:
- Lying about which AMI was booted

Does NOT catch:
- Lying about which NemoClaw version maps to which `min_measurement`
  (the operator publishes the table)
- An operator shipping a custom NemoClaw release under a known
  version label (only catchable with NemoClaw-signed images, see below)

### Mitigation 2 — Sign the NemoClaw Docker image digest at the worker (NOT YET IMPLEMENTED)

Worker, after `nemohermes onboard`:

```bash
NEMOCLAW_VERSION=$(nemohermes --version 2>/dev/null | head -1)
NEMOCLAW_IMAGE=$(nemohermes list --json \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('sandboxes',[{}])[0].get('image','unknown'))")
NEMOCLAW_DIGEST=$(docker images --digests "$NEMOCLAW_IMAGE" --format '{{.Digest}}' | head -1)
```

Write these to `/opt/worker/.nemoclaw_metadata`. Then in `poller.py`'s
`publish_worker_keys`, include the fields. In the `sandbox_attestation`
block of each result envelope, sign:

```python
sig_payload = f"{version}|{digest}|{sb_name}|{x25519_pubkey}|{report_data[:128]}".encode()
sig = ed25519_sign(worker_ed25519_privkey, sig_payload)
sandbox_attestation.update({
    "nemoclaw_version":     version,
    "nemoclaw_image":       image,
    "nemoclaw_image_digest": digest,
    "image_digest_sig":     sig.hex(),
})
```

A requester can verify by:
1. `docker pull nemoclaw/nemoclaw:v0.7.2` locally
2. `docker images --digests ... --format '{{.Digest}}'` → get own digest
3. Read result envelope `sandbox.image_digest`
4. Verify the `image_digest_sig` against the worker's published X25519
   pubkey (which is bound to the SEV-SNP report via `worker_binding`)
5. Compare digests

Catches:
- Worker pulling a different NemoClaw image than the requester expected
- MITM on the `nemohermes` image download
- Operator claiming "v0.7.2" but actually distributing a different binary

Does NOT catch:
- Operator shipping a custom NemoClaw build and calling it v0.7.2
  (the digest binds the binary; the version string is still a claim)

Cost: ~50 lines (capture 25 + sign 25) in `worker/user-data.sh` and
`worker/poller.py`. Plus one test. See Plan 2 of the 2026-06-30 design
session.

### Mitigation 3 — Bake NemoClaw into the AMI (FUTURE WORK, requires AMI build pipeline change)

Modify the AMI build (Packer) to:
1. Download the NemoClaw Docker image at AMI build time
2. Extract into `/var/lib/docker/...` so it's part of the AMI's filesystem
3. Run `nemohermes onboard` once during AMI build, capture the resulting
   image digest
4. Publish a new `min_measurement` table per (NemoClaw version, AMI id)

After this, the SEV-SNP `launch_measurement` covers the NemoClaw
binary — the chip itself attests that the AMI contains
`nemoclaw/nemoclaw:v0.7.2` image contents (via the filesystem hash
flowing into initial memory).

Catches:
- Everything Mitigation 2 catches, PLUS
- Lies about the NemoClaw *version* (the chip hashes the binary)

Does NOT catch:
- Operator shipping a custom NemoClaw build under a known version label
  (the chip attestes to *some* measurement; the measurement-to-version
  mapping is still operator-published)

Cost: AMI build pipeline rewrite + reproducible AMI build verification.

### Mitigation 4 — NemoClaw-signed image manifests (REQUIRES NVIDIA COOPERATION)

The same pattern AMD uses for VLEK/VCEK: NemoClaw publishes a signed
JSON at `https://nemo.nvidia.com/v<X.Y.Z>/manifest.json` proving
`<image_sha256> = version v<X.Y.Z>`. The image-pull step on the worker
verifies the manifest signature against a pinned NVIDIA key.

Catches:
- All of the above, PLUS
- Operator shipping custom NemoClaw builds under known version labels
  (their custom image won't have a manifest signature from NVIDIA)

Does NOT catch:
- Malicious NVIDIA builds (this requires trusting NVIDIA, same as
  trusting AMD via the chip vendor for SEV-SNP)

Cost: requires NemoClaw to ship a signing pipeline + signed manifests
service. Out of scope for the broker deploy.

## Honest summary for the docs

The current state (Mitigation 1 only) lets a requester verify that the
operator's *claims* are self-consistent (the published `min_measurement`
matches the chip's report) but the requester still trusts the operator's
table that maps measurement → version. Mitigation 2 (worker signs Docker
digest, NOT YET SHIPPED) binds the operator's runtime image claim to
the SEV-SNP report, catching the "different image at runtime" attack.
Mitigations 3 and 4 require NemoClaw cooperation and are out of scope.

When updating any docs (`tee-broker-site/src/pages/verify-attestation.astro`,
`topology.astro`, root `AGENT.md`):
- Always describe the trust model honestly — what is hardware-attested
  vs operator-attested vs worker-self-attested.
- Never imply "the chip attests the NemoClaw image" — it doesn't.
- Never imply "the operator can't lie about the NemoClaw version" —
  without Mitigation 4, they can.
- Always frame the residual trust explicitly: "the operator's
  published `min_measurement` table is the trust root for version
  identity."

The "What the broker enforces vs. what you enforce" table in
`tee-broker-site/src/pages/verify-attestation.astro` is the canonical
honest framing — keep it and the "What the operator can lie about"
table in sync with this reference.
