"""Stand-in Cursor Agent CLI and Devin CLI sessions for contract tests (DESIGN.md §12.2).

``FakeCli(broker, "cursor")`` runs ``fakes/fake_harness.py`` copied to a path
ending in ``/cursor-agent`` (the broker's Cursor argv matcher) with the MCP
``clientInfo`` name ``Cursor``; ``FakeCli(broker, "devin")`` runs it as
``<dir>/devin acp`` with ``DEVIN_PROJECT_DIR`` in the env its hooks see, as
Devin's native hooks do. Either way ``switchboard mcp`` and every hook command are
its children, so the broker's real ancestry checks pass. Payloads are the
recorded M0 fixtures (``tests/fixtures/payloads/{cursor,devin}``).
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from conftest import InProcBroker, child_env

from switchboard.install.common import hook_command
from switchboard.paths import hook_sha12

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads"
FAKE = Path(__file__).resolve().parent / "fake_harness.py"
CONV = "00000000-0000-4000-8000-0000000c0de1"
DEVIN_SID = "fuzzy-fixture"


def fixture(harness: str, name: str, **over: Any) -> dict[str, Any]:
    d = json.loads((FIX / harness / f"{name}.json").read_text())
    d = {k: v for k, v in d.items() if not k.startswith("_")}
    if harness == "cursor":
        d["conversation_id"] = d["session_id"] = over.pop("conv", CONV)
    else:
        d["session_id"] = over.pop("sid", DEVIN_SID)
    d.update(over)
    return d


class FakeCli:
    _tags = itertools.count(1)

    def __init__(
        self,
        b: InProcBroker | None,
        harness: str,
        *,
        home: str | Path | None = None,
        env: dict[str, str] | None = None,
    ):
        """``home``: another switchboard home than the broker's (a session on a remote host)."""
        assert harness in ("cursor", "devin")
        self.b = b
        self.home = str(home) if home is not None else str(b.paths.home)  # type: ignore[union-attr]
        self.harness = harness
        self.bindir = Path(tempfile.mkdtemp(prefix="yk-fx-", dir="/tmp"))
        exe = self.bindir / ("cursor-agent" if harness == "cursor" else "devin")
        shutil.copy(FAKE, exe)
        extra = dict(YK_FAKE_HOME=self.home, **(env or {}))
        argv = [sys.executable, str(exe)]
        if harness == "cursor":
            extra["YK_FAKE_CLIENT"] = "Cursor"
        else:
            extra["YK_FAKE_CLIENT"] = "devin"
            extra["DEVIN_PROJECT_DIR"] = "/ws"
            argv.append("acp")
        self.p = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=child_env(**extra), text=True
        )
        hello = self.recv()
        assert hello.get("ready"), hello
        self.pid = hello["pid"]

    def recv(self) -> dict[str, Any]:
        line = self.p.stdout.readline()
        assert line, "fake harness died"
        return json.loads(line)

    def send(self, obj: dict[str, Any]) -> dict[str, Any]:
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()
        return self.recv()

    # ---------------------------------------------------------------- tools
    def tool(self, name: str, **args: Any) -> dict[str, Any]:
        return self.send({"op": "tool", "name": name, "args": args})["result"]

    def tool_bg(self, name: str, **args: Any) -> str:
        tag = f"t{next(self._tags)}"
        assert self.send({"op": "tool", "name": name, "args": args, "tag": tag}) == {"started": tag}
        return tag

    # ---------------------------------------------------------------- hooks
    def command(self, event: str, max_wait: int | None = None) -> str:
        return hook_command(sys.executable, self.home, hook_sha12(), self.harness, event, max_wait)

    def hook(self, payload: dict[str, Any], *, max_wait: int | None = None) -> str:
        r = self.send(
            {"op": "hook", "command": self.command(payload["hook_event_name"], max_wait), "payload": payload}
        )
        assert r["rc"] == 0, r
        return r["stdout"]

    def hook_bg(self, payload: dict[str, Any], *, max_wait: int | None = None, timeout: float = 60) -> str:
        tag = f"h{next(self._tags)}"
        r = self.send(
            {
                "op": "hook",
                "command": self.command(payload["hook_event_name"], max_wait),
                "payload": payload,
                "tag": tag,
                "timeout": timeout,
            }
        )
        assert r == {"started": tag}
        return tag

    def collect(self, tag: str, timeout: float = 30) -> dict[str, Any]:
        return self.send({"op": "collect", "tag": tag, "timeout": timeout})

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
        shutil.rmtree(self.bindir, ignore_errors=True)
