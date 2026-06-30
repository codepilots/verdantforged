#!/usr/bin/env python3
"""Push a local broker-daemon.py change to the live control plane and restart.

boto3-based version of update-broker-daemon.sh. Use this when the shell
wrapper dies with "badly formed help string" from `aws ssm send-command`
(known bug on aws-cli 2.31.x + Python 3.14 — see pitfalls in SKILL.md).
boto3 is unaffected by the CLI parser bug.

Identical semantics to the shell script:
  1. Locate the control plane by Role=control-plane tag.
  2. Pre-flight: confirm the daemon is currently running.
  3. Stage the local file to the artifact bucket.
  4. On the control plane: backup, download, SHA-verify, py_compile, atomic mv.
  5. systemctl restart + health check.
  6. Delete the staging S3 object.

Usage:
  ./update-broker-daemon-boto3.py                          # default path
  ./update-broker-daemon-boto3.py /path/to/other.py        # explicit file

Env overrides:
  AWS_DEFAULT_REGION        (default eu-west-1)
  BROKER_ARTIFACT_BUCKET    (default verdantforged-artifacts-eu-west-1)
  REPO_DAEMON               (default ~/hermes/competition-wt-nemoclaw/tee-broker-deploy/broker-daemon/daemon.py)

Prereqs: boto3 in venv, AWS creds resolving to a principal with
ec2:DescribeInstances, ssm:SendCommand, ssm:GetCommandInvocation,
s3:PutObject + s3:DeleteObject on the artifact bucket.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

import boto3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "src",
        nargs="?",
        default=os.environ.get(
            "REPO_DAEMON",
            str(Path.home() / "hermes/competition-wt-nemoclaw/tee-broker-deploy/broker-daemon/daemon.py"),
        ),
    )
    args = ap.parse_args()

    region = os.environ.get("AWS_DEFAULT_REGION", "eu-west-1")
    bucket = os.environ.get("BROKER_ARTIFACT_BUCKET", "verdantforged-artifacts-eu-west-1")
    live = "/opt/broker-daemon/daemon.py"
    src = Path(args.src).expanduser().resolve()
    if not src.is_file():
        print(f"error: source file not found: {src}", file=sys.stderr)
        return 1

    local_bytes = src.read_bytes()
    local_sha = hashlib.sha256(local_bytes).hexdigest()
    stamp = int(time.time())
    s3_key = f"staging/daemon.py.{stamp}"

    print("=== update-broker-daemon-boto3.py ===")
    print(f"Region:        {region}")
    print(f"Bucket:        {bucket}")
    print(f"Source:        {src} ({len(local_bytes)} bytes, sha256={local_sha})")
    print(f"Live target:   {live}")
    print(f"S3 staging:    s3://{bucket}/{s3_key}")
    print()

    ec2 = boto3.client("ec2", region_name=region)
    ssm = boto3.client("ssm", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    # 1. Find control plane
    res = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Role", "Values": ["control-plane"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    if not res["Reservations"]:
        print("error: no running control-plane instance", file=sys.stderr)
        return 1
    control_id = res["Reservations"][0]["Instances"][0]["InstanceId"]
    print(f"Control plane: {control_id}")

    # 2. Pre-flight: SSM online + daemon running
    info = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [control_id]}]
    )
    if not info["InstanceInformationList"] or info["InstanceInformationList"][0]["PingStatus"] != "Online":
        print(f"error: SSM not online on {control_id}", file=sys.stderr)
        return 1
    pre = ssm.send_command(
        InstanceIds=[control_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [
            "systemctl is-active verdantforged-broker-daemon",
            "pgrep -fa daemon.py | head -1",
        ]},
    )
    pre_id = pre["Command"]["CommandId"]
    time.sleep(3)
    pre_inv = ssm.get_command_invocation(CommandId=pre_id, InstanceId=control_id)
    print("Pre-deploy status:")
    for line in pre_inv.get("StandardOutputContent", "").splitlines():
        print(f"  {line}")

    # 3. Stage to S3
    s3.put_object(Bucket=bucket, Key=s3_key, Body=local_bytes)
    print("Staged to S3.")

    # 4. Push to control plane. SSM runs /bin/sh, not bash — avoid Python
    # heredocs and `[[ ]]`. /bin/sh handles `if [ ... ]; then; fi` fine
    # when the inner double-quotes are balanced (the original shell
    # script's nested-escape hazard goes away in boto3 because we pass
    # plain strings, not bash-quoted JSON).
    push = ssm.send_command(
        InstanceIds=[control_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [
            "set -e",
            "echo === BACKUP ===",
            f"cp -a {live} {live}.bak.pre-update-{stamp}",
            "echo === DOWNLOAD ===",
            f"aws s3 cp s3://{bucket}/{s3_key} {live}.new --region {region}",
            "echo === SHA VERIFY ===",
            f"NEW_SHA=$(sha256sum {live}.new | awk '{{print $1}}')",
            f'if [ "$NEW_SHA" != "{local_sha}" ]; then echo "SHA-MISMATCH local=$NEW_SHA expected={local_sha}"; exit 1; fi',
            "echo SHA-OK",
            "echo === PY COMPILE ===",
            f"python3 -m py_compile {live}.new && echo COMPILE-OK",
            "echo === ATOMIC MOVE ===",
            f"mv {live}.new {live}",
            f"chmod 0755 {live}",
        ]},
    )
    push_id = push["Command"]["CommandId"]
    # Poll for completion
    for _ in range(30):
        time.sleep(2)
        inv = ssm.get_command_invocation(CommandId=push_id, InstanceId=control_id)
        if inv["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
            break
    else:
        print("error: push command did not complete in 60s", file=sys.stderr)
        return 1

    print("Push output:")
    for line in inv.get("StandardOutputContent", "").splitlines():
        print(f"  {line}")
    if inv.get("StandardErrorContent"):
        print("Push stderr:")
        for line in inv["StandardErrorContent"].splitlines():
            print(f"  {line}")
    if inv["Status"] != "Success":
        print(f"error: push failed (status={inv['Status']}); daemon NOT restarted", file=sys.stderr)
        return 1

    # 5. Restart + health check. NOTE: the broker listens via Caddy on
    # 443, not directly on 127.0.0.1:8080. Hit the public URL for the
    # actual healthz; the in-process 8080 probe from the shell script
    # is unreliable on this deployment.
    print("\nRestarting daemon...")
    restart = ssm.send_command(
        InstanceIds=[control_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [
            "systemctl restart verdantforged-broker-daemon",
            "sleep 3",
            "systemctl is-active verdantforged-broker-daemon",
        ]},
    )
    restart_id = restart["Command"]["CommandId"]
    time.sleep(5)
    r_inv = ssm.get_command_invocation(CommandId=restart_id, InstanceId=control_id)
    print("Restart output:")
    for line in r_inv.get("StandardOutputContent", "").splitlines():
        print(f"  {line}")
    if r_inv["Status"] != "Success":
        print(f"error: restart failed (status={r_inv['Status']})", file=sys.stderr)
        return 1

    # 6. Public healthz check
    import requests
    health = requests.get("https://verdant.codepilots.co.uk/healthz", timeout=10)
    print(f"\nPublic healthz: {health.status_code} {health.text.strip()}")

    # 7. Cleanup
    s3.delete_object(Bucket=bucket, Key=s3_key)
    print(f"Done. Staged S3 key removed. Backup: {live}.bak.pre-update-{stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
