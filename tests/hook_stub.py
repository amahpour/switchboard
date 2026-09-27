"""A stub broker socket for hook-script tests: records requests, answers from a function."""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


class StubBroker:
    def __init__(self, home: Path, reply: Callable[[dict[str, Any]], dict[str, Any]] | None = None):
        self.home = Path(home)
        from switchboard.paths import Paths

        self.paths = Paths.from_home(home)
        self.paths.ensure()
        self.reply = reply or (lambda req: {"out": None})
        self.requests: list[dict[str, Any]] = []
        self.acks: list[dict[str, Any]] = []
        self.peer_pids: list[int | None] = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = str(self.paths.sock)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.srv.bind(path)
        os.chmod(path, 0o600)
        self.srv.listen(64)
        self.srv.settimeout(0.02)
        self._stop = False
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self) -> None:
        while not self._stop:
            try:
                c, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket) -> None:
        from switchboard.broker.peer import peer_pid

        c.settimeout(3)
        self.peer_pids.append(peer_pid(c))
        buf = b""
        try:
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    req = json.loads(line)
                    if req.get("method") == "hook.ack":
                        self.acks.append(req["params"])
                        continue
                    self.requests.append(req)
                    res = self.reply(req)
                    c.sendall((json.dumps({"id": req.get("id"), "result": res}) + "\n").encode())
        except OSError:
            pass
        finally:
            c.close()

    def close(self) -> None:
        self._stop = True
        self.srv.close()
        self.t.join(2)
        try:
            os.unlink(str(self.paths.sock))
        except OSError:
            pass
