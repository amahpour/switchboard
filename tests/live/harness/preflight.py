"""Abort a live run if the CLI started an MCP server or helper but switchboard's (DESIGN.md §12.4). Test-only.

switchboard's server is remote (HTTPS), so nothing in a CLI's process tree should look like an MCP server:
a user's own stdio servers (``npx``, ``uvx``, ``node ... server``, anything with ``mcp`` in its command line)
mean the run was not isolated from the user's config.
"""

from __future__ import annotations

import re
import subprocess

SUSPECT = re.compile(r"mcp|npx|uvx|node .*server", re.IGNORECASE)
# switchboard's own: a plugin's `curl` posting to the broker's /mcp endpoints, and its hook scripts
ALLOWED = (
    re.compile(r"curl .* \S+/mcp/(listen|session|event|pretool)\b"),
    re.compile(r"/plugins/(claude|codex|cursor|devin)/"),
)


def parse(ps_output: str) -> list[tuple[int, int, str]]:
    """``ps -A -o pid=,ppid=,args=`` as (pid, ppid, args)."""
    procs: list[tuple[int, int, str]] = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            procs.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
        except ValueError:
            continue
    return procs


def descendants(root: int, procs: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    kids: dict[int, list[tuple[int, int, str]]] = {}
    for p in procs:
        kids.setdefault(p[1], []).append(p)
    found: list[tuple[int, int, str]] = []
    stack = [root]
    while stack:
        for c in kids.get(stack.pop(), []):
            found.append(c)
            stack.append(c[0])
    return found


def problems(
    root: int, allowed: tuple[str, ...] = (), procs: list[tuple[int, int, str]] | None = None
) -> list[str]:
    """Descendants of ``root`` that look like another MCP server, minus the ones whose command line holds
    one of the ``allowed`` words (a CLI's own helper)."""
    if procs is None:
        out = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True, timeout=10
        )
        procs = parse(out.stdout)
    return [
        f"{pid}: {args[:120]}"
        for pid, _ppid, args in descendants(root, procs)
        if SUSPECT.search(args)
        and not any(a in args for a in allowed)
        and not any(a.search(args) for a in ALLOWED)
    ]
