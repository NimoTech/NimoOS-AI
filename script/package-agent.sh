#!/bin/bash
# Build the NimoOS Agent offline distribution package: build image -> docker save ->
# tar it up together with compose/install.sh.
# Usage: bash script/package-agent.sh <version>   (run from the NimoOS-AI repo root)
# Output: dist/nimoos-agent-<version>.tar.gz
#   Upload to: NimoTech/NimoOS-AI/releases/download/agent-<version>/nimoos-agent-<version>.tar.gz
set -euo pipefail

VERSION="${1:?usage: package-agent.sh <version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_REF="localhost/nimoos-agent:bundled"
OUT_DIR="${ROOT}/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

echo "==> Building image ${IMAGE_REF} ..."
# Base image existence guard: offline/mainland-China networks can't reach docker.io, so
# give an actionable hint when the base image is missing instead of an opaque
# "TLS handshake timeout".
BASE_IMAGE="$(awk '/^FROM /{print $2; exit}' "${ROOT}/deploy/agent/Dockerfile")"
if ! docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
  echo "!! Base image ${BASE_IMAGE} is missing locally." >&2
  echo "   In an offline environment, fetch it on a machine with internet access first, then import it here:" >&2
  echo "     docker pull ${BASE_IMAGE}" >&2
  echo "     docker save ${BASE_IMAGE} -o base.tar   # after copying to this machine:  docker load -i base.tar" >&2
  echo "   Or configure registry-mirrors for the local docker daemon (/etc/docker/daemon.json) and retry." >&2
  exit 1
fi
# egress-proxy: precompiled as a static Go binary on the host (Dockerfile just COPYs it in, avoiding a multi-stage golang image dependency).
echo "==> Precompiling the egress-proxy static binary ..."
if ! command -v go >/dev/null 2>&1; then
  echo "!! go not found (egress-proxy must be built on the host). Install go, or place a prebuilt binary at deploy/agent/egress-proxy/egress-proxy" >&2
  exit 1
fi
( cd "${ROOT}/deploy/agent/egress-proxy" && CGO_ENABLED=0 go build -o egress-proxy . )

# DOCKER_BUILDKIT=0: use the classic builder, which reuses the local base image directly
# instead of re-resolving metadata from the registry (BuildKit HEADs docker.io even when
# the image is already local, which times out offline).
DOCKER_BUILDKIT=0 docker build -t "${IMAGE_REF}" -f "${ROOT}/deploy/agent/Dockerfile" "${ROOT}"

echo "==> Exporting image -> agent-image.tar ..."
docker save "${IMAGE_REF}" -o "${STAGE}/agent-image.tar"
cp "${ROOT}/deploy/agent/docker-compose.yml" "${STAGE}/docker-compose.yml"
cp "${ROOT}/deploy/agent/install.sh"        "${STAGE}/install.sh"

mkdir -p "${OUT_DIR}"
TARBALL="${OUT_DIR}/nimoos-agent-${VERSION}.tar.gz"
echo "==> Packaging ${TARBALL} ..."
tar -czf "${TARBALL}" -C "${STAGE}" agent-image.tar docker-compose.yml install.sh

echo "✓ Output: ${TARBALL}"
echo "  Upload: oss://nimoos/NimoTech/NimoOS-AI/releases/download/agent-${VERSION}/$(basename "${TARBALL}")"
