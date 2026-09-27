#!/bin/bash
# Makes the loopback-only broker reachable through Docker's published port
# (docs/SANDBOX.md §3). switchboard binds only 127.0.0.1 (a guardrail); a published
# port arrives on the container's own interface, so this relays that
# interface's :$SWITCHBOARD_PORT to 127.0.0.1:$SWITCHBOARD_PORT. The port is the same on
# both sides, so the broker's Host/Origin check (switchboard.localhost:<port>) holds.
# compose.yaml publishes it on the Mac's 127.0.0.1 only. Run as dev, next to
# `switchboard start --port "$SWITCHBOARD_PORT"`.
set -euo pipefail
PORT="${SWITCHBOARD_PORT:-8765}"
IP=$(getent ahostsv4 "$(hostname)" | awk 'NR==1{print $1}')
case "$IP" in ""|127.*|0.0.0.0) echo "forward.sh: no container address for $(hostname)" >&2; exit 1 ;; esac
echo "forwarding $IP:$PORT -> 127.0.0.1:$PORT" >&2
exec socat "TCP-LISTEN:$PORT,bind=$IP,fork,reuseaddr" "TCP:127.0.0.1:$PORT"
