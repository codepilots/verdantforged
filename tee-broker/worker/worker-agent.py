#!/usr/bin/env python3
"""worker-agent.py — runs INSIDE the NemoClaw sandbox per job.

This script is the per-job execution unit. The host poller (`worker/poller.py`)
copies it into the sandbox at onboard time (`nemohermes worker cp
/opt/worker/worker-agent.py /sandbox/worker-agent.py`), then runs it via
`nemohermes exec --no-tty --timeout 120 --env <json> <sandbox> python3
/sandbox/worker-agent.py`.

Inputs come from --env (set per call by the host poller, passed in the
encrypted envelope rather than on the command line):
  - JOB_ID          job_id from /v1/jobs response
  - SKILL_PROMPT    the prompt template for the requested skill
  - INPUT_DATA      the user's input string (decrypted by the host poller)
  - RESULT_PUBKEY   base64(X25519 worker's per-job ephemeral pubkey) — used
                    by the host poller to encrypt the result envelope, not
                    directly by this script
  - COMPATIBLE_API_KEY  per-job ephemeral broker token (in --env so it
                        never appears on the command line)
  - NEMOCLAW_ENDPOINT_URL  broker proxy URL (e.g. http://172.31.3.184:8080/v1/llm)
  - NEMOCLAW_MODEL         upstream model name (e.g. minimax-m3)

Output: a single JSON object on stdout. The host poller parses this,
verifies signatures, and writes to EFS outbox. Schema:

    {
      "output":         "<LLM response text>",
      "model":          "minimax-m3",
      "usage": {
          "prompt_tokens":     int,
          "completion_tokens": int,
          "total_tokens":      int
      },
      "duration_ms":    int,         # wall time for the LLM call
      "sandbox": {
          "name": "tee-worker",
          "inference_route": "http://<broker_ip>:8080/v1/llm",
          "attested": true
      }
    }

On any failure, print a JSON object with an `error` key on stdout and
exit non-zero. The host poller catches this and records execution_mode =
"nemoclaw-sandbox" with llm_error populated.

Why this script lives in Python (not bash):
- We need to time the call (duration_ms), parse the OpenAI-shaped response,
  and print structured JSON. Bash would need jq + a stopwatch, which is
  brittle and adds another dependency to ship into the sandbox.
- Python's stdlib `urllib.request` is preinstalled in every reasonable
  sandbox image. We avoid `requests` to keep the dependency surface
  minimal.

Why we use urllib.request + POST, not inference.local shim:
- `inference.local` is OpenShell's network policy redirect — it routes
  outbound HTTPS to `localhost:9999` instead of the public network. The
  sandbox gateway then forwards to the broker proxy per the OpenShell
  egress policy. This script POSTs to NEMOCLAW_ENDPOINT_URL directly
  so we don't depend on the gateway being up before we can run.
- If the gateway IS up, OpenShell transparently redirects at the kernel
  level, so this code path is identical. The hardcoding below is for
  the case where inference.local hasn't been configured but the broker
  is reachable on a known IP.

Security note: COMPATIBLE_API_KEY is the per-job ephemeral token issued
by the broker. It is bound to (job_id, result_pubkey, expiry). The
broker validates token + job_id match before forwarding. The token is
single-use — the broker marks it consumed after one successful call.
This script treats it as a Bearer token in the Authorization header.
"""

from __future__ import annotations

import json
import os

# The sandbox launches this script via `nemohermes exec` without a shell,
# so HOME is unset. OpenShell's gateway-registration code reads $HOME to
# locate its metadata dir and crashes ("No gateway metadata found for
# 'nemoclaw'") without it. Set a sane default before any code path reads it.
os.environ.setdefault("HOME", "/root")

import sys
import time
import urllib.error
import urllib.request
from typing import Any


# ---- Constants --------------------------------------------------------------

SANDBOX_API_VERSION = "v1"
LLM_TIMEOUT_S = int(os.environ.get("WORKER_LLM_TIMEOUT_S", "110"))
# Bounded file context for file jobs. The host poller stages uploaded files
# into INPUT_DIR inside the sandbox before launching this script. Without
# reading that directory, `--file BUGS.md --file deploy.sh` only proves that
# files can be uploaded/decrypted, not that the NemoClaw-side agent actually
# reviews them.
INPUT_FILES_MAX_BYTES = int(os.environ.get(
    "WORKER_INPUT_FILES_MAX_BYTES", str(192 * 1024)))
# Slightly less than the nemohermes outer timeout (default 120s) so we
# fail fast inside the call rather than getting killed mid-stream by the
# parent timeout, which would leave the broker unsure whether the LLM
# call landed.

# ---- Read inputs from env ----------------------------------------------------

JOB_ID          = os.environ.get("JOB_ID", "")
SKILL_PROMPT    = os.environ.get("SKILL_PROMPT", "")
INPUT_DATA      = os.environ.get("INPUT_DATA", "")
NEMOCLAW_MODEL  = os.environ.get("NEMOCLAW_MODEL", "minimax-m3")
ENDPOINT_URL    = os.environ.get("NEMOCLAW_ENDPOINT_URL", "")  # e.g. http://172.31.3.184:8080/v1/llm


def render_input_files(input_dir: str, max_bytes: int = INPUT_FILES_MAX_BYTES) -> str:
    """Return a prompt-safe text block for files staged in INPUT_DIR."""
    if not input_dir:
        return ""
    root = os.path.abspath(input_dir)
    if not os.path.isdir(root):
        return ""

    rendered: list[str] = []
    remaining = max(0, int(max_bytes))
    total_files = 0
    truncated = False

    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(base, d)))
        for name in sorted(files):
            path = os.path.join(base, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, root)
            if rel.startswith("..") or os.path.isabs(rel):
                continue
            total_files += 1
            try:
                size = os.path.getsize(path)
                with open(path, "rb") as fh:
                    data = fh.read(remaining if remaining > 0 else 0)
            except OSError as e:
                rendered.append(
                    f"\n--- FILE: {rel} (read error: {type(e).__name__}: {e}) ---\n")
                continue

            if size > len(data):
                truncated = True
            remaining -= len(data)
            text = data.decode("utf-8", errors="replace")
            rendered.append(f"\n--- FILE: {rel} ({size} bytes) ---\n{text}")
            if remaining <= 0:
                truncated = True
                break
        if remaining <= 0:
            break

    if not rendered:
        return ""
    header = f"\n\n---\n\nAttached input files ({total_files} file(s)):\n"
    footer = ""
    if truncated:
        footer = f"\n\n[Input file context truncated at {max_bytes} bytes.]"
    return header + "".join(rendered) + footer


def fail(msg: str, **extra: Any) -> None:
    """Print a structured error JSON and exit non-zero.

    The host poller reads stdout, JSON-parses it, and surfaces the
    `error` field to llm_error in execute_in_envelope. Non-zero exit
    also surfaces as a sandbox-exec failure (RuntimeError in
    dispatch_to_sandbox). Both signals together mean we never silently
    swallow a problem.
    """
    payload: dict[str, Any] = {
        "error":      msg,
        "job_id":     JOB_ID,
        "duration_ms": 0,
        "sandbox":    {
            "name":            os.environ.get("NEMOCLAW_SANDBOX_NAME", ""),
            "inference_route": ENDPOINT_URL,
            "attested":        True,
        },
    }
    payload.update(extra)
    sys.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(1)


def main() -> int:
    # ---- Validate inputs -----------------------------------------------------

    if not JOB_ID:
        fail("JOB_ID not set in env — host poller must pass it via --env")
    if not SKILL_PROMPT:
        fail("SKILL_PROMPT not set in env — skill missing from envelope")
    if not ENDPOINT_URL:
        fail("NEMOCLAW_ENDPOINT_URL not set in env — broker proxy URL missing")

    # ---- Compose the actual prompt ------------------------------------------

    # Two shapes:
    #
    #   (a) SKILL_PROMPT contains "<<INPUT>>" — substitute the user input
    #       in place. This is the template form (recommended).
    #
    #   (b) SKILL_PROMPT has no placeholder — append the user input as
    #       context. Legacy form, kept for backward compatibility with
    #       skills that wrote prompts before the template contract.
    input_files_context = render_input_files(os.environ.get("INPUT_DIR", ""))
    combined_input = INPUT_DATA + input_files_context
    if "<<INPUT>>" in SKILL_PROMPT:
        final_prompt = SKILL_PROMPT.replace("<<INPUT>>", combined_input)
    else:
        final_prompt = f"{SKILL_PROMPT}\n\n---\n\n{combined_input}"

    # ---- Call inference.local (OpenShell-intercepted LLM proxy) -------------

    body = {
        "model": NEMOCLAW_MODEL,
        "messages": [
            {"role": "user", "content": final_prompt},
        ],
        "temperature": 0.7,
        "max_tokens":  512,
        # Pin a stream=false response — we want the full JSON in one
        # round-trip. Streaming would require an SSE client and adds
        # another failure mode we don't need for the demo.
        "stream": False,
    }
    # Authorization header: COMPATIBLE_API_KEY is the per-job ephemeral
    # broker token. NOTE: --env passes the value, not an "Authorization:
    # Bearer " prefix — we add it here so the prefix never appears in
    # the env dump, only in the HTTPS header (which is TLS-encrypted).
    api_key = os.environ.get("COMPATIBLE_API_KEY", "")
    if not api_key:
        fail("COMPATIBLE_API_KEY not set in env — broker token missing")

    # OpenShell / inference.local may rebuild the outbound request and drop
    # both Authorization and custom headers before the broker sees it. Put the
    # Verdant job token in the JSON body as a second sideband; the broker reads
    # and strips this field before applying its upstream LLM whitelist, so it
    # is never forwarded to the real model provider.
    body["verdant_llm_token"] = api_key
    body["verdant_job_id"] = JOB_ID
    body_bytes = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url     = ENDPOINT_URL.rstrip("/") + "/chat/completions",
        data    = body_bytes,
        method  = "POST",
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
            # OpenShell's managed inference.local path may own/replace the
            # provider Authorization header while forwarding to the broker.
            # Send the per-job Verdant token in a broker-specific sideband
            # header as well; the broker prefers this header when present.
            "X-Verdant-LLM-Token": api_key,
            "X-Job-Id":      JOB_ID,
            "User-Agent":    f"verdantforged-worker/{SANDBOX_API_VERSION} (nemo-sandbox)",
        },
    )

    # Initialise vars so the except/parse branches always have a value
    # even if urlopen raises before any assignment. Pyright is correct
    # that the except blocks can fall through without binding these.
    status_code: int = 0
    resp_body: bytes = b""

    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
            status_code = resp.status
            resp_body = resp.read()
    except urllib.error.HTTPError as e:
        # Read the response body even on error — Stripe / Ollama often
        # include useful diagnostic info (rate-limit reason, etc.).
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        dur_ms = int((time.monotonic() - t0) * 1000)
        fail(
            f"LLM HTTP {e.code}: {e.reason}",
            duration_ms=dur_ms,
            status_code=e.code,
            response_body=err_body[:500],
        )
    except urllib.error.URLError as e:
        dur_ms = int((time.monotonic() - t0) * 1000)
        fail(
            f"LLM URL error: {e.reason}",
            duration_ms=dur_ms,
        )
    except Exception as e:
        dur_ms = int((time.monotonic() - t0) * 1000)
        fail(
            f"LLM unexpected: {type(e).__name__}: {e}",
            duration_ms=dur_ms,
        )

    dur_ms = int((time.monotonic() - t0) * 1000)

    if status_code != 200:
        fail(
            f"LLM non-200: {status_code}",
            duration_ms=dur_ms,
            status_code=status_code,
            response_body=resp_body.decode("utf-8", errors="replace")[:500],
        )

    # ---- Parse OpenAI-shaped response ---------------------------------------

    try:
        resp_json = json.loads(resp_body.decode("utf-8"))
    except json.JSONDecodeError as e:
        fail(
            f"LLM returned non-JSON: {e}",
            duration_ms=dur_ms,
            response_body=resp_body[:200].decode("utf-8", errors="replace"),
        )

    output_text = ""
    try:
        # OpenAI shape: { choices: [ { message: { content: "..." } } ] }
        message = resp_json["choices"][0]["message"]
        output_text = message.get("content") or ""
    except (KeyError, IndexError, TypeError) as e:
        fail(
            f"LLM response missing choices[0].message.content: {e}",
            duration_ms=dur_ms,
            response_keys=list(resp_json.keys()),
        )

    # Defense-in-depth: reasoning-class models (e.g. minimax-m3:cloud on
    # Ollama Cloud) sometimes burn their entire completion-token budget
    # on chain-of-thought in a `reasoning` field and leave `content=""`.
    # The operator then sees an empty output.txt with non-zero usage and
    # has no way to tell whether the worker crashed or the model
    # reasoned. If we see that pattern, surface the reasoning as the
    # output (prefixed with a marker) so the artifact is non-empty AND
    # the operator can grep for the marker if they want to filter it.
    # See Pitfall 29 in tee-broker-deploy-config.
    try:
        reasoning_text = message.get("reasoning") or ""
    except Exception:
        reasoning_text = ""
    if not output_text and reasoning_text:
        output_text = (
            "[worker-agent: upstream returned empty content with non-empty "
            "reasoning. Surfacing reasoning as output.]\n\n" + reasoning_text
        )

    # Also surface finish_reason (length / content_filter / stop) so a
    # truncated response is visible to the operator.
    try:
        finish_reason = resp_json["choices"][0].get("finish_reason") or ""
    except (KeyError, IndexError, TypeError):
        finish_reason = ""
    if finish_reason and finish_reason != "stop":
        output_text = (
            f"[worker-agent: finish_reason={finish_reason}]\n\n" + output_text
        )

    usage = resp_json.get("usage") or {}
    model_name = resp_json.get("model") or NEMOCLAW_MODEL

    # Unified file-runtime contract: every prompt skill emits its primary
    # response as output.txt. Executable skills may add further regular files
    # to OUTPUT_DIR; the host poller validates and encrypts the whole tree.
    output_dir = os.environ.get("OUTPUT_DIR", "")
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "output.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(output_text)

    # ---- Emit the structured success payload --------------------------------

    out = {
        "output":      output_text,
        "model":       model_name,
        "usage": {
            "prompt_tokens":     int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "total_tokens":      int(usage.get("total_tokens", 0)),
        },
        "duration_ms": dur_ms,
        "sandbox": {
            "name":            os.environ.get("NEMOCLAW_SANDBOX_NAME", ""),
            "inference_route": ENDPOINT_URL,
            "attested":        True,
        },
    }
    sys.stdout.write(json.dumps(out, sort_keys=True, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        # Last-resort catch — print a JSON error and die. Never let the
        # process die silently; the poller must know something went wrong.
        fail(f"worker-agent uncaught: {type(e).__name__}: {e}")
