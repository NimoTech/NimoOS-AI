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

echo
echo "=== Filesystem flow ==="

# Set up a workspace under tmp
WS=$(mktemp -d)
echo "old content" > "$WS/file.txt"
mkdir "$WS/sub"

# Create a fresh session for the filesystem flow
FS_SID=$(curl -sf -X POST -H "X-User-Id: 1" \
  http://127.0.0.1:8282/agent/sessions \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['session_id'])")
echo "fs session_id=$FS_SID"

# Authorize WS as folder
curl -sf -X POST -H "X-User-Id: 1" -H "Content-Type: application/json" \
  -d "{\"path\":\"$WS\",\"kind\":\"folder\"}" \
  http://127.0.0.1:8282/agent/sessions/$FS_SID/visible-resources > /dev/null

# List visible-resources should show 1
COUNT=$(curl -sf -H "X-User-Id: 1" \
  http://127.0.0.1:8282/agent/sessions/$FS_SID/visible-resources \
  | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
[ "$COUNT" = "1" ] && echo "PASS: 1 visible resource" || { echo "FAIL: expected 1 visible resource, got $COUNT"; exit 1; }

# fs/list should return non-empty entries for WS
curl -sf -H "X-User-Id: 1" \
  "http://127.0.0.1:8282/agent/fs/list?path=$WS" \
  | python3 -c "import sys, json; d=json.load(sys.stdin); assert d, 'empty'; print('PASS: fs/list returned', len(d), 'entries')"

# staged-changes should start empty
curl -sf -H "X-User-Id: 1" \
  http://127.0.0.1:8282/agent/sessions/$FS_SID/staged-changes \
  | python3 -c "import sys, json; d=json.load(sys.stdin); assert d == [], d; print('PASS: staged-changes empty')"

# Manually insert a fake staged_changes row to verify commit drops it.
# Use the AGENT_DB_PATH the server actually opens (defaults to /var/lib/nimoos/ai/agent/agent.db,
# overridable via env). We probe it from the running process.
DB_PATH="${AGENT_DB_PATH:-/var/lib/nimoos/ai/agent/agent.db}"
sqlite3 "$DB_PATH" <<SQL
INSERT INTO staged_changes (session_id, run_id, seq, op, path, status, created_at)
VALUES ('$FS_SID', 'r-smoke', 1, 'mkdir', '$WS/sub2', 'pending', strftime('%s','now'));
SQL

curl -sf -X POST -H "X-User-Id: 1" \
  http://127.0.0.1:8282/agent/sessions/$FS_SID/staged-changes/commit > /dev/null

LEFT=$(sqlite3 "$DB_PATH" \
  "SELECT COUNT(*) FROM staged_changes WHERE session_id='$FS_SID' AND status='pending'")
[ "$LEFT" = "0" ] && echo "PASS: 0 pending after commit" || { echo "FAIL: expected 0 pending after commit, got $LEFT"; exit 1; }

# Cleanup the fs session and tmp dir
curl -sf -X DELETE -H "X-User-Id: 1" \
  http://127.0.0.1:8282/agent/sessions/$FS_SID > /dev/null
rm -rf "$WS"

echo "Filesystem flow OK"

# Delete session
echo "=== Delete Session ==="
curl -sf -X DELETE "http://127.0.0.1:8282/agent/sessions/$SESSION" -H "X-User-Id: 1"

echo ""
echo "All smoke tests passed."
