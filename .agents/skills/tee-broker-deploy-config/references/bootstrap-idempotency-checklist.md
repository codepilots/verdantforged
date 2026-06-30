# Bootstrap idempotency checklist — every package the bootstrap must verify

A fresh Ubuntu 24.04 LTS AMI on AWS does NOT ship the packages a Python
web app + Caddy + EFS mount + CFN bootstrap expects. Every one of these
must be installed (or verified present) in `scripts/bootstrap-control-plane.sh`
**before** anything that depends on it runs. Without this, a fresh
deploy hits "module not found", "binary not found", or "mount: bad
option" — and the broker silently half-boots.

Discovered 2026-06-28 during the London → Ireland broker migration. The
first bootstrap run had a /healthz endpoint that returned 200, but the
daemon couldn't start, caddy wasn't installed, EFS wasn't mounted, and
CFN outputs couldn't be read. Each individual fix was one line, but the
number of fixes (and the order they had to be applied in) made the
session worth documenting.

## The dependency graph (in install order)

```
1. python3-pip           → for `python3 -m pip install aiohttp boto3`
2. unzip + curl          → for AWS zip bundle install of awscli
3. awscli (v2)           → for `aws cloudformation describe-stacks`
4. nfs-common            → for `/sbin/mount.nfs` (EFS mount helper)
5. caddy (Cloudsmith)    → for HTTPS termination + Let's Encrypt
6. mkdir -p /opt/broker-daemon  → BEFORE install of daemon.py
7. mkdir -p /mnt/broker  → BEFORE mount attempt
8. CFN IAM permissions   → BEFORE any aws describe-stacks call
9. systemd unit file     → BEFORE systemctl enable --now
```

Each step's failure mode is silent unless you explicitly check. The
bootstrap must `command -v foo || apt install foo` for every one.

## The 9 idempotency checks (drop into bootstrap-control-plane.sh)

Each check is "is X present? If not, install it. If install fails, log
loudly and exit 1." Don't silently skip — a partial bootstrap leaves
the instance in a state where the next deploy also fails (same root
cause), and you waste hours wondering why your retry didn't help.

```bash
# 1. pip (for aiohttp + boto3 install)
if ! python3 -c "import pip" 2>/dev/null; then
    apt-get update -qq && apt-get install -y -qq python3-pip
fi

# 2. unzip + curl (for AWS zip bundle)
# Most AMIs have curl; unzip is sometimes missing
command -v unzip >/dev/null || apt-get install -y -qq unzip

# 3. awscli (NOT in Ubuntu 24.04 default apt — use AWS zip bundle)
if ! command -v aws >/dev/null 2>&1; then
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" \
        -o /tmp/awscliv2.zip
    unzip -q /tmp/awscliv2.zip -d /tmp/
    /tmp/aws/install
    rm -rf /tmp/awscliv2.zip /tmp/aws
fi

# 4. nfs-common (EFS mount helper — /sbin/mount.nfs)
if ! command -v mount.nfs >/dev/null 2>&1; then
    apt-get install -y -qq nfs-common
fi

# 5. caddy (HTTPS termination + Let's Encrypt)
if ! command -v caddy >/dev/null 2>&1; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" 2>/dev/null \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
    curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" 2>/dev/null \
        | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq && apt-get install -y -qq caddy
fi

# 6. /opt/broker-daemon exists (before install -m 0755 daemon.py .../daemon.py)
install -d -m 0755 /opt/broker-daemon   # portable across distributions

# 7. /mnt/broker exists (before mount attempt)
install -d -m 0755 /mnt/broker

# 8. CFN IAM permissions — needs cloudformation:DescribeStacks on own stack
# (Set in CFN template's ControlPlaneRole IAM policy. See pitfall below.)

# 9. systemd unit (bootstrap writes it; don't rely on CFN user-data)
cat > /etc/systemd/system/verdant-broker-daemon.service <<'EOF'
[Unit]
Description=VerdantForged Broker Daemon
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
EnvironmentFile=/opt/broker-daemon/config.env
ExecStart=/usr/bin/python3 /opt/broker-daemon/daemon.py
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
```

After these pass, the rest of the bootstrap (EFS mount, config.env
generation, daemon install, systemd start) works without surprises.

## Pitfall: bootstrap copies `daemon.py` only, then daemon crashes

`install -m 0755 "$REPO_DIR/broker-daemon/daemon.py" "$BR_DEPLOY/daemon.py"`
copies ONE file. If the daemon imports sibling modules from its own
directory (`import crypto`, `from openshell import ...`), the daemon
crashes at startup with `ModuleNotFoundError: No module named 'crypto'`.

Fix: copy all .py files in `$REPO_DIR/broker-daemon/`, plus any
subdirectories with `__init__.py` (real subpackages). Subdirectories
like `static/`, `caddy/`, `__pycache__/` should NOT be copied — they're
not Python source.

```bash
install -m 0755 "$REPO_DIR/broker-daemon/daemon.py" "$BR_DEPLOY/daemon.py"
for py in "$REPO_DIR"/broker-daemon/*.py; do
    [ "$(basename "$py")" = "daemon.py" ] && continue
    install -m 0644 "$py" "$BR_DEPLOY/"
done
for d in "$REPO_DIR"/broker-daemon/*/; do
    name=$(basename "$d")
    case "$name" in __pycache__|static|caddy) continue ;; esac
    [ -f "$d/__init__.py" ] && cp -r "$d" "$BR_DEPLOY/"
done
```

## Pitfall: CFN template parameters don't auto-export as outputs

Bootstrap reads `VpcId`, `SubnetId`, `WorkerAmiId`,
`ControlPlaneSecurityGroupId` from CFN outputs — but these are
**Parameters**, not Outputs. CFN does NOT auto-export parameters; you
must add them explicitly to the `Outputs:` block in the CFN template.

Without the Outputs entries, the bootstrap reads empty values and writes
`BROKER_VPC_ID=` to config.env. The daemon then fails to launch workers
(falls back to no-network isolation).

Fix in CFN template:

```yaml
Outputs:
  VpcId:
    Description: VPC ID (for worker launch)
    Value: !Ref VpcId
    Export:
      Name: !Sub '${AWS::StackName}-vpc-id'
  SubnetId:
    Description: Subnet ID (for worker launch)
    Value: !Ref SubnetId
    Export:
      Name: !Sub '${AWS::StackName}-subnet-id'
  WorkerAmiId:
    Description: Worker AMI ID
    Value: !Ref WorkerAmiId
    Export:
      Name: !Sub '${AWS::StackName}-worker-ami-id'
  ControlPlaneSecurityGroupId:
    Description: Control plane security group ID
    Value: !Ref ControlPlaneSecurityGroup
    Export:
      Name: !Sub '${AWS::StackName}-control-sg-id'
```

## Pitfall: control plane IAM role needs `cloudformation:DescribeStacks`

The bootstrap calls `aws cloudformation describe-stacks` to read EFS
DNS, SG IDs, etc. Even though the control plane IS the stack being read,
the IAM role needs explicit permission. Default control-plane IAM
policies only grant EC2 + SSM + S3 — not CFN.

Without this perm, the bootstrap's `aws cloudformation describe-stacks`
call fails with `AccessDenied`, the `|| echo ""` fallback returns
empty, and EFS mount fails (no DNS to mount from).

Fix in CFN template, ControlPlaneRole IAM policy:

```yaml
- PolicyName: ReadOwnStack
  PolicyDocument:
    Version: '2012-10-17'
    Statement:
      - Effect: Allow
        Action:
          - cloudformation:DescribeStacks
          - cloudformation:DescribeStackResources
          - cloudformation:DescribeStackEvents
        # Scoped to the control-plane stack ARN — broker can't read other stacks
        Resource: !Ref 'AWS::StackId'
```

## Pitfall: `/v1/discover` 500s on a fresh DB

The daemon's `/v1/discover` queries the `skills` table to merge
built-in skills with registered ones. The `skills` table is only
created when `POST /v1/skills` is first called (or in a fresh DB init
in `register_skill`). On a never-registered broker, the table doesn't
exist and the query 500s.

Defensive query (broker-daemon/daemon.py around the `/v1/discover`
handler):

```python
with db() as conn:
    exists = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='skills'"
    ).fetchone()
    registered_names = set()
    if exists:
        rows = conn.execute(
            "SELECT name FROM skills s1 "
            "WHERE version = (SELECT MAX(version) FROM skills s2 WHERE s2.name = s1.name)"
        ).fetchall()
        registered_names = {r["name"] for r in rows}
```

The same pattern applies to ANY on-demand-created SQLite table — guard
the query with a `sqlite_master` check. Without it, `/v1/discover`
becomes a paper cut that prevents smoke-testing fresh deployments.

## Recipe: turn this checklist into a verifier script

If you want to audit an existing bootstrap against this checklist, run:

```bash
# On a deployed control plane:
sudo /usr/bin/python3 -c "import pip" 2>/dev/null || echo "MISSING: pip"
command -v aws >/dev/null 2>&1 || echo "MISSING: awscli"
command -v caddy >/dev/null 2>&1 || echo "MISSING: caddy"
command -v mount.nfs >/dev/null 2>&1 || echo "MISSING: nfs-common"
test -d /opt/broker-daemon || echo "MISSING: /opt/broker-daemon"
test -d /mnt/broker || echo "MISSING: /mnt/broker"
sudo -E aws cloudformation describe-stacks \
  --stack-name verdantforged-broker-control \
  --region eu-west-1 \
  --query 'Stacks[0].StackStatus' --output text \
  || echo "MISSING: cloudformation:DescribeStacks"
test -f /etc/systemd/system/verdant-broker-daemon.service \
  || echo "MISSING: systemd unit"
```

If any line says `MISSING:`, the corresponding bootstrap check is
either missing or failing silently. Fix the bootstrap, redeploy, re-verify.
