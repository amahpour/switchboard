"""Waking a Codex session on a remote machine (issue #63, DESIGN.md §27.7).

A Codex session on another machine than the broker's is woken by its own
``switchboard mcp`` server, on its own machine: the broker's ``deliver`` push reaches
it through the link (the satellite checks it is for the Codex this server was attested
under), and this module starts the turn through that machine's Codex app-server, with
the checks the broker makes before a local ``turn/start`` (``adapters/codex.py``):

- **The control socket** is this user's, in a directory only this user can write
  (``codex_rpc.check_socket``), from this home's ``[codex] control_socket``.
- **A Codex TUI is attached** to it (one ``lsof -U``, the same parsing as the broker's
  client check): a thread outlives its TUI by about a minute, and a turn started then
  would run with nobody watching. No ``lsof`` or no TUI: no wake (fail closed).
- **The thread proof** (§9.3), once per thread: a ``thread/read`` must show this
  thread's own join code (``yk:j<nonce>``, from the join result this server returned
  to it) in the result of switchboard's ``join`` tool, so a process that only knows a
  thread id can't be woken as that thread.
- **Idle, read now:** a fresh ``thread/read`` on the same connection must say ``idle``.
  ``active`` (a turn, an approval or an input wait) or unloaded: no wake.
- **``turn/start``** carries exactly ``threadId``, ``input`` and ``clientUserMessageId``
  (``codex_rpc`` refuses anything else), on a fresh, never-subscribed connection.

The thread is always one this server served: the broker names it, and it must be a
thread that joined through this very process (``_meta.threadId`` of its ``join``).
Thread contents are searched in memory only, never logged.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from switchboard.adapters.codex_rpc import (
    CodexRpc,
    RpcError,
    SocketRefused,
    check_socket,
    join_proven,
    one_shot,
    thread_status,
    turn_start_params,
)

# refusal codes, reported in ``mcp.posted {ok: false, err}`` (short codes only)
NO_DAEMON = "no_daemon"
NO_TUI = "no_tui"
UNPROVEN = "unproven"
NOT_IDLE = "not_idle"
NOT_LOADED = "not_loaded"
RPC_FAILED = "rpc_failed"


class Refused(Exception):
    """No turn was started. ``code`` is one of the constants above."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def usable_socket(path: str | None) -> str | None:
    """The resolved control socket, if it's there and safely ours; else None."""
    if not path:
        return None
    try:
        return check_socket(path)
    except SocketRefused:
        return None


def tui_attached(sock: str) -> bool:
    """Whether a Codex TUI (a human's session, not an app-server, ``exec`` or ``queue``
    run) is connected to the control socket at ``sock``, the configured path: ``lsof``
    names a socket by the path it was bound with, which may be this one or where it
    resolves (``/tmp`` is ``/private/tmp`` on macOS), so both are matched, as the
    broker's own check does. Fails closed: no ``lsof``, a failed run or an argv that
    can't be read all say no."""
    from switchboard.adapters.codex import _run_lsof, is_tui_argv, lsof_bin, parse_lsof, socket_peers
    from switchboard.broker import proc

    lsof = lsof_bin()
    if lsof is None:
        return False
    try:
        out = _run_lsof(lsof)
    except Exception:
        return False
    recs = parse_lsof(out)
    names = {sock, os.path.realpath(sock)}
    _servers, clients = socket_peers(recs, names)
    clients.discard(os.getpid())
    infos = [i for i in (proc.info(pid) for pid in sorted(clients)) if i is not None]
    if not infos:
        return False
    argvs = proc.argv_many(infos)
    return any(is_tui_argv(argvs.get(i.pid, "")) for i in infos)


def client_id(batch_id: int) -> str:
    """The same ``clientUserMessageId`` the broker gives a local turn/start."""
    return f"yk-b{batch_id}"


async def wake(sock_path: str | None, thread_id: str, nonce: str, text: str, batch_id: int,
               proven: set[str]) -> float:
    """Start a turn in ``thread_id`` with ``text``; returns when the app-server accepted
    it (epoch s). ``proven`` holds the threads whose proof already passed (updated here).
    Raises ``Refused`` when no turn was started."""
    real = usable_socket(sock_path)
    if real is None:
        raise Refused(NO_DAEMON)
    assert sock_path is not None  # usable_socket said yes
    if not await asyncio.to_thread(tui_attached, sock_path):
        raise Refused(NO_TUI)

    async def go(r: CodexRpc) -> Any:
        need_proof = thread_id not in proven
        th = await r.read_thread(thread_id, include_turns=need_proof)
        if need_proof:
            if not join_proven(th, f"yk:j{nonce}"):
                raise Refused(UNPROVEN)
            proven.add(thread_id)
        st = thread_status(th.get("status"))
        if st != "idle":
            raise Refused(NOT_LOADED if st in (None, "offline") else NOT_IDLE)
        return await r.request("turn/start", turn_start_params(thread_id, text, client_id(batch_id)))

    try:
        await one_shot(real, go)
    except Refused:
        raise
    except (RpcError, OSError, ConnectionError, SocketRefused, TimeoutError, asyncio.TimeoutError):
        raise Refused(RPC_FAILED) from None
    return time.time()
