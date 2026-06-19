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

# ── Arbitrary uid:gid support (rootless / Kubernetes runAsUser) ───────────────
# The image defaults to the non-root "weewx" user (uid/gid 1000) but may be run
# under any uid:gid.
# WeeWX's `weectl station create` resolves the running user and group names via
# Python's getpass.getuser() and grp.getgrgid(); these raise exceptions
# when the uid/gid have no /etc/passwd or /etc/group entry. A non-root process
# cannot edit those files, so synthesize the missing entries with nss_wrapper.
if ! getent passwd "$(id -u)" > /dev/null 2>&1 \
  || ! getent group "$(id -g)" > /dev/null 2>&1; then
  nss_passwd="$(mktemp)"
  nss_group="$(mktemp)"
  getent passwd > "${nss_passwd}"
  getent group > "${nss_group}"
  if ! getent passwd "$(id -u)" > /dev/null 2>&1; then
    printf 'weewx:x:%s:%s:weewx:%s:/usr/sbin/nologin\n' \
      "$(id -u)" "$(id -g)" "${HOME:-/home/weewx}" >> "${nss_passwd}"
  fi
  if ! getent group "$(id -g)" > /dev/null 2>&1; then
    printf 'weewx:x:%s:\n' "$(id -g)" >> "${nss_group}"
  fi
  export NSS_WRAPPER_PASSWD="${nss_passwd}"
  export NSS_WRAPPER_GROUP="${nss_group}"
  export LD_PRELOAD="libnss_wrapper.so${LD_PRELOAD:+:${LD_PRELOAD}}"
fi

# ── Data volume sanity check ──────────────────────────────────────────────────
# Under an arbitrary uid:gid the mounted volume may not be writable. Fail fast
# with a clear message instead of a confusing weectl/weewxd error downstream.
permissions_test_file="${WEEWX_ROOT}/.container-permissions-test"
if ! touch "${permissions_test_file}" 2> /dev/null \
  || ! rm -f "${permissions_test_file}" 2> /dev/null; then
  echo "ERROR: ${WEEWX_ROOT} is not writable by the current user (uid:gid $(id -u):$(id -g))." >&2
  echo "       Ensure the volume mounted at ${WEEWX_ROOT} is writable by this user." >&2
  exit 1
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
