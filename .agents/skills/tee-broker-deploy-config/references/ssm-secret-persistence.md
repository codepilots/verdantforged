# SSM Parameter Store as durable secret storage for the broker

Captured 2026-06-28 when wiring `STRIPE_SECRET_KEY` end-to-end. The
broker had been running in DEMO MODE because no deployer had ever set
the env var; the user's preference ("the stripe key should be in your
environment / should be set in STRIPE_SECRET_KEY / yes update the
bootstrap to pull in the value") implied it should be **automatic** —
deploy once, work forever after, with no manual env-var export on
each rebuild.

## Why SSM Parameter Store (vs CFN NoEcho, vs env-var-in-deploy)

Three options were on the table:

| Option | Survives rebuild? | Auditable? | Rotate without redeploy? | Scope of IAM |
|---|---|---|---|---|
| Env var in deploy.sh only | No (lost on instance rebuild) | No (shell history) | No | None (lives in deployer shell) |
| CFN parameter (`StripeSecretKey`) with `NoEcho: true` | Yes (in CFN stack state) | Partial (redacted in describe-stacks, but visible in stack template diffs) | No (forces stack update) | CFN-internal only |
| **SSM Parameter Store (`SecureString`)** | Yes (KMS-encrypted at rest) | Yes (CloudTrail logs every read/write) | Yes (just `put-parameter` again) | Scoped to one parameter path |

SSM wins on all three "operational hygiene" dimensions:
- **Survives rebuild**: the new EC2 instance reads it on bootstrap.
- **Auditable**: `aws cloudtrail lookup-events --lookup-attributes AttributeKey=ResourceName,AttributeValue=/verdantforged/broker/stripe-secret-key` shows every GetParameter and PutParameter call.
- **Rotatable**: change the parameter value without redeploying or restarting the instance — the next bootstrap picks it up.
- **IAM-scoped**: the broker's role can read `arn:aws:ssm:eu-west-1:ACCOUNT:parameter/verdantforged/broker/stripe-secret-key` and nothing else. Cannot enumerate the account's parameters.

## The three-piece wiring

### 1. CFN template — scope IAM to a single parameter path

```yaml
- PolicyName: ReadStripeSecret
  PolicyDocument:
    Version: '2012-10-17'
    Statement:
      - Effect: Allow
        Action: ssm:GetParameter
        Resource: !Sub 'arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/verdantforged/broker/stripe-secret-key'
      - Effect: Allow
        Action: kms:Decrypt
        Resource: '*'   # Tighten to the customer-managed KMS key if not using aws/ssm
        Condition:
          StringEquals:
            'kms:ViaService': !Sub 'ssm.${AWS::Region}.amazonaws.com'
```

The `Resource` is the **exact parameter path** — no wildcard. The role
cannot list parameters in the account, cannot read other parameters,
cannot write to any parameter. Only `ssm:GetParameter` on this one
path, and the matching `kms:Decrypt` call (because SecureString is
KMS-encrypted at rest).

### 2. deploy.sh — write once on deploy (or whenever the env var changes)

```bash
if [ -n "$STRIPE_SECRET_KEY" ]; then
    log "persisting STRIPE_SECRET_KEY to SSM Parameter Store"
    "$PYTHON" - "$REGION" "$STRIPE_SECRET_KEY" <<'PYEOF' || die "..."
import sys, boto3
region, secret = sys.argv[1], sys.argv[2]
assert secret.startswith("sk_")
boto3.client("ssm", region_name=region).put_parameter(
    Name="/verdantforged/broker/stripe-secret-key",
    Value=secret, Type="SecureString",
    Description="...",
    Overwrite=True,
)
PYEOF
    unset STRIPE_SECRET_KEY   # Zero out the shell copy after SSM is the source of truth
fi
```

Three important details:
- **`Overwrite=True`** — makes the deploy idempotent. Re-running with the same value is a no-op (no CloudTrail noise).
- **`unset STRIPE_SECRET_KEY` after the put** — once SSM is the source of truth, the shell copy is dead weight that could leak via process introspection (`/proc/PID/environ`).
- **Don't forward via the bootstrap env file anymore** — the bootstrap will fetch from SSM. Forwarding the secret in the SSM `send_command` Parameters field means it lands in the AWS console's command history (visible to anyone with `ssm:DescribeCommandInvocation` permission). SSM Parameter Store + scoped `GetParameter` is the safer path.

### 3. bootstrap-control-plane.sh — fetch from SSM if env is empty

```bash
# Resolve STRIPE_SECRET_KEY BEFORE the heredoc (see Heredoc pitfall below)
if [ -z "${STRIPE_SECRET_KEY:-}" ]; then
    echo "step 5c: STRIPE_SECRET_KEY not in env — fetching from SSM"
    if STRIPE_SECRET_KEY=$(aws ssm get-parameter \
        --name /verdantforged/broker/stripe-secret-key \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text 2>/dev/null); then
        echo "step 5c: STRIPE_SECRET_KEY fetched from SSM (len=${#STRIPE_SECRET_KEY})"
    else
        echo "step 5c: SSM get-parameter failed — DEMO MODE"
        STRIPE_SECRET_KEY=""
    fi
fi

# Then the heredoc (NOT single-quoted — we still need variable expansion)
cat > "$BR_DEPLOY/config.env" <<ENVEOF
...
STRIPE_SECRET_KEY=$STRIPE_SECRET_KEY
...
ENVEOF
chmod 0600 "$BR_DEPLOY/config.env"
```

The daemon reads `STRIPE_SECRET_KEY` from `/opt/broker-daemon/config.env` at module import time, same as before. From the daemon's POV, nothing changed — it still reads a config.env line. The bootstrap's job is just to make sure that line is populated.

## The Heredoc pitfall — DO NOT embed `$(aws ...)` inside the heredoc

I spent ~30 min on this trap during the 2026-06-28 session. The naive
implementation put the entire SSM-fetch logic **inside the heredoc that
writes config.env**:

```bash
cat > "$BR_DEPLOY/config.env" <<ENVEOF
BROKER_REGION=$REGION
...
if [ -z "${STRIPE_SECRET_KEY:-}" ]; then
    SSM_STRIPE=$(aws ssm get-parameter ...)   # ← inside heredoc body
    ...
fi
ENVEOF
```

This **broke in three different ways**:
1. The heredoc body is treated as text, but bash still performs **command substitution** on `$(...)` and **variable expansion** on `${...}` inside non-quoted heredocs. So `$(aws ssm get-parameter ...)` ran at write time, opening a network round-trip for every config.env write.
2. Subsequent lines that referenced `$SSM_RC` or `$?` got **expanded incorrectly** because the heredoc body's `$?` referred to the previous command in the heredoc, not the bootstrap script.
3. **`set -e` + `||` inside `$(...)` in the heredoc** triggered `command not found` errors at parse time — bash tried to execute the heredoc body as code before reaching `ENVEOF`.

**The fix** is what the code above shows: resolve all variables
**before** the heredoc starts, then use **plain variable references**
inside the heredoc (`STRIPE_SECRET_KEY=$STRIPE_SECRET_KEY`). The heredoc
becomes purely textual from the daemon's POV, but the value going IN is
the resolved one from the bootstrap script's environment.

If you need to embed secret values that MUST NOT be expanded by bash
inside the heredoc, switch to a **single-quoted heredoc** (`<<'ENVEOF'`)
and write the secret via a separate `echo ... >> config.env` after.
But that approach is error-prone for multi-line values — the SSM-fetch
pattern is cleaner.

## Verification: prove the SSM-fetch path works end-to-end

To verify the bootstrap correctly fetches from SSM when the env var
is empty (the whole point of the pattern):

```bash
# 1. Confirm SSM has the parameter
aws ssm get-parameter --name /verdantforged/broker/stripe-secret-key \
  --with-decryption --query 'Parameter.Value' --output text | head -c 15

# 2. Clear the daemon's config.env entry
sudo sed -i 's/^STRIPE_SECRET_KEY=.*/STRIPE_SECRET_KEY=/' /opt/broker-daemon/config.env

# 3. Run bootstrap with STRIPE_SECRET_KEY='' in the env (no key set)
. /tmp/_bootstrap_env.sh    # STRIPE_SECRET_KEY=''
sudo bash scripts/bootstrap-control-plane.sh

# 4. Look for the log line
sudo grep "step 5c" /mnt/broker/logs/control-bootstrap.log
# step 5c: STRIPE_SECRET_KEY not in env — fetching from SSM Parameter Store
# step 5c: STRIPE_SECRET_KEY fetched from SSM (len=107)

# 5. Confirm config.env got the value
grep "^STRIPE_SECRET_KEY" /opt/broker-daemon/config.env \
  | awk -F= '{print "len=", length($2)}'
# len= 107

# 6. Restart daemon and verify it makes live Stripe API calls
sudo systemctl restart verdantforged-broker-daemon
sleep 3
sudo journalctl -u verdantforged-broker-daemon --no-pager -n 5
# INFO stripe message='Request to Stripe api' method=post \
#   url=https://api.stripe.com/v1/payment_intents/pi_***/capture
```

If the daemon log shows `stripe=error` with `No such payment_intent`
from `https://api.stripe.com/v1/...`, the SSM-fetch path is wired
correctly — that's a live Stripe API response, not a demo-mode settle.

## When to use this pattern (decision rules)

Use SSM Parameter Store for a secret when **all** of:
- The daemon reads the secret at module import (not per-request)
- The secret should survive instance rebuilds without re-deploying
- The secret value might rotate (API key, webhook secret, OAuth token)
- You want CloudTrail audit on every read/write
- The IAM role on the consuming host can be scoped to a single parameter path

Use deploy.sh env var only when:
- The secret is one-time bootstrap (e.g., a database password set at stack create time and never rotated)
- The instance is truly ephemeral and won't be rebuilt with the same identity

Use AWS Secrets Manager when:
- You need automatic rotation (Secrets Manager supports this natively)
- The secret has a structured value (JSON with multiple fields, versioned)
- Cost matters — Secrets Manager is $0.40/secret/month vs SSM free tier

For a single API key on a hackathon broker, SSM wins on simplicity and cost. Secrets Manager's rotation features are overkill for a `sk_test_***` key.

## Related pitfalls (cross-references)

- **Pitfall 5 (tee-broker-pattern)** — "Don't claim SEV-SNP without the four-pronged check." Same principle: don't claim the broker has the Stripe key wired just because the code reads `STRIPE_SECRET_KEY`; verify the daemon log shows live API calls.
- **Pitfall 8 (tee-broker-deploy-config)** — bash heredoc value mangling. The Heredoc pitfall in this reference is the same class of bug with a different shape.
- **Pitfall 9 (tee-broker-deploy-config)** — CFN outputs ≠ CFN parameters. The bootstrap reads CFN outputs, but for secrets specifically, the durable home is SSM, not CFN. CFN parameters are for stack INPUTS; SSM is for runtime secrets.
- **`tee-broker-pattern/references/tee-broker-llm-proxy.md`** — the upstream LLM key (Ollama Cloud) has the same SSM-persistence opportunity but is currently passed via SSM env file. The pattern in this reference generalizes — apply it to `BROKER_LLM_API_KEY` next time the broker rebuilds.
