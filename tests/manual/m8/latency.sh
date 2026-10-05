#!/bin/bash
# The cost of SSH on the paths a remote member uses (DESIGN.md §27.4.9). README.md here.
#
# Against an echo server on a Unix socket, directly and through an ssh -R socket
# forward on a loopback sshd:
#   - a new connection plus one JSON-line round trip (what each hook run pays);
#   - a round trip on a connection kept open (an MCP server's BrokerConn).
# The link itself (a forced command's stdio, which M8 uses instead of a forward) is
# measured by forced_command.sh. User-level sshd on 127.0.0.1; see lib.sh.
# shellcheck source=tests/manual/m8/lib.sh
. "$(dirname "$0")/lib.sh"

bg "$PY" -c '
import socket, sys, threading
s = socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(64)
def echo(c):
    for line in c.makefile("rb"):
        c.sendall(line)
    c.close()
while True:
    c, _ = s.accept()
    threading.Thread(target=echo, args=(c,), daemon=True).start()
' "$W/b"
wait_sock "$W/b" || { echo "echo server did not start" >&2; exit 1; }
start_sshd "@CLIENT_KEY@" "AllowStreamLocalForwarding yes" "StreamLocalBindUnlink yes"
bg "${SSH[@]}" -N -o ExitOnForwardFailure=yes -R "$W/r:$W/b" "$DEST"
wait_sock "$W/r" || { echo "the -R socket never appeared" >&2; exit 1; }

"$PY" - "$W/b" "$W/r" <<'EOF'
import socket, statistics, sys, time

def fresh(path, n=200):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        s = socket.socket(socket.AF_UNIX); s.connect(path)
        s.sendall(b'{"id":1}\n'); s.recv(100); s.close()
        out.append((time.perf_counter() - t) * 1000)
    return out

def kept(path, n=500):
    s = socket.socket(socket.AF_UNIX); s.connect(path)
    out = []
    for _ in range(n):
        t = time.perf_counter(); s.sendall(b'{"id":1}\n'); s.recv(100)
        out.append((time.perf_counter() - t) * 1000)
    s.close()
    return out

def q(v):
    v = sorted(v)
    return f"p50 {statistics.median(v):.3f} ms  p95 {v[int(len(v) * 0.95)]:.3f} ms  max {v[-1]:.3f} ms"

for name, path in (("direct", sys.argv[1]), ("ssh -R", sys.argv[2])):
    fresh(path, 20); kept(path, 20)  # warm up
    print(f"{name:7s} new connection + round trip:    {q(fresh(path))}")
    print(f"{name:7s} round trip on an open connection: {q(kept(path))}")
EOF
