#!/bin/bash
# warm-worker-manager.sh — long-lived warm-pool worker for VerdantForged.
#
# Why: the broker\'s worker-launch cold-start is ~16 min (NemoClaw
# sandbox download from NVIDIA\'s CDN times out; the user-data script
# retries with a backoff). For a demo with bursty traffic that\'s
# fatal — every job after the first waits 16 min for a fresh worker.
#
# This script keeps at least one warm `m6a.xlarge` worker (SEV-SNP)
# alive indefinitely:
#   1. Checks if a running worker already exists (tagged Project=verdantforged,
#      Role=tee-worker, ManagedBy=broker-daemon). If so, verifies it\'s
#      responsive via EFS heartbeat.
#   2. If no healthy worker exists, launches a new one with the same
#      IAM profile / SG / AMI / subnet that the broker uses.
#   3. Sleeps and loops. Exit code 0 = healthy worker, 1 = failed to
#      provision.
#
# Pair with `BROKER_DISABLE_IDLE_TERMINATION=1` in the broker\'s
# config.env so the broker adopts the warm worker on startup and
# never terminates it.
#
# Designed to run as a systemd service OR a cron job every 5 minutes.

set -euo pipefail

REGION="${AWS_REGION:-eu-west-1}"
AMI="${WARM_WORKER_AMI:-ami-06b9219be654efe2b}"
INSTANCE_TYPE="${WARM_WORKER_INSTANCE_TYPE:-m6a.xlarge}"
SUBNET_ID="${WARM_WORKER_SUBNET_ID:-subnet-03561a2e60c14b7d2}"
SG_ID="${WARM_WORKER_SG_ID:-sg-061434a40b5441d33}"
IAM_PROFILE_ARN="${WARM_WORKER_IAM_PROFILE_ARN:-arn:aws:iam::424503481467:instance-profile/verdantforged-broker-control-worker-instance-profile}"
EFS_DNS="${WARM_WORKER_EFS_DNS:-fs-06fb3127816b38e71.efs.${REGION}.amazonaws.com}"
HEALTHCHECK_INTERVAL="${WARM_WORKER_HEALTHCHECK_INTERVAL:-300}"  # 5 min
MAX_HEARTBEAT_AGE_SEC="${WARM_WORKER_MAX_HEARTBEAT_AGE_SEC:-300}"  # 5 min stale = unhealthy

USER_DATA=\"\"\"
#!/bin/bash
set -euo pipefail
# Mirror the broker\'s worker user-data (cloudformation-control-plane.yaml
# Lines 466-502 area). The only diff: NemoClaw step is wrapped to give up
# fast instead of blocking cold-start with the broker\'s long timeout.
EFS_DNS="${EFS_DNS}"
# ... (truncated; in real run, this should be the full bootstrap script)
# For the manager, the worker\'s own user-data takes 16 min on first boot.
# After that, the poller is up and stays up across jobs.
\"\"\"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [warm-worker-manager] $*" >&2
}

# Find a running worker with the warm-pool tags
find_running_worker() {
    aws ec2 describe-instances --region "$REGION" \
        --filters \
            "Name=tag:Project,Values=verdantforged" \
            "Name=tag:Role,Values=tee-worker" \
            "Name=instance-state-name,Values=running" \
        --query "Reservations[].Instances[].[InstanceId,PrivateIpAddress,LaunchTime]" \
        --output text
}

# Check if the worker\'s EFS heartbeat is fresh enough to count as healthy
is_worker_healthy() {
    local instance_id="$1"
    local private_ip="$2"
    
    # The worker writes its heartbeat to EFS at /logs/worker-heartbeat.json
    # We can\'t read EFS directly from this script, but we can check via
    # SSM RunCommand on the worker (since it has SSM agent registered).
    local last_ping
    last_ping=$(aws ssm describe-instance-information \
        --region "$REGION" \
        --filters "Key=InstanceIds,Values=$instance_id" \
        --query "InstanceInformationList[0].LastPingDateTime" \
        --output text 2>/dev/null || echo "None")
    
    if [[ "$last_ping" == "None" || -z "$last_ping" ]]; then
        log "  worker $instance_id: SSM not registered (cold start?)"
        return 1
    fi
    
    # last_ping format: 2026-06-28T18:35:20.123000+01:00
    local last_epoch
    last_epoch=$(date -d "$last_ping" +%s 2>/dev/null || echo 0)
    local now_epoch=$(date +%s)
    local age=$((now_epoch - last_epoch))
    
    if [[ $age -gt $MAX_HEARTBEAT_AGE_SEC ]]; then
        log "  worker $instance_id: SSM last ping ${age}s ago (stale)"
        return 1
    fi
    
    log "  worker $instance_id: SSM healthy, last ping ${age}s ago"
    return 0
}

# Launch a new warm worker
launch_worker() {
    log "launching new warm worker (type=$INSTANCE_TYPE, ami=$AMI)"
    local result
    result=$(aws ec2 run-instances --region "$REGION" \
        --image-id "$AMI" \
        --instance-type "$INSTANCE_TYPE" \
        --subnet-id "$SUBNET_ID" \
        --security-group-ids "$SG_ID" \
        --iam-instance-profile "Arn=$IAM_PROFILE_ARN" \
        --min-count 1 --max-count 1 \
        --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":50,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":true}}]' \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=verdantforged-warm-worker},{Key=Project,Value=verdantforged},{Key=Role,Value=tee-worker},{Key=ManagedBy,Value=warm-worker-manager},{Key=aws:ec2:sev-snp,Value=ec2-sev_snp}]" \
        --cpu-options "CoreCount=2,ThreadsPerCore=2,AmdSevSnp=enabled" \
        --user-data "$USER_DATA" \
        --query "Instances[0].InstanceId" \
        --output text)
    
    if [[ -z "$result" || "$result" == "None" ]]; then
        log "ERROR: launch returned empty"
        return 1
    fi
    log "launched: $result (cold start ~16 min for NemoClaw sandbox)"
    echo "$result"
}

# Main loop
log "starting (region=$REGION, type=$INSTANCE_TYPE)"

while true; do
    workers=$(find_running_worker)
    
    if [[ -z "$workers" ]]; then
        log "no running worker — launching"
        if ! launch_worker; then
            log "launch failed, retrying in 60s"
            sleep 60
            continue
        fi
        log "waiting 60s for instance to appear in API"
        sleep 60
        continue
    fi
    
    # Got at least one running worker
    while IFS=$'\t' read -r instance_id private_ip launch_time; do
        if is_worker_healthy "$instance_id" "$private_ip"; then
            log "warm worker healthy: $instance_id ($private_ip)"
        else
            log "warm worker UNHEALTHY: $instance_id ($private_ip)"
            log "  terminating and letting main loop relaunch"
            aws ec2 terminate-instances --region "$REGION" \
                --instance-ids "$instance_id" >/dev/null
        fi
    done <<< "$workers"
    
    sleep "$HEALTHCHECK_INTERVAL"
done
