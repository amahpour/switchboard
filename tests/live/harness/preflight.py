"""Abort a live run if the agent started any MCP server or helper but switchboard's (DESIGN.md §12.4)."""

from __future__ import annotations

import re
import subprocess

SUSPECT = re.compile(r"mcp|npx|uvx|node .*server", re.IGNORECASE)
ALLOWED = (
    re.compile(r"\s-m switchboard mcp\b"),  # switchboard's own MCP server
    re.compile(r"switchboard_hook-[0-9a-f]{12}\.py"),  # a switchboard hook in flight
    re.compile(r"harness/rawrec\.py"),  # the test's payload recorder
)


def descendants(root: int) -> list[tuple[int, int, str]]:
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True,
                         timeout=10).stdout
    procs: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            procs.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
        except ValueError:
            continue
    kids: dict[int, list[tuple[int, int, str]]] = {}
    for p in procs:
        kids.setdefault(p[1], []).append(p)
    found: list[tuple[int, int, str]] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        for c in kids.get(pid, []):
            found.append(c)
            stack.append(c[0])
    return found


def problems(root: int) -> list[str]:
    bad = []
    for pid, _ppid, args in descendants(root):
        if SUSPECT.search(args) and not any(a.search(args) for a in ALLOWED):
            bad.append(f"{pid}: {args[:120]}")
    return bad
