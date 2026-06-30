#!/bin/bash
# swap-dns-ireland.sh — swap verdant.codepilots.co.uk from London
# (eu-west-2, 52.56.93.209) to Ireland (eu-west-1, 176.34.244.180).
#
# Requires Cloudflare API token in $CLOUDFLARE_API_TOKEN with
# Zone:DNS:Edit permission for codepilots.co.uk.
#
# Run this ONCE after confirming Ireland broker is healthy
# (curl http://176.34.244.180/healthz returns ok:true).

set -euo pipefail

if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
    echo "ERROR: set CLOUDFLARE_API_TOKEN (Cloudflare Dashboard → My Profile → API Tokens → Create Token)"
    echo "  Required scope: Zone:DNS:Edit on zone codepilots.co.uk"
    exit 1
fi

ZONE_NAME="codepilots.co.uk"
RECORD_NAME="verdant.codepilots.co.uk"
NEW_IP="176.34.244.180"   # eu-west-1 EIP
OLD_IP="52.56.93.209"     # eu-west-2 EIP (will be released after teardown)

echo "=== Updating $RECORD_NAME → $NEW_IP ==="

# Find zone ID
ZONE_ID=$(curl -sS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    "https://api.cloudflare.com/client/v4/zones?name=$ZONE_NAME" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result'][0]['id'])")
echo "zone id: $ZONE_ID"

# Find existing A record
RECORD_ID=$(curl -sS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records?type=A&name=$RECORD_NAME" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result'][0]['id'])")
echo "record id: $RECORD_ID"

# Update the record
RESPONSE=$(curl -sS -X PUT \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    -H "Content-Type: application/json" \
    --data "{\"type\":\"A\",\"name\":\"$RECORD_NAME\",\"content\":\"$NEW_IP\",\"ttl\":1,\"proxied\":false}" \
    "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records/$RECORD_ID")
echo "$RESPONSE" | python3 -m json.tool

echo ""
echo "=== Verifying ==="
sleep 5
for i in 1 2 3 4 5; do
    RESOLVED=$(dig +short "$RECORD_NAME" A @1.1.1.1)
    echo "  attempt $i: $RESOLVED"
    if [ "$RESOLVED" = "$NEW_IP" ]; then
        echo "✓ DNS updated"
        break
    fi
    sleep 5
done

echo ""
echo "=== Next ==="
echo "Wait ~30s for Let's Encrypt cert to be issued on $NEW_IP, then:"
echo "  curl https://$RECORD_NAME/healthz"
