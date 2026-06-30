# Live regression tests

These tests require live AWS credentials and/or network access. They are
**skipped by default** — `pytest` will only run them if you opt in with a
marker flag.

## Why skipped?

Each test below takes 2-10 minutes, may incur AWS charges, and may leave
side effects (CloudFormation change sets, EC2 launches, EFS writes). They're
appropriate for:

- **Pre-demo verification** (run the night before a hackathon demo)
- **Pre-release verification** (run before tagging a release)
- **Post-incident verification** (run after a deploy to confirm the secret
  handling still works at runtime)

They are NOT appropriate for:

- Running on every commit
- Running in CI on PRs (no AWS credentials there)
- Running unattended overnight

## How to run

### Run all live tests

```bash
make regression-live
# equivalent to:
pytest -m live_cfn -v
```

### Run a specific live test

```bash
pytest tests/verify-cfn-noecho-live.py -v -m live_cfn
```

### Environment overrides

Each test reads sensible defaults but accepts env var overrides:

| Test | Env var | Default | Purpose |
|------|---------|---------|---------|
| `verify-cfn-noecho-live.py` | `CFN_NOECHO_STACK` | `verdantforged-broker-control-001` | Stack to update |
| `verify-cfn-noecho-live.py` | `CFN_NOECHO_REGION` | `eu-west-1` | AWS region |
| `verify-cfn-noecho-live.py` | `CFN_NOECHO_TEMPLATE` | `cloudformation-control-plane.yaml` | Template file |
| `verify-cfn-noecho-live.py` | `CFN_NOECHO_KEY` | auto-generated 54-char | Test Stripe value |
| `verify-cfn-noecho-live.py` | `CFN_NOECHO_KEEP_STACK` | `false` | If `true`, leaves key set after run |

## Test catalog

### `verify-cfn-noecho-live.py`

**Marker:** `live_cfn`  
**Time:** ~2-5 min per run (CFN change-set create + execute + stack-events read)  
**Side effects:** Updates the configured CFN stack with a temporary
`StripeSecretKey` value; restores to empty on completion unless
`CFN_NOECHO_KEEP_STACK=1`.

**What it proves:**

1. `describe-stacks` returns `ParameterValue: ****` for `StripeSecretKey`
   (NOT the plaintext key).
2. The plaintext key value appears zero times in the full describe-stacks
   JSON response.
3. The plaintext key value appears zero times in the entire
   `describe-stack-events` JSON history.

**Why this matters:** the static B5 check in
`tests/verify-stripe-integration.py` only proves the YAML *looks* correct.
This test proves the deployed stack *behaves* correctly. A subtle YAML
regression (e.g. someone moves `StripeSecretKey` from `Parameters:` to
`Resources:`, or strips `NoEcho: true`) would pass the static check
but fail this live check.

See `docs/security/cfn-noecho-regression.md` for the full procedure,
expected output, and history of the bug it caught on 2026-06-28.

### `verify-cfn-noecho-live.py::test_live_cfn_template_matches_live_parameter_shape`

**Marker:** `live_cfn` (same file as the other assertion, same marker)  
**Time:** ~5-10 sec per run (single `aws cloudformation get-template` call)  
**Side effects:** None. Read-only.

**What it proves:** the **local** `cloudformation-control-plane.yaml`'s
`Parameters:` block is structurally identical (same set, same order, same
`Type`, same `NoEcho`, same `Default`) to what is currently **deployed**
on the live stack. Also asserts `StripeSecretKey` is declared under
`Parameters:` with `NoEcho: true` — the exact regression that sibling task
t_86ce871c flagged as a follow-up (the parameter had drifted into
`Resources:`, which would have silently re-broken the live stack at the
next `make deploy`).

**Why this matters:** the NoEcho runtime test (`test_live_cfn_noecho_redacts_stripe_secret_key`)
proves the live stack *currently* behaves correctly. This new test proves
the local checkout *will keep* it behaving correctly across the next
`make deploy` — by failing CI / the dev locally if anyone reintroduces
the drift.



| Test | Credentials | Region |
|------|-------------|--------|
| `verify-cfn-noecho-live.py` | `cloudformation:UpdateStack`, `cloudformation:DescribeStacks`, `cloudformation:DescribeStackEvents`, `cloudformation:CreateChangeSet`, `cloudformation:ExecuteChangeSet` | any (default `eu-west-1`) |

Standard AWS credential resolution applies: `~/.aws/credentials`,
`AWS_PROFILE`, instance role, etc.

## Adding a new live test

1. Add `pytestmark = pytest.mark.live_cfn` (or a new marker) at the top of
   the file.
2. Use `pytest.skip(...)` early if required env vars / resources aren't
   available — never fail the entire suite because AWS creds are missing
   on a dev machine.
3. Always clean up side effects in a `try/finally` or `addfinalizer`.
4. Document it in the table above.
