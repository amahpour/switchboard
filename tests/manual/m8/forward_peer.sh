#!/bin/bash
# What the broker's kernel peer is through an SSH socket forward (DESIGN.md §27, "Measured
# while designing"; the relay and remote-login rules of §27.5.7). README.md here.
#
# A stand-in broker.sock (peer_listener.py) prints, for every connection, the peer's
# process chain up to this script and switchboard's verdicts. Three routes:
#   1. ssh -R <far socket>:<broker.sock>   the peer is the local ssh client (a relay)
#   2. ssh -L <near socket>:<broker.sock>  the peer is sshd's session process (a relay)
#   3. ssh <dest> <client of broker.sock>  the peer is the client itself, under an sshd login
# The key here is an ordinary unrestricted one (forwarding allowed), as a key that opens a
# shell on this machine would be. User-level sshd on 127.0.0.1; see lib.sh.
# shellcheck source=tests/manual/m8/lib.sh
. "$(dirname "$0")/lib.sh"
export LAUNCHER=$$

CLIENT='import socket, sys; s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(sys.argv[1]); s.sendall((sys.argv[2] + "\n").encode()); print("  client", sys.argv[2], "->", s.recv(100).decode().strip())'

bg env PYTHONPATH="$REPO/src" "$PY" "$HERE/peer_listener.py" "$W/b" > "$W/listener.log"
wait_sock "$W/b" || { echo "listener did not start" >&2; exit 1; }
start_sshd "@CLIENT_KEY@" "AllowStreamLocalForwarding yes" "StreamLocalBindUnlink yes"
echo "== user-level sshd on 127.0.0.1:$PORT; stand-in broker socket ready"

echo "== 1. ssh -R: the far end's socket forwards to broker.sock through the local ssh client"
bg "${SSH[@]}" -N -o ExitOnForwardFailure=yes -R "$W/r:$W/b" "$DEST"
wait_sock "$W/r" && "$PY" -c "$CLIENT" "$W/r" via-R || echo "  the -R socket never appeared"

echo "== 2. ssh -L: a local socket forwards to broker.sock through sshd's session process"
bg "${SSH[@]}" -N -o ExitOnForwardFailure=yes -L "$W/l:$W/b" "$DEST"
wait_sock "$W/l" && "$PY" -c "$CLIENT" "$W/l" via-L || echo "  the -L socket never appeared"

echo "== 3. ssh <dest> <command>: a client of broker.sock started over an ssh login"
"${SSH[@]}" -T "$DEST" "$PY" -c "'$CLIENT'" "$W/b" via-ssh-command </dev/null

sleep 0.5
echo "== what the broker side saw (one line per connection; the chain is cut at this script)"
echo "   relay_peer: the relay rule's name for the peer; remote_login_above: the other rule;"
echo "   human_cli, refusal: switchboard's verdict with the default [security] allow_ssh_cli = false"
cat "$W/listener.log"
