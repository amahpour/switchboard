"""A stand-in Claude Code session for contract tests (DESIGN.md §12.2).

``FakeClaude`` runs ``fakes/fake_harness.py`` copied to a path ending in
``/claude`` (so the broker's argv matcher sees Claude): it writes the
sessions registry, starts ``switchboard mcp`` as its child and runs hook commands
as its children, so the broker's real ancestry checks pass. With
``inbox=True`` it also serves a fake inbox socket (``FakeInbox``) at the
``CLAUDE_CODE_MESSAGING_SOCKET`` it gives the MCP server; otherwise that path
never exists, so nothing can post to it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from conftest import InProcBroker, child_env
from fakes.fake_claude_inbox import FakeInbox
from switchboard.install.common import hook_command
from switchboard.paths import hook_sha12

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads" / "claude"
FAKE = Path(__file__).resolve().parent / "fake_harness.py"
SID = "00000000-0000-4000-8000-00000000c1a0"
TOKEN = "tok-not-a-secret"


def fixture(name: str, **over: Any) -> dict[str, Any]:
    d = json.loads((FIX / f"{name}.json").read_text())
    d = {k: v for k, v in d.items() if not k.startswith("_")}
    d["session_id"] = SID
    d.update(over)
    return d


class FakeClaude:
    """A fake harness process; ``as_harness="codex"`` makes it a Codex daemon stand-in."""

    def __init__(self, b: InProcBroker, as_harness: str = "claude", *, inbox: bool = False,
                 reg_socket: str | None = None, token: bool = True):
        self.b = b
        self.bindir = Path(tempfile.mkdtemp(prefix="yk-fc-", dir="/tmp"))
        exe = self.bindir / as_harness
        shutil.copy(FAKE, exe)
        self.inbox_path = str(self.bindir / "inbox.sock")
        self.inbox: FakeInbox | None = FakeInbox(self.inbox_path) if inbox else None
        extra = dict(YK_FAKE_HOME=str(b.paths.home))
        if as_harness == "claude":
            extra.update(
                YK_FAKE_SESSIONS=b.cfg.claude.sessions_dir,
                CLAUDECODE="1",
                CLAUDE_CODE_MESSAGING_SOCKET=self.inbox_path,
                CLAUDE_CODE_SESSION_ID=SID,
            )
            if token:
                extra["CLAUDE_CODE_MESSAGING_TOKEN"] = TOKEN
            if reg_socket:
                extra["YK_FAKE_REG_SOCKET"] = reg_socket
        else:
            extra.update(YK_FAKE_CLIENT="codex-tui")
        env = child_env(**extra)
        self.p = subprocess.Popen([sys.executable, str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  env=env, text=True)
        hello = self.recv()
        assert hello.get("ready"), hello
        self.pid = hello["pid"]

    # the old name, used by the M2 contract tests
    @property
    def inbox_sock(self) -> str:
        return self.inbox_path

    def recv(self) -> dict[str, Any]:
        line = self.p.stdout.readline()
        assert line, "fake claude died"
        return json.loads(line)

    def tool(self, name: str, meta: dict[str, Any] | None = None, **args: Any) -> dict[str, Any]:
        self.p.stdin.write(json.dumps({"op": "tool", "name": name, "args": args, "meta": meta}) + "\n")
        self.p.stdin.flush()
        return self.recv()["result"]

    def hook(self, payload: dict[str, Any], event: str | None = None) -> str:
        ev = event or payload["hook_event_name"]
        cmd = hook_command(sys.executable, str(self.b.paths.home), hook_sha12(), "claude", ev)
        self.p.stdin.write(json.dumps({"op": "hook", "command": cmd, "payload": payload}) + "\n")
        self.p.stdin.flush()
        r = self.recv()
        assert r["rc"] == 0
        return r["stdout"]

    # ------------------------------------------------------------- registry
    @property
    def registry_path(self) -> Path:
        return Path(self.b.cfg.claude.sessions_dir) / f"{self.pid}.json"

    def set_registry(self, status: str, *, updated_ms: int | None = None) -> None:
        """Rewrite the session's registry file the way Claude Code does."""
        p = self.registry_path
        d = json.loads(p.read_text())
        d["status"] = status
        d["statusUpdatedAt"] = int(time.time() * 1000) if updated_ms is None else updated_ms
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(p)

    def close(self) -> None:
        if self.p.poll() is None:
            try:
                self.p.stdin.write(json.dumps({"op": "quit"}) + "\n")
                self.p.stdin.flush()
                self.p.wait(10)
            except Exception:
                self.p.kill()
                self.p.wait(5)
        for f in (self.p.stdin, self.p.stdout):
            try:
                f.close()
            except Exception:
                pass
        if self.inbox is not None:
            self.inbox.close()
        shutil.rmtree(self.bindir, ignore_errors=True)
