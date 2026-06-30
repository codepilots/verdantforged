#!/usr/bin/env python3
"""Bake a validated NemoClaw worker into a reusable EC2 AMI.

Purpose:
  Avoid repeated cold-start downloads of Node/Docker/OpenShell/NemoClaw and the
  sandbox image. Run this after an AWS worker has reached the real ready state
  and passed an end-to-end job with execution_mode=nemoclaw-sandbox.

Typical flow:
  python3 scripts/bake-worker-ami.py --instance-id i-... --apply-control-plane

Safety:
  - Refuses to bake unless nemohermes is installed and a sandbox exists.
  - Refuses to bake unless the worker heartbeat says ready, unless --force.
  - Removes ephemeral worker key material before image capture unless --keep-keys.
  - Does not read or print broker secrets.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError


def log(msg: str) -> None:
    print(f"[{dt.datetime.utcnow().replace(microsecond=0).isoformat()}Z] {msg}", flush=True)


def ssm_run(ssm: Any, instance_id: str, command: str, *, timeout: int = 120) -> tuple[int, str, str]:
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command]},
        TimeoutSeconds=max(timeout, 30),
    )
    command_id = resp["Command"]["CommandId"]
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        try:
            inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            last = inv
        except ClientError as exc:
            if "InvocationDoesNotExist" not in str(exc):
                raise
            time.sleep(2)
            continue
        status = inv["Status"]
        if status in {"Success", "Failed", "Cancelled", "TimedOut", "Cancelling"}:
            out = inv.get("StandardOutputContent", "")
            err = inv.get("StandardErrorContent", "")
            return (0 if status == "Success" else 1, out, err)
        time.sleep(2)
    out = (last or {}).get("StandardOutputContent", "")
    err = (last or {}).get("StandardErrorContent", "")
    return 124, out, err or f"SSM command timed out after {timeout}s"


def discover_worker(ec2: Any) -> str:
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": ["verdantforged"]},
            {"Name": "tag:Role", "Values": ["tee-worker"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    instances: list[dict[str, Any]] = []
    for res in resp.get("Reservations", []):
        instances.extend(res.get("Instances", []))
    if not instances:
        raise SystemExit("no running verdantforged tee-worker instance found; pass --instance-id")
    instances.sort(key=lambda i: str(i.get("LaunchTime") or ""), reverse=True)
    return instances[0]["InstanceId"]


def validate_worker(ssm: Any, instance_id: str, *, force: bool) -> dict[str, Any]:
    probe = r'''
set -eu
export HOME=/root
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH
python3 - <<'PY'
import json, os, pathlib, subprocess, sys
hb_path = pathlib.Path('/mnt/broker/logs/worker-heartbeat.json')
meta_path = pathlib.Path('/opt/worker/.nemoclaw_metadata')
sandbox_path = pathlib.Path('/opt/worker/.nemoclaw_sandbox_name')

def run(cmd):
    try:
        return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except Exception as e:
        class R:
            returncode=99; stdout=''; stderr=str(e)
        return R()

hb = {}
if hb_path.exists():
    try: hb = json.loads(hb_path.read_text())
    except Exception as e: hb = {'parse_error': str(e)}
meta = {}
if meta_path.exists():
    try: meta = json.loads(meta_path.read_text())
    except Exception as e: meta = {'parse_error': str(e)}
name = sandbox_path.read_text().strip() if sandbox_path.exists() else os.environ.get('NEMOCLAW_SANDBOX_NAME', 'worker')
which = run(['bash','-lc','command -v nemohermes || true'])
version = run(['bash','-lc','nemohermes --version 2>/dev/null | head -1 || true'])
listing = run(['bash','-lc','nemohermes list --json 2>/dev/null || true'])
docker = run(['bash','-lc','docker images --digests --format "{{.Repository}}:{{.Tag}} {{.Digest}} {{.Size}}" 2>/dev/null | head -50 || true'])
try:
    sandboxes = json.loads(listing.stdout or '{}').get('sandboxes', [])
except Exception:
    sandboxes = []
print(json.dumps({
    'heartbeat': hb,
    'metadata': meta,
    'sandbox_name': name,
    'nemohermes_path': which.stdout.strip(),
    'nemohermes_version': version.stdout.strip(),
    'sandbox_count': len(sandboxes),
    'sandboxes': sandboxes,
    'docker_images': docker.stdout.splitlines(),
}, indent=2))
PY
'''
    code, out, err = ssm_run(ssm, instance_id, probe, timeout=90)
    if code != 0:
        raise SystemExit(f"worker validation probe failed: {err or out}")
    data = json.loads(out)
    hb = data.get("heartbeat") or {}
    if not data.get("nemohermes_path"):
        raise SystemExit("refusing to bake: nemohermes not found on worker")
    if int(data.get("sandbox_count") or 0) < 1:
        raise SystemExit("refusing to bake: nemohermes reports no sandboxes")
    if not force and hb.get("status") != "ready":
        raise SystemExit(f"refusing to bake: heartbeat status is {hb.get('status')!r}, expected 'ready' (use --force to override)")
    log(f"validated worker {instance_id}: nemohermes={data.get('nemohermes_version') or data.get('nemohermes_path')} sandboxes={data.get('sandbox_count')} status={hb.get('status')}")
    return data


def scrub_for_image(ssm: Any, instance_id: str, *, keep_keys: bool) -> None:
    # Keep installed runtimes/sandbox/docker layers. Remove per-instance identity
    # and job residue so the next boot regenerates worker-keys.json and heartbeat.
    key_rm = "true" if keep_keys else "rm -rf /opt/worker/keys /mnt/broker/logs/worker-keys.json"
    command = f'''
set -eu
systemctl stop worker-poller.service 2>/dev/null || true
{key_rm}
rm -f /mnt/broker/logs/worker-heartbeat.json /mnt/broker/logs/worker-attestation.json 2>/dev/null || true
rm -rf /mnt/broker/jobs/inbox/* /mnt/broker/jobs/outbox/* /mnt/broker/jobs/sandbox-config/* 2>/dev/null || true
cloud-init clean --logs 2>/dev/null || true
sync
'''
    code, out, err = ssm_run(ssm, instance_id, command, timeout=120)
    if code != 0:
        raise SystemExit(f"scrub failed: {err or out}")
    log("scrubbed per-instance worker state before image capture")


def wait_image(ec2: Any, image_id: str, timeout: int = 1800) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        img = ec2.describe_images(ImageIds=[image_id])["Images"][0]
        state = img.get("State")
        log(f"AMI {image_id} state={state}")
        if state == "available":
            return
        if state == "failed":
            raise SystemExit(f"AMI {image_id} failed: {img}")
        time.sleep(20)
    raise SystemExit(f"AMI {image_id} did not become available within {timeout}s")


def apply_control_plane(ssm: Any, ec2: Any, image_id: str, explicit_instance: str | None, region: str) -> None:
    control_id = explicit_instance
    if not control_id:
        resp = ec2.describe_instances(
            Filters=[
                {"Name": "tag:Project", "Values": ["verdantforged"]},
                {"Name": "tag:Role", "Values": ["control-plane"]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        )
        matches = [i for r in resp.get("Reservations", []) for i in r.get("Instances", [])]
        if not matches:
            raise SystemExit("could not find running control-plane instance; pass --control-instance-id")
        matches.sort(key=lambda i: str(i.get("LaunchTime") or ""), reverse=True)
        control_id = matches[0]["InstanceId"]
    command = f'''
set -eu
CFG=/opt/broker-daemon/config.env
cp "$CFG" "$CFG.bak.$(date -u +%Y%m%dT%H%M%SZ)"
if grep -q '^BROKER_WORKER_AMI=' "$CFG"; then
    sed -i 's/^BROKER_WORKER_AMI=.*/BROKER_WORKER_AMI={image_id}/' "$CFG"
else
    printf '\nBROKER_WORKER_AMI={image_id}\n' >> "$CFG"
fi
systemctl restart verdantforged-broker-daemon.service
sleep 3
systemctl is-active verdantforged-broker-daemon.service
'''
    code, out, err = ssm_run(ssm, control_id, command, timeout=90)
    if code != 0:
        raise SystemExit(f"failed to apply AMI on control plane {control_id}: {err or out}")
    log(f"control plane {control_id} now uses BROKER_WORKER_AMI={image_id}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    ap.add_argument("--instance-id", help="ready worker instance to bake; defaults to newest running tee-worker")
    ap.add_argument("--name", help="AMI name; default includes timestamp and worker id")
    ap.add_argument("--description", default="VerdantForged worker with NemoClaw/Hermes sandbox preinstalled")
    ap.add_argument("--force", action="store_true", help="allow bake even if heartbeat status is not ready")
    ap.add_argument("--keep-keys", action="store_true", help="do not remove per-instance worker input keys before bake (not recommended)")
    ap.add_argument("--no-reboot", action="store_true", help="create image without reboot; faster but less filesystem-safe")
    ap.add_argument("--apply-control-plane", action="store_true", help="write BROKER_WORKER_AMI to control plane config.env and restart daemon")
    ap.add_argument("--control-instance-id", help="control-plane EC2 instance for --apply-control-plane")
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region)
    ec2 = session.client("ec2")
    ssm = session.client("ssm")

    instance_id = args.instance_id or discover_worker(ec2)
    validate_worker(ssm, instance_id, force=args.force)
    scrub_for_image(ssm, instance_id, keep_keys=args.keep_keys)

    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    safe_instance = re.sub(r"[^a-zA-Z0-9-]", "-", instance_id)
    name = args.name or f"verdantforged-nemoclaw-worker-{ts}-{safe_instance}"
    log(f"creating AMI from {instance_id}: {name}")
    resp = ec2.create_image(
        InstanceId=instance_id,
        Name=name,
        Description=args.description,
        NoReboot=bool(args.no_reboot),
        TagSpecifications=[{
            "ResourceType": "image",
            "Tags": [
                {"Key": "Name", "Value": name},
                {"Key": "Project", "Value": "verdantforged"},
                {"Key": "Role", "Value": "tee-worker-ami"},
                {"Key": "Contains", "Value": "nemoclaw-hermes-sandbox"},
                {"Key": "SourceInstance", "Value": instance_id},
            ],
        }],
    )
    image_id = resp["ImageId"]
    wait_image(ec2, image_id)
    log(f"AMI ready: {image_id}")

    if args.apply_control_plane:
        apply_control_plane(ssm, ec2, image_id, args.control_instance_id, args.region)

    print(json.dumps({"image_id": image_id, "source_instance_id": instance_id, "name": name}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
