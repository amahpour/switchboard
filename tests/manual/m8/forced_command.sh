#!/bin/bash
# The link key (DESIGN.md §27.4.1, §27.4.3): an authorized_keys line
#   restrict,command="<python> -I echo_link.py" ssh-ed25519 ...
# carries a JSON-lines stdio link, and can do nothing else. README.md here.
#
#   1. baseline: echo_link.py spawned directly, socketpair stdio (no ssh)
#   2. the same over ssh -T through the forced command: setup time, p50/p95, a 1 MiB frame
#   3. the same key asks for a Unix-socket forward (-L), a stdio forward (-W), a pty (-tt)
#      and another command: each refused (or replaced by the forced command)
# User-level sshd on 127.0.0.1 with generated keys; see lib.sh.
. "$(dirname "$0")/lib.sh"
N="${N:-300}"

cp "$HERE/echo_link.py" "$W/echo_link.py"
FORCED="$PY -I $W/echo_link.py"
start_sshd "restrict,command=\"$FORCED\" @CLIENT_KEY@"
echo "== user-level sshd on 127.0.0.1:$PORT; the key's line: restrict,command=\"<python> -I echo_link.py\""

argv_json() { "$PY" -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@"; }

echo "== 1. baseline: echo_link.py spawned directly (no ssh), N=$N"
"$PY" "$HERE/link_bench.py" "$(argv_json "$PY" -I "$W/echo_link.py")" "$N"

echo "== 2. over ssh -T through the forced command, N=$N"
"$PY" "$HERE/link_bench.py" "$(argv_json "${SSH[@]}" -T -x -a -e none "$DEST" switchboard-satellite)" "$N"

echo "== 3a. the key asks for a Unix-socket forward (-L): the channel is refused"
"$PY" -c 'import socket, sys; s = socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(1); import time; time.sleep(30)' "$W/b" &
PIDS="$PIDS $!"
wait_sock "$W/b"
bg "${SSH[@]}" -N -L "$W/l:$W/b" "$DEST" </dev/null
if wait_sock "$W/l"; then
  "$PY" -c '
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(sys.argv[1]); s.sendall(b"{\"t\":\"forged\"}\n")
try:
    print("  through -L: got", s.recv(100) or b"EOF (refused)")
except OSError as e:
    print("  through -L:", type(e).__name__)
' "$W/l"
else
  echo "  the local end never bound"
fi

echo "== 3b. the key asks for a stdio forward (-W)"
OUT=$(with_timeout 10 "${SSH[@]}" -W "127.0.0.1:$PORT" "$DEST" </dev/null 2>&1); echo "  rc=$? $(echo "$OUT" | tr '\r\n' '  ' | cut -c1-160)"

echo "== 3c. the key asks for a pty (-tt): the forced command runs without one"
OUT=$(with_timeout 10 "${SSH[@]}" -tt "$DEST" </dev/null 2>&1); echo "  rc=$? $(echo "$OUT" | tr '\r\n' '  ' | cut -c1-200)"

echo "== 3d. the key asks for another command: the forced command runs instead"
OUT=$(with_timeout 10 "${SSH[@]}" -T "$DEST" 'echo PWNED; id' </dev/null 2>&1); RC=$?
echo "  rc=$RC output: $(echo "$OUT" | tr '\n' ' ' | cut -c1-200)"
# ok only when the forced command itself answered (its hello line), not merely when PWNED is absent
case "$OUT" in
  *PWNED*) echo "  !! the other command ran" ;;
  *'"original_command_given": true'*) echo "  ok: the forced command ran instead (it saw the other command only in SSH_ORIGINAL_COMMAND)" ;;
  *) echo "  ?? no answer from the forced command (rc=$RC): the check did not run" ;;
esac

sleep 0.3
echo "== sshd's view (refusals)"
grep -Ei 'refused|prohibited|streamlocal|pty|forced|original' "$W/sshd.log" | sed -e 's/^/  /' | cut -c1-160 | head -20
