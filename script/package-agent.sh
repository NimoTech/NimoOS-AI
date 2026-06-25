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
# 基础镜像存在性守卫:离线/国内网无法访问 docker.io,缺基础镜像时给出可操作提示,
# 而不是抛一个看不懂的 "TLS handshake timeout"。
BASE_IMAGE="$(awk '/^FROM /{print $2; exit}' "${ROOT}/deploy/agent/Dockerfile")"
if ! docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
  echo "!! 本地缺少基础镜像 ${BASE_IMAGE}。" >&2
  echo "   离线环境请先在能联网的机器获取后导入本机:" >&2
  echo "     docker pull ${BASE_IMAGE}" >&2
  echo "     docker save ${BASE_IMAGE} -o base.tar   # 拷到本机后:  docker load -i base.tar" >&2
  echo "   或给本机 docker 配 registry-mirrors(/etc/docker/daemon.json)再重试。" >&2
  exit 1
fi
# egress-proxy:在宿主用 Go 预编译静态二进制(Dockerfile 直接 COPY,避开多阶段 golang 镜像依赖)。
echo "==> 预编译 egress-proxy 静态二进制 ..."
if ! command -v go >/dev/null 2>&1; then
  echo "!! 未找到 go(需在宿主编译 egress-proxy)。装 go 或把已编好的二进制放到 deploy/agent/egress-proxy/egress-proxy" >&2
  exit 1
fi
( cd "${ROOT}/deploy/agent/egress-proxy" && CGO_ENABLED=0 go build -o egress-proxy . )

# DOCKER_BUILDKIT=0:用经典构建器,基础镜像在本地即直接复用、不去 registry 重新解析元数据
# (BuildKit 即便本地有也会 HEAD docker.io,离线会超时)。
DOCKER_BUILDKIT=0 docker build -t "${IMAGE_REF}" -f "${ROOT}/deploy/agent/Dockerfile" "${ROOT}"

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
