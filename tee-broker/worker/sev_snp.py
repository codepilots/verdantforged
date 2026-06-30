"""Real SEV-SNP attestation for the VerdantForged broker worker.

Fetches the actual SEV-SNP attestation report using the kernel's
modern TSM (Trusted Security Module) configfs API
(/sys/kernel/config/tsm/report/). This replaces the old approach of
requiring the `snpguest` userspace tool (which is not packaged in
Ubuntu 24.04 and would need a Rust toolchain to build from source).

Why this approach works:
  - The `sev_guest` and `tsm_report` kernel modules are auto-loaded on
    SEV-SNP-enabled instances (Ubuntu 24.04 kernel 6.17+)
  - The kernel exposes configfs under /sys/kernel/config/tsm/report/
  - The `sev_guest` provider gives us the actual attestation report
    via a simple read-after-write interface
  - No external toolchain, no Rust, no compilation

What we get from the SEV-SNP attestation report:
  - report:          1184-byte SEV-SNP attestation report (ECDSA P-384
                     signed by the AMD chip)
  - cert_chain:      list of DER certificates from auxblob (VLEK for
                     current-gen AMD CPUs; ASK + ARK fallback for older)
  - measurement:     48-byte SHA-384 of the initial VM memory contents
                     (the "real" attestation measurement)
  - chip_id:         unique identifier of the AMD SEV-SNP chip
  - family_id:       16-byte VM family ID
  - image_id:        16-byte VM image ID
  - policy:          hex SEV-SNP guest policy

Output format matches tee-broker-pattern/agent-skills.md BrokerAttestation.
"""
import os, subprocess, json, base64, hashlib, glob, struct, shutil, tempfile, urllib.request


def _which(name):
    """Return path to executable or None."""
    return shutil.which(name)


def _tsm_fetch_report(extra_user_data: bytes = b"") -> dict | None:
    """Fetch an SEV-SNP attestation report via the TSM configfs API.

    The flow:
      1. mkdir /sys/kernel/config/tsm/report/X
      2. echo 0 > X/privlevel       (request VMPL0 = fully privileged)
      3. write 64 bytes of user-data to X/inblob (this becomes report_data)
      4. read X/outblob (1184 bytes = the SEV-SNP report)
      5. read X/auxblob (cert chain, varies in size)
      6. rmdir X (cleanup; triggers generation increment)

    All operations require root (sudo). Returns parsed dict on success,
    None on any failure.
    """
    config_root = "/sys/kernel/config/tsm/report"
    if not os.path.isdir(config_root):
        return None

    name = f"verdant_{os.urandom(4).hex()}"
    path = os.path.join(config_root, name)
    try:
        os.makedirs(path)
    except (PermissionError, OSError):
        return None

    try:
        # privlevel = 0 (VMPL0, fully privileged — what the SEV-SNP
        # spec requires for the launch measurement to be meaningful)
        privpath = os.path.join(path, "privlevel")
        try:
            with open(privpath, "w") as f:
                f.write("0\n")
        except (PermissionError, OSError):
            return None

        # inblob = user-data; SEV-SNP report_data field is exactly 64 bytes.
        # Anything longer gets truncated by the kernel.
        user_data = (extra_user_data + b"\x00" * 64)[:64]
        with open(os.path.join(path, "inblob"), "wb") as f:
            f.write(user_data)

        # Read the report
        with open(os.path.join(path, "outblob"), "rb") as f:
            report_bytes = f.read()
        if len(report_bytes) != 1184:
            return None

        # Read the cert chain (auxblob); may be empty or contain 1+ certs.
        # The kernel's TSM auxblob format (see drivers/virt/coco/tsm.c
        # tsm_report_read()): the first 48 bytes are TSM report metadata
        # (8-byte descriptor, 8-byte sub-header, 32-byte type field). After
        # that comes one or more DER X.509 certs concatenated.
        #
        # For SEV-SNP the first cert is the VLEK (Versioned Loaded
        # Endorsement Key). ASK and ARK are AMD root certs that are NOT
        # bundled — they must be fetched separately from AMD's KDS server
        # (kdsintf.amd.com) if the verifier wants to verify the chain.
        certs_der = []
        aux_path = os.path.join(path, "auxblob")
        if os.path.exists(aux_path):
            with open(aux_path, "rb") as f:
                aux = f.read()
            # Skip the 48-byte TSM header
            off = 48
            while off + 4 < len(aux):
                # Each cert is ASN.1 SEQUENCE (tag 0x30) with 2- or 3-byte length
                if aux[off] != 0x30:
                    break
                if aux[off + 1] & 0x80:
                    num_len_bytes = aux[off + 1] & 0x7F
                    if num_len_bytes < 1 or num_len_bytes > 4:
                        break
                    cert_len = int.from_bytes(aux[off + 2:off + 2 + num_len_bytes], "big")
                    header_len = 2 + num_len_bytes
                else:
                    cert_len = aux[off + 1]
                    header_len = 2
                total_len = header_len + cert_len
                if off + total_len > len(aux):
                    break
                certs_der.append(aux[off:off + total_len])
                off += total_len

        # Parse SEV-SNP report fields per the spec
        # (https://www.amd.com/system/files/TechDocs/56860.pdf section 8)
        #
        #   bytes 16-31:    family_id (16 bytes)
        #   bytes 32-47:    image_id (16 bytes)
        #   bytes 144-191:  launch_measurement (48 bytes, SHA-384)
        #   bytes 416-479:  chip_id (64 bytes)
        family_id = report_bytes[16:32]
        image_id = report_bytes[32:48]
        measurement = report_bytes[144:192]
        report_data = report_bytes[80:144]
        chip_id = report_bytes[416:480]

        certs_b64 = [base64.b64encode(c).decode() for c in certs_der]

        return {
            "report": base64.b64encode(report_bytes).decode(),
            "cert_chain": certs_b64,
            "measurement": measurement.hex(),
            "report_data": report_data.hex(),
            "family_id": family_id.hex(),
            "image_id": image_id.hex(),
            "chip_id": chip_id.hex(),
            "raw_size": len(report_bytes),
            "source": "tsm_configfs",
        }
    except Exception:
        return None
    finally:
        # Cleanup; rmdir triggers generation increment
        try:
            os.rmdir(path)
        except OSError:
            pass


def _snpguest_fetch_report() -> dict | None:
    """Fallback to snpguest if TSM API not available.

    snpguest is the most common userspace tool but isn't packaged in
    Ubuntu 24.04. If the operator has installed it (e.g. via cargo
    install snpguest), prefer it for backward compatibility.
    """
    if not _which("snpguest"):
        return None

    tmpdir = tempfile.mkdtemp(prefix="sev-snp-")
    try:
        result = subprocess.run(
            ["snpguest", "fetch-report", tmpdir],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        report_path = os.path.join(tmpdir, "report.bin")
        if not os.path.exists(report_path):
            return None

        with open(report_path, "rb") as f:
            report_bytes = f.read()
        if len(report_bytes) != 1184:
            return None

        subprocess.run(
            ["snpguest", "fetch-ca", tmpdir],
            capture_output=True, timeout=30,
        )
        certs = []
        for name in ("vcek.pem", "ask.pem", "ark.pem"):
            cert_path = os.path.join(tmpdir, name)
            if os.path.exists(cert_path):
                with open(cert_path, "rb") as f:
                    pem = f.read().decode()
                lines = [
                    l.strip() for l in pem.split("\n")
                    if l and not l.startswith("-----")
                ]
                der = base64.b64decode("".join(lines))
                certs.append(base64.b64encode(der).decode())

        return {
            "report": base64.b64encode(report_bytes).decode(),
            "cert_chain": certs,
            "measurement": report_bytes[144:192].hex(),
            "family_id": report_bytes[16:32].hex(),
            "image_id": report_bytes[32:48].hex(),
            "chip_id": report_bytes[416:480].hex(),
            "policy": report_bytes[8:16].hex(),
            "report_data": report_bytes[80:144].hex(),
            "raw_size": len(report_bytes),
            "source": "snpguest",
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _compute_default_report_data() -> bytes:
    """Build the 64-byte report_data payload the SEV-SNP report will embed.

    The reviewer-side Check 4 (worker_binding) hashes the worker's published
    X25519 pubkey together with the policy_hash from
    /mnt/broker/logs/openshell-policy.yaml, then compares the first 32 bytes
    of that HMAC against the first 32 bytes of report_data. To make the
    binding verifiable, we compute the same digest and pass it as the
    inblob (TSM configfs) — the kernel signs whatever we put there.

    The env var BROKER_ATTESTATION_REPORT_DATA_HEX, when set, still wins
    so the cloud-init / poller paths can inject their own binding
    sequence; this default just ensures the report is *bound* even when
    the script is invoked outside user-data.sh (e.g. by the deploy
    refresh or by systemd at job time).
    """
    try:
        import base64 as _b64
        import hashlib as _hashlib
        from pathlib import Path as _P
        from cryptography.hazmat.primitives import serialization as _ser
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey as _X25519Priv,
        )

        priv_path = _P("/opt/worker/keys/worker_input_x25519.priv")
        if not priv_path.exists():
            return b""
        priv = _X25519Priv.from_private_bytes(priv_path.read_bytes())
        public = priv.public_key().public_bytes(
            _ser.Encoding.Raw, _ser.PublicFormat.Raw)
        policy_path = _P("/mnt/broker/logs/openshell-policy.yaml")
        policy_hash = (_hashlib.sha256(policy_path.read_bytes()).hexdigest()
                       if policy_path.exists() else "")
        binding = _hashlib.sha256(
            b"verdantforged-worker-input-v1\0" + public + b"\0" +
            (bytes.fromhex(policy_hash) if policy_hash else b"")).hexdigest()
        # The reviewer-side verifier (Check 4) compares the first 64
        # hex chars (32 bytes) of report_data against the full
        # 32-byte binding digest. Write the binding in the first
        # 32 bytes of the 64-byte TSM inblob; the remaining 32 bytes
        # are zero-padded (reserved for future use).
        return bytes.fromhex(binding) + b"\0" * 32
    except Exception:
        return b""


def fetch_sev_snp_attestation() -> dict | None:
    """Try to fetch a real SEV-SNP attestation report.

    Returns a dict with report/cert_chain/measurement/chip_id etc, or None
    if SEV-SNP is not available (no /dev/sev-guest, no TSM configfs,
    no snpguest tool).

    Order:
      1. TSM configfs API (kernel module, no userspace tool needed)
      2. snpguest (if installed)
      3. None (caller decides fallback)
    """
    # TSM configfs is the modern kernel interface and works without
    # any extra userspace tool. Try it first.
    report_data_hex = os.environ.get("BROKER_ATTESTATION_REPORT_DATA_HEX", "")
    try:
        report_data = bytes.fromhex(report_data_hex) if report_data_hex else b""
    except ValueError:
        report_data = b""
    if not report_data:
        report_data = _compute_default_report_data()
    if os.path.exists("/dev/sev-guest"):
        att = _tsm_fetch_report(report_data)
        if att:
            return att

    # Fall back to snpguest if available
    return _snpguest_fetch_report()


def get_sev_snp_measurement() -> str:
    """Get the SEV-SNP launch measurement. Returns:
       - Real 96-char-hex SHA-384 measurement if SEV-SNP available
       - SHA-256 of instance ID if IMDSv2 + instance-id available
       - 'stub-no-measurement' otherwise
    """
    att = fetch_sev_snp_attestation()
    if att:
        return att["measurement"]
    # Fall back to IMDSv2 instance-id hash
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            token = r.read().decode().strip()
    except Exception:
        token = ""

    if token:
        try:
            req = urllib.request.Request(
                "http://169.254.169.254/latest/meta-data/instance-id",
                headers={"X-aws-ec2-metadata-token": token},
            )
            with urllib.request.urlopen(req, timeout=2) as r:
                iid = r.read().decode().strip()
            if iid:
                return hashlib.sha256(iid.encode()).hexdigest()
        except Exception:
            pass
    return "stub-no-measurement"


def get_full_attestation() -> dict:
    """Get the full attestation object for /v1/discover.

    Returns a dict matching the tee-broker-pattern BrokerAttestation schema.
    Always returns something (real or stub) so /v1/discover never crashes.
    """
    att = fetch_sev_snp_attestation()
    if att:
        return att
    # Stub: just the measurement from instance-id
    measurement = get_sev_snp_measurement()
    return {
        "report": "",
        "cert_chain": [],
        "enclave_pubkey": "",
        "measurement": measurement,
        "source": "stub",
    }


if __name__ == "__main__":
    # When called directly, print the full attestation
    print(json.dumps(get_full_attestation(), indent=2))
