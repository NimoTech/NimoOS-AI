#!/bin/bash
# 离线安装/更新 NimoOS Agent 容器。由 script/package-agent.sh 打的包解压后运行本脚本。
# 幂等:重复运行 = 更新到包内镜像版本。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_ID="nimoos-agent"
APP_DIR="/var/lib/nimoos/apps/${APP_ID}"
IMAGE_TAR="${HERE}/agent-image.tar"
IMAGE_REF="localhost/nimoos-agent:bundled"
DATA_DIR="/var/lib/nimoos/ai/agent"
HEALTH_URL="http://127.0.0.1:8282/healthz"
HEALTH_TIMEOUT=60

[[ -f "${IMAGE_TAR}" ]] || { echo "✗ 找不到镜像包 ${IMAGE_TAR}" >&2; exit 1; }

# 就绪检查:避免 /DATA 未挂载时把空目录绑进容器(数据写错地方)。
# 接受「是 mountpoint」或「已初始化(有 .system_data 或非空)」。
data_ready() {
  local p="$1"
  mountpoint -q "$p" && return 0
  [[ -d "$p/.system_data" ]] && return 0
  [[ -d "$p" ]] && [[ -n "$(ls -A "$p" 2>/dev/null)" ]] && return 0
  return 1
}
if ! data_ready /DATA; then
  echo "✗ /DATA 尚未就绪(非挂载点且为空)。请先确保数据盘挂载,再重试。" >&2
  exit 1
fi

echo "==> [1/4] 载入离线镜像 ${IMAGE_REF} ..."
docker load -i "${IMAGE_TAR}"

echo "==> [2/4] 部署 compose 到 ${APP_DIR} ..."
mkdir -p "${APP_DIR}" "${DATA_DIR}"
cp "${HERE}/docker-compose.yml" "${APP_DIR}/docker-compose.yml"

echo "==> [3/4] 启动 ${APP_ID} ..."
docker compose -p "${APP_ID}" -f "${APP_DIR}/docker-compose.yml" up -d

echo "==> [4/4] 等待 Agent 就绪(最多 ${HEALTH_TIMEOUT}s,轮询 ${HEALTH_URL})..."
deadline=$(( SECONDS + HEALTH_TIMEOUT ))
while (( SECONDS < deadline )); do
  if curl -fsS "${HEALTH_URL}" 2>/dev/null | grep -q '"ok"'; then
    echo "✓ NimoOS Agent 已就绪并运行中。"
    exit 0
  fi
  sleep 2
done
echo "✗ 超时:Agent 未在 ${HEALTH_TIMEOUT}s 内就绪。" >&2
echo "  排查:docker logs ${APP_ID}-agent-1" >&2
exit 1
