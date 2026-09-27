"""Per-launch Codex hook trust for a live test (test-only; never in the wheel).

Asks a **private** stdio ``codex app-server`` (never the shared daemon) for
``hooks/list`` in the scratch workspace, with the test's own ``-c``
overrides, and builds the ``-c hooks.state=...`` table for one launch: the
workspace's project hooks (switchboard's, from ``install codex --print-args``)
trusted at their current hash, every other non-managed hook disabled (so the
user's own hooks don't fire in a test session). The user's ``config.toml``
is never written: Codex reads trust state from the ``-c`` session layer too.
Derived from the M0 Codex hooks experiments.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from harness.tmuxdrv import clean_env


def stdio_calls(codex_bin: str, path: str, cwd: Path, overrides: list[str],
                calls: list[tuple[str, dict[str, Any]]], timeout: float = 60) -> list[dict[str, Any]]:
    """Run a private stdio app-server, make ``calls`` in order, return each response."""
    p = subprocess.Popen([codex_bin, "app-server", *overrides], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=clean_env(path), cwd=str(cwd), text=True,
                         start_new_session=True)
    assert p.stdin is not None and p.stdout is not None
    out: list[dict[str, Any]] = []
    try:
        def send(obj: dict[str, Any]) -> None:
            p.stdin.write(json.dumps(obj) + "\n")
            p.stdin.flush()

        def recv(want: int) -> dict[str, Any]:
            while True:
                line = p.stdout.readline()
                if not line:
                    raise RuntimeError("private app-server closed")
                msg = json.loads(line)
                if msg.get("id") == want and "method" not in msg:
                    return msg

        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "yk-live-trust", "version": "0"}}})
        recv(1)
        send({"method": "initialized"})
        for i, (method, params) in enumerate(calls, 2):
            send({"id": i, "method": method, "params": params})
            out.append(recv(i))
    finally:
        p.terminate()
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)
    return out


def hooks_state(codex_bin: str, path: str, ws: Path, overrides: list[str]) -> tuple[str, list[dict[str, Any]]]:
    """(the TOML inline table for ``-c hooks.state=``, the hooks/list entries)."""
    [res] = stdio_calls(codex_bin, path, ws, overrides, [("hooks/list", {"cwds": [str(ws)]})])
    if "error" in res:
        raise RuntimeError(f"hooks/list failed: {res['error']}")
    parts: list[str] = []
    hooks: list[dict[str, Any]] = []
    for entry in res["result"]["data"]:
        for h in entry["hooks"]:
            hooks.append({k: h.get(k) for k in ("key", "eventName", "source", "trustStatus", "enabled", "isManaged")})
            if h["source"] == "project":
                parts.append(f'{json.dumps(h["key"])}={{trusted_hash={json.dumps(h["currentHash"])}}}')
            elif not h.get("isManaged"):
                parts.append(f'{json.dumps(h["key"])}={{enabled=false}}')
    return "{" + ",".join(parts) + "}", hooks
