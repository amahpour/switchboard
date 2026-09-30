"""A Codex session on a remote host is woken on its own machine (issue #63, DESIGN.md §27.7).

The Pi session is ``fakes/fake_claude.py`` run as ``codex`` with the Pi home: its MCP server
and hooks dial the Pi home's satellite, and it can hold a "TUI" connection to a fake Codex
app-server on the Pi side, whose control socket the Pi home's ``config.toml`` names. The
broker has no Codex daemon of its own in play: every ``turn/start`` below reached the Pi's.
The satellite runs with the PID shift, so a desktop probe of a Pi pid finds no process.

Covered: the ``codex:link`` tier and its join guidance; an idle wake that becomes a
``turn/start`` with exactly three keys on the Pi's app-server, confirmed as
``link:turn/start``; the approval (and input) wait holding it until idle; the thread proof
refusing an unproven thread; no TUI attached, no wake; SessionEnd stopping wakes until the
thread runs again; pull only (``codex:hook``) without a control socket there.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from fakes.fake_claude import FakeClaude
from fakes.fake_codex_daemon import FakeCodexDaemon
from fakes.fake_link import FakeLink, wait_for
from switchboard.adapters import remote_codex
from switchboard.config import Config
from switchboard.install.common import hook_command
from switchboard.paths import hook_sha12

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
TID = "019a0000-0000-7000-8000-0000000c0de1"


class World:
    def __init__(self, *, daemon: bool = True):
        self.dir = Path(tempfile.mkdtemp(prefix="yk-cxl-", dir="/tmp"))
        os.chmod(self.dir, 0o700)
        self.sock = str(self.dir / "app-server-control.sock")
        self.d = FakeCodexDaemon(self.sock).start() if daemon else None
        self.link = FakeLink(kind="inproc", broker_cfg=FAST)
        cfg = self.link.pi / "config.toml"
        cfg.write_text(cfg.read_text() + f'[codex]\ncontrol_socket = "{self.sock}"\n')
        self.link.start()
        self.cx: FakeClaude | None = None

    def close(self) -> None:
        if self.cx is not None:
            self.cx.close()
        self.link.close()
        if self.d is not None:
            self.d.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    # ------------------------------------------------------------------ parts
    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        con = sqlite3.connect(f"file:{self.link.desk_paths.db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def part(self) -> sqlite3.Row:
        [row] = self.q("SELECT * FROM participants WHERE harness='codex'")
        return row

    def batches(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM batches WHERE path='turn_start' ORDER BY id")

    def say(self, text: str) -> int:
        return self.link.call("human.say", {"room": "#fpga", "text": text})["id"]

    def start_codex(self, *, tui: bool = True) -> None:
        self.cx = FakeClaude(None, as_harness="codex", home=self.link.pi)
        if tui:
            self.op({"op": "attach", "path": self.sock})

    def op(self, cmd: dict[str, Any]) -> dict[str, Any]:
        assert self.cx is not None
        self.cx.p.stdin.write(json.dumps(cmd) + "\n")
        self.cx.p.stdin.flush()
        return self.cx.recv()

    def join(self, *, prove: bool = True) -> dict[str, Any]:
        if self.d is not None:
            self.d.add_thread(TID)
        assert self.cx is not None
        r = self.cx.tool("join", meta={"threadId": TID}, room="#fpga", screen_name="cx")
        assert r["ok"], r
        if prove and self.d is not None:
            self.d.prove(TID, r["text"])
        return r

    def hook(self, event: str, **extra: Any) -> str:
        payload = {"hook_event_name": event, "session_id": TID, "cwd": "/ws", "permission_mode": "default",
                   **extra}
        cmd = hook_command(sys.executable, str(self.link.pi), hook_sha12(), "codex", event)
        r = self.op({"op": "hook", "command": cmd, "payload": payload})
        assert r["rc"] == 0, r
        return r["stdout"]

    def idle(self) -> None:
        """The join turn ended: hooks seen, idle."""
        self.hook("UserPromptSubmit", prompt="join switchboard room #fpga as cx")
        self.hook("Stop")
        assert wait_for(lambda: self.part()["status"] == "idle", what="idle")

    def turn_starts(self) -> list[dict[str, Any]]:
        assert self.d is not None
        return self.d.calls("turn/start")


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_codex, "SEND_BACKOFF_S", (0.2, 0.5))
    monkeypatch.setattr(remote_codex, "REROUTE_BACKOFF_S", (0.2, 0.5))


@pytest.fixture
def w() -> Any:
    world = World()
    try:
        yield world
    finally:
        world.close()


# ----------------------------------------------------------------------------
def test_an_idle_remote_codex_is_woken_through_its_own_app_server(w: World) -> None:
    w.start_codex()
    j = w.join()
    assert j["tier"] == "codex:link", j
    assert "You don't need to call wait()" in j["text"] and "starts `[switchboard]`" in j["text"]
    p = w.part()
    assert p["host"] == "fpga-pi" and (p["tier"], p["tier_note"]) == ("codex:link", None)
    w.idle()
    mid = w.say("please flash the new bitstream")
    [call] = wait_for(w.turn_starts, what="a turn/start on the Pi's app-server")
    assert set(call) == {"threadId", "input", "clientUserMessageId"}
    [item] = call["input"]
    assert call["threadId"] == TID and item["text"].startswith("[switchboard]")
    assert "flash the new bitstream" in item["text"] and f"id={mid}" in item["text"]
    [b] = wait_for(lambda: [x for x in w.batches() if x["state"] == "confirmed"], what="confirmed")
    assert b["evidence"] == "link:turn/start" and call["clientUserMessageId"] == f"yk-b{b['id']}"
    assert b["turn_start_at"] is not None and b["budget_counted"] == 1
    assert w.q("SELECT state FROM deliveries WHERE message_id=?", mid)[0][0] == "in_context"


@pytest.mark.parametrize("flag", ["waitingOnApproval", "waitingOnUserInput"])
def test_a_wait_on_the_pi_holds_the_wake_until_idle(w: World, flag: str) -> None:
    w.start_codex()
    w.join()
    w.idle()
    assert w.d is not None
    w.d.set_status(TID, "active", [flag])  # a prompt the broker's hooks haven't seen yet
    mid = w.say("held while you decide")
    # the Pi's MCP server read the status itself and refused: re-routed, nothing started
    assert wait_for(lambda: w.d.calls("thread/read"), what="the status read on the Pi")
    time.sleep(1.0)
    assert w.turn_starts() == []
    assert w.q("SELECT state FROM deliveries WHERE message_id=?", mid)[0][0] != "in_context"
    assert w.part()["push_expiries"] == 0  # a re-route, not a failure
    w.d.set_status(TID, "idle")
    [call] = wait_for(w.turn_starts, timeout=15.0, what="the wake once idle")
    assert "held while you decide" in call["input"][0]["text"]


def test_an_unproven_thread_is_never_woken(w: World) -> None:
    w.start_codex()
    w.join(prove=False)
    w.idle()
    w.say("are you there")
    assert wait_for(lambda: len(w.d.calls("thread/read")) >= 2, timeout=10.0, what="refused proof reads")
    assert w.turn_starts() == []
    assert w.d.calls("thread/read")[0]["includeTurns"] is True


def test_no_tui_attached_means_no_wake(w: World) -> None:
    w.start_codex(tui=False)
    w.join()
    w.idle()
    w.say("anyone home?")
    time.sleep(2.0)
    assert w.turn_starts() == []
    assert w.d.calls("thread/read") == []  # refused before any RPC
    # a TUI attaches: the next try wakes it
    w.op({"op": "attach", "path": w.sock})
    assert wait_for(w.turn_starts, timeout=15.0, what="the wake once a TUI is attached")


def test_session_end_stops_wakes_until_the_thread_runs_again(w: World) -> None:
    w.start_codex()
    w.join()
    w.idle()
    w.hook("SessionEnd")
    w.say("after the session ended")
    time.sleep(1.5)
    assert w.turn_starts() == []
    # a later hook of the thread: it runs again (a resume), and the wake goes out
    w.hook("UserPromptSubmit", prompt="resumed by its human")
    w.hook("Stop")
    assert wait_for(w.turn_starts, timeout=15.0, what="the wake after the resume")


def test_without_a_control_socket_it_stays_pull_only() -> None:
    world = World(daemon=False)
    try:
        world.start_codex(tui=False)
        j = world.join()
        assert j["tier"] == "codex:hook", j
        assert "wait(" in j["text"] and "switchboard can't start a turn" in j["text"]
        assert (world.part()["tier"], world.part()["tier_note"]) == ("codex:hook", "remote Codex: pull only")
    finally:
        world.close()
