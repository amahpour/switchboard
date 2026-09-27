"""A fake Codex app-server for contract tests (DESIGN.md §12.2).

WebSocket over a Unix socket (``websockets`` ``unix_serve``), JSON-RPC 2.0
without the ``jsonrpc`` field, in its own thread and event loop. It speaks
just enough of the protocol for switchboard: ``initialize``, ``thread/loaded/list``,
``thread/read`` (whose history carries a canary secret; like 0.156.1 it shows
an in-progress turn without its items), ``turn/start`` and
``turn/steer`` (``-32600`` when there is no such active turn). It records
every message it receives, answers any other method with ``-32601`` and
records that too, broadcasts ``thread/status/changed`` / ``thread/closed`` to
every connection, and can send an unsolicited approval request, recording
any answer (switchboard must never answer one).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
from typing import Any

CANARY = "sk-canary-codex-7f3a9c1e5b"
KNOWN = frozenset({"initialize", "initialized", "thread/loaded/list", "thread/read", "turn/start", "turn/steer"})


class FakeCodexDaemon:
    def __init__(self, path: str):
        self.path = path
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.server: Any = None
        self.conns: dict[int, Any] = {}
        self._cid = itertools.count(1)
        self._turn = itertools.count(1)
        self._req = itertools.count(9000)
        self.received: list[tuple[int, dict[str, Any]]] = []  # (conn id, message)
        self.loaded: set[str] = set()
        self.threads: dict[str, dict[str, Any]] = {}
        self.server_requests: dict[int, str] = {}  # id -> method
        self.answers: list[dict[str, Any]] = []  # client responses to our server requests
        self.fail_next: dict[str, tuple[int, str]] = {}  # method -> (code, message) once
        self.steers_land = True  # a steer's text enters the active turn's history
        self.auto_idle_s: float | None = None  # after turn/start, go idle after this long
        # 0.156.1: thread/read lists an in-progress turn (id, status) without its items (M4 live)
        self.hide_in_progress_items = True
        self.lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "FakeCodexDaemon":
        ready = threading.Event()

        def run() -> None:
            from websockets.asyncio.server import unix_serve

            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            async def main() -> None:
                self.server = await unix_serve(self._handler, self.path, ping_interval=None, compression=None)
                ready.set()
                await self.server.wait_closed()

            try:
                self.loop.run_until_complete(main())
            finally:
                self.loop.close()

        self.thread = threading.Thread(target=run, name="fake-codex", daemon=True)
        self.thread.start()
        assert ready.wait(5), "fake codex daemon did not start"
        return self

    def stop(self) -> None:
        if self.loop is None or self.server is None or self.loop.is_closed():
            return

        async def close() -> None:
            self.server.close()
            for ws in list(self.conns.values()):
                await ws.close()

        try:
            asyncio.run_coroutine_threadsafe(close(), self.loop).result(5)
        except Exception:
            pass
        if self.thread is not None:
            self.thread.join(5)

    def _call(self, coro: Any) -> Any:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(5)

    # ------------------------------------------------------------- threads
    def add_thread(self, tid: str, status: str = "idle", *, loaded: bool = True, ephemeral: bool = False,
                   thread_source: str | None = None) -> None:
        """``ephemeral`` with ``thread_source="thread_title"``: like 0.157.0's title
        thread (the app-server's own, no TUI)."""
        with self.lock:
            self.threads[tid] = {"id": tid, "status": {"type": status} if status != "active" else
                                 {"type": "active", "activeFlags": []}, "ephemeral": ephemeral,
                                 "threadSource": thread_source, "turns": [
                                     {"id": "turn-0", "status": "completed", "items": [
                                         {"type": "agentMessage", "id": "i0", "text": f"the key is {CANARY}"}]}]}
            if loaded:
                self.loaded.add(tid)

    def add_item(self, tid: str, item: dict[str, Any]) -> None:
        with self.lock:
            self.threads[tid]["turns"][-1]["items"].append(item)

    def prove(self, tid: str, join_text: str) -> None:
        """The join's MCP tool result, as it would appear in the thread's history."""
        self.add_item(tid, {"type": "mcpToolCall", "id": "join", "server": "switchboard", "tool": "join",
                            "status": "completed", "result": {"content": [{"type": "text", "text": join_text}]}})

    def begin_turn(self, tid: str) -> str:
        """The human typed a prompt: a turn in progress."""
        turn = f"turn-{next(self._turn)}"
        with self.lock:
            self.threads[tid]["turns"].append({"id": turn, "status": "inProgress", "items": [
                {"type": "userMessage", "id": f"u-{turn}", "content": [{"type": "text", "text": "human prompt"}]}]})
        self.set_status(tid, "active")
        return turn

    def end_turn(self, tid: str, status: str = "completed") -> None:
        with self.lock:
            for t in self.threads[tid]["turns"]:
                if t["status"] == "inProgress":
                    t["status"] = status
        self.set_status(tid, "idle")

    def set_status(self, tid: str, kind: str, flags: list[str] | None = None) -> None:
        st: dict[str, Any] = {"type": kind}
        if kind == "active":
            st["activeFlags"] = list(flags or [])
        with self.lock:
            if tid in self.threads:
                self.threads[tid]["status"] = st
        self.broadcast({"method": "thread/status/changed", "params": {"threadId": tid, "status": st}})

    def close_thread(self, tid: str) -> None:
        with self.lock:
            self.loaded.discard(tid)
        self.broadcast({"method": "thread/closed", "params": {"threadId": tid}})

    def broadcast(self, msg: dict[str, Any]) -> None:
        async def go() -> None:
            for ws in list(self.conns.values()):
                try:
                    await ws.send(json.dumps(msg))
                except Exception:
                    pass

        self._call(go())

    def send_server_request(self, method: str = "item/commandExecution/requestApproval") -> int:
        rid = next(self._req)
        self.server_requests[rid] = method
        self.broadcast({"id": rid, "method": method, "params": {"threadId": "t", "itemId": "x", "command": "rm -rf /"}})
        return rid

    # ------------------------------------------------------------ records
    def calls(self, method: str) -> list[dict[str, Any]]:
        with self.lock:
            return [m.get("params") or {} for _c, m in self.received if m.get("method") == method and "id" in m]

    def methods(self) -> set[str]:
        with self.lock:
            return {m["method"] for _c, m in self.received if isinstance(m.get("method"), str)}

    def open_connections(self) -> int:
        return len(self.conns)

    # ------------------------------------------------------------ protocol
    async def _handler(self, ws: Any) -> None:
        cid = next(self._cid)
        self.conns[cid] = ws
        try:
            async for frame in ws:
                msg = json.loads(frame)
                with self.lock:
                    self.received.append((cid, msg))
                if "method" not in msg:
                    if msg.get("id") in self.server_requests:
                        with self.lock:
                            self.answers.append(msg)
                    continue
                if "id" not in msg:
                    continue  # a notification (initialized)
                out = self._answer(msg["method"], msg.get("params") or {})
                out["id"] = msg["id"]
                await ws.send(json.dumps(out))
                if msg["method"] == "turn/start" and "result" in out and self.auto_idle_s is not None:
                    tid = msg["params"]["threadId"]
                    asyncio.get_running_loop().call_later(self.auto_idle_s, lambda t=tid: self._end_soon(t))
        except Exception:
            pass
        finally:
            self.conns.pop(cid, None)

    def _end_soon(self, tid: str) -> None:
        with self.lock:
            for t in self.threads[tid]["turns"]:
                if t["status"] == "inProgress":
                    t["status"] = "completed"
            self.threads[tid]["status"] = {"type": "idle"}
        msg = json.dumps({"method": "thread/status/changed", "params": {"threadId": tid, "status": {"type": "idle"}}})
        for ws in list(self.conns.values()):
            asyncio.ensure_future(ws.send(msg))

    def _err(self, code: int, message: str) -> dict[str, Any]:
        return {"error": {"code": code, "message": message}}

    def _answer(self, method: str, p: dict[str, Any]) -> dict[str, Any]:
        if method in self.fail_next:
            code, message = self.fail_next.pop(method)
            return self._err(code, message)
        if method not in KNOWN:
            return self._err(-32601, f"method not found: {method}")
        if method == "initialize":
            return {"result": {"userAgent": "fake/0.156.1", "codexHome": "/fake", "platformFamily": "unix",
                               "platformOs": "macos"}}
        if method == "thread/loaded/list":
            with self.lock:
                return {"result": {"data": sorted(self.loaded), "nextCursor": None}}
        tid = p.get("threadId")
        with self.lock:
            th = self.threads.get(tid)
            if th is None:
                return self._err(-32600, "thread not found")
            if method == "thread/read":
                turns = json.loads(json.dumps(th["turns"])) if p.get("includeTurns") else []
                if self.hide_in_progress_items:
                    for t in turns:
                        if t["status"] == "inProgress":
                            t["items"] = []
                view = {"id": tid, "status": th["status"], "preview": f"secret {CANARY}", "cwd": "/ws",
                        "ephemeral": th.get("ephemeral", False), "threadSource": th.get("threadSource"),
                        "turns": turns}
                return {"result": {"thread": view}}
            text = "".join(i.get("text", "") for i in p.get("input") or [])
            item = {"type": "userMessage", "id": f"m{time.time_ns()}", "clientId": p.get("clientUserMessageId"),
                    "content": [{"type": "text", "text": text}]}
            if method == "turn/start":
                if tid not in self.loaded:
                    return self._err(-32600, "thread not found")
                active = [t for t in th["turns"] if t["status"] == "inProgress"]
                if active:  # merged into the running turn, like a steer
                    active[-1]["items"].append(item)
                    return {"result": {"turn": {"id": active[-1]["id"], "status": "inProgress", "items": []}}}
                turn = f"turn-{next(self._turn)}"
                th["turns"].append({"id": turn, "status": "inProgress", "items": [item]})
                th["status"] = {"type": "active", "activeFlags": []}
                note = {"method": "thread/status/changed", "params": {"threadId": tid, "status": th["status"]}}
                for ws in list(self.conns.values()):
                    asyncio.ensure_future(ws.send(json.dumps(note)))
                return {"result": {"turn": {"id": turn, "status": "inProgress", "items": []}}}
            # turn/steer
            active = [t for t in th["turns"] if t["status"] == "inProgress"]
            if not active:
                return self._err(-32600, "no active turn to steer")
            if active[-1]["id"] != p.get("expectedTurnId"):
                return self._err(-32600, f"expected active turn id {p.get('expectedTurnId')} but found"
                                         f" {active[-1]['id']}")
            if self.steers_land:
                active[-1]["items"].append(item)
            return {"result": {"turnId": active[-1]["id"]}}
