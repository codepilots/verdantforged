#!/bin/bash
# VerdantForged Skill Library — full deploy.
# Run on the control plane (i-05117b9649db5b343 in eu-west-1) as root.
#
# Idempotent: safe to re-run after a partial deploy.
#
# Steps:
#   1. Pull skill_library/ + systemd unit + scripts/ from S3
#   2. Ensure Python deps installed (fastapi + uvicorn + pydantic + httpx)
#   3. Generate SKILL_LIBRARY_API_KEY if absent
#   4. Wire env vars into /opt/broker-daemon/config.env
#   5. Create /mnt/broker/skill-library/{db,files}
#   6. Install systemd unit + reload daemon
#   7. Merge skill-library routes into /etc/caddy/Caddyfile (idempotent)
#   8. Validate + reload Caddy (no broker restart)
#   9. Push existing skills via scripts/push_skills.sh
#  10. Smoke test public endpoints
#
# Usage:
#   sudo ./scripts/deploy_skill_library.sh
#
# Or from a remote operator machine via SSM:
#   aws ssm send-command --instance-ids i-05117b9649db5b343 \
#     --document-name AWS-RunShellScript \
#     --parameters commands="aws s3 cp s3://verdantforged-artifacts-eu-west-1/deploy_skill_library.sh /tmp/deploy_skill_library.sh && sudo bash /tmp/deploy_skill_library.sh"
set -euo pipefail

# ----- Config (overridable via env) -----
REPO_DIR="${REPO_DIR:-/home/ubuntu/tee-broker-deploy}"
BR_DEPLOY="${BR_DEPLOY:-/opt/broker-daemon}"
BR_EFS="${BR_EFS:-/mnt/broker}"
REGION="${BROKER_REGION:-${AWS_REGION:-eu-west-1}}"
ARTIFACT_BUCKET="${BROKER_ARTIFACT_BUCKET:-verdantforged-artifacts-${REGION}}"
DOMAIN="${BROKER_DOMAIN:-verdant.codepilots.co.uk}"
LOG="$BR_EFS/logs/skill-library-deploy.log"
mkdir -p "$BR_EFS/logs"
chmod 0755 "$BR_DEPLOY" 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1
echo "=== skill-library deploy $(date -u +%FT%TZ) ==="

# ----- Pre-flight -----
if [[ "$(id -u)" -ne 0 ]]; then
    echo "must be root (or run with sudo)" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null; then
    echo "systemctl not found — must be run on a systemd-based Linux" >&2
    exit 1
fi
if ! command -v caddy >/dev/null && ! command -v /usr/bin/caddy >/dev/null; then
    echo "WARNING: caddy not on PATH — Caddy integration will be skipped." >&2
    SKIP_CADDY=1
else
    SKIP_CADDY=0
fi

# ----- 1. Pull code from S3 -----
echo "=== 1. Pull skill_library/ + scripts + systemd unit from S3 ==="
# The repo itself is in /home/ubuntu/tee-broker-deploy (or wherever REPO_DIR points).
# On a fresh instance, sync from the artifact bucket first.
if [[ ! -d "$REPO_DIR/skill_library" ]]; then
    echo "  pulling skill_library/ from s3://$ARTIFACT_BUCKET/tmp/skill_library/"
    mkdir -p "$REPO_DIR"
    aws s3 cp --recursive "s3://$ARTIFACT_BUCKET/tmp/skill_library/" "$REPO_DIR/skill_library/"
fi
if [[ ! -f "$REPO_DIR/scripts/push_skills.sh" ]]; then
    echo "  pulling scripts/push_skills.sh"
    mkdir -p "$REPO_DIR/scripts"
    aws s3 cp "s3://$ARTIFACT_BUCKET/tmp/push_skills.sh" "$REPO_DIR/scripts/push_skills.sh"
    chmod +x "$REPO_DIR/scripts/push_skills.sh"
fi

# Deploy the live code from REPO_DIR into BR_DEPLOY (only the library bits — leave
# daemon.py / poller.py untouched).
mkdir -p "$BR_DEPLOY/skill_library"
cp -r "$REPO_DIR/skill_library/." "$BR_DEPLOY/skill_library/"
[[ -f "$REPO_DIR/scripts/push_skills.sh" ]] && install -m 0755 "$REPO_DIR/scripts/push_skills.sh" "$BR_DEPLOY/scripts/push_skills.sh"

# ----- 2. Python deps -----
echo "=== 2. Install Python deps ==="
# PEP 668 on Ubuntu 24.04 forces --break-system-packages (system pip controls deps,
# not the agent's venv).
if ! python3 -c "import fastapi, uvicorn, pydantic, httpx" 2>/dev/null; then
    echo "  installing fastapi + uvicorn + pydantic + httpx via pip"
    python3 -m pip install --break-system-packages --quiet \
        fastapi==0.115.0 uvicorn==0.30.6 pydantic==2.9.2 httpx==0.27.2 2>&1 | tail -3
fi

# ----- 3. API key -----
echo "=== 3. Generate SKILL_LIBRARY_API_KEY if absent ==="
if ! grep -q "^SKILL_LIBRARY_API_KEY=" "$BR_DEPLOY/config.env" 2>/dev/null; then
    KEY="sklib_$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    echo "  generating new key"
    {
        echo ""
        echo "# Skill Library API key — generate with: python3 -c 'import secrets; print(\"SKILL_LIBRARY_API_KEY=\" + secrets.token_urlsafe(48))'"
        echo "SKILL_LIBRARY_API_KEY=$KEY"
        echo "SKILL_LIBRARY_DB=$BR_EFS/skill-library/db/skill_library.db"
        echo "SKILL_LIBRARY_FILES_DIR=$BR_EFS/skill-library/files"
    } >> "$BR_DEPLOY/config.env"
    chmod 600 "$BR_DEPLOY/config.env"
    echo "  key written to $BR_DEPLOY/config.env (chmod 600)"
else
    echo "  SKILL_LIBRARY_API_KEY already present in config.env"
fi

# ----- 4. Ensure DB/files paths exist -----
echo "=== 4. Create DB + files dirs ==="
mkdir -p "$BR_EFS/skill-library/db" "$BR_EFS/skill-library/files"
chmod 0755 "$BR_EFS/skill-library" "$BR_EFS/skill-library/db" "$BR_EFS/skill-library/files"

# ----- 5. Install systemd unit -----
echo "=== 5. Install systemd unit + start service ==="
install -m 0644 "$REPO_DIR/systemd/skill-library.service" /etc/systemd/system/skill-library.service
systemctl daemon-reload
systemctl enable skill-library.service
if systemctl is-active --quiet skill-library.service; then
    systemctl restart skill-library.service
else
    systemctl start skill-library.service
fi
sleep 3
if ! systemctl is-active --quiet skill-library.service; then
    echo "ERROR: skill-library.service failed to start" >&2
    journalctl -u skill-library.service -n 30 --no-pager
    exit 1
fi
echo "  service active"

# ----- 6. Caddy integration -----
if [[ $SKIP_CADDY -eq 0 ]]; then
    echo "=== 6. Merge library routes into Caddyfile ==="
    NEEDS_LIBS=false
    if ! grep -q "handle_path /library" /etc/caddy/Caddyfile; then
        NEEDS_LIBS=true
    fi
    if [[ "$NEEDS_LIBS" == "true" ]]; then
        cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%s)
        # Insert the library routes before the closing brace of each site block.
        # We use sed to do an in-place insertion right before the closing
        # braces of the two site blocks. Pattern matches the FIRST site (with
        # {$BROKER_DOMAIN...}) and the SECOND (the :80 block).
        python3 <<'PY'
import re, sys
with open('/etc/caddy/Caddyfile') as f:
    src = f.read()
# Library routes to inject before the closing `}` of each site block.
LIB_BLOCK = '''    # Skill Library service (deployed via scripts/deploy_skill_library.sh).
    # handle_path strips /library prefix so callers hit /library/v1/... which
    # becomes http://127.0.0.1:8091/v1/... in the library. Same CORS origin
    # as the broker UI (verdant.codepilots.co.uk). Reads are public; writes
    # (POST/DELETE) require Authorization: Bearer *** library_docs exposes the
    # library's interactive Swagger UI.
    handle_path /library/* {
        reverse_proxy 127.0.0.1:8091
    }
    handle_path /library-docs/* {
        reverse_proxy 127.0.0.1:8091
    }
'''
# Insert before the first occurrence of the closing brace of the HTTPS site
# (after the @preflight handle) AND before the closing brace of the :80 block.
# Simple approach: replace the LAST `handle / {` block in the HTTPS site (right
# before its closing `}`) — we add LIB_BLOCK before that `}` in BOTH sites.
# Heuristic: insert LIB_BLOCK before the first standalone `}` that follows a
# `handle / {` block.
# Cleaner: replace each `handle / {` (the file_server block at the site root)
# with LIB_BLOCK + handle / {.
src = src.replace('handle / {\n        root * /opt/broker-daemon/static\n        file_server\n    }',
                  LIB_BLOCK + '    handle / {\n        root * /opt/broker-daemon/static\n        file_server\n    }')
with open('/etc/caddy/Caddyfile', 'w') as f:
    f.write(src)
PY
        # Validate
        if caddy adapt --config /etc/caddy/Caddyfile --envfile "$BR_DEPLOY/config.env" > /tmp/caddy_adapted.json 2>/tmp/caddy_adapt_err.txt; then
            echo "  caddy config valid — reloading"
            systemctl reload caddy
            sleep 2
        else
            echo "ERROR: caddy config invalid; reverting" >&2
            cat /tmp/caddy_adapt_err.txt
            cp /etc/caddy/Caddyfile.bak.$(ls -t /etc/caddy/Caddyfile.bak.* | head -1 | xargs basename | sed 's/.*\.bak\.//') /etc/caddy/Caddyfile
            exit 1
        fi
    else
        echo "  library routes already present in Caddyfile"
    fi
fi

# ----- 7. Push existing skills -----
echo "=== 7. Push existing skills ==="
if [[ -d "$BR_DEPLOY/worker/skills" ]]; then
    KEY=$(grep "^SKILL_LIBRARY_API_KEY=" "$BR_DEPLOY/config.env" | cut -d= -f2-)
    cd "$BR_DEPLOY/worker/skills"
    bash "$BR_DEPLOY/scripts/push_skills.sh" \
        --source-dir . \
        --library-url "http://127.0.0.1:8091" \
        --api-key "$KEY" || echo "  push_skills.sh exited non-zero — investigate"
else
    echo "  no worker/skills/ dir on this host; skipping"
fi

# ----- 8. Smoke test -----
echo "=== 8. Smoke test ==="
echo "  internal /healthz:"
curl -sS http://127.0.0.1:8091/healthz
echo
echo "  public /library/healthz:"
curl -sS "https://$DOMAIN/library/healthz"
echo
echo "  public /library/v1/library/skills:"
curl -sS "https://$DOMAIN/library/v1/library/skills"

echo "=== deploy complete $(date -u +%FT%TZ) ==="
echo "next: Hermes skill scripts at ~/.hermes/skills/devops/skill-library-browse/ now point at https://$DOMAIN/library"