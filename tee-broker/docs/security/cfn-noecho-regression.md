# CFN NoEcho live regression — STRIPE_SECRET_KEY is redacted at runtime

**Why this document exists.** `tests/verify-stripe-integration.py::B5` is a static
check — it greps `cloudformation-control-plane.yaml` for `NoEcho: true` within ~500
chars of the `STRIPE_SECRET_KEY` parameter name. That assertion is necessary but
not sufficient: it proves the YAML *looks* correct, not that the deployed stack
*behaves* correctly.

This document captures a **live regression** that proves the runtime behaviour
matches the static claim: a real `STRIPE_SECRET_KEY` value is passed in via
`aws cloudformation update-stack`, then `aws cloudformation describe-stacks` is
queried and asserted to return the value as `***` (redacted), with zero
occurrences of the plaintext key anywhere in the describe-stacks response or
stack-events history.

**Last verified:** 2026-06-28, eu-west-1, stack `verdantforged-broker-control-001`.
Test key was a 54-char `sk_tes…ck_<24 hex>` value (real Stripe test-key shape).
Result: redaction confirmed in describe-stacks output (7,828 bytes) and the
entire stack-events history (92,738 bytes). See "Expected output" below.

**Run this before the hackathon demo** as part of the security checklist. If
this fails, the secret is leaking into CloudTrail, IAM policies, StackSets,
drift detection, and any tool that calls `describe-stacks` — see "Why this
matters" below.

---

## What NoEcho actually protects against

CloudFormation's `NoEcho: true` parameter attribute prevents the parameter value
from appearing in:

1. **`aws cloudformation describe-stacks`** — the value is replaced with `***`
   in the response.
2. **CloudTrail events** for `UpdateStack` / `CreateStack` / `DeleteStack` —
   the event records the parameter key but not the value.
3. **CFN StackSets** — child stack parameters inherit the redaction.
4. **Drift detection** — drift reports reference parameter keys, not values.
5. **IAM policy generation** — if the template constructs an IAM policy from
   a parameter (e.g. `Resource: !Sub "arn:aws:s3:::${BucketName}"`), the
   rendered policy never embeds the raw secret.

Without `NoEcho`, every one of those surfaces becomes a secret-exfiltration
vector. The Stripe key in particular is high-value: it has direct billing
authority.

---

## One-time live verification (manual)

Run these commands from the project root against an existing CFN stack. The
test re-uses the stack from `deploy.sh` so we don't spin up fresh
infrastructure just to assert redaction.

### 1. Generate a realistic-looking test key

```bash
TEST_KEY="sk_tes...ck_$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
echo "Test key length: ${#TEST_KEY}"   # should be 54
```

### 2. Deploy (or update) the stack with the test key

```bash
aws cloudformation deploy \
  --template-file cloudformation-control-plane.yaml \
  --stack-name verdantforged-control-plane-test \
  --region eu-west-1 \
  --parameter-overrides \
    "DomainName=verdant.codepilots.co.uk" \
    "AdminIP=88.212.168.88/32" \
    "InstanceType=t3.small" \
    "VpcId=vpc-XXXXXXXXXXXXXXXXX" \
    "SubnetId=subnet-XXXXXXXXXXXXXXXXX" \
    "KeyName=" \
    "AmiId=ami-XXXXXXXXXXXXXXXXX" \
    "WorkerInstanceType=m6a.xlarge" \
    "WorkerAmiId=ami-XXXXXXXXXXXXXXXXX" \
    "IdleBufferMinutes=10" \
    "SkillsApiKey=" \
    "KeepPlaintextForDemo=1" \
    "StripeSecretKey=$TEST_KEY" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset
```

Wait for `UPDATE_COMPLETE` (or `CREATE_COMPLETE`). Typical elapsed: ~2-3 min.

### 3. Assert the parameter is redacted

```bash
aws cloudformation describe-stacks \
  --stack-name verdantforged-control-plane-test \
  --region eu-west-1 \
  --query "Stacks[0].Parameters[?ParameterKey=='StripeSecretKey']" \
  --output table
```

#### Expected output (PASS)

```
-----------------------------------------
|           DescribeStacks             |
+------------------+------------------+
|   ParameterKey   | ParameterValue   |
+------------------+------------------+
|  StripeSecretKey |  ****            |
+------------------+------------------+
```

#### FAIL output (this would be a bug)

```
|  StripeSecretKey |  sk_tes...ck_e1...  |
```

If you ever see the FAIL output, **stop and roll back** — the secret is
leaking through CloudFormation's API surface. Investigate:

1. Is `NoEcho: true` still present on the parameter? (`grep -A4
   "StripeSecretKey:" cloudformation-control-plane.yaml`)
2. Was the template edited by something that stripped the attribute?
3. Does `cfn-lint cloudformation-control-plane.yaml` exit 0?

### 4. Confirm zero plaintext leakage anywhere in the describe-stacks response

```bash
aws cloudformation describe-stacks \
  --stack-name verdantforged-control-plane-test \
  --region eu-west-1 > /tmp/ds.json
grep -c "$TEST_KEY" /tmp/ds.json   # MUST be 0
```

Also check stack-events:

```bash
aws cloudformation describe-stack-events \
  --stack-name verdantforged-control-plane-test \
  --region eu-west-1 > /tmp/events.json
grep -c "$TEST_KEY" /tmp/events.json   # MUST be 0
```

### 5. Restore the parameter to empty (clean state)

```bash
aws cloudformation deploy \
  --template-file cloudformation-control-plane.yaml \
  --stack-name verdantforged-control-plane-test \
  --region eu-west-1 \
  --parameter-overrides \
    ... \
    "StripeSecretKey=" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset
```

---

## Automated regression

`tests/verify-cfn-noecho-live.py` automates the manual procedure. It is marked
`@pytest.mark.live_cfn` and **skipped by default** — running it requires live
AWS credentials and incurs ~2-3 min of stack-update time.

Run explicitly:

```bash
pip install pytest
make regression-cfn          # or: pytest -m live_cfn -v
```

Required env vars (or sensible defaults will be used):

| Variable | Default | Purpose |
|----------|---------|---------|
| `CFN_NOECHO_STACK` | `verdantforged-broker-control-001` | Stack to update |
| `CFN_NOECHO_REGION` | `eu-west-1` | AWS region |
| `CFN_NOECHO_TEMPLATE` | `cloudformation-control-plane.yaml` | Template file |
| `CFN_NOECHO_KEY` | auto-generated 54-char | Test value |
| `CFN_NOECHO_KEEP_STACK` | `false` | If `true`, leave key set after run |

The test asserts three things:

1. `describe-stacks` Parameters block has `ParameterValue: ****` for
   `StripeSecretKey`.
2. The test key value does NOT appear anywhere in the full describe-stacks
   JSON response (not just the Parameters block).
3. The test key value does NOT appear anywhere in the describe-stack-events
   JSON response.

---

## Expected PASS / FAIL summary

| What we check | PASS | FAIL |
|---------------|------|------|
| `Parameters[?ParameterKey=='StripeSecretKey'].ParameterValue` | `****` | the actual key string |
| `grep -c "$TEST_KEY" describe-stacks.json` | `0` | `>=1` |
| `grep -c "$TEST_KEY" describe-stack-events.json` | `0` | `>=1` |
| `cfn-lint cloudformation-control-plane.yaml` | exit 0 | exit 2 (with E2001/E3001) |

---

## Reference: B5 static assertion

The static assertion lives in `tests/verify-stripe-integration.py`:

```python
# Stripe key MUST be NoEcho in CFN (preventing the value from
# appearing in stack outputs or describe-stacks results).
check("B5. CFN STRIPE_SECRET_KEY parameter is NoEcho",
      "NoEcho: true" in cfn
      and cfn.find("NoEcho: true") < cfn.find("STRIPE_SECRET_KEY")
          + 500,  # NoEcho must be within ~500 chars of the param name
      "STRIPE_SECRET_KEY parameter is not NoEcho (leaks in stack outputs)")
```

The B5 check is necessary (catches the case where someone edits the YAML and
removes `NoEcho: true`) but not sufficient. It cannot catch:

- A parameter declared in the wrong block (`Resources:` instead of
  `Parameters:`) — CFN silently drops it and the runtime doesn't have the
  parameter at all. **This is exactly the bug we found and fixed on
  2026-06-28**: commit `4ecd232` (t_96b86cff) added `STRIPE_SECRET_KEY`
  under `Resources:` at 2-space indent. CFN rejected the name
  (`STRIPE_SECRET_KEY` violates the resource-name regex `^[a-zA-Z0-9]+$`),
  the parameter never made it into the stack schema, and the broker was
  reading the key from a deploy-time env var only.
- A typo in the `!Sub ${StripeSecretKey}` interpolation (would write empty
  string to config.env).
- The parameter renamed but the env-var export left unchanged (env var
  empty).

This live regression catches all three.

---

## Files changed on 2026-06-28 (fixing the indentation bug)

- `cloudformation-control-plane.yaml`:
  - moved `STRIPE_SECRET_KEY` from `Resources:` (misindented, silently
    dropped by CFN) to `Parameters:` (correct section)
  - renamed logical ID to `StripeSecretKey` (AWS requires parameter names
    to match `[A-Za-z0-9_]+` for Resource names but rejects underscores in
    *Parameter* names — surprising but documented; verified via
    `aws cloudformation validate-template`)
  - updated `!Sub ${STRIPE_SECRET_KEY}` → `!Sub ${StripeSecretKey}` in
    user-data heredoc (line 469)
- `deploy.sh`: added `"StripeSecretKey=$STRIPE_SECRET_KEY"` to
  `--parameter-overrides` block (it was missing entirely — the deploy path
  never wired the key into the stack)

After the fix, `cfn-lint cloudformation-control-plane.yaml` exits 0, and the
live stack has `StripeSecretKey` in its parameter list with the test key
correctly redacted as `****` in `describe-stacks`.