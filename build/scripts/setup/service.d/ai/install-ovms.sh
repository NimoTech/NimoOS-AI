#!/bin/bash
# Idempotently install the OVMS (OpenVINO Model Server) bare binary to /opt/ovms; called by setup-ai.sh during deploy.
# Machines are homogeneous, so pin the ubuntu24 / python_off package (Debian13 lacks libpython, so python_on won't work).
# Failure only warns and exits 0 — must never block the AI service install. Runs as root, no sudo needed.
set -u

DEST="/opt/ovms"
VERSION="${OVMS_VERSION:-2026.2.1}"
PKG="${OVMS_PKG:-ovms_ubuntu24_${VERSION}_python_off.tar.gz}"
URL="${OVMS_URL:-https://storage.openvinotoolkit.org/repositories/openvino_model_server/packages/${VERSION}/${PKG}}"
WORK="/tmp/ovms-dl"

if [ -x "${DEST}/bin/ovms" ]; then
    echo "✅ OVMS already present at ${DEST}/bin/ovms, skipping download."
    exit 0
fi

echo "==> Installing OVMS: ${PKG}"
mkdir -p "${WORK}" || { echo "⚠ Failed to create ${WORK}, skipping OVMS install."; exit 0; }
cd "${WORK}" || { echo "⚠ Failed to enter ${WORK}, skipping OVMS install."; exit 0; }

dl() {
    local url="$1" out="$2"
    if command -v aria2c >/dev/null 2>&1; then
        aria2c -x16 -s16 -c --file-allocation=none -o "$out" "$url"
    elif command -v curl >/dev/null 2>&1; then
        curl -fL -C - -o "$out" "$url"
    else
        wget -c -O "$out" "$url"
    fi
}

if ! dl "${URL}" "${PKG}"; then
    echo "⚠ OVMS download failed (${URL}); skipping. Rerun the install later or install manually."
    exit 0
fi
if ! tar tzf "${PKG}" >/dev/null 2>&1; then
    echo "⚠ Downloaded ${PKG} is not a valid gzip (possibly an error page); skipping OVMS install."
    exit 0
fi

rm -rf "${WORK}/extract"; mkdir -p "${WORK}/extract"
if ! tar xzf "${PKG}" -C "${WORK}/extract"; then
    echo "⚠ Failed to extract ${PKG}; skipping."; exit 0
fi
ovms_bin="$(find "${WORK}/extract" -type f -path '*/bin/ovms' | head -1)"
if [ -z "${ovms_bin}" ]; then
    echo "⚠ bin/ovms not found after extraction; skipping."; exit 0
fi
binroot="$(dirname "$(dirname "${ovms_bin}")")"   # directory containing bin/ and lib/
rm -rf "${DEST}"
if ! cp -a "${binroot}" "${DEST}"; then
    echo "⚠ Failed to copy to ${DEST}; skipping."; exit 0
fi
if [ -x "${DEST}/bin/ovms" ]; then
    echo "✅ OVMS installed to ${DEST}."
else
    echo "⚠ ${DEST}/bin/ovms is missing; install may be incomplete."
fi
exit 0
