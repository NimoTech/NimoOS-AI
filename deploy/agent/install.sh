#!/bin/bash
# Offline install/update of the NimoOS Agent container. Run this script after
# extracting the package built by script/package-agent.sh.
# Idempotent: rerunning it updates to the image version bundled in the package.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_ID="nimoos-agent"
APP_DIR="/var/lib/nimoos/apps/${APP_ID}"
IMAGE_TAR="${HERE}/agent-image.tar"
IMAGE_REF="localhost/nimoos-agent:bundled"
DATA_DIR="/var/lib/nimoos/ai/agent"
HEALTH_URL="http://127.0.0.1:8282/healthz"
HEALTH_TIMEOUT=60

[[ -f "${IMAGE_TAR}" ]] || { echo "✗ Image package not found: ${IMAGE_TAR}" >&2; exit 1; }

# Readiness check: avoid binding an empty directory into the container when /DATA isn't
# mounted (data would land in the wrong place). Accepts either "is a mountpoint" or
# "already initialized (has .system_data or is non-empty)".
data_ready() {
  local p="$1"
  mountpoint -q "$p" && return 0
  [[ -d "$p/.system_data" ]] && return 0
  [[ -d "$p" ]] && [[ -n "$(ls -A "$p" 2>/dev/null)" ]] && return 0
  return 1
}
if ! data_ready /DATA; then
  echo "✗ /DATA is not ready yet (not a mountpoint and empty). Make sure the data disk is mounted, then retry." >&2
  exit 1
fi

echo "==> [1/4] Loading offline image ${IMAGE_REF} ..."
docker load -i "${IMAGE_TAR}"

echo "==> [2/4] Deploying compose to ${APP_DIR} ..."
mkdir -p "${APP_DIR}" "${DATA_DIR}"
cp "${HERE}/docker-compose.yml" "${APP_DIR}/docker-compose.yml"

# L4 audit-log tamper resistance: mark it append-only. The container shares this inode
# with the host (bind mount), and even a root process inside the container can only
# append, never truncate/delete/rewrite — this is the only OS-level backing for the
# audit log's "the agent can't alter it" promise (otherwise it rests solely on the L1
# shell classifier, which fails open the moment it's relaxed). Best-effort: if the
# filesystem doesn't support +a (some btrfs/overlay setups), just warn without blocking.
# WARNING: run `chattr -a` before log rotation.
AUDIT_LOG="${DATA_DIR}/audit.log"
touch "${AUDIT_LOG}" 2>/dev/null || true
if chattr +a "${AUDIT_LOG}" 2>/dev/null; then
  echo "    Audit log set to append-only: ${AUDIT_LOG}"
else
  echo "    ⚠ Could not set append-only on the audit log (filesystem may not support chattr +a), skipped." >&2
fi

echo "==> [3/4] Starting ${APP_ID} ..."
docker compose -p "${APP_ID}" -f "${APP_DIR}/docker-compose.yml" up -d

echo "==> [4/4] Waiting for the Agent to become ready (up to ${HEALTH_TIMEOUT}s, polling ${HEALTH_URL}) ..."
deadline=$(( SECONDS + HEALTH_TIMEOUT ))
while (( SECONDS < deadline )); do
  if curl -fsS "${HEALTH_URL}" 2>/dev/null | grep -q '"ok"'; then
    echo "✓ NimoOS Agent is ready and running."
    exit 0
  fi
  sleep 2
done
echo "✗ Timed out: Agent did not become ready within ${HEALTH_TIMEOUT}s." >&2
echo "  Troubleshoot: docker logs ${APP_ID}-agent-1" >&2
exit 1
