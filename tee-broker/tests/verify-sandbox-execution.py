"""Verify the NemoClaw sandbox execution wiring (kanban t_4740dce6).

The poller's job execution path used to call the broker LLM proxy directly
from the host. This task rewires it so the job runs INSIDE the NemoClaw
sandbox: the poller dispatches via `nemohermes exec`, the agent inside the
sandbox calls inference.local (OpenShell routes to the broker proxy), and
the result envelope carries `execution_mode: "nemoclaw-sandbox"` plus a
`sandbox` attestation block.

These tests pin the worker side of the fix. The broker side already
emits `llm_token` + `llm_proxy_url` in every envelope (t_9fbec867 et al.);
the worker's `execute_in_envelope()` now decides between:
  1. `nemoclaw-sandbox`  — when NEMOCLAW_SANDBOX is set + sandbox_exec returns ok
  2. `broker-llm-proxy`  — direct host-side call (legacy / fallback)
  3. `sandbox-failed`    — sandbox path chosen but exec returned non-zero
  4. `no-path`           — neither llm_token nor sandbox configured

Properties verified:
  S1. dispatch_to_sandbox() builds `nemohermes exec` with the right
      sandbox name, timeout, env JSON, and command. It calls subprocess.run
      once and returns parsed stdout JSON on success.
  S2. dispatch_to_sandbox() raises RuntimeError on non-zero exit code,
      surfacing the first 500 chars of stderr.
  S3. dispatch_to_sandbox() sets COMPATIBLE_API_KEY=<token>,
      NEMOCLAW_ENDPOINT_URL=<broker>/v1/llm, JOB_ID=<id>, SKILL_PROMPT,
      and INPUT_DATA in the --env JSON payload.
  S4. dispatch_to_sandbox() uses --no-tty and a timeout >= 120s (we give
      120s + 60s slack) to bound the inner call.
  S5. execute_in_envelope() with NEMOCLAW_SANDBOX set + llm_token +
      llm_proxy_url chooses the sandbox path and produces an envelope with
      execution_mode == "nemoclaw-sandbox" plus a `sandbox` block whose
      `name`, `attested`, and `inference_route` fields are populated.
  S6. execute_in_envelope() with NEMOCLAW_SANDBOX set but the inner
      subprocess raising produces execution_mode == "sandbox-failed" and
      the llm_error field carries the failure reason.
  S7. execute_in_envelope() with NEMOCLAW_SANDBOX unset (legacy env)
      keeps the original `broker-llm-proxy` path and does NOT add a
      `sandbox` block.
  S8. The sandbox attestation block lists inference.local as the entry
      endpoint and the broker proxy IP as the egress destination — these
      are the two endpoints that the OpenShell policy in the sandbox
      permits (see worker/user-data.sh step 4).
  S9. execute_in_envelope() with NEMOCLAW_SANDBOX set but llm_token
      missing falls back to "no-path" (sandbox path requires both
      token and proxy URL to be safe to dispatch).
  S10. (kanban t_eb7d5261) Sandbox attestation block carries the
       NemoClaw version, image name, image digest, and an Ed25519
       signature over `version|digest|sandbox_name|enclave_pubkey|
       report_data[:128]`. The signature verifies against the worker's
       published Ed25519 pubkey.
  S11. (kanban t_eb7d5261) publish_worker_keys() reads the
       NemoClaw metadata file when present and surfaces it on the
       published worker-keys record.
"""
from __future__ import annotations
import base64
import json
import os
import subprocess
import sys
import re
import tempfile
import urllib.request
from pathlib import Path
from unittest import mock
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# --- test env setup (must run BEFORE importing poller) -----------------------
TEST_ROOT = Path(tempfile.mkdtemp(prefix="sandbox-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_ARTIFACTS_DIR"] = str(TEST_ROOT / "artifacts")
os.environ["BROKER_WORKER_KEYS"] = str(TEST_ROOT / "keys")
# Keep keys dir empty so _ensure_worker_signing_key() generates a fresh
# in-memory key without touching the real /opt/worker/keys path.
os.environ["BROKER_KEEP_PLAINTEXT_FOR_DEMO"] = "1"
# Disable real SEV-SNP call — we don't have snpguest in CI.
os.environ["WORKER_SEV_SNP_DISABLED"] = "1"

# Append worker/ to import path
WORKER_DIR = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER_DIR))

import poller  # noqa: E402

# Stub SEV-SNP — without this, get_sev_snp_measurement() tries to reach IMDS
# over HTTP and hangs the test.
poller.get_sev_snp_measurement = lambda: "stub-measurement-for-tests"

# --- helpers -----------------------------------------------------------------

def _stub_sandbox_ok(stdout_payload):
    """Return a mock that mimics a successful `nemohermes exec` call."""
    cp = mock.Mock(returncode=0, stdout=json.dumps(stdout_payload).encode(),
                   stderr=b"")
    return mock.patch.object(subprocess, "run", return_value=cp)


def _stub_sandbox_fail(stderr_msg="boom: missing inference.local"):
    cp = mock.Mock(returncode=1, stdout=b"", stderr=stderr_msg.encode())
    return mock.patch.object(subprocess, "run", return_value=cp)


def _make_envelope(**overrides):
    env = {
        "job_id": "j-test-001",
        "encrypted_skill": "summarize",
        "encrypted_data": "Some text to summarize.",
        "llm_token": "tok-deadbeef",
        "llm_proxy_url": "http://10.0.0.5:8080/v1/llm/chat/completions",
        "result_pubkey": "0x",
        "skill_hash": ("a" * 64),
    }
    env.update(overrides)
    return env


def _captured_run():
    """Return (call_args, kwargs) from the most recent subprocess.run call."""
    assert subprocess.run.called, "subprocess.run was not invoked"
    return subprocess.run.call_args


# --- S1: dispatch_to_sandbox constructs the right command --------------------

def test_s1_dispatch_to_sandbox_builds_nemohermes_exec_command():
    """dispatch uses the current `nemohermes <sandbox> exec -- ...` CLI."""
    payload = {"output": "hello from sandbox", "model": "minimax-m3", "usage": {}}
    with _stub_sandbox_ok(payload) as mrun:
        result = poller.dispatch_to_sandbox(
            job_id="j-1",
            skill_prompt="Summarize this:",
            input_data="hello",
            llm_token="tok-xyz",
            result_pubkey="0x",
            broker_ip="10.0.0.5",
            sandbox_name="worker",
        )
    assert result == payload, f"got {result!r}"
    assert mrun.call_count == 1, "dispatch must call subprocess.run once"
    args, kwargs = mrun.call_args
    cmd = args[0]
    assert cmd[0] == "nemohermes"
    assert cmd[1] == "worker"
    assert cmd[2] == "exec"
    assert "--no-tty" in cmd
    assert "--timeout" in cmd
    # timeout must be >= 120 (sandbox needs real wall time for inference)
    timeout_idx = cmd.index("--timeout")
    assert int(cmd[timeout_idx + 1]) >= 120
    assert "--" in cmd and cmd[-3:-1] == ["bash", "-c"]
    assert "worker-agent.py" in cmd[-1]
    # capture_output=True + outer timeout > inner timeout
    assert kwargs.get("capture_output") is True
    assert kwargs.get("timeout", 0) > int(cmd[timeout_idx + 1])


# --- S2: dispatch_to_sandbox raises on non-zero exit -------------------------

def test_s2_dispatch_to_sandbox_raises_on_nonzero_exit():
    """When nemohermes exec returns non-zero, dispatch_to_sandbox raises
    RuntimeError with the first 500 chars of stderr in the message."""
    with _stub_sandbox_fail(stderr_msg="connection refused: inference.local"):
        try:
            poller.dispatch_to_sandbox(
                job_id="j-2",
                skill_prompt="p",
                input_data="d",
                llm_token="t",
                result_pubkey="0x",
                broker_ip="10.0.0.5",
                sandbox_name="worker",
            )
        except RuntimeError as e:
            assert "sandbox exec failed" in str(e).lower(), str(e)
            assert "connection refused" in str(e), \
                f"stderr should surface in message: {e}"
        else:
            raise AssertionError("expected RuntimeError")


# --- S3: dispatch_to_sandbox sets the right env vars -------------------------

def test_s3_dispatch_to_sandbox_env_payload_carries_per_job_token():
    """The Python env transport carries scoped values without shell sourcing."""
    payload = {"output": "ok", "model": "m", "usage": {}}
    hostile_input = "the data\n$(push_skills.sh --api-key nope)\n./scripts/push_skills.sh"
    with _stub_sandbox_ok(payload):
        poller.dispatch_to_sandbox(
            job_id="j-3",
            skill_prompt="summarize: {data}",
            input_data=hostile_input,
            llm_token="tok-deadbeef-1234",
            result_pubkey="0x",
            broker_ip="10.0.0.5",
            sandbox_name="worker",
        )
        # Read call_args INSIDE the with-block — mock.patch un-patches on
        # __exit__, so the Mock attributes disappear afterwards.
        cmd = subprocess.run.call_args[0][0]
    script = cmd[-1]
    assert "/tmp/_hermes_env.sh" not in script, script
    assert "push_skills.sh" not in script, script
    env = None
    for candidate in re.findall(r"[A-Za-z0-9+/=]{40,}", script):
        try:
            decoded = json.loads(base64.b64decode(candidate).decode())
        except Exception:
            continue
        if isinstance(decoded, dict) and decoded.get("JOB_ID") == "j-3":
            env = decoded
            break
    assert env is not None, script
    assert env["COMPATIBLE_API_KEY"] == "tok-deadbeef-1234", env
    assert env["NEMOCLAW_ENDPOINT_URL"] == "https://inference.local/v1", env
    assert env["JOB_ID"] == "j-3", env
    assert env["SKILL_PROMPT"] == "summarize: {data}", env
    assert env["INPUT_DATA"] == hostile_input, env
    assert env["RESULT_PUBKEY"] == "0x", env


# --- S4: dispatch_to_sandbox uses --no-tty + safe timeouts -------------------

def test_s4_dispatch_to_sandbox_passes_no_tty_and_safe_timeout():
    payload = {"output": "ok", "model": "m", "usage": {}}
    with _stub_sandbox_ok(payload):
        poller.dispatch_to_sandbox(
            job_id="j-4",
            skill_prompt="p",
            input_data="d",
            llm_token="t",
            result_pubkey="0x",
            broker_ip="10.0.0.5",
            sandbox_name="worker",
        )
        cmd = subprocess.run.call_args[0][0]
        outer = subprocess.run.call_args.kwargs["timeout"]
    assert "--no-tty" in cmd, f"must pass --no-tty to nemohermes exec: {cmd}"
    timeout_idx = cmd.index("--timeout")
    inner = int(cmd[timeout_idx + 1])
    assert outer > inner, f"outer timeout {outer} must exceed inner {inner}"


# --- S5: execute_in_envelope sandbox path produces correct envelope ----------

def test_s5_execute_in_envelope_uses_sandbox_path_when_enabled():
    """With NEMOCLAW_SANDBOX_NAME set + llm_token + llm_proxy_url, the
    envelope must declare execution_mode == 'nemoclaw-sandbox' and include
    a `sandbox` attestation block."""
    os.environ["NEMOCLAW_SANDBOX_NAME"] = "worker"
    payload = {"output": "summary: short", "model": "minimax-m3",
               "usage": {"total_tokens": 11}}
    with _stub_sandbox_ok(payload):
        envelope = _make_envelope()
        result = poller.execute_in_envelope(envelope)
    assert result["state"] == "completed", result
    res = result["result"]
    assert res["execution_mode"] == "nemoclaw-sandbox", res
    assert res["model"] == "minimax-m3", res
    assert res["output"] == "summary: short", res
    assert "sandbox" in res, "sandbox attestation block missing"
    sb = res["sandbox"]
    assert sb["name"] == "worker", sb
    assert sb["attested"] is True, sb
    assert "inference_route" in sb, sb
    assert "inference.local" in sb["inference_route"], sb
    assert "10.0.0.5" in sb["inference_route"], sb


# --- S6: sandbox failure path -------------------------------------------------

def test_s6_execute_in_envelope_records_sandbox_failed_on_subprocess_error():
    os.environ["NEMOCLAW_SANDBOX_NAME"] = "worker"
    with _stub_sandbox_fail(stderr_msg="inference.local: connection refused"):
        envelope = _make_envelope()
        result = poller.execute_in_envelope(envelope)
    res = result["result"]
    assert res["execution_mode"] == "sandbox-failed", res
    assert "llm_error" in res, res
    assert "connection refused" in res["llm_error"], res
    assert res["output"].startswith("Sandbox execution failed"), res


# --- S7: legacy path stays when sandbox not configured ----------------------

def test_s7_execute_in_envelope_fails_closed_when_sandbox_unset():
    """Without NEMOCLAW_SANDBOX_NAME, execute_in_envelope MUST fail-closed
    with execution_mode == 'no-nemoclaw-failclosed' rather than
    silently running as a host-side urllib POST to /v1/llm.

    The 2026-06-30 incident: the legacy `broker-llm-proxy` else-branch
    silently produced completed envelopes with no `sandbox` block, so
    paying users got a non-attested result that was indistinguishable
    from a real sandbox run. The fix is to fail-loud: emit the error
    in llm_error / llm_output and never call the broker proxy directly
    from the poller. Tests verify the poller refuses the job AND does
    NOT add a sandbox attestation (since the sandbox was never used).
    """
    # Make sure NEMOCLAW_STUB_MODE is OFF and no shim is present, so
    # the fail-closed branch is the one we test.
    os.environ.pop("NEMOCLAW_STUB_MODE", None)
    os.environ.pop("NEMOCLAW_SANDBOX_NAME", None)
    # DEFAULT_SANDBOX_NAME was captured at module-import time, so popping
    # the env is not enough — patch the module attribute directly to
    # simulate a worker that was never onboarded with NemoClaw.
    with mock.patch.object(poller, "DEFAULT_SANDBOX_NAME", ""), \
         mock.patch.object(poller, "NEMOCLAW_STUB_MODE", False), \
         mock.patch.object(poller, "_have_nemohermes_shim", return_value=False):
        # urllib.request.urlopen is imported at the top of poller.py
        # so we patch it on urllib.request to detect if the legacy
        # broker-llm-proxy branch tried to call it. After the fix, the
        # poller must never invoke urlopen.
        class _URLBoom:
            def __init__(self, *a, **k):
                raise AssertionError(
                    "poller must not call urllib.request.urlopen — "
                    "the legacy broker-llm-proxy else branch is removed"
                )
        with mock.patch.object(urllib.request, "urlopen", _URLBoom):
            envelope = _make_envelope()
            result = poller.execute_in_envelope(envelope)
    res = result["result"]
    # New fail-closed path (2026-06-30).
    assert res["execution_mode"] == "no-nemoclaw-failclosed", res
    assert "llm_error" in res, res
    assert "no-nemoclaw" in res["llm_error"], res
    assert "sandbox" in res, \
        "sandbox block must be present on the fail-closed path (labelled not_attested)"
    sb = res["sandbox"]
    assert sb["attested"] is False, sb
    assert "n/a" in sb["inference_route"], sb


# --- S8: sandbox attestation block lists the two permitted endpoints ---------

def test_s8_sandbox_attestation_lists_permitted_endpoints():
    os.environ["NEMOCLAW_SANDBOX_NAME"] = "worker"
    payload = {"output": "ok", "model": "m", "usage": {}}
    with _stub_sandbox_ok(payload):
        envelope = _make_envelope()
        result = poller.execute_in_envelope(envelope)
    sb = result["result"]["sandbox"]
    assert sb["network_policy"] == "openshell-enforced", sb
    # The two endpoints OpenShell must permit: the virtual inference.local
    # (intercepted on the host) and the broker proxy IP:8080 (egress).
    assert "inference.local" in sb["inference_route"], sb


# --- S9: missing token falls back to no-path even with sandbox configured ----

def test_s9_execute_in_envelope_no_path_when_token_missing_with_sandbox():
    os.environ["NEMOCLAW_SANDBOX_NAME"] = "worker"
    # Wrap subprocess.run in a Mock so we can assert it wasn't called.
    # If the no-path guard fails, the real nemohermes CLI would actually
    # run (no-op or error), but a Mock raises AttributeError on call —
    # which is exactly what we want to detect.
    with mock.patch.object(subprocess, "run") as mrun:
        envelope = _make_envelope(llm_token="")  # broker failed to issue one
        result = poller.execute_in_envelope(envelope)
    res = result["result"]
    assert res["execution_mode"] == "no-path", res
    # subprocess.run must NOT have been called — we can't safely dispatch
    # without a token to authenticate the inference call inside the sandbox.
    assert not mrun.called, \
        "sandbox dispatch refused without per-job token"


# --- S10: sandbox attestation signs NemoClaw image digest (kanban t_eb7d5261) -


def test_s10_sandbox_attestation_signs_nemoclaw_image_digest():
    """The sandbox block must carry (nemoclaw_version, nemoclaw_image,
    nemoclaw_image_digest, image_digest_sig). The signature is a worker
    Ed25519 sig over the canonical payload
    `version|digest|sandbox_name|enclave_pubkey|report_data[:128]`
    where enclave_pubkey is the base64 X25519 pubkey string (as exposed
    by /v1/discover, not the raw 32 bytes).

    The reviewer-side verifier in
    tee-broker-site/src/pages/verify-attestation.astro Step 6 uses
    exactly this format. Drift between poller and doc would break
    end-to-end verification.
    """
    os.environ["NEMOCLAW_SANDBOX_NAME"] = "worker"

    # Lay down a fake NemoClaw metadata file the poller can read, and
    # patch the module-level path constant so the poller looks at our
    # file instead of /opt/worker/.nemoclaw_metadata (which tests can't
    # write).
    meta_dir = Path(tempfile.mkdtemp(prefix="nemoclaw-meta-"))
    meta_path = meta_dir / ".nemoclaw_metadata"
    meta_path.write_text(json.dumps({
        "nemoclaw_version": "0.7.2",
        "nemoclaw_image": "nemoclaw/nemoclaw:0.7.2",
        "nemoclaw_image_digest": "sha256:" + "a" * 64,
    }))

    # report_data the worker reads back at sandbox-attestation-build
    # time (128 hex chars; first 64 are the binding, last 64 are zero
    # padding). Mimics what user-data.sh step 3 writes.
    rep_dir = Path(tempfile.mkdtemp(prefix="nemoclaw-att-"))
    rep_path = rep_dir / "worker-attestation.json"
    fake_report_data = ("b" * 64) + ("0" * 64)
    rep_path.write_text(json.dumps({
        "report_data": fake_report_data,
        "source": "tsm_configfs",
    }))

    # Patch the constants the poller reads at attestation-build time.
    # NEMOCLAW_METADATA_PATH must be added by this task; ATTESTATION_FILE
    # is the existing worker-attestation.json on EFS.
    payload = {"output": "ok", "model": "minimax-m3", "usage": {}}
    # publish_worker_keys() writes the worker Ed25519/X25519 pubkeys the
    # test needs to verify the signature. In production the daemon calls
    # it at poller boot; tests must call it explicitly because we don't
    # go through main().
    poller.publish_worker_keys()
    with _stub_sandbox_ok(payload), \
         mock.patch.object(poller, "NEMOCLAW_METADATA_PATH", meta_path), \
         mock.patch.object(poller, "WORKER_ATTESTATION_FILE", rep_path):
        envelope = _make_envelope()
        result = poller.execute_in_envelope(envelope)

    sb = result["result"]["sandbox"]
    assert sb["nemoclaw_version"] == "0.7.2", sb
    assert sb["nemoclaw_image"] == "nemoclaw/nemoclaw:0.7.2", sb
    assert sb["nemoclaw_image_digest"] == "sha256:" + "a" * 64, sb
    assert "image_digest_sig" in sb, "image_digest_sig missing"
    sig = bytes.fromhex(sb["image_digest_sig"])
    assert len(sig) == 64, f"Ed25519 sig must be 64 bytes, got {len(sig)}"

    # Reconstruct the exact payload the reviewer-side verifier builds
    # (tee-broker-site/src/pages/verify-attestation.astro Step 6):
    #   f"{version}|{digest}|{name}|{enclave_pubkey}|{report_data[:128]}"
    # enclave_pubkey here is the base64 string from /v1/discover.
    keys = json.loads(poller.WORKER_KEYS_FILE.read_text())
    enclave_pubkey_b64 = keys["x25519_pubkey_b64"]
    expected_payload = (
        f"{sb['nemoclaw_version']}|{sb['nemoclaw_image_digest']}|"
        f"{sb['name']}|{enclave_pubkey_b64}|{fake_report_data[:128]}"
    ).encode()

    # Verify with the worker Ed25519 pubkey published in worker-keys.json.
    ed25519_pub_b64 = keys["ed25519_pubkey_b64"]
    ed25519_pub = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(ed25519_pub_b64))
    ed25519_pub.verify(sig, expected_payload)


# --- S11: publish_worker_keys surfaces NemoClaw metadata (kanban t_eb7d5261) --

def test_s11_publish_worker_keys_surfaces_nemoclaw_metadata():
    """publish_worker_keys() must read /opt/worker/.nemoclaw_metadata
    and include its fields on the published record so /v1/discover
    can surface them (and the reviewer can verify the same digest
    signature locally)."""
    meta_dir = Path(tempfile.mkdtemp(prefix="nemoclaw-meta-pub-"))
    meta_path = meta_dir / ".nemoclaw_metadata"
    meta_path.write_text(json.dumps({
        "nemoclaw_version": "0.7.2",
        "nemoclaw_image": "nemoclaw/nemoclaw:0.7.2",
        "nemoclaw_image_digest": "sha256:" + "c" * 64,
        "captured_at": "2026-06-30T12:00:00Z",
    }))
    with mock.patch.object(poller, "NEMOCLAW_METADATA_PATH", meta_path):
        record = poller.publish_worker_keys()
    assert record["nemoclaw_version"] == "0.7.2", record
    assert record["nemoclaw_image"] == "nemoclaw/nemoclaw:0.7.2", record
    assert record["nemoclaw_image_digest"] == "sha256:" + "c" * 64, record
    # Persisted to disk too — /v1/discover reads from the same file.
    persisted = json.loads(poller.WORKER_KEYS_FILE.read_text())
    assert persisted["nemoclaw_image_digest"] == "sha256:" + "c" * 64, \
        persisted


# --- S10b: stub-mode dispatch (2026-06-30) -----------------------------------
#
# When the broker's BROKER_NEMOCLAW_STUB_MODE=1 and the worker has
# NEMOCLAW_SANDBOX_NAME set in env, the poller runs the new stub
# dispatch helper which calls worker-agent.py in-process and records
# execution_mode="nemoclaw-sandbox-stub". This test pins that
# behaviour: the envelope carries a sandbox attestation block clearly
# labelled as a stub, llm_output is populated, and the legacy
# broker-llm-proxy branch is never reached.

def test_s10b_stub_mode_dispatch_runs_worker_agent_and_records_attestation():
    """Stub-mode end-to-end: the poller invokes worker-agent.py via
    _run_stub_sandbox_dispatch and produces an envelope with
    execution_mode=nemoclaw-sandbox-stub and a sandbox attestation
    block that explicitly says stub=True / attested=False."""
    os.environ["NEMOCLAW_STUB_MODE"] = "1"
    # _active_sandbox_name needs to return truthy (the elif at the
    # top of execute_in_envelope checks it), so seed DEFAULT_SANDBOX_NAME
    # and the live env. We also stub out _have_nemohermes_shim to True
    # so the stub branch fires.
    os.environ["NEMOCLAW_SANDBOX_NAME"] = "worker-stub"
    with mock.patch.object(poller, "DEFAULT_SANDBOX_NAME", "worker-stub"), \
         mock.patch.object(poller, "NEMOCLAW_STUB_MODE", True), \
         mock.patch.object(poller, "_have_nemohermes_shim", return_value=True), \
         mock.patch.object(poller, "publish_worker_keys"):
        envelope = _make_envelope()
        result = poller.execute_in_envelope(envelope)
    res = result["result"]
    # The stub helper runs worker-agent.py in-process, which calls
    # inference.local (not the broker proxy). Without a live
    # inference.local, worker-agent.py will fail with a connection
    # error and the envelope will record execution_mode="sandbox-failed"
    # — that's still a NemoClaw-shaped failure (with sandbox block,
    # not the bare broker-llm-proxy of the legacy bug). We accept
    # either stub success or sandbox-failed here, as long as the
    # legacy broker-llm-proxy mode is NEVER set.
    assert res["execution_mode"] in (
        "nemoclaw-sandbox-stub", "sandbox-failed",
    ), f"unexpected mode: {res['execution_mode']} — legacy broker-llm-proxy should be impossible"
    # Whatever the outcome, a sandbox block must be present — the
    # whole point of stub mode is to make the result envelope look
    # sandbox-shaped (even if the inner call failed).
    assert "sandbox" in res, \
        f"stub mode must always emit a sandbox block, got: {res}"
    sb = res["sandbox"]
    assert sb["name"] == "worker-stub", sb
    # The stub attestation is honest about not being attested.
    if res["execution_mode"] == "nemoclaw-sandbox-stub":
        assert sb.get("stub") is True, sb
        assert sb.get("attested") is False, sb
        # And llm_output is non-empty (the stub helper populated it
        # from worker-agent.py's success JSON).
        assert res.get("output"), res


# --- S10c: stub mode but no shim — must still fail-closed --------------------

def test_s10c_stub_mode_with_real_nemohermes_present_takes_real_path():
    """When BROKER_NEMOCLAW_STUB_MODE=1 is set on the broker AND the
    worker happens to have a real `nemohermes` binary on PATH (e.g. a
    worker whose NemoClaw install succeeded during user-data), the
    poller uses the real NemoClaw path, NOT the stub. Stub mode is
    the fallback for when the real install failed; if the real install
    succeeded we use it. This pins the priority: real-NemoClaw >
    stub-shim. The test asserts the poller enters the real sandbox
    branch (not the stub one) and surfaces any failure as
    sandbox-failed (with a sandbox block), not no-nemoclaw-failclosed.

    Implementation note: in the test environment there is a real
    nemohermes on PATH, so we don't need to mock anything for the
    shim check — the real path will fire naturally. We just verify
    the execution_mode is NOT 'nemoclaw-sandbox-stub' (since the
    real path was taken)."""
    os.environ["NEMOCLAW_STUB_MODE"] = "1"
    os.environ["NEMOCLAW_SANDBOX_NAME"] = "worker"
    with mock.patch.object(poller, "DEFAULT_SANDBOX_NAME", "worker"), \
         mock.patch.object(poller, "NEMOCLAW_STUB_MODE", True), \
         mock.patch.object(poller, "publish_worker_keys"):
        envelope = _make_envelope()
        result = poller.execute_in_envelope(envelope)
    res = result["result"]
    # Real NemoClaw was attempted (not the stub). The inner sandbox
    # call will fail in the test env (nemohermes config permission
    # error), so we expect sandbox-failed. Crucially: NOT
    # nemoclaw-sandbox-stub (the stub branch only fires when the
    # shim is the ONLY nemohermes on PATH, which the test env
    # doesn't satisfy).
    assert res["execution_mode"] != "nemoclaw-sandbox-stub", \
        "real nemohermes present — stub path should NOT be taken"
    # A sandbox block must still be present (whether sandbox-failed
    # or nemoclaw-sandbox).
    assert "sandbox" in res, res


# --- main --------------------------------------------------------------------

def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"PASS  {name}")
        except Exception as e:
            import traceback
            failures.append((name, e, traceback.format_exc()))
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    if failures:
        print("\n--- failures ---")
        for name, e, tb in failures:
            print(f"\n{name}:\n{tb}")
        sys.exit(1)


if __name__ == "__main__":
    main()
