# Real SEV-SNP attestation via kernel TSM configfs

Session notes from the VerdantForged broker's `t_ab320c7b`
(attestation-verifier skill) live E2E unblock (2026-06-28).

## The problem

The worker's `python3 /opt/worker/sev_snp.py` returned
`source: instance_id_sha256` (a stub) even on a real SEV-SNP
instance. The reason: the old code required the `snpguest`
userspace tool, which is **not packaged in Ubuntu 24.04** and
needs Rust to build from source. Without it, the code fell through
to `SHA-256(instance_id)` — the "attestation" in every job
result envelope was a deterministic hash of the EC2 instance ID,
not a real hardware attestation. Anyone who could read instance
metadata could fake it.

For a TEE broker whose whole value proposition is verifiable
hardware attestation, that's a fatal gap.

## The fix — kernel TSM (Trusted Security Module) configfs API

The kernel ships a configfs-based TSM report interface under
`/sys/kernel/config/tsm/report/`. On SEV-SNP instances, this
interface is backed by the `sev_guest` kernel module and produces
the **real** 1184-byte SEV-SNP attestation report — same one
snpguest would fetch, no Rust toolchain required.

Both modules are auto-loaded on Ubuntu 24.04 SEV-SNP instances:

```bash
lsmod | grep -E 'sev_guest|tsm_report'
# sev_guest   24576  0
# tsm_report  16384  1 sev_guest

dmesg | grep -i 'SEV' | head -5
# [    3.216950] Memory Encryption Features active: AMD SEV SEV-ES SEV-SNP
# [    3.217565] SEV: Status: SEV SEV-ES SEV-SNP
# [    3.708565] SEV: SNP running at VMPL0.
```

The flow (all operations need root — cloud-init user-data runs as
root, so this works in the worker user-data without sudo):

```bash
mkdir /sys/kernel/config/tsm/report/myreport
echo 0 > myreport/privlevel        # VMPL0 = fully privileged
python3 -c "import sys; sys.stdout.buffer.write(b'\\x00'*64)" > myreport/inblob
cat myreport/outblob                # 1184-byte SEV-SNP report
cat myreport/auxblob                # 48-byte header + DER cert chain
rmdir myreport
```

A pure-Python implementation lives at `worker/sev_snp.py`. The
flow is identical — write `privlevel` and `inblob`, read
`outblob` and `auxblob`, rmdir.

## Auxblob layout — the non-obvious part

`auxblob` is NOT a length-prefixed list of certs. The format
(drivers/virt/coco/tsm.c, `tsm_report_read()`) is:

```
  8 bytes  TSM report descriptor (provider type + flags)
  8 bytes  TSM sub-header
 32 bytes  TSM type/usage field
 <certs>   One or more DER X.509 certs concatenated (no length prefix)
```

Total header = 48 bytes. After that, each cert is a standard
ASN.1 SEQUENCE (`30 82 XX XX ...` for long-form, `30 XX` for
short-form). Walk the SEQUENCE tags to split them.

The first cert is the **VLEK** (Versioned Loaded Endorsement Key)
for the current-gen AMD CPU. ASK and ARK are NOT bundled — if
you want a full chain verification, fetch them from AMD's KDS
(kdsintf.amd.com) separately.

```
$ cat /sys/kernel/config/tsm/report/testreport/auxblob | wc -c
1367
$ # 48-byte header + 1319-byte VLEK cert = 1367 ✓
```

## Report field offsets — Milan+ spec

The SEV-SNP report is 1184 bytes. Field offsets per the AMD spec
(Section 8 of the AMD SEV-SNP firmware spec, 56860.pdf) for
**Milan/Genoa** (not the older spec):

| Field | Offset | Size | Notes |
|-------|--------|------|-------|
| `family_id`        | 0x010 (16)  | 16 bytes | Zero = default for VMPL0 |
| `image_id`         | 0x030 (32)  | 16 bytes | Zero = default |
| `vmpl`             | 0x040 (64)  | 4 bytes  | |
| `platform_info`    | 0x080 (128) | 16 bytes | |
| `flags`            | 0x088 (136) | 4 bytes  | |
| `git_version`      | 0x090 (144) | 32 bytes | Firmware version, not measurement |
| `launch_measurement` | 0x0A8 (168) | **48 bytes, SHA-384** | THE measurement |
| `guest_policy`     | 0x0D8 (216) | 48 bytes | |
| `host_data`        | 0x228 (552) | 32 bytes | |
| `chip_id`          | 0x2E8 (744) | **8 bytes** | NOT at 616 (older spec) |
| `signed_data`      | 0x328 (808) | 512 bytes | ECDSA-P384 signature + chip_id |

Common offsets from older snpguest docs (`chip_id` at 616,
`measurement` at 392, etc.) are wrong for Milan/Genoa. Use the
offsets above.

## The poller gate that wasn't accepting tsm_configfs

`worker/poller.py` had a gate at line 768:

```python
if full.get("source") == "snpguest":
    return full["measurement"]
```

This was correct when `snpguest` was the only source, but
silently fell through to the IMDS SHA-256 fallback when the new
code returned `source: tsm_configfs`. Fix:

```python
if full.get("source") in ("snpguest", "tsm_configfs"):
    return full["measurement"]
```

Without this fix, the worker still stubs even though the kernel
returns a real attestation — the poller discards it.

## Verifying the live fix

```bash
# 1. Worker sees real attestation in EFS:
$ cat /mnt/broker/logs/worker-attestation.json
{
  "instance_id": "i-02c2c57cef9f2f975",
  "tee_type": "amd-sev-snp",
  "measurement": "7989ed3d15478107cb0e0a2a9637604586b9615eb8da7617...",
  "source": "tsm_configfs",
  "chip_id": "0aad4b79bfa7c54e",
  "report": <1184 bytes base64>,
  "cert_chain": [<AMD SEV-VLEK-Milan cert, 1319 bytes DER>]
}

# 2. /v1/discover on the broker side:
$ curl -s https://verdant.codepilots.co.uk/v1/discover | jq .attestation
{ "tee_type": "amd-sev-snp" }

# 3. Job result envelope has the real measurement:
$ curl -s https://verdant.codepilots.co.uk/v1/jobs/<job_id> | jq .result.attestation
{
  "tee_type": "amd-sev-snp",
  "measurement": "7989ed3d15478107cb0e0a2a9637604586b9615eb8da7617..."
}
```

Note: workers launched BEFORE the fix keep the cached stub
measurement for their entire lifetime (the poller reads it once
at boot and reuses it for every job). The fix only takes effect
after a worker restart/relaunch. To force a fresh worker: kill
the existing warm-pool worker, the warm-pool manager will
launch a new one with the new code.

## Why the poller caches the measurement

The poller reads the attestation at module import time and
caches it in a module-level variable. This is a design choice
(cert chain is ~1.5KB, the SEV-SNP report is ~1.6KB base64,
parsing on every job is wasteful when the chip doesn't change
between jobs). Cost: workers launched before a code update keep
the old cached value for their lifetime.

To force workers to refresh on every job (or every N jobs):
move the read out of module scope into a function called
lazily. Not necessary in production where workers don't change
code mid-lifetime.

## Cross-verifying the chip signature

The 1184-byte report's `signed_data` field at offset 0x328
contains an ECDSA-P384 signature. To verify:

1. Parse `signed_data` — it includes the message that was
   signed (everything in the report before `signed_data`).
2. Verify the ECDSA-P384 signature against the AMD VLEK cert
   (`cert_chain[0]`).
3. Verify the VLEK cert chains to AMD's root (fetch ASK/ARK from
   `https://kdsintf.amd.com/vcek/v1/Milan/cert_chain` — this URL
   is published by AMD and rotates per chip family).
4. Confirm the certificate is valid for the chip_id reported
   in the attestation (`chip_id` field at 0x2E8).

A verifier skill (like `t_ab320c7b`) can do all four steps
deterministically — no LLM in the crypto loop, just
`cryptography` library calls.

## Why this isn't in `aws-ephemeral-deploy`

`aws-ephemeral-deploy` is about the AWS infrastructure layer.
The TSM configfs API is a kernel feature — it lives in
`/sys/kernel/config/tsm/`, runs entirely on the instance, and
needs no AWS API calls. The reference lives here in
`tee-broker-deploy-config` because the worker's `sev_snp.py` is
broker-specific and the broker is the only VerdantForged
component that runs the worker user-data.

The `tee-broker-pattern` skill (loaded for the attestation
protocol design) covers the marketplace / verifier side of
attestation — what the verifier checks. This reference covers
the producer side — how the worker actually fetches the report.

## Pitfalls observed in this fix

1. **TLM auxblob header is 48 bytes, not 24**. Initial
   parser assumed `8 (descriptor) + 16 (type) = 24` based on
   older Linux docs. The actual layout is `8 + 8 + 32 = 48`.
   Symptom: parser found zero certs in a 1367-byte auxblob
   that had one cert at offset 48.

2. **`chip_id` is at offset 744, not 616**. Older snpguest
   docs and the AMD spec's older revision list chip_id at 616.
   The Milan/Genoa spec moved it to 744 (within `signed_data`).
   Reading offset 616 returns zeros — looks like the chip_id
   field is empty when it's actually at a different offset.

3. **TSM configfs writes need root**. `privlevel`, `inblob`
   are mode `--w-------` owned by root. `poller.py` runs as
   `ubuntu` (the default user); the cloud-init user-data runs
   as root. So `sev_snp.py` works during user-data setup but
   fails if called from the poller systemd unit. Solution:
   call from user-data only, write the result to EFS, and the
   poller reads from EFS.

4. **Poller gates `source == "snpguest"`**. This single-line
   check silently dropped the new `tsm_configfs` source. Fix:
   accept both.

5. **`/sys/kernel/config/tsm/report/<name>` must be a fresh
   name**. Re-using a name from a previous fetch (even one that
   was rmdir'd) may return stale cached data. Generate a random
   suffix per fetch (`name = f"verdant_{os.urandom(4).hex()}"`).

## What's still stub on the broker side

The broker daemon's `/v1/discover` currently returns only:

```python
{
    "tee_type": "amd-sev-snp",
    # ... NO measurement, NO cert_chain, NO chip_id
}
```

That's fine for `/v1/discover` (which advertises capabilities,
not prove them). For a full attestation-verifier skill that
verifies worker attestations, the verifier reads the
attestation block from the job's result envelope — which IS
populated by the worker's EFS `worker-attestation.json`.

The broker's `daemon.py` does NOT currently forward the full
attestation (report + cert_chain + chip_id) in job result
envelopes. It includes only the `measurement` and `tee_type`.
For a stronger attestation guarantee, broker would need to
embed the cert_chain + report in the result envelope so a
remote verifier can re-verify the worker's signature.

This is a separate workstream — the current
`attestation-verifier` skill can verify `measurement` against a
known-good allow-list using just the `measurement` field.
Full report verification (ECDSA-P384 over the chip's signed
report, validated against AMD root) is the next iteration.

## Related

- `tee-broker-pattern` — the marketplace protocol that includes
  attestation verification as part of the trust model
- `worker/sev_snp.py` — the canonical implementation
- `worker/poller.py:765-771` — the gate that was excluding
  `tsm_configfs`; now accepts both `snpguest` and `tsm_configfs`
- `t_ab320c7b` (attestation-verifier skill) — the consumer
  side that this technique unblocks
