#!/bin/bash
# 打 NimoOS Agent 离线分发包:build 镜像 -> docker save -> 连同 compose/install.sh 打 tar。
# 用法: bash script/package-agent.sh <version>   (在 NimoOS-AI 仓库根运行)
# 产物: dist/nimoos-agent-<version>.tar.gz
#   上传到: NimoTech/NimoOS-AI/releases/download/agent-<version>/nimoos-agent-<version>.tar.gz
set -euo pipefail

VERSION="${1:?用法: package-agent.sh <version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_REF="localhost/nimoos-agent:bundled"
OUT_DIR="${ROOT}/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

echo "==> 构建镜像 ${IMAGE_REF} ..."
docker build -t "${IMAGE_REF}" -f "${ROOT}/deploy/agent/Dockerfile" "${ROOT}"

echo "==> 导出镜像 -> agent-image.tar ..."
docker save "${IMAGE_REF}" -o "${STAGE}/agent-image.tar"
cp "${ROOT}/deploy/agent/docker-compose.yml" "${STAGE}/docker-compose.yml"
cp "${ROOT}/deploy/agent/install.sh"        "${STAGE}/install.sh"

mkdir -p "${OUT_DIR}"
TARBALL="${OUT_DIR}/nimoos-agent-${VERSION}.tar.gz"
echo "==> 打包 ${TARBALL} ..."
tar -czf "${TARBALL}" -C "${STAGE}" agent-image.tar docker-compose.yml install.sh

echo "✓ 产物: ${TARBALL}"
echo "  上传: oss://nimoos/NimoTech/NimoOS-AI/releases/download/agent-${VERSION}/$(basename "${TARBALL}")"
