#!/bin/bash

set -e

# Install pypdf via apt or pip (it's a runtime dep of wiki_summary_worker.sampler)
if command -v apt-get >/dev/null 2>&1; then
    echo "Installing python3-pypdf..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3-pypdf 2>/dev/null \
        || pip3 install --break-system-packages pypdf 2>/dev/null \
        || pip3 install pypdf || true
fi

readonly CONF_PATH=/etc/nimoos
readonly CONF_FILE=${CONF_PATH}/wiki.conf
readonly CONF_SAMPLE=${CONF_PATH}/wiki-summary.conf.sample

# Append the [wiki-summary] section to /etc/nimoos/wiki.conf if not already present.
# wiki.conf is owned by the wiki service; we add to it rather than create a separate file.
if [ -f "${CONF_FILE}" ]; then
    if ! grep -q "^\[wiki-summary\]" "${CONF_FILE}"; then
        echo "Appending [wiki-summary] section to ${CONF_FILE}..."
        echo "" >> "${CONF_FILE}"
        cat "${CONF_SAMPLE}" >> "${CONF_FILE}"
    fi
else
    echo "warning: ${CONF_FILE} not found — wiki service must be installed first"
fi

# Create runtime directory for rate-limit calls.log (root-writable, world-readable).
install -d -m 0755 /var/lib/nimoos/wiki-summary

# Migrate calls.log from the old per-user cache location, if present.
# Worker runs as root, so the only realistic stale location is /root/.cache/.
# Best-effort: ignore failures (file may not exist on fresh installs).
if [ -f /root/.cache/nimoos-wiki-summary/calls.log ] \
        && [ ! -e /var/lib/nimoos/wiki-summary/calls.log ]; then
    mv /root/.cache/nimoos-wiki-summary/calls.log \
       /var/lib/nimoos/wiki-summary/calls.log || true
fi
rm -rf /root/.cache/nimoos-wiki-summary 2>/dev/null || true

systemctl daemon-reload

echo "Enabling timer..."
systemctl enable --force --no-ask-password "nimoos-wiki-summary.timer"
