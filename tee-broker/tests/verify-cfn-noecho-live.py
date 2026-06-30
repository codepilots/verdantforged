#!/usr/bin/env python3
"""Live CFN NoEcho regression — STRIPE_SECRET_KEY is redacted at runtime.

This test automates the manual procedure documented in
docs/security/cfn-noecho-regression.md. It is SKIPPED by default — running
it requires live AWS credentials and ~2-3 min of stack-update time per run.

What it does:
  1. Generates a realistic-looking 54-char sk_test_... value.
  2. Runs `aws cloudformation deploy` to update an existing stack with that
     value as the StripeSecretKey parameter.
  3. Polls until UPDATE_COMPLETE.
  4. Runs `aws cloudformation describe-stacks` and asserts:
     - Parameters[StripeSecretKey].ParameterValue == '****'
     - The test key string appears ZERO times anywhere in the
       describe-stacks JSON.
     - The test key string appears ZERO times in describe-stack-events JSON.
  5. Unless CFN_NOECHO_KEEP_STACK=1, restores the parameter to ''.

Marker: @pytest.mark.live_cfn — skipped by default.
Run with: pytest -m live_cfn -v tests/verify-cfn-noecho-live.py
Or via: make regression-cfn
"""
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_cfn

WORKSPACE = Path(__file__).resolve().parent.parent
TEMPLATE = WORKSPACE / "cloudformation-control-plane.yaml"

# Defaults match the live stack used by deploy.sh.
DEFAULT_STACK = "verdantforged-broker-control-001"
DEFAULT_REGION = "eu-west-1"


def _check(name, condition, detail=""):
    """Same one-liner shape as the rest of the verify-*.py suite."""
    status = "PASS" if condition else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def _run(cmd, check=True, capture=True, timeout=300):
    """Run a shell command; raise with stderr on failure."""
    print(f"  $ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(
        cmd, capture_output=capture, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        sys.stderr.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        raise RuntimeError(
            f"command failed (exit {result.returncode}): {cmd}"
        )
    return result


def _generate_test_key():
    """Generate a 54-char sk_test_...-shaped key (real Stripe test keys are
    typically 56+ chars; we use 54 for round-number simplicity)."""
    return "sk_tes...ession_check_" + secrets.token_hex(12)  # 25 + 24 = 49 chars
    # NOTE: actual length depends on the prefix; we don't care about exact
    # value, only that describe-stacks should NOT return it.


def _get_existing_params(stack, region):
    """Return the list of (ParameterKey, UsePreviousValue=True) tuples needed
    to update an existing stack. We send UsePreviousValue for every parameter
    already in the stack, then override StripeSecretKey with our test value."""
    result = _run([
        "aws", "cloudformation", "describe-stacks",
        f"--stack-name={stack}",
        f"--region={region}",
        "--query", "Stacks[0].Parameters[].ParameterKey",
        "--output", "text",
    ])
    keys = result.stdout.strip().split()
    out = []
    for k in keys:
        if k == "StripeSecretKey":
            continue  # overridden below
        out.append(f"ParameterKey={k},UsePreviousValue=true")
    return out


def _deploy(stack, region, template, test_key):
    """Run aws cloudformation deploy with the test key as StripeSecretKey."""
    params = _get_existing_params(stack, region)
    params.append(f"ParameterKey=StripeSecretKey,ParameterValue={test_key}")

    cmd = [
        "aws", "cloudformation", "create-change-set",
        f"--stack-name={stack}",
        f"--region={region}",
        f"--change-set-name=noecho-regression-{int(time.time())}",
        f"--template-body=file://{template.resolve()}",
        "--parameters",
    ]
    cmd.extend(params)
    cmd.extend(["--capabilities", "CAPABILITY_NAMED_IAM"])

    _run(cmd)

    # Wait for the change set to reach CREATE_COMPLETE (i.e. be fully
    # analysed by CFN — required before we can execute it).
    cs_marker = "--change-set-name="
    cs_name = next(
        a[len(cs_marker):] for a in cmd if a.startswith(cs_marker)
    )
    deadline = time.time() + 300
    while time.time() < deadline:
        cs_status = _run([
            "aws", "cloudformation", "describe-change-set",
            f"--stack-name={stack}",
            f"--region={region}",
            f"--change-set-name={cs_name}",
            "--query", "Status",
            "--output", "text",
        ]).stdout.strip()
        print(f"  [{time.strftime('%H:%M:%S')}] change-set status: {cs_status}")
        if cs_status == "CREATE_COMPLETE":
            break
        if cs_status in ("FAILED", "EXECUTE_FAILED"):
            reason = _run([
                "aws", "cloudformation", "describe-change-set",
                f"--stack-name={stack}",
                f"--region={region}",
                f"--change-set-name={cs_name}",
                "--query", "StatusReason",
                "--output", "text",
            ], check=False).stdout.strip()
            raise RuntimeError(f"change-set {cs_name} {cs_status}: {reason}")
        time.sleep(5)
    else:
        raise RuntimeError(f"change-set {cs_name} never reached CREATE_COMPLETE")

    _run([
        "aws", "cloudformation", "execute-change-set",
        f"--stack-name={stack}",
        f"--region={region}",
        f"--change-set-name={cs_name}",
    ])

    # Poll for UPDATE_COMPLETE (CFN doesn't have a --wait flag for change sets
    # that completes cleanly in all cases; poll is more reliable).
    deadline = time.time() + 600  # 10 min worst case
    while time.time() < deadline:
        result = _run([
            "aws", "cloudformation", "describe-stacks",
            f"--stack-name={stack}",
            f"--region={region}",
            "--query", "Stacks[0].StackStatus",
            "--output", "text",
        ], check=False)
        status = result.stdout.strip()
        print(f"  [{time.strftime('%H:%M:%S')}] stack status: {status}")
        if status in ("UPDATE_COMPLETE", "CREATE_COMPLETE"):
            return
        if status in ("UPDATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED",
                      "CREATE_FAILED"):
            raise RuntimeError(f"stack update failed: {status}")
        time.sleep(15)
    raise RuntimeError("stack update timed out after 600s")


def _assert_redacted(stack, region, test_key):
    """Assert that describe-stacks / describe-stack-events contain NO
    occurrences of the test key (apart from the redacted '****' marker)."""
    # 1) Parameters[StripeSecretKey].ParameterValue == '****'
    result = _run([
        "aws", "cloudformation", "describe-stacks",
        f"--stack-name={stack}",
        f"--region={region}",
        "--query", "Stacks[0].Parameters[?ParameterKey=='StripeSecretKey']",
        "--output", "json",
    ])
    params = json.loads(result.stdout)
    _check(
        "L1. StripeSecretKey ParameterValue is '****' (redacted)",
        bool(params) and params[0].get("ParameterValue") == "****",
        f"got: {params}",
    )

    # 2) Full describe-stacks response has zero occurrences of test_key
    result = _run([
        "aws", "cloudformation", "describe-stacks",
        f"--stack-name={stack}",
        f"--region={region}",
        "--output", "json",
    ])
    full = result.stdout
    occurrences = full.count(test_key)
    _check(
        f"L2. test key (prefix '{test_key[:12]}...') appears 0 times in "
        f"describe-stacks JSON ({len(full)} bytes)",
        occurrences == 0,
        f"got {occurrences} occurrences — KEY LEAKED",
    )

    # 3) Stack-events response has zero occurrences of test_key
    result = _run([
        "aws", "cloudformation", "describe-stack-events",
        f"--stack-name={stack}",
        f"--region={region}",
        "--output", "json",
    ])
    events = result.stdout
    event_occurrences = events.count(test_key)
    _check(
        f"L3. test key appears 0 times in describe-stack-events JSON "
        f"({len(events)} bytes)",
        event_occurrences == 0,
        f"got {event_occurrences} occurrences — KEY LEAKED",
    )


def _restore_empty(stack, region, template):
    """Set StripeSecretKey back to '' to leave the stack in a clean state."""
    params = _get_existing_params(stack, region)
    params.append("ParameterKey=StripeSecretKey,ParameterValue=")

    cmd = [
        "aws", "cloudformation", "create-change-set",
        f"--stack-name={stack}",
        f"--region={region}",
        f"--change-set-name=noecho-regression-restore-{int(time.time())}",
        f"--template-body=file://{template.resolve()}",
        "--parameters",
    ]
    cmd.extend(params)
    cmd.extend(["--capabilities", "CAPABILITY_NAMED_IAM"])
    _run(cmd)

    cs_marker = "--change-set-name="
    cs_name = next(
        a[len(cs_marker):] for a in cmd if a.startswith(cs_marker)
    )
    deadline = time.time() + 300
    while time.time() < deadline:
        cs_status = _run([
            "aws", "cloudformation", "describe-change-set",
            f"--stack-name={stack}",
            f"--region={region}",
            f"--change-set-name={cs_name}",
            "--query", "Status",
            "--output", "text",
        ]).stdout.strip()
        if cs_status == "CREATE_COMPLETE":
            break
        if cs_status in ("FAILED", "EXECUTE_FAILED"):
            reason = _run([
                "aws", "cloudformation", "describe-change-set",
                f"--stack-name={stack}",
                f"--region={region}",
                f"--change-set-name={cs_name}",
                "--query", "StatusReason",
                "--output", "text",
            ], check=False).stdout.strip()
            # "No updates are to be performed" — that's fine, just bail.
            if "No updates" in reason:
                print(f"  restore change-set: {reason} (skipping execute)")
                return
            raise RuntimeError(f"change-set {cs_name} {cs_status}: {reason}")
        time.sleep(5)
    else:
        raise RuntimeError(f"change-set {cs_name} never reached CREATE_COMPLETE")

    _run([
        "aws", "cloudformation", "execute-change-set",
        f"--stack-name={stack}",
        f"--region={region}",
        f"--change-set-name={cs_name}",
    ])

    deadline = time.time() + 600
    while time.time() < deadline:
        result = _run([
            "aws", "cloudformation", "describe-stacks",
            f"--stack-name={stack}",
            f"--region={region}",
            "--query", "Stacks[0].StackStatus",
            "--output", "text",
        ], check=False)
        status = result.stdout.strip()
        if status in ("UPDATE_COMPLETE", "CREATE_COMPLETE"):
            return
        if status in ("UPDATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED"):
            raise RuntimeError(f"restore failed: {status}")
        time.sleep(15)
    raise RuntimeError("restore timed out after 600s")


# -------------------------------------------------------------- helpers

def _load_cfn_template(path):
    """Parse a CloudFormation YAML template. CFN intrinsic tags (!Ref,
    !Not, !Equals, !Sub, ...) are tolerated by a custom multi-constructor
    that returns their scalar/sequence/mapping payload unchanged.

    Returns a Python dict.
    """
    try:
        import yaml
    except ImportError as e:
        pytest.skip(f"PyYAML not installed: {e}")

    class _CfnYamlLoader(yaml.SafeLoader):
        pass

    def _ignore_cfn_tags(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)

    _CfnYamlLoader.add_multi_constructor("!", _ignore_cfn_tags)

    with open(path) as f:
        return yaml.load(f, Loader=_CfnYamlLoader)


def _parameter_signature(template_dict):
    """Reduce a CFN template's Parameters block to a structural signature:

        [(name, type, noecho, default), ...]

    Order is preserved. Only fields that affect update-stack compatibility
    are included -- Description strings (which can contain em-dash vs '?'
    encoding differences between AWS and local) are intentionally NOT
    compared, since they don't affect parameter shape or runtime behaviour.
    """
    params = (template_dict or {}).get("Parameters") or {}
    sig = []
    for name, spec in params.items():
        if not isinstance(spec, dict):
            # CFN intrinsic-tagged value; shouldn't happen for Parameters
            sig.append((name, None, False, None))
            continue
        sig.append((
            name,
            spec.get("Type", ""),
            bool(spec.get("NoEcho", False)),
            spec.get("Default", None),
        ))
    return sig


# -------------------------------------------------------------- tests

def test_live_cfn_noecho_redacts_stripe_secret_key():
    """Live regression: deploying a CFN stack with a real StripeSecretKey
    value must result in describe-stacks redacting it as '****'.

    Mirrors docs/security/cfn-noecho-regression.md § 'Automated regression'.
    """
    stack = os.environ.get("CFN_NOECHO_STACK", DEFAULT_STACK)
    region = os.environ.get("CFN_NOECHO_REGION", DEFAULT_REGION)
    template = Path(os.environ.get("CFN_NOECHO_TEMPLATE", str(TEMPLATE)))
    keep = os.environ.get("CFN_NOECHO_KEEP_STACK", "").lower() in ("1", "true")

    if not template.exists():
        pytest.skip(f"template not found: {template}")

    # Sanity: AWS creds work
    cred_check = _run([
        "aws", "sts", "get-caller-identity",
        f"--region={region}",
        "--output", "json",
    ], check=False)
    if cred_check.returncode != 0:
        pytest.skip(f"AWS credentials not configured: {cred_check.stderr.strip()}")
    print(f"  AWS identity: {json.loads(cred_check.stdout).get('Arn', '?')}")

    # Sanity: stack exists
    stack_check = _run([
        "aws", "cloudformation", "describe-stacks",
        f"--stack-name={stack}",
        f"--region={region}",
        "--query", "Stacks[0].StackName",
        "--output", "text",
    ], check=False)
    if stack_check.returncode != 0:
        pytest.skip(f"stack {stack} not found in {region} — create it first via deploy.sh")
    print(f"  Using stack: {stack} in {region}")

    test_key = _generate_test_key()
    print(f"  Test key: {test_key[:12]}... (len {len(test_key)})")

    try:
        print("  Step 1/3: deploying stack with test key as StripeSecretKey...")
        _deploy(stack, region, template, test_key)
        print("  Step 2/3: asserting describe-stacks redacts the key...")
        _assert_redacted(stack, region, test_key)
        print("  Step 3/3: assertions passed — redaction confirmed at runtime.")
    finally:
        if not keep:
            print("  Cleanup: restoring StripeSecretKey to empty...")
            try:
                _restore_empty(stack, region, template)
                print("  Cleanup complete.")
            except Exception as e:
                print(f"  Cleanup FAILED: {e}", file=sys.stderr)
                raise

def test_live_cfn_template_matches_live_parameter_shape():
    """Local-vs-live template regression: the LOCAL Parameters block must
    structurally match what is currently deployed in AWS. A drift between
    these two would mean that `make deploy` is about to re-regress the live
    stack back to an old/broken parameter shape (the exact class of bug that
    sibling task t_86ce871c flagged as follow-up: STRIPE_SECRET_KEY drifted
    out of Parameters: into Resources: between the live stack and the local
    checkout).

    Comparison is structural (parameter name + Type + NoEcho + Default), not
    byte-level: em-dash vs '?' in Description text varies between the local
    file and what AWS returns via get-template, and is not material to
    update-stack compatibility.

    Marker: live_cfn (skipped by default -- needs AWS creds).
    """
    stack = os.environ.get("CFN_NOECHO_STACK", DEFAULT_STACK)
    region = os.environ.get("CFN_NOECHO_REGION", DEFAULT_REGION)
    template = Path(os.environ.get("CFN_NOECHO_TEMPLATE", str(TEMPLATE)))

    if not template.exists():
        pytest.skip(f"template not found: {template}")

    # Sanity: AWS creds work (same shape as the other live test).
    cred_check = _run([
        "aws", "sts", "get-caller-identity",
        f"--region={region}",
        "--output", "json",
    ], check=False)
    if cred_check.returncode != 0:
        pytest.skip(f"AWS credentials not configured: {cred_check.stderr.strip()}")
    print(f"  AWS identity: {json.loads(cred_check.stdout).get('Arn', '?')}")

    # Load local template
    local_doc = _load_cfn_template(template)
    local_sig = _parameter_signature(local_doc)
    print(f"  Local Parameters: {len(local_sig)} entries")
    for name, ptype, noecho, default in local_sig:
        marker = " (NoEcho)" if noecho else ""
        print(f"    - {name}: {ptype}{marker}")

    # Fetch live template body
    result = _run([
        "aws", "cloudformation", "get-template",
        f"--stack-name={stack}",
        f"--region={region}",
        "--output", "json",
    ])
    try:
        live_body = json.loads(result.stdout).get("TemplateBody", "")
    except json.JSONDecodeError as e:
        pytest.skip(f"could not parse get-template response as JSON: {e}")
    if not live_body:
        pytest.skip(f"stack {stack} returned empty TemplateBody")

    # Live template is a YAML string; parse it with the same CFN-aware loader.
    # We need to write it to a temp path because _load_cfn_template opens by path.
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as tf:
        tf.write(live_body)
        live_tmp_path = tf.name
    try:
        live_doc = _load_cfn_template(live_tmp_path)
    finally:
        os.unlink(live_tmp_path)

    live_sig = _parameter_signature(live_doc)
    print(f"  Live Parameters: {len(live_sig)} entries")
    for name, ptype, noecho, default in live_sig:
        marker = " (NoEcho)" if noecho else ""
        print(f"    - {name}: {ptype}{marker}")

    # Compare structural signatures
    _check(
        f"D1. parameter count matches "
        f"(local={len(local_sig)}, live={len(live_sig)})",
        len(local_sig) == len(live_sig),
        f"local has {len(local_sig)} params, live has {len(live_sig)} -- "
        f"a parameter was added/removed; review deploy.sh + template together",
    )

    local_names = [s[0] for s in local_sig]
    live_names = [s[0] for s in live_sig]
    _check(
        "D2. parameter names match (same set, same order)",
        local_names == live_names,
        f"local={local_names}, live={live_names} -- name/order drift between "
        f"local checkout and deployed stack; the next `make deploy` would "
        f"alter the parameter shape on the live stack",
    )

    # Per-parameter structural diff
    by_name_local = {s[0]: s for s in local_sig}
    by_name_live = {s[0]: s for s in live_sig}
    drifts = []
    for name in local_names:
        l = by_name_local[name]
        r = by_name_live.get(name)
        if r is None:
            drifts.append(f"  - {name}: present locally but NOT in live stack")
            continue
        if l[1:] != r[1:]:
            drifts.append(
                f"  - {name}: local=(type={l[1]}, noecho={l[2]}, default={l[3]!r}), "
                f"live=(type={r[1]}, noecho={r[2]}, default={r[3]!r})"
            )
    for name in live_names:
        if name not in by_name_local:
            drifts.append(f"  - {name}: present in LIVE stack but NOT in local template")
    _check(
        "D3. each parameter's (Type, NoEcho, Default) matches the live stack",
        not drifts,
        "template drift detected:\n" + "\n".join(drifts) if drifts else "",
    )

    # Targeted regression for the bug this whole task was filed against:
    # StripeSecretKey must be in the Parameters block with NoEcho=True.
    stripe_local = (local_doc.get("Parameters") or {}).get("StripeSecretKey")
    _check(
        "D4. StripeSecretKey is declared under Parameters: (not Resources:)",
        isinstance(stripe_local, dict),
        "StripeSecretKey missing from local Parameters: -- this is the exact "
        "regression that sibling task t_86ce871c flagged as follow-up. "
        "If this drifts again, the live stack will lose NoEcho: true at "
        "the next `make deploy`, leaking the Stripe key into describe-stacks.",
    )
    if isinstance(stripe_local, dict):
        _check(
            "D5. StripeSecretKey has NoEcho: true",
            bool(stripe_local.get("NoEcho", False)),
            "NoEcho not set on StripeSecretKey -- key would leak into "
            "describe-stacks, CloudTrail, IAM policy generation, etc.",
        )

    print("  Parameter block matches live stack -- no drift detected.")
