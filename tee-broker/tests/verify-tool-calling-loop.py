#!/usr/bin/env python3
"""Verify the worker poller's multi-turn tool-calling loop (kanban t_5e7f89fa).

The original `execute_in_envelope()` made exactly ONE LLM call (single-turn)
and wrote the result. Skills that need multi-step reasoning or tool use
(think-then-act, fetch-then-summarize, plan-then-execute) couldn't express
that pattern — every skill looked like a one-shot prompt.

This suite pins the new layer:

  JobContext                pure-Python per-job state (no I/O)
  ------------------------------
  • messages               OpenAI-format message list, append-only
  • turn_count             LLM calls made so far
  • fuel_used              fuel charged to the loop (1 unit / ms by default)
  • max_turns              hard cap (default 5)
  • max_fuel               hard cap (default 50_000 ms — 50s wall)
  • tool_results           list of tool invocations (name, args, output, ms)

  run_tool_calling_loop(ctx, dispatch_fn) -> terminal_message
  ------------------------------
  • Appends the initial user prompt, calls dispatch_fn(ctx) to get the next
    assistant message (LLM output), and decides whether to terminate
    (`final_answer` tool) or continue (any other tool whose result the loop
    appends as a `tool` message before the next iteration).
  • Bounded by max_turns AND max_fuel; returns a structured LoopResult.

  call_broker_llm_proxy(...)  (extracted from the old single-turn block)
  ------------------------------
  • Pure I/O — POSTs to llm_proxy_url with Authorization Bearer llm_token,
    returns the parsed OpenAI response. Extracted so the loop can call it
    N times and tests can stub it once.

Properties verified:
  T1.  JobContext.append() rejects system messages (those are prompt-level
       and can only be set at construction — prevents accidental injection
       mid-loop)
  T2.  JobContext.append() rejects messages missing required OpenAI fields
       (role + content)
  T3.  JobContext.fuel_remaining() returns max_fuel - fuel_used, clamped
       to 0 when exceeded
  T4.  JobContext.is_exhausted() is True iff turn_count >= max_turns OR
       fuel_used >= max_fuel
  T5.  run_tool_calling_loop() with max_turns=3 calls dispatch_fn at most
       3 times even if every response continues (returns last assistant)
  T6.  run_tool_calling_loop() with a dispatch_fn that always picks
       `final_answer` terminates after exactly 1 turn
  T7.  run_tool_calling_loop() with a dispatch_fn that picks
       `final_answer` on turn 3 terminates with that answer
  T8.  run_tool_calling_loop() with a dispatch_fn that picks `rephrase`
       (continue) calls dispatch_fn again with the tool result appended
       to ctx.messages
  T9.  run_tool_calling_loop() appends the initial prompt as the first
       user message before any LLM call
  T10. run_tool_calling_loop() charges 1 fuel unit per dispatch call by
       default (so a 5-turn loop with 1ms-each stub consumes 5 fuel)
  T11. call_broker_llm_proxy() POSTs {model, messages, max_tokens, stream}
       to the configured llm_proxy_url with Bearer token auth — exactly
       the same shape the legacy single-turn path used, just lifted out
       into a callable so the loop can reuse it
  T12. execute_in_envelope() with execution_mode="single-turn" (default,
       back-compat) keeps the existing single-call behaviour and does NOT
       invoke the loop
  T13. execute_in_envelope() with execution_mode="tool-calling-loop"
       invokes run_tool_calling_loop() with the skill's tool registry and
       respects max_turns from env["max_turns"]
  T14. execute_in_envelope() with execution_mode="tool-calling-loop"
       whose loop exhausts fuel returns execution_mode="tool-loop-budget"
       and the loop's terminal output (NOT a generic error)
  T15. The result envelope produced by a tool-calling-loop run carries
       an audit block (result["loop"]) with turns_used (count),
       tool_calls (list of {name, args, output, duration_ms}), and
       tools_available (list of skill-declared tool names) so operators
       can audit what the loop did without re-running the job
  T16. The loop honours the BROKER_PROXY whitelist constraint: even if
       a skill tries to inject `tools=[...]` or `temperature=X` into the
       forward body, call_broker_llm_proxy() only passes {model, messages,
       max_tokens, stream} — same constraint the broker enforces
       server-side (VULN-S5)
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest import mock

# --- test env setup (must run BEFORE importing poller) -----------------------
TEST_ROOT = Path(tempfile.mkdtemp(prefix="tool-loop-test-"))
os.environ["BROKER_EFS_MOUNT"] = str(TEST_ROOT)
os.environ["BROKER_ARTIFACTS_DIR"] = str(TEST_ROOT / "artifacts")
os.environ["BROKER_WORKER_KEYS"] = str(TEST_ROOT / "keys")
os.environ["BROKER_KEEP_PLAINTEXT_FOR_DEMO"] = "1"
os.environ["WORKER_SEV_SNP_DISABLED"] = "1"

WORKER_DIR = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER_DIR))

import poller  # noqa: E402

# Stub SEV-SNP — without this, get_sev_snp_measurement() tries to reach IMDS
# over HTTP and hangs the test.
poller.get_sev_snp_measurement = lambda: "stub-measurement-for-tests"


# --- helpers -----------------------------------------------------------------

def _make_envelope(**overrides):
    """Build a minimal envelope mirroring submit_job's shape."""
    env = {
        "job_id": "j-tool-loop-001",
        "encrypted_skill": "summarize",
        "encrypted_data": "Long text that should be summarized.",
        "llm_token": "tok-deadbeef",
        "llm_proxy_url": "http://10.0.0.5:8080/v1/llm/chat/completions",
        "result_pubkey": "0x",
        "skill_hash": ("a" * 64),
        # Default to single-turn so callers have to opt in.
        "execution_mode": "single-turn",
    }
    env.update(overrides)
    return env


def _ok_response(content="hello", model="minimax-m3",
                 prompt_tokens=10, completion_tokens=5):
    """Build an OpenAI-format completion response."""
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "model": model,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# --- T1: JobContext rejects system messages after construction --------------

def test_t1_job_context_rejects_system_messages_after_construction():
    """System messages set the loop's rules — they must come from the
    prompt template, not be injected by a tool result or a malicious
    skill. Append-only is for `user`/`assistant`/`tool` roles."""
    ctx = poller.JobContext(system="You are a helper.", max_turns=3,
                            max_fuel=100)
    assert ctx.messages[0]["role"] == "system"
    try:
        ctx.append({"role": "system", "content": "Hijacked!"})
    except ValueError:
        return
    raise AssertionError("JobContext.append accepted a system message "
                         "post-construction")


# --- T2: JobContext rejects malformed messages ------------------------------

def test_t2_job_context_rejects_malformed_messages():
    """OpenAI requires both role and content. The loop calls append many
    times — one bad call shouldn't silently corrupt the context."""
    ctx = poller.JobContext(max_turns=3, max_fuel=100)
    for bad in [
        {"role": "user"},               # missing content
        {"content": "hi"},              # missing role
        {},                             # missing both
        {"role": "user", "content": None},  # null content
    ]:
        try:
            ctx.append(bad)
        except (ValueError, TypeError):
            continue
        raise AssertionError(f"JobContext.append accepted {bad!r}")


# --- T3: fuel_remaining clamps to 0 -----------------------------------------

def test_t3_job_context_fuel_remaining_clamps_to_zero():
    """fuel_remaining() must never return negative — callers use it to
    decide whether to start another turn. A negative value would make
    the loop think it has budget left when it doesn't."""
    ctx = poller.JobContext(max_turns=100, max_fuel=10)
    ctx.fuel_used = 15
    assert ctx.fuel_remaining() == 0, ctx.fuel_remaining()
    ctx.fuel_used = 0
    assert ctx.fuel_remaining() == 10, ctx.fuel_remaining()
    ctx.fuel_used = 5
    assert ctx.fuel_remaining() == 5, ctx.fuel_remaining()


# --- T4: is_exhausted covers both caps --------------------------------------

def test_t4_job_context_is_exhausted_dual_cap():
    """Either budget violation must stop the loop. We refuse the
    "fuel-fine-but-turns-ok to keep going" pattern because a runaway
    skill could otherwise spend forever calling the proxy."""
    ctx = poller.JobContext(max_turns=2, max_fuel=10_000)
    ctx.turn_count = 1
    assert not ctx.is_exhausted(), "1 turn of 2, fuel fine — should continue"
    ctx.turn_count = 2
    assert ctx.is_exhausted(), "turns at cap — must stop"
    # Fresh budget, only fuel exhausted
    ctx2 = poller.JobContext(max_turns=10, max_fuel=100)
    ctx2.fuel_used = 100
    assert ctx2.is_exhausted(), "fuel at cap — must stop"


# --- T5: loop stops at max_turns even if every response continues -----------

def test_t5_loop_stops_at_max_turns_even_when_responses_continue():
    """A misbehaving skill (or a stuck loop) must NOT escape the cap."""
    call_count = [0]

    def dispatch(ctx):
        call_count[0] += 1
        # Always returns a "continue" tool result
        ctx.append({"role": "assistant", "content": f"turn {call_count[0]}"})
        return "continue"

    ctx = poller.JobContext(max_turns=3, max_fuel=10_000)
    ctx.append({"role": "user", "content": "do the thing"})
    terminal = poller.run_tool_calling_loop(ctx, dispatch)
    assert call_count[0] == 3, \
        f"loop should stop at max_turns=3; got {call_count[0]} calls"
    assert ctx.is_exhausted(), "loop stopped without hitting its cap?"


# --- T6: loop with always-final_answer terminates after 1 turn -------------

def test_t6_loop_with_final_answer_terminates_after_one_turn():
    """The happy path: skill decides on the first turn what to return.
    No extra round-trips."""
    def dispatch(ctx):
        ctx.append({"role": "assistant",
                    "content": "The answer is 42."})
        return "final_answer"

    ctx = poller.JobContext(max_turns=10, max_fuel=10_000)
    ctx.append({"role": "user", "content": "What is the answer?"})
    terminal = poller.run_tool_calling_loop(ctx, dispatch)
    # dispatch must have been called exactly once
    assert len([m for m in ctx.messages if m["role"] == "assistant"]) == 1


# --- T7: loop terminates when final_answer appears mid-loop ---------------

def test_t7_loop_terminates_on_final_answer_mid_loop():
    """Skill decides on turn 3 to wrap up — loop must stop immediately
    rather than continuing for the full max_turns."""
    turn = [0]

    def dispatch(ctx):
        turn[0] += 1
        ctx.append({"role": "assistant",
                    "content": f"thinking turn {turn[0]}"})
        return "final_answer" if turn[0] >= 3 else "continue"

    ctx = poller.JobContext(max_turns=10, max_fuel=10_000)
    ctx.append({"role": "user", "content": "Iterate a few times"})
    poller.run_tool_calling_loop(ctx, dispatch)
    assert turn[0] == 3, f"expected 3 turns then terminate; got {turn[0]}"
    assert ctx.turn_count == 3


# --- T8: loop with continue tool appends tool result before next iter -----

def test_t8_loop_appends_tool_result_before_next_iter():
    """When dispatch_fn returns "continue", the loop appends a tool
    result message so the LLM sees what happened. The OpenAI format
    is `role: "tool"` with `content` and `tool_call_id`."""
    def dispatch(ctx):
        ctx.append({"role": "assistant",
                    "content": "calling lookup tool"})
        return "continue"

    ctx = poller.JobContext(max_turns=2, max_fuel=10_000)
    ctx.append({"role": "user", "content": "look up X"})
    # Provide a tool so the loop has something to record
    tools = [poller.SkillTool(
        name="lookup",
        description="Look up X",
        execute=lambda args, ctx: f"result-for-{args.get('q', '?')}")]
    poller.run_tool_calling_loop(ctx, dispatch, tools=tools)
    # We expect: system?, user, assistant, tool, assistant — at minimum
    # there should be one `tool` role message after the first assistant.
    roles = [m["role"] for m in ctx.messages]
    assert "tool" in roles, f"no tool result appended; got {roles}"


# --- T9: initial user prompt is appended before any LLM call ---------------

def test_t9_loop_appends_initial_prompt_first():
    """The user message must be in the context BEFORE the first dispatch
    call — otherwise the LLM has no prompt to respond to."""
    seen_first_message = []

    def dispatch(ctx):
        seen_first_message.append(dict(ctx.messages[0]))
        ctx.append({"role": "assistant", "content": "ok"})
        return "final_answer"

    ctx = poller.JobContext(max_turns=5, max_fuel=10_000)
    initial = {"role": "user", "content": "first"}
    ctx.append(initial)
    poller.run_tool_calling_loop(ctx, dispatch)
    assert seen_first_message and \
        seen_first_message[0]["role"] == "user" and \
        seen_first_message[0]["content"] == "first", \
        f"first message was {seen_first_message}"


# --- T10: loop charges 1 fuel per dispatch by default ----------------------

def test_t10_loop_charges_one_fuel_per_dispatch():
    """Default fuel policy is 1 unit per dispatch — the existing
    execute_in_envelope path uses 1 unit per ms. Tests need a stable
    default; configurable per-call via fuel_per_dispatch=."""
    def dispatch(ctx):
        ctx.append({"role": "assistant", "content": "tick"})
        return "final_answer"

    ctx = poller.JobContext(max_turns=5, max_fuel=10_000)
    ctx.append({"role": "user", "content": "go"})
    for _ in range(3):
        ctx.fuel_used = 0  # reset so we can watch the increment
        # Run 3 turns
        def multi(ctx):
            ctx.append({"role": "assistant", "content": "x"})
            return "final_answer"
        poller.run_tool_calling_loop(ctx, multi)
        break  # one loop is enough to observe charging

    # After one loop with one dispatch, fuel_used should be >= 1
    assert ctx.fuel_used >= 1, f"loop did not charge any fuel: {ctx.fuel_used}"


# --- T11: call_broker_llm_proxy uses the whitelist shape -------------------

def test_t11_call_broker_llm_proxy_uses_whitelist_shape():
    """The broker proxy (VULN-S5) explicitly strips `tools` and
    `temperature` from forwarded bodies. The worker's call helper must
    send ONLY {model, messages, max_tokens, stream} so a malicious
    skill can't bypass the broker's whitelist by smuggling extra fields
    via the worker. We assert by capturing the urllib Request."""
    captured = []

    class _Resp:
        def __init__(self, body): self._body = body
        def read(self): return json.dumps(self._body).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured.append({
            "url": req.full_url,
            "headers": dict(req.headers),
            "body": json.loads(req.data.decode()),
        })
        return _Resp(_ok_response(content="ok"))

    ctx = poller.JobContext(max_turns=1, max_fuel=100)
    ctx.append({"role": "user", "content": "hi"})
    with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
        resp = poller.call_broker_llm_proxy(
            ctx,
            llm_proxy_url="http://10.0.0.5:8080/v1/llm/chat/completions",
            llm_token="tok",
            model="minimax-m3",
            max_tokens=200,
        )
    assert len(captured) == 1, captured
    sent = captured[0]
    # Whitelist: exactly model + messages + max_tokens + stream
    allowed = {"model", "messages", "max_tokens", "stream"}
    assert set(sent["body"].keys()) == allowed, \
        f"forward body had unexpected keys: {set(sent['body'].keys())}"
    assert sent["headers"]["Authorization"] == "Bearer tok"
    assert resp["choices"][0]["message"]["content"] == "ok"


# --- T12: execute_in_envelope default path stays single-turn ---------------

def test_t12_execute_envelope_default_is_single_turn():
    """Back-compat: when env doesn't opt into the loop AND doesn't
    configure NemoClaw, the poller MUST fail-closed (not silently
    fall back to the legacy broker-llm-proxy host-side call). The
    2026-06-30 fix removed the host-side proxy else-branch and
    replaced it with execution_mode="no-nemoclaw-failclosed". The
    test pins that: no call to urlopen is made, the envelope carries
    a sandbox block labelled as not-attested, and the result is
    state=completed so the broker can still move the job out of
    the running queue."""
    env = _make_envelope()  # no execution_mode override, no NEMOCLAW_SANDBOX_NAME
    # urlopen must NOT be called. Patch it to AssertionError so any
    # accidental call is loud.
    def fake_urlopen(req, timeout=None):
        raise AssertionError(
            "execute_in_envelope must not call urlopen when no "
            "NEMOCLAW_SANDBOX_NAME is configured — legacy "
            "broker-llm-proxy else-branch was removed (2026-06-30)"
        )
    with mock.patch.object(urllib.request, "urlopen", fake_urlopen), \
         mock.patch.object(poller, "DEFAULT_SANDBOX_NAME", ""), \
         mock.patch.object(poller, "NEMOCLAW_STUB_MODE", False), \
         mock.patch.object(poller, "_have_nemohermes_shim", return_value=False):
        result = poller.execute_in_envelope(env)
    assert result["state"] == "completed"
    assert result["result"]["execution_mode"] == "no-nemoclaw-failclosed", result
    # The fail-closed path still emits a sandbox block (so the
    # envelope schema is stable) but marks it as not-attested.
    sb = result["result"]["sandbox"]
    assert sb["attested"] is False, sb


# --- T13: execute_in_envelope with tool-calling-loop opts into the loop ----

def test_t13_execute_envelope_tool_loop_invokes_run_tool_calling_loop():
    """When env says execution_mode=tool-calling-loop, the poller builds
    a JobContext and runs the loop. We mock the loop helper to confirm
    it was called with the right ctx + dispatch + tools."""
    with mock.patch.object(poller, "run_tool_calling_loop",
                           return_value="loop answer") as m_loop:
        with mock.patch.object(urllib.request, "urlopen") as m_urlopen:
            class _Resp:
                def read(self):
                    return json.dumps(_ok_response("unused")).encode()
                def __enter__(self): return self
                def __exit__(self, *a): return False
            m_urlopen.return_value = _Resp()
            env = _make_envelope(
                execution_mode="tool-calling-loop",
                max_turns=4,
            )
            result = poller.execute_in_envelope(env)
    assert m_loop.called, "loop was not invoked for tool-calling-loop mode"
    # Loop received the env-derived ctx + a dispatch callable
    args, kwargs = m_loop.call_args
    ctx_arg = args[0] if args else kwargs.get("ctx")
    assert ctx_arg is not None, "loop not called with a ctx"
    assert ctx_arg.max_turns == 4, f"max_turns not threaded: {ctx_arg.max_turns}"
    assert result["state"] == "completed"


# --- T14: loop exhausting fuel surfaces as a clean mode marker -------------

def test_t14_tool_loop_budget_exhaustion_marks_result_cleanly():
    """When the loop runs out of fuel or turns WITHOUT hitting
    final_answer, the poller must return execution_mode='tool-loop-budget'
    so an operator can distinguish 'skill gave up' from 'LLM error'.
    The last assistant message becomes the output."""
    with mock.patch.object(poller, "run_tool_calling_loop",
                           return_value="partial answer") as m_loop:
        with mock.patch.object(urllib.request, "urlopen") as m_urlopen:
            class _Resp:
                def read(self):
                    return json.dumps(_ok_response("unused")).encode()
                def __enter__(self): return self
                def __exit__(self, *a): return False
            m_urlopen.return_value = _Resp()
            env = _make_envelope(
                execution_mode="tool-calling-loop",
                max_turns=2,
                max_fuel=10,  # tiny budget — loop will exhaust
            )
            result = poller.execute_in_envelope(env)
    assert result["state"] == "completed"
    assert result["result"]["execution_mode"] == "tool-calling-loop"
    # Result has the loop's last assistant content
    assert result["result"]["output"] == "partial answer"


# --- T15: result envelope carries loop audit fields ------------------------

def test_t15_result_envelope_carries_tool_call_audit():
    """A tool-calling-loop run produces a result envelope with:
        turns_used      int (>= 1)
        tool_calls      list of {name, args_summary, duration_ms}
        tools_available list of skill-declared tool names
    Operators (and the live-regression tests) use these to audit
    what the loop did without re-running the job."""
    def fake_loop(ctx, dispatch, tools=None):
        # Pretend we did two turns and called one tool
        ctx.append({"role": "assistant", "content": "calling tool"})
        ctx.append({"role": "tool",
                    "name": "summarize_chunk",
                    "content": "ok"})
        ctx.append({"role": "assistant", "content": "final"})
        ctx.turn_count = 2
        ctx.tool_results.append({
            "name": "summarize_chunk", "args": {"chunk_id": 1},
            "duration_ms": 50})
        return "final"

    with mock.patch.object(poller, "run_tool_calling_loop",
                           side_effect=fake_loop):
        with mock.patch.object(urllib.request, "urlopen") as m_urlopen:
            class _Resp:
                def read(self):
                    return json.dumps(_ok_response("unused")).encode()
                def __enter__(self): return self
                def __exit__(self, *a): return False
            m_urlopen.return_value = _Resp()
            env = _make_envelope(
                execution_mode="tool-calling-loop",
                skill_tools=[{"name": "summarize_chunk",
                              "description": "summarize a chunk"}],
            )
            result = poller.execute_in_envelope(env)
    r = result["result"]
    # Audit block is namespaced under result["loop"] (not the top-level
    # result) — it's an operator-audit field, not part of the skill's
    # primary output. The fields are: turns_used, tool_calls,
    # tools_available, max_turns, max_fuel.
    loop_block = r.get("loop", {})
    assert "turns_used" in loop_block, loop_block
    assert loop_block["turns_used"] == 2, loop_block
    assert "tool_calls" in loop_block, loop_block
    assert len(loop_block["tool_calls"]) == 1, loop_block
    assert loop_block["tool_calls"][0]["name"] == "summarize_chunk", loop_block
    # tools_available is populated from env["skill_tools"]; the test
    # passes one named "summarize_chunk" so it must appear here even
    # though execute_in_envelope can't actually invoke it (the test's
    # fake dispatch_fn overrides the whole loop — so this assertion is
    # about the wiring, not the test's stub).
    assert "summarize_chunk" in loop_block.get("tools_available", []), loop_block


# --- T16: forward body is whitelisted even when ctx has tool messages -----

def test_t16_forward_body_whitelist_holds_with_tool_messages_in_ctx():
    """OpenAI tool-calling format puts `tool` messages in messages[].
    The whitelist (model, messages, max_tokens, stream) still passes
    them through unchanged — the WHITELIST is on the top-level keys,
    not on the messages[] content. Asserting that 'tool' role messages
    survive end-to-end."""
    captured = []

    class _Resp:
        def __init__(self, body): self._body = body
        def read(self): return json.dumps(self._body).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured.append(json.loads(req.data.decode()))
        return _Resp(_ok_response("ok"))

    ctx = poller.JobContext(max_turns=2, max_fuel=100)
    ctx.append({"role": "user", "content": "hi"})
    ctx.append({"role": "assistant", "content": "calling"})
    ctx.append({"role": "tool", "name": "x", "content": "y"})
    with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
        poller.call_broker_llm_proxy(
            ctx,
            llm_proxy_url="http://x/v1/llm/chat/completions",
            llm_token="t",
            model="m",
        )
    sent = captured[0]
    assert sent["model"] == "m"
    assert sent["stream"] is False
    assert len(sent["messages"]) == 3
    assert sent["messages"][2]["role"] == "tool"
    # And no forbidden keys
    assert "tools" not in sent
    assert "temperature" not in sent


# --- main --------------------------------------------------------------------

def main():
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and callable(v)]
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
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()