"""A stand-in broker.sock that prints what the broker would see of each peer (README.md here).

    python peer_listener.py SOCKET_PATH

One JSON line per accepted connection: the kernel peer's pid and argv, its
process chain up to (not including) the process named by the LAUNCHER env var
(the measuring script, so the output doesn't depend on what runs it, as
tests/unit/test_peer.py's below_pytest does), and the verdicts of switchboard's
own code (``broker/peer.py``): the relay name, the SSH refusal and whether the
production policy would grant human_cli with the chain cut there. Answers each
connection with one ``pong`` line. ``$HOME`` is printed as ``~``.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from itertools import takewhile

from switchboard.broker import proc
from switchboard.broker.peer import Peer, ProcessPeerPolicy, relay_name, remote_login_name

HOME = os.path.expanduser("~")


def short(argv: str, n: int = 80) -> str:
    if HOME and HOME != "/":
        argv = argv.replace(HOME, "~")
    return argv[:n]


def serve(conn: socket.socket, launcher: int) -> None:
    peer = Peer.from_socket(conn)
    below: list[proc.ProcInfo] = []
    if peer.pid:
        chain, _ = proc.ancestry_to_root(peer.pid)
        below = list(takewhile(lambda p: p.pid != launcher, chain))
    argvs = proc.argv_many(below)
    pol = ProcessPeerPolicy(chain_fn=lambda pid: (below, True))
    check = Peer(pid=peer.pid, uid=peer.uid, start=peer.start)
    rec = {
        "peer_pid": peer.pid,
        "same_uid": peer.uid == os.getuid(),
        "peer_argv": short(argvs.get(peer.pid or 0, "")),
        "chain": [short(argvs.get(p.pid, "") or p.comm, 60) for p in below],
        "relay_peer": relay_name(argvs.get(peer.pid or 0, "")),
        "remote_login_above": sorted({n for p in below[1:] if (n := remote_login_name(argvs.get(p.pid, "")))}),
        "human_cli": pol.human_cli_allowed(check),
        "refusal": pol.refusal(check),
    }
    try:
        conn.settimeout(3)
        rec["got"] = conn.recv(4096).decode(errors="replace").strip()[:80]
        conn.sendall(b"pong\n")
    except OSError as e:
        rec["error"] = type(e).__name__
    finally:
        conn.close()
    print(json.dumps(rec), flush=True)


def main() -> None:
    path = sys.argv[1]
    launcher = int(os.environ.get("LAUNCHER", "0"))
    old = os.umask(0o077)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.umask(old)
    srv.listen(16)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve, args=(conn, launcher), daemon=True).start()


if __name__ == "__main__":
    main()
