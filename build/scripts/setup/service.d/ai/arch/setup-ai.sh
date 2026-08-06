#!/bin/bash

set -e

# Install runtime dependencies for attachments (ffprobe + libmagic)
if command -v pacman >/dev/null 2>&1; then
    echo "Installing ffmpeg and file (libmagic)..."
    pacman -S --noconfirm --needed ffmpeg file || true
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

echo "Enabling and starting nimoos-openvino service..."
systemctl enable --force --no-ask-password nimoos-openvino.service || echo "⚠ enable nimoos-openvino failed"
systemctl start --force --no-ask-password nimoos-openvino.service || echo "⚠ start nimoos-openvino failed (OVMS binary missing or GPU/driver issue?)"
