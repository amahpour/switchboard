#!/bin/sh
# `switchboard` in the container image (the repo's Dockerfile; docs/DEPLOY.md). The real
# command is /opt/switchboard/bin/switchboard, and it never runs as root:
#
# - Started as root (a Render disk or a bind mount belongs to root, and `docker exec` runs as
#   root unless -u says otherwise), this drops to the `switchboard` user first: setpriv with
#   that user's own group only, no capabilities, and no_new_privs (no setuid way back).
# - Only when the container itself starts as root, before any process of that user exists,
#   does it first make $SWITCHBOARD_HOME that user's private directory (0700), creating it if
#   needed. Never recursively: what is inside is the broker's own. A `docker exec` never
#   touches the home as root.
# - As PID 1 (the container starting) it runs the command under tini, which passes
#   `docker stop`'s SIGTERM on to the broker and reaps children.
#
# Started as the `switchboard` user (Kubernetes' runAsUser, `docker run -u 10001`), it only
# adds tini as PID 1.
set -eu

real=/opt/switchboard/bin/switchboard
tini=""
if [ "$$" = 1 ]; then
  tini="/usr/bin/tini --"
fi

if [ "$(id -u)" != 0 ]; then
  # tini is either empty or the intentional two-word command prefix "/usr/bin/tini --".
  # shellcheck disable=SC2086
  exec $tini "$real" "$@"
fi

if [ "$$" = 1 ]; then
  home=${SWITCHBOARD_HOME:?SWITCHBOARD_HOME is not set}
  if [ -L "$home" ] || { [ -e "$home" ] && [ ! -d "$home" ]; }; then
    echo "switchboard: $home is not a directory" >&2
    exit 1
  fi
  mkdir -p "$home"
  chown -h switchboard:switchboard "$home"
  chmod 0700 "$home"
fi

export HOME=/home/switchboard USER=switchboard LOGNAME=switchboard
# tini is the same optional command prefix on the privilege-dropping path.
# shellcheck disable=SC2086
exec setpriv --reuid=switchboard --regid=switchboard --init-groups --no-new-privs \
  --inh-caps=-all --bounding-set=-all -- $tini "$real" "$@"
