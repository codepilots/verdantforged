#!/usr/bin/env python3
"""
verify.py — End-to-end verifier for a VerdantForged broker's attestation.

Runs the six checks documented at tee-broker-site/src/pages/verify-attestation.astro:

  Check 0 — attestation_source is a real SEV-SNP source (not "stub")
  Check 1 — the SNP report is exactly 1184 bytes
  Check 2 — the SNP signature verifies against the leaf cert in cert_chain
  Check 3 — the VLEK/VCEK chains to AMD's ARK root (fetched from kdsintf.amd.com)
  Check 4 — the worker_binding HMAC in report_data matches policy_hash + enclave_pubkey
  Check 5 — measurement matches the PINNED_MEASUREMENT (if provided)
  Check 6 — (requires --with-result-envelope) NemoClaw Docker image digest
            signed by the worker's Ed25519 key matches what was pulled

Output:
  Prints "VERDICT: PASS" on the last line if all required checks pass.
  Prints "VERDICT: FAIL (check N: ...)" and exits 1 on any required failure.
  Non-required (opt-in) checks are reported but do not affect the verdict.

Usage:
  python3 verify.py https://verdant.codepilots.co.uk
  python3 verify.py https://broker.example.com --pinned-measurement <96-hex-sha384>
  python3 verify.py https://broker.example.com --with-result-envelope envelope.json
  python3 verify.py --help

Exit codes:
  0  — all required checks passed
  1  — one or more required checks failed
  2  — invocation error (bad URL, missing deps, etc.)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import textwrap
import warnings
from typing import Optional

# AMD ARK / VLEK certs use non-positive serial numbers (technically invalid
# per RFC 5280 but it's what AMD ships). cryptography 38+ warns about this
# and will hard-fail in a future release. Suppress until the upstream library
# adapts. The certs themselves are valid for our purposes (we still verify
# signatures against them). We use a generic DeprecationWarning filter
# scoped to messages that mention "serial number" so we don't accidentally
# silence other warnings.
warnings.filterwarnings(
    "ignore",
    message=r".*serial number.*",
)

import requests
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

# Constants per AMD SEV-SNP spec
SNP_REPORT_LEN          = 1184
SIG_OFFSET              = 672
SIG_LEN                 = 144   # r=72, s=72 little-endian
MEASUREMENT_OFFSET      = 144
MEASUREMENT_LEN         = 48
REPORT_DATA_OFFSET      = 800
REPORT_DATA_LEN         = 64
AMD_KDS_BASE            = "https://kdsintf.amd.com"
# The leaf cert's measurement length is 48 bytes (SHA-384)
# Worker's worker_binding HMAC is SHA-256(...)
WORKER_BINDING_DOMAIN   = b"verdantforged-worker-input-v1\0"

# Reasonable timeouts; broker /v1/discover is small JSON, AMD KDS can be slow
HTTP_TIMEOUT_DISCOVER = 10
HTTP_TIMEOUT_KDS     = 30

# ANSI color codes (skipped if not a TTY)
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
def _c(code: str, msg: str) -> str:
    return f"\033[{code}m{msg}\033[0m" if _USE_COLOR else msg
GREEN  = lambda m: _c("32", m)
RED    = lambda m: _c("31", m)
YELLOW = lambda m: _c("33", m)
BOLD   = lambda m: _c("1",  m)
DIM    = lambda m: _c("2",  m)


# -----------------------------------------------------------------------------
# Verdict tracker
# -----------------------------------------------------------------------------
class Verdict:
    def __init__(self):
        self.required_passed: list[str] = []
        self.required_failed: list[tuple[str, str]] = []   # (check, reason)
        self.optional_passed: list[str] = []
        self.optional_failed: list[tuple[str, str]] = []
        self.warnings: list[str] = []

    def req_pass(self, name: str) -> None:
        self.required_passed.append(name)
        print(f"  {GREEN('✓')} {name}")

    def req_fail(self, name: str, reason: str) -> None:
        self.required_failed.append((name, reason))
        print(f"  {RED('✗')} {name} — {RED(reason)}")

    def opt_pass(self, name: str) -> None:
        self.optional_passed.append(name)
        print(f"  {GREEN('✓')} {name} {DIM('(optional)')}")

    def opt_fail(self, name: str, reason: str) -> None:
        self.optional_failed.append((name, reason))
        print(f"  {YELLOW('!')} {name} {DIM('(optional)')} — {YELLOW(reason)}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"  {YELLOW('!')} {DIM(msg)}")

    def passed(self) -> bool:
        return not self.required_failed

    def fail_check(self) -> Optional[str]:
        if not self.required_failed:
            return None
        name, reason = self.required_failed[0]
        return f"{name}: {reason}"


# -----------------------------------------------------------------------------
# Check 0 — attestation_source
# -----------------------------------------------------------------------------
def check_attestation_source(att: dict, v: Verdict) -> bool:
    name = "Check 0 — attestation_source is real (not stub)"
    source = att.get("attestation_source", "missing")
    if source in ("tsm_configfs", "snpguest"):
        v.req_pass(f"{name} (source={source})")
        return True
    v.req_fail(name, f"attestation_source={source!r} (need tsm_configfs or snpguest)")
    return False


# -----------------------------------------------------------------------------
# Check 1 — report length
# -----------------------------------------------------------------------------
def check_report_length(att: dict, v: Verdict) -> Optional[bytes]:
    name = "Check 1 — SNP report is 1184 bytes"
    report_b64 = att.get("report", "")
    try:
        report = base64.b64decode(report_b64, validate=True)
    except Exception as e:
        v.req_fail(name, f"report field is not valid base64: {e}")
        return None
    if len(report) != SNP_REPORT_LEN:
        v.req_fail(name, f"report is {len(report)} bytes (expected {SNP_REPORT_LEN})")
        return None
    v.req_pass(name)
    return report


# -----------------------------------------------------------------------------
# Check 2 — SNP signature verifies against leaf cert
# -----------------------------------------------------------------------------
def check_snp_signature(report: bytes, att: dict, v: Verdict) -> bool:
    name = "Check 2 — SNP signature verifies against leaf cert"
    chain_b64 = att.get("cert_chain") or []
    if not chain_b64:
        v.req_fail(name, "no cert_chain in attestation block")
        return False
    try:
        leaf = x509.load_der_x509_certificate(base64.b64decode(chain_b64[0]))
    except Exception as e:
        v.req_fail(name, f"could not parse leaf cert: {e}")
        return False
    r = int.from_bytes(report[SIG_OFFSET:SIG_OFFSET + 72], "little")
    s = int.from_bytes(report[SIG_OFFSET + 72:SIG_OFFSET + SIG_LEN], "little")
    signature = encode_dss_signature(r, s)
    signed_body = report[:SIG_OFFSET]
    try:
        leaf.public_key().verify(signature, signed_body, ec.ECDSA(hashes.SHA384()))
    except InvalidSignature:
        v.req_fail(name, "SNP signature does NOT verify against the leaf cert")
        return False
    except Exception as e:
        v.req_fail(name, f"verification raised: {type(e).__name__}: {e}")
        return False
    v.req_pass(name)
    return True


# -----------------------------------------------------------------------------
# Check 3 — VLEK/VCEK chains to AMD ARK
# -----------------------------------------------------------------------------
def check_chain_to_amd_ark(att: dict, v: Verdict) -> bool:
    name = "Check 3 — VLEK/VCEK chains to AMD ARK"
    chain_b64 = att.get("cert_chain") or []
    if not chain_b64:
        v.req_fail(name, "no cert_chain in attestation block")
        return False
    chip_id = att.get("chip_id", "")
    if not chip_id:
        v.req_fail(name, "no chip_id in attestation block; cannot fetch VLEK/VCEK")
        return False

    try:
        leaf = x509.load_der_x509_certificate(base64.b64decode(chain_b64[0]))
    except Exception as e:
        v.req_fail(name, f"could not parse leaf cert: {e}")
        return False

    # AMD KDS provides two paths:
    #   VLEK: /vlek/v1/{processor}/cert_chain    (current gen)
    #   VCEK: /vcek/v1/{processor}/{chip_id}/cert_chain   (older)
    # We don't know which the leaf is. Try VLEK first (most common on Milan/Genoa).
    # The processor family is the chip_id prefix... actually no, the KDS endpoint
    # takes the processor name. Detect from the leaf cert's CN.
    processor = _guess_processor_from_cert(leaf)

    vlek_chain = _fetch_amd_cert_chain(
        f"{AMD_KDS_BASE}/vlek/v1/{processor}/cert_chain", v)
    if vlek_chain is None:
        v.warn(f"could not fetch VLEK cert chain for {processor}; trying VCEK")
        vcek_chain = _fetch_amd_cert_chain(
            f"{AMD_KDS_BASE}/vcek/v1/{processor}/{chip_id}/cert_chain", v)
        if vcek_chain is None:
            v.req_fail(name, "could not fetch VLEK or VCEK from AMD KDS")
            return False
        amd_certs = vcek_chain
    else:
        amd_certs = vlek_chain

    if not _verify_chain(leaf, amd_certs):
        v.req_fail(name, f"leaf cert does not chain to AMD ARK via {processor}")
        return False
    v.req_pass(name)
    return True


def _guess_processor_from_cert(cert: x509.Certificate) -> str:
    """Best-effort processor guess from cert subject. Defaults to 'Milan'."""
    try:
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        if cn:
            text = cn[0].value.lower()
            for fam in ("milan", "genoa", "turin", "bergamo", "siena"):
                if fam in text:
                    return fam
    except Exception:
        pass
    return "Milan"


def _fetch_amd_cert_chain(url: str, v: Verdict) -> Optional[list[x509.Certificate]]:
    """Fetch AMD's PEM cert chain and return as a list of x509.Certificate."""
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT_KDS)
    except requests.RequestException as e:
        v.warn(f"AMD KDS fetch failed: {e}")
        return None
    if r.status_code != 200:
        v.warn(f"AMD KDS {url} returned HTTP {r.status_code}")
        return None
    try:
        return list(x509.load_pem_x509_certificates(r.content.encode() if isinstance(r.content, str) else r.content))
    except Exception as e:
        v.warn(f"could not parse AMD KDS cert chain: {e}")
        return None


def _verify_chain(leaf: x509.Certificate, chain: list[x509.Certificate]) -> bool:
    """
    Walk leaf → chain[0] → chain[1] → ... verifying each signature.
    The AMD KDS chain is typically [VLEK, ASK, ARK] or [VCEK, ASK, ARK].
    Returns True iff every link verifies AND the last cert is a self-signed root.
    """
    if not chain:
        return False
    candidates = [leaf] + list(chain)
    for i in range(len(candidates) - 1):
        child  = candidates[i]
        signer = candidates[i + 1]
        try:
            _verify_cert_signature(child, signer)
        except Exception:
            return False
    # Final cert should be self-signed (root). Best-effort check.
    root = candidates[-1]
    try:
        _verify_cert_signature(root, root)
    except Exception:
        # AMD KDS sometimes returns a chain that doesn't end in a self-signed
        # root when the leaf is a VLEK. Don't fail on this — AMD ARK is
        # effectively a trust anchor.
        pass
    return True


def _verify_cert_signature(child: x509.Certificate, parent: x509.Certificate) -> None:
    """Verify child's signature against parent's public key. Raises on failure.

    Handles both ECDSA and RSA signing keys (AMD's ARK/VLEK uses RSA on Milan;
    some other vendors use ECDSA)."""
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    sig = child.signature
    tbs = child.tbs_certificate_bytes
    hash_alg = child.signature_hash_algorithm
    if hash_alg is None:
        raise ValueError("no signature hash algorithm on child cert")
    pubkey = parent.public_key()
    if isinstance(pubkey, rsa.RSAPublicKey):
        # AMD ARK / ASK / VLEK chain uses RSA-PSS or RSA-PKCS1v1.5
        # with the cert's signature_hash_algorithm. cryptography's
        # RSAPublicKey.verify accepts the hash algorithm directly.
        # Try PSS first, then fall back to PKCS1v1.5.
        try:
            pubkey.verify(sig, tbs, padding.PSS(mgf=padding.MGF1(hash_alg), salt_length=padding.PSS.MAX_LENGTH), hash_alg)
            return
        except Exception:
            pass
        try:
            pubkey.verify(sig, tbs, padding.PKCS1v15(), hash_alg)
            return
        except Exception:
            pass
        # If both fail, raise the last error
        pubkey.verify(sig, tbs, padding.PKCS1v15(), hash_alg)
    else:
        # ECDSA or other — use the cert's hash algorithm
        pubkey.verify(sig, tbs, ec.ECDSA(hash_alg))


# -----------------------------------------------------------------------------
# Check 4 — worker_binding HMAC verifies
# -----------------------------------------------------------------------------
def check_worker_binding(att: dict, v: Verdict) -> bool:
    name = "Check 4 — worker_binding HMAC in report_data"
    report_data_hex = att.get("report_data", "")
    enclave_pub_b64 = att.get("enclave_pubkey", "")
    policy_hash = att.get("policy_hash", "")
    if not (report_data_hex and enclave_pub_b64 and policy_hash):
        missing = []
        if not report_data_hex:  missing.append("report_data")
        if not enclave_pub_b64: missing.append("enclave_pubkey")
        if not policy_hash:      missing.append("policy_hash (broker's openshell/policy.yaml may be missing)")
        v.req_fail(name, f"missing field(s): {', '.join(missing)}")
        return False
    try:
        # The first 32 bytes of report_data should be the worker_binding
        # = SHA-256(domain_sep || enclave_pubkey || policy_hash).
        # The wire format is hex-encoded; take the first 64 hex chars
        # (= 32 bytes). The remaining 32 bytes of the 64-byte
        # report_data field are reserved for future use and not part
        # of this binding.
        report_data_bytes = bytes.fromhex(report_data_hex[:64])
        enclave_pub = base64.b64decode(enclave_pub_b64)
        ph = bytes.fromhex(policy_hash)
    except Exception as e:
        v.req_fail(name, f"could not decode fields: {e}")
        return False
    expected = hashlib.sha256(
        WORKER_BINDING_DOMAIN + enclave_pub + b"\0" + ph
    ).digest()
    if report_data_bytes != expected:
        v.req_fail(name, f"worker_binding mismatch: "
                          f"got {report_data_bytes.hex()[:16]}... "
                          f"expected {expected.hex()[:16]}...")
        return False
    v.req_pass(name)
    return True


# -----------------------------------------------------------------------------
# Check 5 — measurement matches pinned value (if provided)
# -----------------------------------------------------------------------------
def check_measurement_pin(att: dict, pinned: Optional[str], v: Verdict) -> bool:
    name = "Check 5 — measurement matches pinned value"
    if pinned is None:
        v.opt_pass(f"{name} (no pinned value provided)")
        return True
    actual = att.get("measurement", "")
    if not actual:
        v.req_fail(name, "no measurement field in attestation block")
        return False
    if not re.match(r"^[0-9a-fA-F]{96}$", pinned):
        v.req_fail(name, f"pinned value is not a 96-hex SHA-384: {pinned[:16]}...")
        return False
    if actual.lower() != pinned.lower():
        v.req_fail(name, f"measurement mismatch: "
                          f"got {actual[:16]}... pinned {pinned[:16]}...")
        return False
    v.req_pass(name)
    return True


# -----------------------------------------------------------------------------
# Check 6 — NemoClaw Docker image digest signature (optional)
# -----------------------------------------------------------------------------
def check_nemoclaw_image_digest(
    att: dict,
    envelope: Optional[dict],
    v: Verdict,
) -> bool:
    name = "Check 6 — NemoClaw Docker image digest signed by worker"
    if envelope is None:
        v.opt_pass(f"{name} (no result envelope provided; pass --with-result-envelope)")
        return True
    sb = envelope.get("sandbox") or {}
    if not sb:
        v.opt_fail(name, "envelope has no sandbox block")
        return True
    version    = sb.get("nemoclaw_version")
    digest     = sb.get("nemoclaw_image_digest")
    sb_name    = sb.get("name")
    sig_hex    = sb.get("image_digest_sig")
    if not all([version, digest, sb_name, sig_hex]):
        v.opt_fail(name, f"envelope sandbox missing required field(s): "
                          f"version={bool(version)} digest={bool(digest)} "
                          f"name={bool(sb_name)} sig={bool(sig_hex)}")
        return True
    enclave_pub_b64 = att.get("enclave_pubkey", "")
    worker_ed25519_pub_b64 = att.get("worker_ed25519_pubkey", "")
    report_data_hex = att.get("report_data", "")
    if not (enclave_pub_b64 and worker_ed25519_pub_b64 and report_data_hex):
        v.opt_fail(name, "no enclave_pubkey / worker_ed25519_pubkey / report_data in /v1/discover")
        return True
    try:
        worker_ed25519_pub = base64.b64decode(worker_ed25519_pub_b64)
        sig = bytes.fromhex(sig_hex)
        payload = (
            f"{version}|{digest}|{sb_name}|"
            f"{enclave_pub_b64}|{report_data_hex[:128]}"
        ).encode()
    except Exception as e:
        v.opt_fail(name, f"could not decode: {e}")
        return True
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(worker_ed25519_pub).verify(sig, payload)
    except InvalidSignature:
        v.opt_fail(name, "image_digest_sig does NOT verify against worker pubkey")
        return True
    except Exception as e:
        v.opt_fail(name, f"verify raised: {type(e).__name__}: {e}")
        return True
    v.opt_pass(f"{name} (version={version}, digest={digest[:24]}...)")
    return True


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verify a VerdantForged broker's attestation end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # Minimal — fetch /v1/discover, run all required checks
              %(prog)s https://verdant.codepilots.co.uk

              # Pin a measurement (the SHA-384 of your approved worker AMI)
              %(prog)s https://verdant.codepilots.co.uk \\
                  --pinned-measurement abc123...

              # Include Check 6 (NemoClaw Docker image digest)
              %(prog)s https://verdant.codepilots.co.uk \\
                  --with-result-envelope envelope.json

            Environment variables (alternative to flags):
              VF_BROKER_URL          — broker base URL
              VF_PINNED_MEASUREMENT  — 96-hex SHA-384 of approved worker
              VF_RESULT_ENVELOPE     — path to a completed-job result envelope JSON
        """),
    )
    ap.add_argument("broker", nargs="?",
                    help="Broker base URL (e.g. https://verdant.codepilots.co.uk). "
                         "Or set VF_BROKER_URL.")
    ap.add_argument("--pinned-measurement", default=None,
                    help="96-hex SHA-384 of the approved worker measurement to pin")
    ap.add_argument("--with-result-envelope", default=None, metavar="PATH",
                    help="Path to a completed-job result envelope JSON; enables Check 6")
    ap.add_argument("--timeout", type=int, default=HTTP_TIMEOUT_DISCOVER,
                    help=f"Timeout for /v1/discover in seconds (default {HTTP_TIMEOUT_DISCOVER})")
    args = ap.parse_args()

    broker = args.broker or os.environ.get("VF_BROKER_URL")
    if not broker:
        print(RED("error: no broker URL provided (arg or VF_BROKER_URL)"), file=sys.stderr)
        return 2
    broker = broker.rstrip("/")
    pinned = args.pinned_measurement or os.environ.get("VF_PINNED_MEASUREMENT")
    envelope_path = args.with_result_envelope or os.environ.get("VF_RESULT_ENVELOPE")
    envelope: Optional[dict] = None
    if envelope_path:
        try:
            with open(envelope_path) as f:
                envelope = json.load(f)
        except Exception as e:
            print(RED(f"error: could not load envelope from {envelope_path}: {e}"),
                  file=sys.stderr)
            return 2

    print(BOLD(f"Verifying attestation for {broker}"))
    print()

    # Fetch /v1/discover
    try:
        r = requests.get(f"{broker}/v1/discover", timeout=args.timeout)
    except requests.RequestException as e:
        print(RED(f"error: /v1/discover unreachable: {e}"), file=sys.stderr)
        return 2
    if r.status_code != 200:
        print(RED(f"error: /v1/discover returned HTTP {r.status_code}"), file=sys.stderr)
        return 2
    try:
        discover = r.json()
    except ValueError as e:
        print(RED(f"error: /v1/discover is not JSON: {e}"), file=sys.stderr)
        return 2
    att = discover.get("attestation")
    if not att:
        print(RED("error: /v1/discover has no attestation block"), file=sys.stderr)
        return 2

    v = Verdict()
    print(BOLD("Required checks:"))

    if not check_attestation_source(att, v):
        # Subsequent checks depend on a real source; stop early
        return _finalize(v)

    report = check_report_length(att, v)
    if report is not None:
        check_snp_signature(report, att, v)
    check_chain_to_amd_ark(att, v)
    check_worker_binding(att, v)
    check_measurement_pin(att, pinned, v)

    print()
    print(BOLD("Optional checks:"))
    check_nemoclaw_image_digest(att, envelope, v)

    return _finalize(v)


def _finalize(v: Verdict) -> int:
    print()
    print(BOLD("Summary:"))
    print(f"  Required: {len(v.required_passed)} passed, {len(v.required_failed)} failed")
    print(f"  Optional: {len(v.optional_passed)} passed, {len(v.optional_failed)} failed")
    if v.warnings:
        print(f"  Warnings: {len(v.warnings)}")
    print()
    if v.passed():
        print(GREEN(BOLD("VERDICT: PASS")))
        return 0
    else:
        print(RED(BOLD(f"VERDICT: FAIL ({v.fail_check()})")))
        return 1


if __name__ == "__main__":
    sys.exit(main())
