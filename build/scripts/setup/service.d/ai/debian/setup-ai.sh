#!/bin/bash

set -e

# Install runtime dependencies for attachments (ffprobe + libmagic)
if command -v apt-get >/dev/null 2>&1; then
    echo "Installing ffmpeg and libmagic1..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ffmpeg libmagic1 || true
fi

## base variables
readonly APP_NAME="nimoos-ai"
readonly APP_NAME_SHORT="ai"

# copy config files
readonly CONF_PATH=/etc/nimoos
readonly CONF_FILE=${CONF_PATH}/${APP_NAME_SHORT}.conf
readonly CONF_FILE_SAMPLE=${CONF_PATH}/${APP_NAME_SHORT}.conf.sample

if [ ! -f "${CONF_FILE}" ]; then \
    echo "Initializing config file..."
    cp -v "${CONF_FILE_SAMPLE}" "${CONF_FILE}"; \
fi

# enable and start service
systemctl daemon-reload

echo "Enabling service..."
systemctl enable --force --no-ask-password "${APP_NAME}.service"

#echo "Starting service..."
#systemctl start --force --no-ask-password "${APP_NAME}.service"

# OpenVINO Model Server (OVMS): install binary + start service (best-effort, failure only warns, never blocks)
echo "Installing OVMS (OpenVINO Model Server)..."
bash "$(dirname "${BASH_SOURCE[0]}")/../install-ovms.sh" || echo "⚠ OVMS install step failed; OpenVINO unavailable until installed."

# Model directory + servable repo directory (user drops model IR files into models/)
mkdir -p /var/lib/nimoos/ai/models /var/lib/nimoos/ai/openvino/models

# Only enable the unit if the binary is actually runnable here. It was enabled
# unconditionally before, so a host where OVMS could not be installed — or
# where it installed but cannot load its libraries, which is every Debian 12
# box, see install-ovms.sh — got a unit restarting every five seconds forever.
# A feature that is unavailable should be absent, not permanently crashing.
if [ -x /opt/ovms/bin/ovms ] && /opt/ovms/bin/ovms --version >/dev/null 2>&1; then
    echo "Enabling and starting nimoos-openvino service..."
    systemctl enable --force --no-ask-password nimoos-openvino.service || echo "⚠ enable nimoos-openvino failed"
    systemctl start --force --no-ask-password nimoos-openvino.service || echo "⚠ start nimoos-openvino failed (GPU/driver issue?)"
else
    echo "⚠ OVMS is not runnable on this host; leaving nimoos-openvino disabled."
    systemctl disable --now nimoos-openvino.service 2>/dev/null || true
fi
