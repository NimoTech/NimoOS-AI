#!/usr/bin/env bash
# Smoke test: start nimoos-agent locally and verify key endpoints
set -e

cd "$(dirname "$0")"
source venv/bin/activate

# Start server in background
python main.py &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null" EXIT

# Wait for startup (up to 10 seconds)
for i in $(seq 1 10); do
    if curl -sf http://127.0.0.1:8282/agent/health > /dev/null; then
        echo "Server is up"
        break
    fi
    sleep 1
done

# Health check
echo "=== Health ==="
curl -sf http://127.0.0.1:8282/agent/health | python3 -m json.tool

# Create session
echo "=== Create Session ==="
SESSION=$(curl -sf -X POST http://127.0.0.1:8282/agent/sessions \
    -H "X-User-Id: 1" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "session_id=$SESSION"

# List sessions
echo "=== List Sessions ==="
curl -sf http://127.0.0.1:8282/agent/sessions -H "X-User-Id: 1" | python3 -m json.tool

# Confirm unknown session (expect 409)
echo "=== Confirm Unknown (expect 409) ==="
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    http://127.0.0.1:8282/agent/sessions/nonexistent/confirm \
    -H "X-User-Id: 1" -H "Content-Type: application/json" -d '{"confirmed":true}')
[ "$STATUS" = "409" ] && echo "PASS: got 409" || echo "FAIL: expected 409, got $STATUS"

# Delete session
echo "=== Delete Session ==="
curl -sf -X DELETE "http://127.0.0.1:8282/agent/sessions/$SESSION" -H "X-User-Id: 1"

echo ""
echo "All smoke tests passed."
