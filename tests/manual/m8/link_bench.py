"""Round trips of JSON lines over a spawned link's stdio (forced_command.sh, README.md here).

    python link_bench.py ARGV_JSON [N]

Spawns ARGV_JSON (an ``ssh ...`` argv whose forced command is echo_link.py, or
echo_link.py itself for the baseline) with socketpair stdio, as the broker's
RemoteManager will (DESIGN.md §27.4.1), reads the hello line, then times N
round trips of a ~250-byte frame and one 1 MiB frame. Prints one JSON line.
"""

from __future__ import annotations

import json
import socket
import statistics
import subprocess
import sys
import time


def main() -> None:
    argv = json.loads(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    ours, theirs = socket.socketpair()
    t0 = time.monotonic()
    p = subprocess.Popen(argv, stdin=theirs, stdout=theirs, stderr=subprocess.PIPE)
    theirs.close()

    def readline() -> bytes:
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = ours.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
        return buf

    hello = json.loads(readline())
    setup_ms = (time.monotonic() - t0) * 1000

    def rt(obj: dict) -> bytes:
        ours.sendall((json.dumps(obj) + "\n").encode())
        return readline()

    lat = []
    for i in range(n):
        a = time.perf_counter()
        r = rt({"t": "req", "c": 1, "line": {"id": i, "method": "hook.event", "params": {"x": "y" * 200}}})
        lat.append((time.perf_counter() - a) * 1000)
        assert r, "link closed"
    a = time.perf_counter()
    r = rt(
        {"t": "req", "c": 2, "line": {"id": 1, "method": "agent.say", "params": {"text": "z" * (1 << 20)}}}
    )
    big_ms = (time.perf_counter() - a) * 1000
    ours.shutdown(socket.SHUT_WR)
    ours.close()
    rc = p.wait(10)
    err = p.stderr.read().decode(errors="replace").strip() if p.stderr else ""
    lat.sort()
    print(
        json.dumps(
            {
                "hello": hello,
                "setup_ms": round(setup_ms, 1),
                "n": n,
                "p50_ms": round(statistics.median(lat), 3),
                "p95_ms": round(lat[int(n * 0.95)], 3),
                "max_ms": round(lat[-1], 3),
                "frame_1MiB_ms": round(big_ms, 1),
                "frame_1MiB_ok": len(r) > (1 << 20),
                "rc": rc,
                "stderr": err[-200:],
            }
        )
    )


if __name__ == "__main__":
    main()
