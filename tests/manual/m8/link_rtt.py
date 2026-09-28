"""Round trips over a live remote link, timed on the remote machine (DESIGN.md §27.4.9).

Run it on the remote (the satellite's machine) with any Python 3, while the link is up:

    python3 link_rtt.py <remote home>/run/broker.sock [N]

Two measurements, each N times (default 50), printed as p50/p95/max in ms:
- ``new``: connect to the satellite's socket, send one request, read the answer,
  close: what a hook pays (the request crosses the link to the broker and back);
- ``open``: one request on an already-open connection: what an MCP server pays.

The request is ``agent.who`` without a credential: the satellite relays it (it is
in the link's method allowlist), the broker answers it with an error, and nothing
changes anywhere. Standard library only; it writes nothing.
"""

from __future__ import annotations

import json
import socket
import statistics
import sys
import time


def request(f, n: int) -> dict:
    f.write((json.dumps({"id": n, "method": "agent.who", "params": {"room": "#none"}}) + "\n").encode())
    f.flush()
    line = f.readline()
    if not line:
        raise SystemExit("the satellite closed the connection (is the link up?)")
    return json.loads(line)


def connect(path: str):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(path)
    return s, s.makefile("rwb")


def summary(name: str, xs: list[float]) -> str:
    xs = sorted(xs)
    p95 = xs[max(0, int(len(xs) * 0.95) - 1)]
    return f"{name}: n={len(xs)} p50={statistics.median(xs):.2f} ms p95={p95:.2f} ms max={xs[-1]:.2f} ms"


def main() -> None:
    path = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    new: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        s, f = connect(path)
        r = request(f, i + 1)
        new.append((time.perf_counter() - t0) * 1000)
        s.close()
        if i == 0:
            print("first answer:", json.dumps(r)[:200])
    s, f = connect(path)
    request(f, 1)
    opened: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        request(f, i + 2)
        opened.append((time.perf_counter() - t0) * 1000)
    s.close()
    print(summary("new", new))
    print(summary("open", opened))


if __name__ == "__main__":
    main()
