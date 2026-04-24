#!/bin/bash

set -e

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
