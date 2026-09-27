"""A fake Claude inbox socket (DESIGN.md §12.2): a UDS NDJSON recorder.

It accepts connections like ``$CLAUDE_CODE_MESSAGING_SOCKET`` does (never
replies, never closes first) and records, per connection, every line and how
long the poster held the connection open. Tests read ``frames`` (one entry
per ``user`` line, with the auth line of its connection) to see what switchboard
posted, and play Claude's part by firing the UserPromptSubmit hook.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Conn:
    t_accept: float
    lines: list[dict[str, Any]] = field(default_factory=list)
    raw: bytes = b""
    t_close: float | None = None

    @property
    def held_s(self) -> float | None:
        return None if self.t_close is None else self.t_close - self.t_accept


class FakeInbox:
    def __init__(self, path: str):
        self.path = str(path)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        os.chmod(self.path, 0o600)
        self.srv.listen(16)
        self.srv.settimeout(0.05)
        self.conns: list[Conn] = []
        self.lock = threading.Lock()
        self._stop = False
        self.t = threading.Thread(target=self._loop, name="fake-inbox", daemon=True)
        self.t.start()

    def _loop(self) -> None:
        while not self._stop:
            try:
                c, _ = self.srv.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            rec = Conn(t_accept=time.time())
            with self.lock:
                self.conns.append(rec)
            threading.Thread(target=self._read, args=(c, rec), daemon=True).start()

    def _read(self, c: socket.socket, rec: Conn) -> None:
        c.settimeout(10)
        buf = b""
        try:
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        obj = {"_bad": line.decode(errors="replace")}
                    with self.lock:
                        rec.lines.append(obj)
                        rec.raw += line + b"\n"
        except OSError:
            pass
        finally:
            rec.t_close = time.time()
            c.close()

    # ---------------------------------------------------------------- views
    def frames(self) -> list[tuple[Conn, dict[str, Any]]]:
        """(connection, user frame) pairs in arrival order."""
        with self.lock:
            return [(c, obj) for c in self.conns for obj in c.lines if obj.get("type") == "user"]

    def texts(self) -> list[str]:
        return [f["message"]["content"] for _c, f in self.frames()]

    def wait_frames(self, n: int, timeout: float = 5.0) -> list[tuple[Conn, dict[str, Any]]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            fr = self.frames()
            if len(fr) >= n:
                return fr
            time.sleep(0.02)
        return self.frames()

    def close(self) -> None:
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass
