#!/bin/bash

set -o nounset
set -o errexit
set -o pipefail

WEEWX_ROOT="/data"
CONF_FILE="${WEEWX_ROOT}/weewx.conf"

# echo version
if [ $# -gt 0 ] && [ "$1" = "--version" ]; then
  cat "$(dirname "$0")/image_version.txt"
  exit 0
fi

if [ ! -f "${CONF_FILE}" ]; then
  weectl station create --no-prompt ${WEEWX_ROOT}
  echo "A new set of configurations was created."

  # Append the logging configuration to the generated weewx.conf
  cat << EOF >> "${CONF_FILE}"

[Logging]
    [[root]]
      level = INFO
      handlers = console,
EOF

  echo "Console logging configuration has been appended to ${CONF_FILE}."
  echo "Please review and update ${CONF_FILE} as needed, then restart the container."
  exit 0
fi

# if we have any parameters we'll send them to weectl

if [ $# -gt 0 ]; then
  weectl "$@" --config ${CONF_FILE}
  exit 0
else
  weewxd --config ${CONF_FILE}
fi
