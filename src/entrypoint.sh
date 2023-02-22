#!/bin/bash

set -o nounset
set -o errexit
set -o pipefail

CONF_FILE="/data/weewx.conf"

# echo version before starting syslog so we don't confound our tests
if [ "$1" = "--version" ]; then
  gosu weewx:weewx ./bin/weewxd --version
  exit 0
fi

if [ "$(id -u)" = 0 ]; then
  # set timezone using environment
  ln -snf /usr/share/zoneinfo/"${TIMEZONE:-UTC}" /etc/localtime
  # start the syslog daemon as root
  /sbin/syslogd -n -S -O - &
  if [ "${WEEWX_UID:-weewx}" != 0 ]; then
    # drop privileges and restart this script
    echo "Switching uid:gid to ${WEEWX_UID:-weewx}:${WEEWX_GID:-weewx}"
    gosu "${WEEWX_UID:-weewx}:${WEEWX_GID:-weewx}" "$(readlink -f "$0")" "$@"
    exit 0
  fi
fi

copy_default_config() {
  # create a default configuration on the data volume
  echo "Creating a configration file on the container data volume."
  cp weewx.conf "${CONF_FILE}"
  echo "The default configuration has been copied."
  # Change the default location of the SQLITE database to the volume
  echo "Setting SQLITE_ROOT to the container volume."
  sed "s/SQLITE_ROOT =.*/SQLITE_ROOT = \/data/g" "${CONF_FILE}" > /tmp/weewx.conf
  mv /tmp/weewx.conf "${CONF_FILE}"
}

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

if [ "$1" = "--upgrade" ]; then
  ./bin/wee_config --upgrade --no-prompt --dist-config weewx.conf "${CONF_FILE}"
  exit $?
fi

if [ ! -f "${CONF_FILE}" ]; then
  copy_default_config
  echo "Running configuration tool."
  ./bin/wee_config --reconfigure "${CONF_FILE}"
  exit 1
fi

./bin/weewxd "$@"
