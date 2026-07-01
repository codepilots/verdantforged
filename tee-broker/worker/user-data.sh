set -u
MARKER="/var/log/cloud-init-verdantforged-worker.marker"
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$MARKER"; }
touch "$MARKER"
EFS_DNS="__EFS_DNS__"
log "Worker setup starting (efs=$EFS_DNS)"
BOOT_START_EPOCH=$(date +%s)
BOOT_STAGES="packages efs attestation nemoclaw sandbox skills poller ready"
update_boot_stage() {
    local stage="$1" detail="${2:-}"
    local now epoch elapsed
    now=$(date -u +%FT%TZ)
    epoch=$(date +%s)
    elapsed=$((epoch - BOOT_START_EPOCH))
    local hb="/mnt/broker/logs/worker-heartbeat.json"
    mkdir -p /mnt/broker/logs 2>/dev/null || true
    python3 -c "
import json,sys
try:
    with open('$hb') as f: h=json.load(f)
except: h={}
h['boot_stage']='$stage'
h['boot_detail']='$detail'
h['boot_started_at']='$now'
h['boot_elapsed_seconds']=$elapsed
with open('$hb','w') as f: json.dump(h,f,indent=2)
" 2>/dev/null || true
}
update_boot_stage "starting" "Worker setup beginning"
log "step 1: packages"
update_boot_stage "packages" "Installing base packages"
for i in 1 2 3; do
    apt-get update -qq && apt-get install -y -qq nfs-common docker.io python3-pip python3-cryptography >/dev/null 2>&1 && break
    sleep 10
done
usermod -aG docker ubuntu 2>/dev/null || true
log "step 2: EFS"
update_boot_stage "efs" "Mounting EFS and creating directories"
mkdir -p /mnt/broker
sed -i '\#/mnt/broker nfs4#d' /etc/fstab
echo "$EFS_DNS:/ /mnt/broker nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2 0 0" >> /etc/fstab
EFS_OK=false
for i in 1 2 3 4 5; do
    mount -a 2>/dev/null && mountpoint -q /mnt/broker && { log "step 2: EFS mounted"; EFS_OK=true; break; }
    sleep 5
done
[ "$EFS_OK" = "true" ] || { log "step 2: FATAL — EFS not mounted"; exit 1; }
mkdir -p /opt/worker
if [ -f /mnt/broker/logs/worker-sev-snp.py ]; then
    install -m 0644 /mnt/broker/logs/worker-sev-snp.py /opt/worker/sev_snp.py
else
    log "step 2: FATAL — worker-sev-snp.py missing from EFS"
    exit 1
fi
log "step 3: heartbeat"
update_boot_stage "attestation" "Acquiring attestation and writing heartbeat"
IMDS_TOKEN=$(curl -sf --max-time 5 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null || echo "")
IMDS_HEADER=()
[ -n "$IMDS_TOKEN" ] && IMDS_HEADER=(-H "X-aws-ec2-metadata-token: $IMDS_TOKEN")
INSTANCE_ID=$(curl -sf --max-time 5 "http://169.254.169.254/latest/meta-data/instance-id" "${IMDS_HEADER[@]}" 2>/dev/null || echo unknown)
PRIVATE_IP=$(curl -sf --max-time 5 "http://169.254.169.254/latest/meta-data/local-ipv4" "${IMDS_HEADER[@]}" 2>/dev/null || echo 0.0.0.0)
[ -n "$IMDS_TOKEN" ] && log "step 3: IMDSv2 token acquired" || log "step 3: WARNING IMDSv2 unavailable, using 'unknown'"
python3 -c "import json,pathlib;p=pathlib.Path('/mnt/broker/logs/worker-heartbeat.json');h=json.loads(p.read_text())if p.exists()else{};h['instance_id']='$INSTANCE_ID';h['private_ip']='$PRIVATE_IP';p.write_text(json.dumps(h))" 2>/dev/null || true
log "step 3: attestation"
mkdir -p /opt/worker/keys /mnt/broker/logs
chmod 0700 /opt/worker/keys
INPUT_BINDING=$(python3 - "$INSTANCE_ID" <<'PY'
import base64,hashlib,json,sys;from pathlib import Path;from cryptography.hazmat.primitives import serialization;from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
i=sys.argv[1];k=Path('/opt/worker/keys/worker_input_x25519.priv')
p=X25519PrivateKey.from_private_bytes(k.read_bytes()) if k.exists() else X25519PrivateKey.generate()
if not k.exists():k.write_bytes(p.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()));k.chmod(0o600)
u=p.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
y=Path('/mnt/broker/logs/openshell-policy.yaml');h=hashlib.sha256(y.read_bytes()).hexdigest() if y.exists() else ''
b=hashlib.sha256(b'verdantforged-worker-input-v1\0'+u+b'\0'+(bytes.fromhex(h) if h else b'')).hexdigest()
Path('/mnt/broker/logs/worker-keys.json').write_text(json.dumps({'instance_id':i,'key_id':'wk_'+hashlib.sha256(u).hexdigest()[:16],'x25519_pubkey_b64':base64.b64encode(u).decode(),'policy_hash':h,'attestation_binding_sha256':b},indent=2))
print(b)
PY
)
export BROKER_ATTESTATION_REPORT_DATA_HEX="${INPUT_BINDING}$(printf '0%.0s' {1..64})"
SEV_ATT_JSON=$(python3 /opt/worker/sev_snp.py 2>/dev/null || echo "{}")
SEV_SOURCE=$(echo "$SEV_ATT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('source','stub'))" 2>/dev/null || echo "stub")
SEV_MEASUREMENT=$(echo "$SEV_ATT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('measurement',''))" 2>/dev/null || echo "")
SEV_REPORT=$(echo "$SEV_ATT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('report',''))" 2>/dev/null || echo "")
SEV_CERT_CHAIN=$(echo "$SEV_ATT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('cert_chain', [])))" 2>/dev/null || echo "[]")
SEV_CHIP_ID=$(echo "$SEV_ATT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('chip_id',''))" 2>/dev/null || echo "")
SEV_FAMILY_ID=$(echo "$SEV_ATT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('family_id',''))" 2>/dev/null || echo "")
SEV_REPORT_DATA=$(echo "$SEV_ATT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('report_data',''))" 2>/dev/null || echo "")
log "step 3: attestation source=$SEV_SOURCE measurement=${SEV_MEASUREMENT:0:20}..."
if [ -z "$SEV_MEASUREMENT" ] || [ "$SEV_MEASUREMENT" = "stub-no-measurement" ]; then
    SEV_MEASUREMENT=$(echo -n "$INSTANCE_ID" | sha256sum | awk '{print $1}')
    [ "$SEV_SOURCE" = "snpguest" ] || SEV_SOURCE="instance_id_sha256"
fi
mkdir -p /mnt/broker/logs /mnt/broker/jobs/inbox /mnt/broker/jobs/outbox

log "step 3: writing worker-attestation.json (source=$SEV_SOURCE)"
export SEV_SOURCE SEV_MEASUREMENT SEV_REPORT SEV_CERT_CHAIN SEV_CHIP_ID SEV_FAMILY_ID SEV_REPORT_DATA
ATTEST_EPOCH=$(date +%s)
python3 - "$INSTANCE_ID" "$ATTEST_EPOCH" <<PY
import json, os, pathlib, sys
instance_id, attest_epoch = sys.argv[1], sys.argv[2]
env = {
    'SEV_SOURCE': os.environ.get('SEV_SOURCE', ''),
    'SEV_MEASUREMENT': os.environ.get('SEV_MEASUREMENT', ''),
    'SEV_REPORT': os.environ.get('SEV_REPORT', ''),
    'SEV_REPORT_DATA': os.environ.get('SEV_REPORT_DATA', ''),
    'SEV_CHIP_ID': os.environ.get('SEV_CHIP_ID', ''),
    'SEV_FAMILY_ID': os.environ.get('SEV_FAMILY_ID', ''),
}
import_base = os.environ.get('SEV_CERT_CHAIN', '[]')
try:
    cert_chain = json.loads(import_base) if import_base else []
except Exception:
    cert_chain = []
record = {
    'instance_id': instance_id,
    'tee_type': 'amd-sev-snp',
    'measurement': env['SEV_MEASUREMENT'],
    'source': env['SEV_SOURCE'] or 'stub',
    'report': env['SEV_REPORT'],
    'report_data': env['SEV_REPORT_DATA'],
    'cert_chain': cert_chain,
    'chip_id': env['SEV_CHIP_ID'],
    'family_id': env['SEV_FAMILY_ID'],
    'measured_at': attest_epoch,
}
out = pathlib.Path('/mnt/broker/logs/worker-attestation.json')
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(record, indent=2))
local = pathlib.Path('/opt/worker/worker-attestation.json')
local.write_text(json.dumps(record, indent=2))
print(f"wrote {out} ({out.stat().st_size} bytes) source={record['source']}")
PY
ATT_STATUS=$?
[ "$ATT_STATUS" = "0" ] || log "step 3: ERROR python write of worker-attestation.json exited $ATT_STATUS"
ls -la /mnt/broker/logs/worker-attestation.json /opt/worker/worker-attestation.json
log "step 3: done"

log "step 4: NemoClaw"
update_boot_stage "nemoclaw" "Installing NemoClaw runtime"
curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
apt-get install -y -qq nodejs >/dev/null 2>&1
log "step 4: Node $(node --version 2>/dev/null || echo missing)"
export HOME=/root
export NEMOCLAW_AGENT=hermes
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
export NEMOCLAW_SANDBOX_NAME=worker
export NEMOCLAW_PROVIDER=custom
export NEMOCLAW_ENDPOINT_URL="https://verdant.codepilots.co.uk/v1/llm"
export NEMOCLAW_MODEL="minimax-m3:cloud"
export COMPATIBLE_API_KEY="${BROKER_ONBOARD_TOKEN:-onboard-placeholder}"
export NEMOCLAW_PROVIDER_KEY="${BROKER_ONBOARD_TOKEN:-onboard-placeholder}"
log "step 4: NemoClaw install via official installer"
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_PROVIDER=custom
export NEMOCLAW_AGENT=hermes
export NEMOCLAW_SANDBOX_NAME="${NEMOCLAW_SANDBOX_NAME:-tee-worker}"
export NEMOCLAW_NO_EXPRESS=1
export NEMOCLAW_NO_OLLAMA_AUTOSTART=1
export HOME=/root
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
find_nemohermes() {
    hash -r 2>/dev/null || true
    for cand in /usr/bin/nemohermes /usr/local/bin/nemohermes "$HOME/.local/bin/nemohermes"; do
        if [ -x "$cand" ]; then
            printf '%s\n' "$cand"
            return 0
        fi
    done
    command -v nemohermes 2>/dev/null || return 1
}
NM_OK=false
NEMOH_PATH="$(find_nemohermes || true)"
if [ -n "$NEMOH_PATH" ]; then
    log "step 4: prebaked NemoClaw detected at $NEMOH_PATH — skipping installer download"
    NM_OK=true
else
    for i in 1 2; do
        log "step 4: NemoClaw install attempt $i"
        timeout 1800 bash -c 'set -o pipefail; curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash -s -- --non-interactive --yes-i-accept-third-party-software 2>&1 | tail -20' || log "step 4: NemoClaw install attempt $i timed out/failed"
        NEMOH_PATH="$(find_nemohermes || true)"
        if [ -n "$NEMOH_PATH" ]; then
            log "step 4: nemohermes found at $NEMOH_PATH"
            NM_OK=true
            break
        fi
        log "step 4: install attempt $i did not produce nemohermes, retrying in 15s"
        sleep 15
    done
fi
if [ "$NM_OK" = "true" ]; then
    log "step 4: NemoClaw installed, nemohermes at $(command -v nemohermes)"
    sleep 5
    SBX=$(nemohermes list --json 2>/dev/null | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('sandboxes',[])))" 2>/dev/null || echo 0)
    if [ "$SBX" = "0" ]; then
        log "step 4: no sandbox after onboard — running onboard explicitly"
        timeout 1200 nemohermes onboard --non-interactive --yes --yes-i-accept-third-party-software 2>&1 | tail -20 || log "step 4: explicit onboard timed out/failed"
        sleep 5
        SBX=$(nemohermes list --json 2>/dev/null | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('sandboxes',[])))" 2>/dev/null || echo 0)
    fi
    if [ "$SBX" != "0" ]; then
        log "step 4: NemoClaw sandbox OK ($SBX sandbox(es))"
        update_boot_stage "sandbox" "Sandbox created, installing skills"
        echo "$NEMOCLAW_SANDBOX_NAME" > /opt/worker/.nemoclaw_sandbox_name
        log "step 4: adding OpenShell local-inference policy"
        nemohermes sandbox policy add "$NEMOCLAW_SANDBOX_NAME" local-inference --yes >/tmp/nemoclaw-policy-local-inference.log 2>&1 || \
            log "step 4: WARNING local-inference policy add failed (see /tmp/nemoclaw-policy-local-inference.log)"
        mkdir -p /etc/systemd/system/worker-poller.service.d
        cat > /etc/systemd/system/worker-poller.service.d/nemoclaw.conf <<EOF
[Service]
Environment=NEMOCLAW_SANDBOX_NAME=$NEMOCLAW_SANDBOX_NAME
EOF
        systemctl daemon-reload
        NEMOCLAW_VERSION="$(nemohermes --version 2>/dev/null | head -1 || true)"
        [ -z "$NEMOCLAW_VERSION" ] && {
            log "step 4b: WARNING nemohermes --version not available, using 'unknown'"
            NEMOCLAW_VERSION="unknown"
        }
        NEMOCLAW_IMAGE="$(docker ps -a \
            --filter "label=openshell.ai/sandbox-name=$NEMOCLAW_SANDBOX_NAME" \
            --format '{{.Image}}' 2>/dev/null | head -1 || true)"
        if [ -z "$NEMOCLAW_IMAGE" ]; then
            NEMOCLAW_IMAGE="$(nemohermes list --json 2>/dev/null | \
                python3 -c "import json,sys; d=json.load(sys.stdin); \
                    sb=d.get('sandboxes',[{}])[0]; print(sb.get('image','unknown'))" \
                    2>/dev/null || echo unknown)"
        fi
        if [ "$NEMOCLAW_IMAGE" = "unknown" ] || [ -z "$NEMOCLAW_IMAGE" ]; then
            NEMOCLAW_IMAGE_DIGEST="unknown"
        else
            NEMOCLAW_IMAGE_DIGEST="$(docker image inspect "$NEMOCLAW_IMAGE" \
                --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null \
                | sed -n 's/^.*@//p' | head -1 || true)"
            if [ -z "$NEMOCLAW_IMAGE_DIGEST" ]; then
                NEMOCLAW_IMAGE_DIGEST="$(docker image inspect "$NEMOCLAW_IMAGE" \
                    --format '{{.Id}}' 2>/dev/null || true)"
            fi
            if [ -z "$NEMOCLAW_IMAGE_DIGEST" ]; then
                NEMOCLAW_IMAGE_DIGEST="$(docker images --digests "$NEMOCLAW_IMAGE" \
                    --format '{{.Digest}}' 2>/dev/null | grep -v '^<none>$' | head -1 || true)"
            fi
            [ -z "$NEMOCLAW_IMAGE_DIGEST" ] && {
                log "step 4b: WARNING docker did not return a digest for $NEMOCLAW_IMAGE"
                NEMOCLAW_IMAGE_DIGEST="unknown"
            }
        fi
        log "step 4b: nemoclaw_version=$NEMOCLAW_VERSION"
        log "step 4b: image=$NEMOCLAW_IMAGE digest=$NEMOCLAW_IMAGE_DIGEST"
        export NEMOCLAW_VERSION NEMOCLAW_IMAGE NEMOCLAW_IMAGE_DIGEST
        python3 -c 'import json,os,datetime;print(json.dumps({"nemoclaw_version":os.environ.get("NEMOCLAW_VERSION","unknown"),"nemoclaw_image":os.environ.get("NEMOCLAW_IMAGE","unknown"),"nemoclaw_image_digest":os.environ.get("NEMOCLAW_IMAGE_DIGEST","unknown"),"captured_at":datetime.datetime.utcnow().replace(microsecond=0).isoformat()+"Z"},indent=2))' > /opt/worker/.nemoclaw_metadata
        chmod 0644 /opt/worker/.nemoclaw_metadata
        log "step 4b: metadata written to /opt/worker/.nemoclaw_metadata"
        if [ -d /mnt/broker/logs/skills ]; then
            for skill_dir in /mnt/broker/logs/skills/*/; do
                [ -d "$skill_dir" ] || continue
                sname=$(basename "$skill_dir")
                nemohermes "$NEMOCLAW_SANDBOX_NAME" skill install "$skill_dir" 2>&1 | tail -3 \
                    && log "step 4: skill $sname installed" \
                    || log "step 4: WARNING skill $sname install failed"
            done
        fi
        if [ -f /mnt/broker/logs/worker-agent.py ]; then
            cp /mnt/broker/logs/worker-agent.py /opt/worker/worker-agent.py
            chmod 0755 /opt/worker/worker-agent.py
            nemohermes "$NEMOCLAW_SANDBOX_NAME" exec --no-tty --timeout 60 \
                -- bash -c 'mkdir -p /sandbox && cat > /sandbox/worker-agent.py && chmod 0755 /sandbox/worker-agent.py' \
                < /opt/worker/worker-agent.py 2>/dev/null || true
            log "step 4: worker-agent.py loaded into sandbox"
        fi
    else
        log "step 4: FATAL — no sandbox after onboard"
        update_boot_stage "failed" "NemoClaw installed but no sandbox was created"
        exit 1
    fi
else
    log "step 4: FATAL — NemoClaw install failed"
    log "step 4: jobs will fail-closed until NemoClaw is available"
    update_boot_stage "failed" "NemoClaw install failed"
    exit 1
fi
log "step 5: poller"
update_boot_stage "poller" "Installing poller service"
mkdir -p /opt/worker
if [ -f /mnt/broker/logs/worker-poller.py ]; then
    cp /mnt/broker/logs/worker-poller.py /opt/worker/poller.py
    chmod +x /opt/worker/poller.py
    log "step 5: poller installed from EFS"
else
    log "step 5: FATAL — poller not found on EFS"
    exit 1
fi
cat > /etc/systemd/system/worker-poller.service <<SVCEOF
[Unit]
Description=VerdantForged TEE Worker job poller
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
Environment=BROKER_ARTIFACT_BUCKET=__ARTIFACT_BUCKET__
Environment=BROKER_ARTIFACT_REGION=__ARTIFACT_REGION__
ExecStart=/usr/bin/python3 /opt/worker/poller.py
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload
systemctl enable --now worker-poller
sleep 2
systemctl is-active --quiet worker-poller && log "step 5: poller active" || log "step 5: WARNING poller not active"
update_boot_stage "ready" "Worker setup complete, poller active"
log "Worker setup complete"
heartbeat_status=$(grep -oP '"status": "\K[^"]+' /mnt/broker/logs/worker-heartbeat.json 2>/dev/null || echo unknown)
log "heartbeat status: $heartbeat_status"
