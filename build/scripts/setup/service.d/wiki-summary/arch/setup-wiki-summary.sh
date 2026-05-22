#!/bin/bash

set -e

# pypdf via pacman or pip
if command -v pacman >/dev/null 2>&1; then
    pacman -S --noconfirm python-pypdf 2>/dev/null \
        || pip3 install --break-system-packages pypdf 2>/dev/null \
        || pip3 install pypdf || true
fi

readonly CONF_PATH=/etc/nimoos
readonly CONF_FILE=${CONF_PATH}/wiki.conf
readonly CONF_SAMPLE=${CONF_PATH}/wiki-summary.conf.sample

if [ -f "${CONF_FILE}" ]; then
    if ! grep -q "^\[wiki-summary\]" "${CONF_FILE}"; then
        echo "" >> "${CONF_FILE}"
        cat "${CONF_SAMPLE}" >> "${CONF_FILE}"
    fi
fi

# Create runtime directory for rate-limit calls.log (root-writable, world-readable).
install -d -m 0755 /var/lib/nimoos/wiki-summary

# Migrate calls.log from the old per-user cache location, if present.
if [ -f /root/.cache/nimoos-wiki-summary/calls.log ] \
        && [ ! -e /var/lib/nimoos/wiki-summary/calls.log ]; then
    mv /root/.cache/nimoos-wiki-summary/calls.log \
       /var/lib/nimoos/wiki-summary/calls.log || true
fi
rm -rf /root/.cache/nimoos-wiki-summary 2>/dev/null || true

systemctl daemon-reload
systemctl enable --force --no-ask-password "nimoos-wiki-summary.timer"
