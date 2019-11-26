#!/bin/sh

set -o nounset
set -o errexit
# Sha-bang cannot be /bin/bash (not available), but
# the container's /bin/sh does support pipefail.
# shellcheck disable=SC2039
set -o pipefail

CONF_FILE="/data/weewx.conf"

copy_default_config() {
  # create a default configuration on the data volume
  echo "A configration file not found on the container data volume."
  cp weewx.conf ${CONF_FILE}
  echo "The default configuration has been copied."
  # Change the default location of the SQLITE database to the volume
  echo "Setting SQLITE_ROOT to the container volume."
  sed -i "s/SQLITE_ROOT =.*/SQLITE_ROOT = \/data/g" /data/weewx.conf
}

if [ "$1" = "--version" ]; then
  ./bin/weewxd --version
  exit 0
fi

if [ "$1" = "--gen-test-config" ]; then
  copy_default_config
  echo "Generating a test configuration."
  ./bin/wee_config --reconfigure --no-prompt "${CONF_FILE}"
  exit 0
fi

if [ "$1" = "--shell" ]; then
  /bin/sh
  exit $?
fi

if [ ! -f ${CONF_FILE} ]; then
  copy_default_config
  echo "Running configuration tool."
  ./bin/wee_config --reconfigure "${CONF_FILE}"
  exit 1
fi

./bin/weewxd "$@"
