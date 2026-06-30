# VerdantForged — Bug Log

Known issues and defects in the broker, the worker, and supporting systems. Each entry: symptoms → repro → root cause → proposed fix → status.

Last updated: 2026-06-30

---

## BUG-004 — `worker/poller.py` `execute_in_envelope` rewrites the LLM proxy URL to the EC2 IMDS endpoint

**Status:** OPEN — surfaced 2026-06-30, pre-existing

**Affected components:**
- `tee-broker-deploy/worker/poller.py` — `execute_in_envelope()` (around the `llm_proxy_url` path) and any fallback/recovery code
- `tee-broker-deploy/tests/verify-medium-severity-fixes.py:690` — H4 assertion

**Severity:** High — the worker is *not* using the broker's LLM proxy when the envelope carries a URL, it is being redirected to the EC2 instance metadata service (`169.254.169.254`). On EC2 that endpoint returns IMDS JSON, which `urllib.request` happily parses as a non-OpenAI-shaped response; on any non-EC2 worker it returns the IMDSv1 forbidden message. Either way, real LLM calls via this code path cannot succeed.

### Symptoms

`tests/verify-medium-severity-fixes.py` H4 fails with:

```
[FAIL] H4. worker uses the envelope's llm_proxy_url verbatim (no fallback rewrite)
  (got captured_url='http://169.254.169.254/latest/meta-data/instance-id')
```

The test sets a worker-chosen URL (`http://worker-chosen.example.com:9999/v1/llm/chat/completions`) in the envelope and asserts the poller hits exactly that URL. The captured URL comes back as the EC2 IMDS endpoint instead — meaning `execute_in_envelope()` overwrites the supplied URL with a hardcoded fallback.

### Data / evidence

- Test output: `tests/verify-medium-severity-fixes.py` H4 line 690.
- The expected URL: `http://worker-chosen.example.com:9999/v1/llm/chat/completions`
- The actual URL: `http://169.254.169.254/latest/meta-data/instance-id`
- Related: H1, H2, H3 (in the same test file) all PASS — so the *removal* of a hardcoded `172.31.x.x` IP is complete, but a *new* hardcoded `169.254.169.254` IMDS path is in place, presumably as a "last-resort" when both `llm_token` and `llm_proxy_url` are missing. The bug is that the last-resort is also taken when the URL *is* provided.

### Root cause (suspected)

`execute_in_envelope()` (or a wrapper it calls) has a fallback branch that, on any `urllib.error.URLError` / connection failure, swaps the user-supplied `llm_proxy_url` for an IMDS path — likely intended as a "ping something to see if the network is up" diagnostic that should *not* be used as the LLM endpoint. The H4 test triggers this by making the example.com host fail to resolve.

### Repro (from the test)

```python
# The test's runnable shape — execute_in_envelope called with:
envelope = {
    "llm_token": "llm_test_token_fake",
    "llm_proxy_url": "http://worker-chosen.example.com:9999/v1/llm/chat/completions",
    # ... other fields ...
}
# Expected captured URL:  http://worker-chosen.example.com:9999/v1/llm/chat/completions
# Actual captured URL:    http://169.254.169.254/latest/meta-data/instance-id
```

### Proposed fix

In `worker/poller.py` `execute_in_envelope()` (or its LLM-call helper), remove the IMDS fallback. The contract is: the envelope's `llm_proxy_url` is authoritative. If it's missing or invalid, fail with `execution_mode='no-path'` (matching the H2 test expectation) — do not silently swap in a non-LLM endpoint. Audit any other references to `169.254.169.254` in `worker/` while in there.

### Workaround

For jobs where the worker is expected to call the broker's LLM proxy, ensure the envelope is rendered with `llm_proxy_url` set to the real broker URL before the poller picks it up. (This is what happens in production; the failure only shows up in the unit test where the URL is intentionally bad.)

---

## BUG-005 — `verify-medium-severity-fixes.py` `test_discover_single_attestation_read` test mock is missing `fetchone()`

**Status:** OPEN — test bug, surfaced 2026-06-30, pre-existing

**Affected components:**
- `tee-broker-deploy/tests/verify-medium-severity-fixes.py:704-711` — `_EmptySkillsCursor` mock class
- `tee-broker-deploy/broker-daemon/daemon.py:3685` — `discover()` calls `fetchone()` on a `sqlite_master` query result

**Severity:** Low — does not affect production code, only the test suite. The daemon's `discover()` is correct; the test fixture is incomplete.

### Symptoms

Running `python3 tests/verify-medium-severity-fixes.py` ends with:

```
=== I. discover() reads attestation once (CQ-1) ===
Traceback (most recent call last):
  File ".../verify-medium-severity-fixes.py", line 830, in main
    test_discover_single_attestation_read()
  File ".../verify-medium-severity-fixes.py", line 754, in test_discover_single_attestation_read
    resp = asyncio.run(run_discover())
  ...
  File ".../daemon.py", line 3685, in discover
    ).fetchone()
AttributeError: '_EmptySkillsCursor' object has no attribute 'fetchone'.
Did you mean: 'fetchall'?
```

I0 (the source-level assertion that `attestation_path.read_text()` is called exactly once) **passes** — that is the actual point of the CQ-1 fix and it is verified. The test then crashes before I1 can run, so the I1 assertion (that the attestation block in the JSON response is fully populated) is never executed.

### Root cause

The test mock `_EmptySkillsCursor` defines only `fetchall()`. `discover()` first runs `SELECT name FROM sqlite_master WHERE type='table' AND name='skills'` and calls `.fetchone()` on the result (daemon.py:3685). The mock returns the same cursor class for both queries, so `.fetchone()` is missing.

The mock was written when the only `SELECT` it had to fake was the second one (`SELECT name FROM skills s1 ...`), which uses `.fetchall()`. The existence-check SELECT was added in a later edit without updating the mock.

### Proposed fix

Add `fetchone` to `_EmptySkillsCursor` (returning `None`, since the test scenario is "skills table doesn't exist yet"):

```python
class _EmptySkillsCursor:
    def fetchall(self_inner): return []
    def fetchone(self_inner): return None
```

After that, the I0 assertion already passes; I1 will execute and the test will return a clean pass/fail per-check.

### Workaround

None needed in production. To run I0 in isolation while BUG-005 is open, comment out the `asyncio.run(run_discover())` call at line 754.

---

## BUG-003 — `push_skills.sh` `--api-key` parsing hardcodes the bearer token to literal `***`

**Status:** FIXED 2026-06-29 (commit pending — same patch as the smoke-test fix)

**Affected components:**
- `tee-broker-deploy/scripts/push_skills.sh:21`

**Severity:** Blocker — `push_skills.sh` was silently failing every authenticated request to the library when run via the normal `--api-key $VAR` CLI form.

### Symptoms

When a user runs:

```bash
./scripts/push_skills.sh --source-dir worker/skills \
    --library-url http://127.0.0.1:8091 --api-key "$SKILL_LIBRARY_API_KEY"
```

…the library responds with `{"error":"invalid library API key","code":"library_auth_invalid"}` to every request. The script doesn't notice because the auth 401 is treated as a non-fatal error (same code path as 409-already-registered) — it still prints `Done. pushed=3 failed=0`.

This bug was caught only by the **live smoke test on the control plane**, because local tests passed the literal `--api-key devkey` *and* the script's `***` happens to be a non-empty string. The combination fails as soon as the api key actually has to match what's on the server side.

### Root cause

The T8 subagent's first write_file corrupted line 21 from the intended:

```bash
--api-key) API_KEY="$2"; shift 2;;
```

…to:

```bash
--api-key) API_KEY="***"; shift 2;;
```

The literal `***` was a redaction artifact from the tool-call payload (the agent saw the original `$2` survive redaction in the previous T4 call but not in this one). Three Authorization headers on lines 103, 137, 149 correctly say `Bearer $API_KEY` — they expand to `Bearer ***`, which never matches any real key.

### Fix

Re-wrote line 21 to use the form that survived redaction during the patch tool's diff:

```bash
--api-key) API_KEY="${2}"; shift 2;;
```

Verified by base64 dump that the on-disk bytes are `***"${2}"***`. Local pytest + live smoke test both pass now.

### Verification (post-fix)

```bash
# Local shadow
bash scripts/push_skills.sh --source-dir worker/skills \
    --library-url http://127.0.0.1:18091 --api-key devkey
# → 3 skills registered, GET /v1/library/skills lists 3 names

# Live smoke test on control plane (no broker impact)
ps -eo pid,etime,cmd | grep daemon.py | grep -v grep
# → PID 16258, uptime 02:53:32 — unchanged across test
```

### Lessons

- Hermes tool-call redaction munges **tokens** (anything that looks like a secret pattern, e.g. "Bearer X") even in source code where they're clearly not secrets (parameter name capture).
- Pre-commit byte-level verification (base64 dump + `wc -c` after each patch) catches what `grep -c "Bearer"` cannot, because grep + redaction hide the truth.
- The TDD loop must include an end-to-end test that exercises the CLI arg form (`--api-key $VAR`), not just an inline literal (`--api-key devkey`), because the two paths are different code paths through bash's arg parsing.

This is **BUG-003** to distinguish from BUG-002 (same file, different bug — the path-prefix-strip on line 117, also caught in this session).

---

## BUG-002 — `push_skills.sh` uploads files under nested paths instead of skill-relative

**Status:** FIXED 2026-06-29 (commit `5b430a0`)

**Affected components:**
- `tee-broker-deploy/scripts/push_skills.sh:117` — `rel="${file_path#$skill_dir/}"`

**Severity:** Blocker for the script's intended happy path (broker had no `skill_library.db` so this never silently caused user-visible wrong data — but every upload would have landed at a wrong key like `worker/skills/summarize/SKILL.md` instead of `SKILL.md`).

### Symptoms

When pushing the three existing skills (`worker/skills/{code-review, photo-glow-up, summarize}`) to the library, files uploaded successfully (201), but a follow-up `GET /v1/library/skills/summarize@0.1.0/files/SKILL.md` returned **404 `file_not_found`**. The actual file was stored under the nested key `worker/skills/summarize/SKILL.md`.

### Root cause

The for-loop at line ~85 iterates `for skill_dir in "$SOURCE"/*/`. Bash's path-expansion preserves the trailing slash, so `$skill_dir` for the `summarize` skill is `worker/skills/summarize/` (with the slash). The line:

```bash
rel="${file_path#$skill_dir/}"
```

…contains `$skill_dir/` which bash expands as `worker/skills/summarize//` (two slashes because `$skill_dir` ends with one and the literal `/` adds another). Meanwhile `file_path` for `SKILL.md` is exactly `worker/skills/summarize/SKILL.md` (one slash). The prefix doesn't match, so `rel` ends up being the full path instead of just `SKILL.md`.

### Fix

Normalise the trailing slash before stripping:

```bash
rel="${file_path#${skill_dir%/}/}"
```

`${skill_dir%/}` strips the trailing slash; the trailing `/` then matches the real separator. Now `rel` is just `SKILL.md` as intended.

### Verification

```bash
./scripts/push_skills.sh --source-dir worker/skills --library-url http://127.0.0.1:18091 --api-key devkey
# GET /v1/library/skills/code-review@0.1.0/files/SKILL.md → 200 + 890 bytes
```

This was caught by the T8 subagent's end-to-end smoke test. Plan-side, the same bug exists in the plan's published script (`/home/autumn/hermes/competition-wt-nemoclaw/tee-broker-deploy/.hermes/plans/2026-06-29_161941-skill-library-service.md` line 1245 — the `rel="${file_path#$skill_dir/}"` snippet is verbatim from the plan). The on-disk `scripts/push_skills.sh` is patched; the plan is the source of truth for future re-implementations and should also be fixed — flagged for the next planning pass.

---

## BUG-001 — Stripe capture fails on jobs < $0.50 due to amount padding

**Status:** OPEN — pre-existing, surfaced this session

**Affected components:**
- `tee-broker-deploy/broker-daemon/daemon.py` — payment capture path (`capture_payment()` / similar)
- All jobs with `stripe_pi_amount_cents < 50` (USD)

**Severity:** Medium — does not block demo (≥$0.50 jobs charge successfully), but causes two of every three test jobs to land as `stripe_status=error`.

### Symptoms

Daemons logs show two alternating errors at capture time on small PIs:

1. `error_code=payment_intent_unexpected_state error_message='The remaining amount on this PaymentIntent could not be captured because the remainder of the authorized amount has been released.'`
2. `error_code=payment_intent_unexpected_state error_message='This PaymentIntent could not be captured because it has already been captured.'`

Job still transitions to `state=completed` (LLM output was returned successfully), but `jobs.stripe_status='error'` and `stripe_capture_amount` recorded as the original authorized amount (e.g. 20 cents), not the padded $0.50.

### Data from live DB (32 jobs, fetched 2026-06-29 15:55 UTC)

```
stripe_status distribution:
  succeeded: 17
  error:     14
  (unset):   1
```

All 14 errors are jobs authorized for less than 50 cents.

### Root cause

Stripe USD has a 50-cent capture minimum. The daemon tries to honour this by:
1. Capturing the full authorized amount first (e.g. 20 cents)
2. THEN making a second `capture` request for the *difference* between the captured amount and the configured currency minimum (50 cents − 20 cents = 30 cents)

This is wrong. Two failure modes:

a) **Race between the two requests.** Stripe processes the first 20-cent capture immediately, releases the authorization remainder (since `capture_method=automatic` with full-amount capture doesn't leave anything to recapture), then the second 30-cent request fails with `payment_intent_unexpected_state`. → Error message #1.

b) **Retry loop on already-captured PI.** If the daemon retries (e.g. polling loop), the second attempt hits an already-captured PI → Error message #2.

Both errors are confirmed in `/mnt/broker/logs/broker-daemon.log` lines from 2026-06-29 13:18:23–13:18:55.

### Repro

```python
import stripe, os, requests, json
stripe.api_key = os.environ['STRIPE_SECRET_KEY']
pi = stripe.PaymentIntent.create(
    amount=20, currency='usd',
    payment_method_types=['card'],
    payment_method='pm_card_visa',
    confirm=True
)
print('PI:', pi.id, pi.status)

r = requests.post('https://verdant.codepilots.co.uk/v1/jobs', json={
    'client_req_id': f'bug001-{int(time.time())}',
    'encrypted_skill': 'summarize',
    'encrypted_data': 'Hello world.',
    'requester_sig': '0x0000',
    'result_pubkey': '0x0000',
    'stripe_pi_id': pi.id
})
print(r.status_code, r.json())
```

Watch daemon log: capture attempt → error #1 (or #2 on retry). Job state moves to `completed` with `stripe_status='error'`.

### Proposed fix

Two options, listed by correctness:

**Option A (correct but smallest change): Drop the padding entirely.** Let the daemon capture the authorized amount as-is, even if it falls below the 50-cent Stripe minimum. Some captures will fail Stripe-side, but the broker's bookkeeping remains accurate (`stripe_capture_amount` reflects reality) and jobs >$0.50 are unaffected.

```python
# daemon.py around the capture call:
amount_to_capture = job_authorised_amount  # not the padded amount
stripe.PaymentIntent.capture(pi_id, amount_to_capture=amount_to_capture)
```

**Option B (most correct but bigger change): Authorize all PIs for the currency minimum upfront.** In the submit endpoint (or the test harness), create the PI for `max(client_amount, 50)` cents. The broker only ever captures what it captures; no padding request is ever made.

```python
# At PI creation:
pi_amount = max(client_requested_amount_cents, 50)
pi = stripe.PaymentIntent.create(amount=pi_amount, currency='usd', ...)
```

Recommend **Option B** for production: clean ledger, no padding logic in the broker, and jobs <$0.50 get charged the platform minimum (which is reasonable for a marketplace). Option A is a one-line hotfix.

### Workaround for testing today

Just create PIs for ≥$0.50:

```python
pi = stripe.PaymentIntent.create(amount=50, currency='usd', payment_method='pm_card_visa', confirm=True)
```

These job submissions will succeed end-to-end including Stripe capture.

### Related

- **Observed:** 17 of 32 jobs in `/mnt/broker/logs/broker.db` show `stripe_status='succeeded'` — all of these are jobs ≥$0.50.
- **Not blocking demo:** The LLM pipeline (`worker-agent.py` → `inference.local` → broker LLM proxy → Ollama Cloud) is fully working regardless of payment status. Payment capture is a side-effect, not a precondition for result delivery.

---

## Bug log conventions (for future entries)

When adding a new bug, copy this header format:

```
## BUG-NNN — <short title>

**Status:** OPEN / FIXED / WONTFIX
**Affected components:** <files>
**Severity:** Blocker / High / Medium / Low

### Symptoms
<what the user sees, with literal error strings>

### Data / evidence
<links to logs, DB queries, line counts>

### Root cause
<minimal explanation, code references with line numbers>

### Repro
<smallest script that triggers the bug>

### Proposed fix
<code snippet + reasoning>

### Workaround
<what works today, while the fix is pending>
```

Last line of each entry: date and who logged it. Keep entries short and clinical — no narrative, no apologies.
