#!/bin/bash
# Ad-hoc verification for the LLM integration in worker/user-data.sh.
# Tests that execute_in_envelope:
#   1. Reads the API key from /mnt/broker/logs/llm-api-key
#   2. Calls the LLM API and returns real output (not a stub)
#   3. Includes model name in result
#   4. Includes token usage in result
#   5. Includes attestation with measurement
#   6. Handles missing API key gracefully
#   7. Handles LLM API error gracefully
# Uses a mock HTTP server to avoid needing real credentials.

set -uo pipefail

WORK=$(mktemp -d -t hermes-verify-llm-XXXXXX)
trap "rm -rf '$WORK'" EXIT
echo "[verify] working dir: $WORK"

USER_DATA="/home/autumn/hermes/competition/tee-broker-deploy/worker/user-data.sh"
POLLER_SRC="/home/autumn/hermes/competition/tee-broker-deploy/worker/poller.py"
if [ ! -f "$USER_DATA" ] || [ ! -f "$POLLER_SRC" ]; then
    echo "[FAIL] source file not found"
    exit 1
fi

# Poller is now a standalone file, not embedded in user-data
POLLER_PY="$WORK/poller.py"
cp "$POLLER_SRC" "$POLLER_PY"

if [ ! -s "$POLLER_PY" ]; then
    echo "[FAIL] could not extract poller.py from user-data.sh"
    exit 1
fi

echo "[verify] extracted poller.py ($(wc -l < "$POLLER_PY") lines)"

# Patch paths to use our mock dirs
MOCK_ROOT="$WORK/broker"
INBOX="$MOCK_ROOT/jobs/inbox"
OUTBOX="$MOCK_ROOT/jobs/outbox"
LOGS="$MOCK_ROOT/logs"
mkdir -p "$INBOX" "$OUTBOX" "$LOGS"

sed -i "s|/mnt/broker/jobs/inbox|$INBOX|g" "$POLLER_PY"
sed -i "s|/mnt/broker/jobs/outbox|$OUTBOX|g" "$POLLER_PY"
sed -i "s|/mnt/broker/logs/worker-heartbeat.json|$LOGS/worker-heartbeat.json|g" "$POLLER_PY"
sed -i "s|/mnt/broker/logs/llm-api-key|$LOGS/llm-api-key|g" "$POLLER_PY"

# Pre-create heartbeat
echo '{"instance_id":"i-test","status":"starting"}' > "$LOGS/worker-heartbeat.json"

PASS=0
FAIL=0

check() {
    local label="$1" condition="$2"
    if eval "$condition"; then
        echo "[PASS] $label"
        PASS=$((PASS+1))
    else
        echo "[FAIL] $label"
        FAIL=$((FAIL+1))
    fi
}

# --- Static checks on the extracted poller ---
check "1. poller reads llm-api-key from EFS" "grep -q 'llm-api-key' '$POLLER_PY'"
check "2. poller uses urllib.request (stdlib, no pip install needed)" "grep -q 'urllib.request' '$POLLER_PY'"
check "3. poller has skill_prompts dict" "grep -q 'skill_prompts' '$POLLER_PY'"
check "4. poller has code-review prompt" "grep -q 'code-review' '$POLLER_PY'"
check "5. poller has summarize prompt" "grep -q 'summarize' '$POLLER_PY'"
check "6. poller has photo-glow-up prompt" "grep -q 'photo-glow-up' '$POLLER_PY'"
check "7. poller result includes model field" "grep -q '\"model\"' '$POLLER_PY'"
check "8. poller result includes usage field" "grep -q 'usage' '$POLLER_PY'"
check "9. poller handles missing API key" "grep -q 'no_llm_path' '$POLLER_PY'"
check "10. poller handles LLM error" "grep -q 'llm_error' '$POLLER_PY'"
check "11. poller does NOT have stub note" "! grep -q 'stub.*NemoClaw' '$POLLER_PY'"
check "12. poller calls chat/completions endpoint" "grep -q 'chat/completions' '$POLLER_PY'"

# --- Functional test: mock API key + mock HTTP server ---
echo ""
echo "=== Functional tests (mock HTTP) ==="

# Write a fake API key
echo "test-api-key-12345" > "$LOGS/llm-api-key"

# Create a mock LLM response server using Python
MOCK_SERVER="$WORK/mock_server.py"
cat > "$MOCK_SERVER" <<'MOCKEOF'
import http.server, json, sys, threading

class MockLLMHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if "/chat/completions" in self.path:
            # Check auth
            auth = self.headers.get("Authorization", "")
            if "test-api-key-12345" not in auth:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"Invalid API key"}}')
                return
            # Return mock LLM response
            resp = {
                "id": "chatcmpl-mock-001",
                "model": "minimax-m3:cloud",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "This is a mock LLM response for testing."},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 12, "completion_tokens": 15, "total_tokens": 27}
            }
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # suppress logs

server = http.server.HTTPServer(("127.0.0.1", 18765), MockLLMHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
print("mock server on :18765", flush=True)
import time; time.sleep(30)
MOCKEOF

# Start mock server in background
python3 "$MOCK_SERVER" &
MOCK_PID=$!
sleep 1

# Patch the poller to use our mock server URL
sed -i "s|https://ollama.com/v1|http://127.0.0.1:18765/v1|g" "$POLLER_PY"

# Write a test job
JOB_ID="job_llm_test_001"
echo "{\"job_id\":\"$JOB_ID\",\"encrypted_skill\":\"summarize\",\"encrypted_data\":\"This is test content to summarize.\"}" > "$INBOX/${JOB_ID}.json"

# Run poller
timeout 5 python3 "$POLLER_PY" >/dev/null 2>&1 || true

OUTBOX_FILE="$OUTBOX/${JOB_ID}.json"
check "13. outbox file created with LLM call" "[ -f '$OUTBOX_FILE' ]"

if [ -f "$OUTBOX_FILE" ]; then
    check "14. outbox has job_id" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); exit(0 if d['job_id']=='$JOB_ID' else 1)\""
    check "15. outbox has state=completed" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); exit(0 if d['state']=='completed' else 1)\""
    check "16. result has real LLM output (not stub)" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); r=d['result']; exit(0 if 'mock LLM response' in r.get('output','') else 1)\""
    check "17. result has model=minimax-m3:cloud" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); r=d['result']; exit(0 if r.get('model')=='minimax-m3:cloud' else 1)\""
    check "18. result has usage with tokens" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); r=d['result']; u=r.get('usage',{}); exit(0 if u.get('total_tokens')==27 else 1)\""
    check "19. result has attestation" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); r=d['result']; exit(0 if 'attestation' in r else 1)\""
    check "20. result does NOT have stub note" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); r=d['result']; exit(0 if 'stub' not in r.get('output','') else 1)\""
    check "21. result does NOT have llm_error" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE')); r=d['result']; exit(0 if 'llm_error' not in r else 1)\""
fi

# --- Missing API key test ---
echo ""
echo "=== Missing API key test ==="
rm "$LOGS/llm-api-key"
JOB_ID2="job_llm_test_002"
echo "{\"job_id\":\"$JOB_ID2\",\"encrypted_skill\":\"summarize\",\"encrypted_data\":\"test\"}" > "$INBOX/${JOB_ID2}.json"
timeout 5 python3 "$POLLER_PY" >/dev/null 2>&1 || true
OUTBOX_FILE2="$OUTBOX/${JOB_ID2}.json"
check "22. missing key: outbox created" "[ -f '$OUTBOX_FILE2' ]"
if [ -f "$OUTBOX_FILE2" ]; then
    # The poller has 3 paths: broker proxy (needs llm_token in envelope),
    # NemoClaw (needs nemoclaw-api-key file), direct LLM (needs llm-api-key).
    # Without any of these, the result includes llm_error explaining why.
    check "23. missing key: result has llm_error" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE2')); r=d['result']; exit(0 if 'llm_error' in r else 1)\""
    check "24. missing key: error indicates no key/path" "python3 -c \"import json; d=json.load(open('$OUTBOX_FILE2')); r=d['result']; err=r.get('llm_error',''); exit(0 if 'no_' in err or 'key' in err or 'path' in err else 1)\""
fi

# Kill mock server
kill $MOCK_PID 2>/dev/null || true

echo ""
echo "=== Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo "Ad-hoc verification only (mock HTTP server, no live LLM API calls)."
echo "Scope: LLM integration in worker/user-data.sh execute_in_envelope()"

[ "$FAIL" = "0" ] && exit 0 || exit 1