"""Verify the worker poller honors env["skill_prompt"] from the EFS envelope.

Without this, a registered prompt_template (stored via POST /v1/skills) never
reaches the LLM — the poller's hardcoded ``skill_prompts`` dict is the only
path. Tests capture the actual prompt that hits the LLM proxy by intercepting
urllib.request.urlopen.

Properties verified:
  P1. skill NOT in hardcoded dict, NO skill_prompt in envelope:
      -> poller uses the generic "Process this request: ..." fallback
  P2. skill NOT in hardcoded dict, skill_prompt present in envelope:
      -> poller uses env["skill_prompt"] verbatim (the registered prompt wins)
  P3. skill IS in hardcoded dict ("summarize"), NO skill_prompt in envelope:
      -> poller uses the hardcoded "Summarize the following text..." prompt
  P4. skill IS in hardcoded dict ("summarize"), skill_prompt present in envelope:
      -> poller prefers env["skill_prompt"] (registered overrides built-in)
  P5. skill_prompt with {{input}} placeholder:
      -> poller substitutes {{input}} with the encrypted_data value
      (chose {{input}} as the placeholder because it's already used in the
      existing tests' good_manifest; keeps the contract unambiguous)
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        print(f"[PASS] {label}")
        PASS += 1
    else:
        print(f"[FAIL] {label}  ({detail})")
        FAIL += 1
        FAILURES.append(label)


# Path constants for the sandboxed copy of poller.py
POLLER_SRC = Path(__file__).resolve().parent.parent / "worker" / "poller.py"

# The actual prompt sent to the LLM is captured in a JSON file the test writes
# from inside the sandbox. A sentinel file in /tmp bridges sandbox -> parent.
SENTINEL_TEMPLATE = "/tmp/_poller_captured_{tag}.json"


def make_sandbox(tag: str) -> tuple[str, str]:
    """Return (sandbox_dir, poller_py_path) for a fresh poller copy.

    The poller already supports BROKER_WORKER_KEYS and BROKER_EFS_LOGS env
    overrides (commit cfb0d04), so we only need to rewrite the legacy
    hardcoded /mnt/broker paths. We also pass the env vars through to the
    subprocess that runs the poller.
    """
    sandbox_dir = tempfile.mkdtemp(prefix=f"poller-dispatch-{tag}-")
    sb_efs = sandbox_dir + "/broker"
    Path(sb_efs + "/jobs/inbox").mkdir(parents=True)
    Path(sb_efs + "/jobs/outbox").mkdir(parents=True)
    Path(sb_efs + "/logs").mkdir(parents=True)
    sb_keys = sandbox_dir + "/worker/keys"
    Path(sb_keys).mkdir(parents=True)
    src = POLLER_SRC.read_text()
    # Rewrite only the legacy /mnt/broker paths. KEY_DIR + LOGS already
    # respect BROKER_WORKER_KEYS / BROKER_EFS_LOGS env vars.
    src = src.replace('Path("/mnt/broker/jobs/inbox")',
                      f'Path("{sb_efs}/jobs/inbox")')
    src = src.replace('Path("/mnt/broker/jobs/outbox")',
                      f'Path("{sb_efs}/jobs/outbox")')
    poller_path = sandbox_dir + f"/poller_{tag}.py"
    Path(poller_path).write_text(src)
    return sandbox_dir, poller_path


def run_poller(sandbox_dir: str, poller_path: str, env: dict,
               capture_tag: str) -> dict:
    """Execute the sandboxed poller's execute_in_envelope with `env`.

    Captures the prompt actually sent to the LLM by hooking urllib via a
    tiny monkeypatch shim that captures the request body and returns a
    canned response.

    Returns the full result envelope from execute_in_envelope().
    """
    # Inject a fake per-job LLM token so the poller takes the broker-proxy
    # path (otherwise it short-circuits with "no_llm_path" and the LLM is
    # never called). Also override the proxy URL to a value we can match in
    # the captured body.
    env = {**env,
           "llm_token": "llm_test_token_fake",
           "llm_proxy_url": "http://127.0.0.1:9999/v1/llm/chat/completions"}
    # Write the env dict to a JSON file so the driver can load it without
    # going through repr() (which would quote it as a string literal).
    env_file = Path(sandbox_dir) / f"env_{capture_tag}.json"
    env_file.write_text(json.dumps(env))
    driver = """
import sys, os, json, base64, urllib.request
sys.path.insert(0, %s)
import poller_%s as mod  # noqa: E402

# ---- capture shim: intercept urllib.request.urlopen and capture the body ----
_captured = {}
_orig_open = urllib.request.urlopen

def _capture(req, *a, **kw):
    body = req.data
    if isinstance(body, bytes):
        try:
            decoded = json.loads(body.decode())
        except Exception:
            decoded = {"_raw": body.decode(errors='replace')}
    else:
        decoded = {"_body_was_none": True, "_body_type": type(body).__name__}
    _captured["body"] = decoded
    _captured["url"] = req.full_url
    _captured["auth"] = req.headers.get("Authorization", "")
    # Mimic urllib.response.addinfourl — poller uses `with urlopen(req) as resp`
    # then calls resp.read(). Returning a real addinfourl object keeps the
    # contract identical so the poller doesn't fall into the exception branch.
    import io
    payload = json.dumps({
        "choices": [{"message": {"content": "STUB_LLM_OUTPUT"}}],
        "model": "stub-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "_billing": {"tokens_used": 15, "tokens_cap": 50000},
    }).encode()
    return urllib.request.addinfourl(
        io.BytesIO(payload),  # fp
        headers={"Content-Type": "application/json"},  # headers
        url=req.full_url,  # url
        code=200,  # code
    )

urllib.request.urlopen = _capture

with open(%s) as f:
    env = json.loads(f.read())
result = mod.execute_in_envelope(env)
print("CAPTURED:" + json.dumps(_captured))
print("RESULT_JSON:" + json.dumps(result))
""" % (repr(sandbox_dir), capture_tag, repr(str(env_file)))
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        env={**os.environ,
             "BROKER_KEEP_PLAINTEXT_FOR_DEMO": "1",  # plaintext to inspect output
             "BROKER_LLM_BASE_URL": "http://127.0.0.1:1",
             "BROKER_WORKER_KEYS": sandbox_dir + "/worker/keys",
             "BROKER_EFS_LOGS": sandbox_dir + "/broker/logs",
             "BROKER_ARTIFACTS_DIR": sandbox_dir + "/broker/jobs/artifacts"},
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        print(f"[FAIL] subprocess crashed (tag={capture_tag}): rc={proc.returncode}")
        print(f"  stdout: {proc.stdout!r}")
        print(f"  stderr: {proc.stderr!r}")
        return {"_crashed": True, "_stdout": proc.stdout, "_stderr": proc.stderr}
    captured = {}
    result = {}
    for line in proc.stdout.splitlines():
        if line.startswith("CAPTURED:"):
            captured = json.loads(line[len("CAPTURED:"):])
        elif line.startswith("RESULT_JSON:"):
            result = json.loads(line[len("RESULT_JSON:"):])
    return {"captured": captured, "result": result}


def llm_prompt_text(captured: dict) -> str:
    """Extract the user-role prompt string the poller sent to the LLM."""
    body = captured.get("body") or {}
    msgs = body.get("messages", []) if isinstance(body, dict) else []
    return "\n".join(m.get("content", "") for m in msgs if isinstance(m, dict))


def main() -> int:
    print("=== P1. unknown skill, no skill_prompt -> generic fallback ===")
    sb, pp = make_sandbox("p1")
    out = run_poller(sb, pp, {
        "job_id": "job_p1",
        "encrypted_skill": "totally-unknown-skill",
        "encrypted_data": "hello data",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_p1",
        # skill_hash is required by the poller (VULN-S hardening from
        # t_bf00a075). In production the broker always emits it in the
        # envelope; tests must mirror that to reach the LLM call.
        "skill_hash": hashlib.sha256(b"totally-unknown-skill").hexdigest(),
    }, "p1")
    if out.get("_crashed"):
        check("P1. poller did not crash", False, out.get("_stderr", "")[:200])
    else:
        prompt = llm_prompt_text(out["captured"])
        if not prompt:
            print(f"  [DEBUG P1] captured={out['captured']!r}")
        check("P1. unknown-skill uses generic 'Process this request' fallback",
              "Process this request" in prompt and "hello data" in prompt,
              f"prompt[:120]={prompt[:120]!r}")

    print()
    print("=== P2. unknown skill WITH skill_prompt -> registered prompt used ===")
    sb, pp = make_sandbox("p2")
    out = run_poller(sb, pp, {
        "job_id": "job_p2",
        "encrypted_skill": "attestation-verifier",  # not in hardcoded dict
        "encrypted_data": '{"tee_type":"amd-sev-snp"}',
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_p2",
        "skill_prompt": "REGISTERED PROMPT: parse the attestation and decide pass/fail. Input: {{input}}",
        "skill_hash": hashlib.sha256(b"attestation-verifier").hexdigest(),
    }, "p2")
    if out.get("_crashed"):
        check("P2. poller did not crash", False, out.get("_stderr", "")[:200])
    else:
        prompt = llm_prompt_text(out["captured"])
        check("P2. registered skill_prompt verbatim in LLM prompt",
              "REGISTERED PROMPT: parse the attestation" in prompt,
              f"prompt[:160]={prompt[:160]!r}")
        check("P2b. registered prompt overrides generic fallback",
              "Process this request" not in prompt,
              "env.skill_prompt must win over the generic 'Process this request' fallback")

    print()
    print("=== P3. built-in 'summarize', no skill_prompt -> hardcoded prompt ===")
    sb, pp = make_sandbox("p3")
    out = run_poller(sb, pp, {
        "job_id": "job_p3",
        "encrypted_skill": "summarize",  # in hardcoded dict
        "encrypted_data": "the quick brown fox",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_p3",
        "skill_hash": hashlib.sha256(b"summarize").hexdigest(),
    }, "p3")
    if out.get("_crashed"):
        check("P3. poller did not crash", False, out.get("_stderr", "")[:200])
    else:
        prompt = llm_prompt_text(out["captured"])
        check("P3. built-in 'summarize' uses hardcoded prompt",
              "Summarize the following text" in prompt,
              f"prompt[:120]={prompt[:120]!r}")
        check("P3b. encrypted_data is interpolated into the prompt",
              "the quick brown fox" in prompt,
              f"prompt={prompt[:200]!r}")

    print()
    print("=== P4. built-in 'summarize' WITH skill_prompt -> registered wins ===")
    sb, pp = make_sandbox("p4")
    out = run_poller(sb, pp, {
        "job_id": "job_p4",
        "encrypted_skill": "summarize",  # in hardcoded dict — but overridden
        "encrypted_data": "the quick brown fox",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_p4",
        "skill_prompt": "OVERRIDE: produce a haiku. Input: {{input}}",
        "skill_hash": hashlib.sha256(b"summarize").hexdigest(),
    }, "p4")
    if out.get("_crashed"):
        check("P4. poller did not crash", False, out.get("_stderr", "")[:200])
    else:
        prompt = llm_prompt_text(out["captured"])
        check("P4. registered skill_prompt overrides built-in 'summarize'",
              "OVERRIDE: produce a haiku" in prompt,
              f"prompt[:160]={prompt[:160]!r}")
        check("P4b. built-in prompt is NOT used when skill_prompt is set",
              "Summarize the following text" not in prompt,
              "registered skill_prompt must win over the hardcoded dict entry")

    print()
    print("=== P5. skill_prompt with {data} substitution -> interpolated ===")
    sb, pp = make_sandbox("p5")
    out = run_poller(sb, pp, {
        "job_id": "job_p5",
        "encrypted_skill": "attestation-verifier",
        "encrypted_data": "PAYLOAD-ABC123",
        "result_pubkey": "0x",
        "stripe_pi_id": "pi_p5",
        "skill_prompt": "Process this payload: {data}",
        "skill_hash": hashlib.sha256(b"attestation-verifier").hexdigest(),
    }, "p5")
    if out.get("_crashed"):
        check("P5. poller did not crash", False, out.get("_stderr", "")[:200])
    else:
        prompt = llm_prompt_text(out["captured"])
        check("P5. {data} placeholder substituted with encrypted_data",
              "PAYLOAD-ABC123" in prompt,
              f"prompt={prompt[:200]!r}")
        check("P5b. {data} placeholder NOT left in prompt verbatim",
              "{data}" not in prompt,
              "substitution must replace every {data} occurrence")

    print()
    print(f"=== Summary: PASS={PASS} FAIL={FAIL} ===")
    if FAIL:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())