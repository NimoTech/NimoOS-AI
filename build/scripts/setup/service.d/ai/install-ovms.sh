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

# OVMS exists to serve models on Intel GPUs. On a machine without one it is 432
# MB of download that can never be useful — a clean install on an AMD box
# (Ryzen AI MAX+ 395 / Radeon 8060S, no /dev/dri at all) pulled the whole thing
# down and enabled the service anyway.
#
# Read the PCI class and vendor out of sysfs rather than shelling out to lspci,
# which minimal images often do not ship. Class 0x03xxxx is the display
# controller class; 0x8086 is Intel.
#
# No Intel GPU and no way to tell means skip: installing an Intel-only component
# on a machine where nothing indicates an Intel GPU is the wrong default, and
# OVMS_FORCE=1 is there for anyone who knows better than the probe.
has_intel_gpu() {
    local dev vendor class
    for dev in /sys/bus/pci/devices/*; do
        [ -r "${dev}/vendor" ] && [ -r "${dev}/class" ] || continue
        read -r vendor < "${dev}/vendor" || continue
        [ "${vendor}" = "0x8086" ] || continue
        read -r class < "${dev}/class" || continue
        case "${class}" in 0x03*) return 0 ;; esac
    done
    return 1
}

if [ "${OVMS_FORCE:-0}" != "1" ] && ! has_intel_gpu; then
    echo "⚠ No Intel GPU found on this host; skipping OVMS (it only serves models on Intel GPUs)."
    echo "  Set OVMS_FORCE=1 to install it anyway."
    exit 0
fi

# The pinned package is built for Ubuntu 24.04 and its libraries want glibc
# 2.38. Debian 12 bookworm — which nimoos-install.sh supports and which most
# NAS boxes run — ships 2.36. The download and extract both succeed there, so
# 432 MB lands in /opt/ovms and every single start fails with
#   /opt/ovms/bin/ovms: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38'
#   not found (required by /opt/ovms/lib/libopenvino.so.2621)
# under Restart=always/RestartSec=5, i.e. twelve times a minute forever.
#
# The header above says "machines are homogeneous", and that was true while
# this only ran on our own boxes. It stops being true the moment anyone else
# installs NimoOS.
need_glibc="2.38"
have_glibc="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
if [ -n "${have_glibc}" ] \
   && [ "$(printf '%s\n%s\n' "${need_glibc}" "${have_glibc}" | sort -V | head -1)" != "${need_glibc}" ]; then
    echo "⚠ OVMS ${VERSION} needs glibc >= ${need_glibc}, this host has ${have_glibc}; skipping."
    echo "  OpenVINO stays unavailable. Nothing else depends on it."
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
