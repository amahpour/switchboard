#!/usr/bin/env bash
# Shared helpers for the M8 measurement scripts (README.md here). Sourced, not run.
#
# Everything lives in one throwaway work dir under /tmp (short, so Unix socket
# paths fit), with keys generated for the run and a user-level sshd on
# 127.0.0.1:<free port>. Nothing reads or writes ~/.ssh, the system sshd or its
# config, or any switchboard home. The work dir is removed on exit (KEEP=1 keeps it).

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
PY="${PY:-$REPO/.venv/bin/python3}"        # the checkout's venv (uv sync)
SSHD_BIN="${SSHD_BIN:-/usr/sbin/sshd}"
export PYTHONDONTWRITEBYTECODE=1
[ -x "$PY" ] || { echo "no $PY: run 'uv sync' in the checkout first (or set PY)" >&2; exit 2; }
[ -x "$SSHD_BIN" ] || { echo "no sshd at $SSHD_BIN (set SSHD_BIN)" >&2; exit 2; }
if ! command -v ssh >/dev/null || ! command -v ssh-keygen >/dev/null; then
  echo "needs ssh and ssh-keygen" >&2; exit 2
fi

W="$(mktemp -d /tmp/sb-m8-XXXXXX)"
chmod 700 "$W"
PIDS=""
cleanup() {
  for p in $PIDS; do kill "$p" 2>/dev/null; done
  wait 2>/dev/null
  if [ -n "${KEEP:-}" ]; then
    echo "kept the work dir: $W"
  else
    "$PY" -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$W"
  fi
}
trap cleanup EXIT

bg() { "$@" & PIDS="$PIDS $!"; }   # run in the background, killed on exit

# with_timeout SECONDS CMD...: run CMD, killed after SECONDS (exit 124). GNU coreutils'
# `timeout` is not on stock macOS, so this uses the checkout's Python.
with_timeout() {
  "$PY" -c '
import subprocess, sys
try:
    sys.exit(subprocess.run(sys.argv[2:], timeout=float(sys.argv[1])).returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)
' "$@"
}

free_port() {
  "$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

wait_port() {
  "$PY" - "$1" <<'EOF'
import socket, sys, time
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", int(sys.argv[1])), 0.2).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.05)
sys.exit(1)
EOF
}

wait_sock() {
  for _ in $(seq 100); do [ -S "$1" ] && return 0; sleep 0.05; done
  return 1
}

# start_sshd AUTHORIZED_KEYS_LINE [extra sshd_config lines...]
# Generates the host key hk and the client key ck, starts sshd, and sets PORT,
# DEST and SSH (the client argv prefix: no config file, no agent, only ck, the
# run's own known_hosts).
start_sshd() {
  [ -f "$W/hk" ] || ssh-keygen -q -t ed25519 -N '' -f "$W/hk" -C sb-m8-host
  [ -f "$W/ck" ] || ssh-keygen -q -t ed25519 -N '' -f "$W/ck" -C sb-m8-client
  local line="${1//@CLIENT_KEY@/$(cat "$W/ck.pub")}"
  shift
  printf '%s\n' "$line" > "$W/ak"
  PORT="$(free_port)"
  { printf '%s\n' "Port $PORT" "ListenAddress 127.0.0.1" "HostKey $W/hk" "PidFile $W/sshd.pid" \
      "AuthorizedKeysFile $W/ak" "StrictModes no" "UsePAM no" "PasswordAuthentication no" \
      "KbdInteractiveAuthentication no" "PubkeyAuthentication yes" "PermitUserRC no" "LogLevel VERBOSE"
    [ $# -gt 0 ] && printf '%s\n' "$@"; } > "$W/sshd_config"
  echo "[127.0.0.1]:$PORT $(cut -d' ' -f1,2 "$W/hk.pub")" > "$W/known_hosts"
  bg "$SSHD_BIN" -D -e -f "$W/sshd_config" 2>>"$W/sshd.log"
  wait_port "$PORT" || { echo "sshd did not start:" >&2; cat "$W/sshd.log" >&2; exit 1; }
  # These variables are read by scripts that source this file.
  # shellcheck disable=SC2034
  DEST="$(id -un)@127.0.0.1"
  # shellcheck disable=SC2034
  SSH=(ssh -F /dev/null -i "$W/ck" -o IdentitiesOnly=yes -o IdentityAgent=none
       -o UserKnownHostsFile="$W/known_hosts" -o GlobalKnownHostsFile=/dev/null
       -o StrictHostKeyChecking=yes -o BatchMode=yes -o LogLevel=ERROR -p "$PORT")
}
