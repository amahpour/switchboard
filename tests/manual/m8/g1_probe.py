#!/usr/bin/env python3
"""Gate G1, part 1 (DESIGN.md §27.15): does Claude Code on this machine give its MCP
servers the inbox and write the session registry the satellite checks (§27.5.3)?

A minimal stdio MCP server (stdlib only). Claude Code starts it; it records, for the
session that started it, which ``CLAUDE_CODE_MESSAGING_*`` names are set, whether the
socket is an absolute path to a socket, and whether ``~/.claude/sessions/<parent
pid>.json`` names the same socket, a ``pid`` and a ``status``. Names and booleans
only: never the token's value, never the socket path. It reads only its own parent's
session file, at start, after 1, 3, 6 and 10 s, and when its one tool is called.
Records go to ``g1-record-<pid>.json`` next to this script, or in ``$G1_OUT``.
See README.md for the command (per-invocation flags only; no settings file is edited).
"""

import json
import os
import stat
import sys
import threading
import time

OUT = os.path.join(
    os.environ.get("G1_OUT") or os.path.dirname(os.path.abspath(__file__)), f"g1-record-{os.getpid()}.json"
)
PPID = os.getppid()


def parent_name() -> str:
    try:
        with open(f"/proc/{PPID}/cmdline", "rb") as f:
            argv0 = f.read().split(b"\0")[0].decode(errors="replace")
        return os.path.basename(argv0)
    except OSError:
        return "?"


def snapshot(tag: str) -> dict:
    env_names = sorted(k for k in os.environ if k.startswith("CLAUDE_CODE_MESSAGING_"))
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    sock_is_socket = False
    if sock:
        try:
            sock_is_socket = stat.S_ISSOCK(os.stat(sock).st_mode)
        except OSError:
            sock_is_socket = False
    reg_path = os.path.expanduser(f"~/.claude/sessions/{PPID}.json")
    reg = None
    try:
        with open(reg_path, encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError):
        reg = None
    rec = {
        "tag": tag,
        "t": round(time.time(), 1),
        "parent_argv0_basename": parent_name(),
        "env_names": env_names,
        "socket_abs": bool(sock and sock.startswith("/")),
        "socket_is_socket": sock_is_socket,
        "session_file_exists": reg is not None,
    }
    if isinstance(reg, dict):
        st = reg.get("status")
        rec.update(
            {
                "session_file_keys": sorted(reg.keys()),
                "session_pid_is_parent": reg.get("pid") == PPID,
                "session_socket_matches_env": bool(sock) and reg.get("messagingSocketPath") == sock,
                "session_status": st if isinstance(st, str) and len(st) <= 32 else None,
                "session_has_statusUpdatedAt": "statusUpdatedAt" in reg,
            }
        )
    return rec


RECORDS: list = []
LOCK = threading.Lock()


def record(tag: str) -> dict:
    r = snapshot(tag)
    with LOCK:
        RECORDS.append(r)
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(RECORDS, f, indent=1)
        os.replace(tmp, OUT)
    return r


def later() -> None:
    for d in (1, 3, 6, 10):
        time.sleep(d)
        record(f"after_{d}s")


def reply(rid, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


TOOL = {
    "name": "g1_probe",
    "description": "Records whether this session exposes its messaging socket to MCP servers. Call it once.",
    "inputSchema": {"type": "object", "properties": {}},
}


def main() -> None:
    record("start")
    threading.Thread(target=later, daemon=True).start()
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid, method = req.get("id"), req.get("method")
        if rid is None:
            continue  # a notification
        if method == "initialize":
            pv = (req.get("params") or {}).get("protocolVersion", "2025-06-18")
            reply(
                rid,
                {
                    "protocolVersion": pv,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "g1probe", "version": "1"},
                },
            )
        elif method == "tools/list":
            reply(rid, {"tools": [TOOL]})
        elif method == "tools/call":
            time.sleep(2)
            r = record("tool_call")
            summary = {
                k: r.get(k)
                for k in ("env_names", "session_file_exists", "session_socket_matches_env", "session_status")
            }
            reply(rid, {"content": [{"type": "text", "text": json.dumps(summary)}], "isError": False})
        elif method == "ping":
            reply(rid, {})
        else:
            reply(rid, error={"code": -32601, "message": "method not found"})


if __name__ == "__main__":
    main()
