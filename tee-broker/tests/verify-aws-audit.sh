#!/bin/bash
# Ad-hoc verification for /home/autumn/.hermes/scripts/aws-audit.sh
# Stub AWS CLI that dispatches by (subcommand, region, kind) to canned JSON files.
# No live AWS calls. Cleans up its own temp dir on exit.

set -uo pipefail

WORK=$(mktemp -d -t hermes-verify-aws-audit-XXXXXX)
trap "rm -rf '$WORK'" EXIT
echo "[verify] working dir: $WORK"

mkdir -p "$WORK/bin"
cat > "$WORK/bin/aws" <<'STUB'
#!/bin/bash
args=("$@")
SUBCMD=""
for a in "${args[@]}"; do
  case "$a" in ec2|cloudformation|sts|iam) SUBCMD="$a"; break ;; esac
done
if [ "$SUBCMD" = "sts" ]; then echo "424503481467"; exit 0; fi
REGION=""
i=0
while [ $i -lt ${#args[@]} ]; do
  if [ "${args[$i]}" = "--region" ]; then REGION="${args[$((i+1))]}"; break; fi
  i=$((i+1))
done
KIND=""
for a in "${args[@]}"; do
  case "$a" in
    describe-instances) KIND="instances"; break ;;
    describe-addresses) KIND="addresses"; break ;;
    list-stacks)        KIND="stacks"; break ;;
  esac
done
F="$AWS_STUB_DIR/${SUBCMD}.${REGION}.${KIND}"
[ -s "$F" ] && cat "$F"
exit 0
STUB
chmod +x "$WORK/bin/aws"

# Copy the audit script under test
cp /home/autumn/.hermes/scripts/aws-audit.sh "$WORK/aws-audit.sh"

# Set up HOME with whitelist
mkdir -p "$WORK/home/.hermes/config"
cat > "$WORK/home/.hermes/config/aws-audit-whitelist" <<'WL'
# Known-intentional AWS resources.
verdantforged
verdantforged-broker-control
verdantforged-broker-worker
WL

export AWS_STUB_DIR="$WORK"
export HOME="$WORK/home"
export PATH="$WORK/bin:$PATH"

# Stub for failure case: returns non-zero
mkdir -p "$WORK/fail-bin"
cat > "$WORK/fail-bin/aws" <<'FAIL'
#!/bin/bash
exit 1
FAIL
chmod +x "$WORK/fail-bin/aws"

PASS=0
FAIL=0

assert() {
  local label="$1" want_code="$2" got_code="$3" want_in="$4" got_out="$5" want_not_in="$6"
  local ok=1
  [ "$got_code" = "$want_code" ] || ok=0
  if [ -n "$want_in" ]; then
    echo "$got_out" | grep -qF "$want_in" || ok=0
  fi
  if [ -n "$want_not_in" ]; then
    echo "$got_out" | grep -qF "$want_not_in" && ok=0
  fi
  if [ "$ok" = "1" ]; then
    echo "[PASS] $label"
    PASS=$((PASS+1))
  else
    echo "[FAIL] $label  exit=$got_code (want $want_code)"
    echo "       stdout: ${got_out:0:400}"
    FAIL=$((FAIL+1))
  fi
}

reset_stubs() {
  rm -f "$WORK"/ec2.* "$WORK"/cloudformation.*
}

# Real AWS output format from the audit script's JMESPath query:
# describe-instances: ID<TAB>Type<TAB>Name<TAB>Project<TAB>Stack<TAB>LaunchTime
# describe-addresses: PublicIp<TAB>AllocationId
# list-stacks: Name<TAB>Status<TAB>CreationTime

inst_wl="i-1234"$'\t'"t3.small"$'\t'"verdantforged-broker-control-control"$'\t'"verdantforged"$'\t'"verdantforged-broker-control"$'\t'"2026-06-27T10:00:30+00:00"

# 1. Whitelisted instance only
reset_stubs
echo "$inst_wl" > "$WORK/ec2.eu-west-2.instances"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
assert "1. whitelisted instance -> silent, exit 0" 0 "$code" "" "$out" "UNKNOWN"

# 2. Unknown instance
reset_stubs
inst_bad="i-bad"$'\t'"t3.micro"$'\t'"random-name"$'\t'"unknown-project"$'\t'"some-stack"$'\t'"2026-06-27T11:00:00+00:00"
echo "$inst_bad" > "$WORK/ec2.eu-west-2.instances"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
assert "2. unknown instance -> alert, ID+UNKNOWN in stdout" 2 "$code" "i-bad" "$out" ""

# 3. Unknown stack
reset_stubs
echo "my-orphan-stack"$'\t'"CREATE_COMPLETE"$'\t'"2026-06-27T10:00:00+00:00" > "$WORK/cloudformation.us-east-1.stacks"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
assert "3. unknown stack -> alert" 2 "$code" "my-orphan-stack" "$out" ""

# 4. Orphan EIP
reset_stubs
echo "1.2.3.4"$'\t'"eipalloc-deadbeef" > "$WORK/ec2.eu-west-2.addresses"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
assert "4. orphan EIP -> alert" 2 "$code" "1.2.3.4" "$out" ""

# 5. Whitelisted Project tag exempts instance
reset_stubs
inst_proj="i-wl"$'\t'"t3.small"$'\t'"mystery-name"$'\t'"verdantforged"$'\t'"stack-x"$'\t'"2026-06-27T10:00:30+00:00"
echo "$inst_proj" > "$WORK/ec2.eu-west-2.instances"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
assert "5. whitelisted Project tag exempts instance" 0 "$code" "" "$out" "UNKNOWN"

# 6. Whitelisted stack name exempts instance
reset_stubs
inst_stk="i-wl"$'\t'"t3.small"$'\t'"name-x"$'\t'"other-project"$'\t'"verdantforged-broker-control"$'\t'"2026-06-27T10:00:30+00:00"
echo "$inst_stk" > "$WORK/ec2.eu-west-2.instances"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
assert "6. whitelisted stack name exempts instance" 0 "$code" "" "$out" "UNKNOWN"

# 7. AWS creds broken -> AUDIT_FAIL, exit 1
orig_path="$PATH"
export PATH="$WORK/fail-bin:$PATH"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
export PATH="$orig_path"
assert "7. AWS creds broken -> AUDIT_FAIL, exit 1" 1 "$code" "AUDIT_FAIL" "$out" ""

# 8. Multiple issues across regions aggregated
reset_stubs
inst_a="i-a"$'\t'"t3.micro"$'\t'"n1"$'\t'"unknown-proj"$'\t'"s1"$'\t'"2026-06-27T10:00:30+00:00"
inst_b="i-b"$'\t'"t3.micro"$'\t'"n2"$'\t'"unknown-proj"$'\t'"s2"$'\t'"2026-06-27T10:00:30+00:00"
echo "$inst_a" > "$WORK/ec2.us-east-1.instances"
echo "5.6.7.8"$'\t'"eipalloc-aaaa" > "$WORK/ec2.us-east-1.addresses"
echo "$inst_b" > "$WORK/ec2.us-west-2.instances"
echo "orphan-stack-a"$'\t'"CREATE_COMPLETE"$'\t'"2026-06-27T10:00:00+00:00" > "$WORK/cloudformation.us-west-2.stacks"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
# All four IDs present (don't care about order)
all_present=1
for s in "i-a" "i-b" "orphan-stack-a" "5.6.7.8" "ORPHAN EIP"; do
  echo "$out" | grep -qF "$s" || { all_present=0; break; }
done
if [ "$code" = "2" ] && [ "$all_present" = "1" ]; then
  echo "[PASS] 8. multiple issues across regions aggregated"
  PASS=$((PASS+1))
else
  echo "[FAIL] 8. multiple issues across regions aggregated  exit=$code"
  echo "       stdout: ${out:0:500}"
  FAIL=$((FAIL+1))
fi

# 9. Whitelisted instance + orphan EIP -> exit 2
reset_stubs
echo "$inst_wl" > "$WORK/ec2.eu-west-2.instances"
echo "9.9.9.9"$'\t'"eipalloc-ffff" > "$WORK/ec2.eu-west-2.addresses"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
assert "9. whitelisted instance + orphan EIP -> exit 2 (EIP always alerts)" 2 "$code" "9.9.9.9" "$out" "UNKNOWN instance"

# 10. Completely empty -> silent
reset_stubs
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
assert "10. no resources anywhere -> silent, exit 0" 0 "$code" "" "$out" "UNKNOWN"

# 11. Whitelisted instance Name tag exempts
reset_stubs
inst_name="i-1"$'\t'"t3.micro"$'\t'"verdantforged-broker-worker"$'\t'"y"$'\t'"stack-x"$'\t'"2026-06-27T10:00:30+00:00"
echo "$inst_name" > "$WORK/ec2.eu-west-2.instances"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
assert "11. whitelisted instance Name tag exempts" 0 "$code" "" "$out" "UNKNOWN"

# 12. Estimated run-rate appears
reset_stubs
inst_x="i-x"$'\t'"t3.small"$'\t'"name"$'\t'"y"$'\t'"stack-x"$'\t'"2026-06-27T10:00:30+00:00"
echo "$inst_x" > "$WORK/ec2.eu-west-2.instances"
out=$(bash "$WORK/aws-audit.sh" 2>&1); code=$?
assert "12. Estimated run-rate line appears in alert output" 2 "$code" "Estimated run-rate" "$out" ""

echo ""
echo "=== Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo "Ad-hoc verification only (stubbed AWS CLI, no live AWS calls)."
echo "Scope: behavior of /home/autumn/.hermes/scripts/aws-audit.sh"

[ "$FAIL" = "0" ] && exit 0 || exit 1