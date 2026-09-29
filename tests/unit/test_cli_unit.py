"""CLI pieces that don't need a broker: line format, argument parsing, test-mode guard."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from switchboard import cli
from switchboard.broker.daemon import check_test_mode
from switchboard.paths import Paths


def test_format_line_strips_terminal_escapes() -> None:
    m = {"ts": 0, "from": "claude-1", "text": "hi\x1b]0;pwned\x07\x1b[2J there", "kind": "chat", "via": "mcp"}
    line = cli.format_line(m)
    assert "\x1b" not in line and "\x07" not in line
    assert line.endswith("<claude-1> hi]0;pwned[2J there")
    assert cli.format_line({**m, "via": "cli", "text": "x"}).endswith("<claude-1> (via cli) x")
    assert "* claude-1 joined" in cli.format_line({**m, "kind": "join", "text": "joined"})
    assert "* claude-1 left" in cli.format_line({**m, "kind": "leave", "text": ""})
    assert "-!- hello" in cli.format_line({**m, "kind": "notice", "text": "hello"})
    multi = cli.format_line({**m, "text": "a\nb"})
    assert multi.splitlines()[1] == " " * 11 + "b"


def test_parser_accepts_home_before_or_after_the_verb() -> None:
    p = cli.build_parser()
    a = p.parse_args(["--home", "/tmp/x", "say", "#b", "hi", "there"])
    assert a.home == "/tmp/x" and a.text == ["hi", "there"]
    a = p.parse_args(["say", "--home", "/tmp/y", "#b", "hi"])
    assert a.home == "/tmp/y"
    a = p.parse_args(["tail", "#b"])
    assert not hasattr(a, "home") and a.lines == 20 and not a.no_follow
    a = p.parse_args(["start", "--foreground", "--port", "0", "--test-mode", "--test-trust-uds"])
    assert a.foreground and a.port == 0 and a.test_mode and a.test_trust_uds


def test_check_test_mode(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = Paths.from_home(tmp_home)
    assert check_test_mode(p, home_given=True) is None
    assert "--home" in (check_test_mode(p, home_given=False) or "")
    monkeypatch.delenv("SWITCHBOARD_TEST")
    assert "SWITCHBOARD_TEST" in (check_test_mode(p, home_given=True) or "")
    monkeypatch.setenv("SWITCHBOARD_TEST", "1")
    (tmp_home / ".switchboard-test").unlink()
    assert "marker" in (check_test_mode(p, home_given=True) or "")
    outside = Path(os.path.expanduser("~")) / "not-tmp"
    if not str(os.path.realpath(outside)).startswith(("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")):
        assert "temp" in (check_test_mode(Paths.from_home(outside), home_given=True) or "")


@pytest.mark.parametrize("message,want", [
    ("human.login_link arrived through ssh or another remote login (sshd above the caller); human commands"
     " must come from a terminal on this machine, or set [security] allow_ssh_cli = true",
     ["run `switchboard login` in a terminal on this machine.",
      "No link here: this command arrived through ssh or another remote login (sshd above the caller)"]),
    ("login links are only issued to a terminal you typed in: run `switchboard login` there",
     ["To sign in, run `switchboard login` in your own terminal."]),
])
def test_start_says_why_it_gave_no_login_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              message: str, want: list[str]) -> None:
    """`switchboard start` asks for a login link right after the broker is up; when the
    SSH rules (DESIGN.md §27.5.7) refuse it, the hint names the reason instead of
    sending the user to `switchboard login` over the same ssh login."""
    import io

    from switchboard.broker import daemon
    from switchboard.mcp.client import RpcError

    pings = iter([None, {"pid": 4242, "port": 7777, "test_mode": False}])

    class Child:
        def poll(self) -> None:
            return None

    def refuse(*a: object, **kw: object) -> None:
        raise RpcError("forbidden", message)

    monkeypatch.setattr(daemon, "ping", lambda *a, **kw: next(pings))
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **kw: Child())
    monkeypatch.setattr(daemon, "call_sync", refuse)
    out = io.StringIO()
    old_umask = os.umask(0o022)
    os.umask(old_umask)
    try:
        assert daemon.start(Paths.from_home(tmp_path / "home"), out=out) == 0
    finally:
        os.umask(old_umask)  # start() sets 077
    text = out.getvalue()
    assert "switchboard is running (pid 4242)" in text
    for w in want:
        assert w in text, text


# ------------------------------------------------ closed rooms and delete (#16)
def test_rooms_parser() -> None:
    p = cli.build_parser()
    a = p.parse_args(["rooms"])
    assert a.func is cli.cmd_rooms and a.rooms_cmd is None and not a.json and not a.closed
    assert p.parse_args(["rooms", "--json"]).json
    assert p.parse_args(["rooms", "--closed"]).closed
    a = p.parse_args(["rooms", "delete", "#x", "--yes"])
    assert a.rooms_cmd == "delete" and a.room == "#x" and a.yes
    a = p.parse_args(["rooms", "delete", "--home", "/tmp/h", "#build~closed-7"])
    assert a.room == "#build~closed-7" and not a.yes and a.home == "/tmp/h"


def _ts(s: str) -> float:
    import time

    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))


COUNTS = {"rooms": 1, "messages": 412, "memberships": 3, "deliveries": 1830, "batches": 57, "events": 960}
CLOSED_PLAN = {"room_id": 7, "name": "#build~closed-7", "display": "#build", "state": "closed",
               "created_at": _ts("2026-09-28 09:00"), "closed_at": _ts("2026-09-28 14:02"), "closed_by": "alice",
               "counts": COUNTS, "backup": "/h/switchboard.db.delete-build-7.bak"}


class Calls:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, answers: dict[str, object]) -> None:
        self.calls: list[tuple[str, dict, float]] = []
        self.answers = answers

        def fake(args: object, method: str, params: dict | None = None, timeout: float = 10.0) -> object:
            params = params or {}
            self.calls.append((method, params, timeout))
            key = method + (":plan" if params.get("dry_run") else ":apply" if "room_id" in params
                            else ":closed" if params.get("closed") else "")
            return self.answers[key]

        monkeypatch.setattr(cli, "_call", fake)
        monkeypatch.setattr(cli, "_satellite_home", lambda args: False)


def test_rooms_delete_prints_the_plan_and_needs_a_terminal_or_yes(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    c = Calls(monkeypatch, {"room.delete:plan": CLOSED_PLAN})
    assert cli.main(["rooms", "delete", "#build"]) == 1  # pytest's stdin is no terminal
    out, err = capsys.readouterr()
    assert out == (
        "switchboard rooms delete #build~closed-7:\n"
        "  #build~closed-7: was #build, closed 2026-09-28 14:02 by alice\n"
        "  removes 1 room, 412 message(s), 3 membership(s), 1830 delivery row(s), 57 batch(es), 960 event(s)\n"
        "  a checked backup of the whole database is written first: /h/switchboard.db.delete-build-7.bak\n"
        "  this can't be undone, except by restoring that backup\n"
        "not applied\n")
    assert "--yes" in err
    assert c.calls == [("room.delete", {"room": "#build", "dry_run": True}, 30.0)]


def test_rooms_delete_yes_applies_the_plans_room_id(monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    plan = {**CLOSED_PLAN, "room_id": 12, "name": "#scratch", "display": "#scratch", "state": "open",
            "closed_at": None, "closed_by": None, "created_at": _ts("2026-09-28 13:00"),
            "backup": "/h/switchboard.db.delete-scratch-12.bak"}
    removed = {**COUNTS, "messages": 2, "memberships": 0, "deliveries": 0, "batches": 0, "events": 1}
    c = Calls(monkeypatch, {"room.delete:plan": plan, "room.delete:apply": {
        "room_id": 12, "name": "#scratch", "display": "#scratch", "removed": removed,
        "backup": "/h/switchboard.db.delete-scratch-12.bak"}})
    assert cli.main(["rooms", "delete", "scratch", "--yes"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "  #scratch: open, no agents, created 2026-09-28 13:00"
    assert lines[-2:] == [
        "deleted #scratch: 1 room, 2 message(s), 0 membership(s), 0 delivery row(s), 0 batch(es), 1 event(s)",
        "backup: /h/switchboard.db.delete-scratch-12.bak (0600, checked); it still holds the room:"
        " remove it once you no longer need it"]
    assert c.calls[1] == ("room.delete", {"room": "scratch", "room_id": 12}, 120.0)


def test_rooms_delete_on_a_satellite_home(monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    c = Calls(monkeypatch, {})
    monkeypatch.setattr(cli, "_satellite_home", lambda args: True)
    monkeypatch.setattr(cli, "_desktop", lambda args: "desk")
    assert cli.main(["rooms", "delete", "#build", "--yes"]) == 1
    assert "run this on the desktop (desk)" in capsys.readouterr().err
    assert c.calls == []


def test_rooms_lists_the_closed_count_and_the_closed_rooms(monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    import json

    open_room = {"id": 3, "name": "#fpga", "members": 2, "settings": {"paused": True}}
    closed_row = {"id": 7, "name": "#build~closed-7", "display": "#build", "created_at": 0.0,
                  "closed_at": _ts("2026-09-28 14:02"), "closed_by": "alice", "messages": 412, "reopenable": True}
    answers: dict[str, object] = {"room.list": {"rooms": [], "closed": 2},
                                  "room.list:closed": {"rooms": [closed_row], "closed": 1}}
    Calls(monkeypatch, answers)
    assert cli.main(["rooms"]) == 0
    assert capsys.readouterr().out == "no open rooms (2 closed: switchboard rooms --closed)\n"
    answers["room.list"] = {"rooms": [open_room], "closed": 2}
    assert cli.main(["rooms"]) == 0
    assert capsys.readouterr().out == "#fpga  2 agent(s)  [paused]\n(2 closed: switchboard rooms --closed)\n"
    answers["room.list"] = {"rooms": [], "closed": 0}
    assert cli.main(["rooms"]) == 0
    assert capsys.readouterr().out == "no rooms yet (create one in the web UI)\n"
    assert cli.main(["rooms", "--closed"]) == 0
    assert capsys.readouterr().out == "#build~closed-7  was #build, closed 2026-09-28 14:02 by alice, 412 message(s)\n"
    assert cli.main(["rooms", "--closed", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [closed_row]
    answers["room.list:closed"] = {"rooms": [{**closed_row, "closed_at": None, "closed_by": None}], "closed": 1}
    assert cli.main(["rooms", "--closed"]) == 0
    assert capsys.readouterr().out == ("#build~closed-7  was #build, closed at an unknown time by ?,"
                                       " 412 message(s)\n")
    answers["room.list:closed"] = {"rooms": [], "closed": 0}
    assert cli.main(["rooms", "--closed"]) == 0
    assert capsys.readouterr().out == "no closed rooms\n"


def test_status_shows_the_closed_rooms(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    st: dict[str, object] = {"version": "0.3.0", "pid": 1, "uptime_s": 60, "url": "http://127.0.0.1:7419/",
                             "home": "/h", "hooks": "ok", "codex_link": "off", "rooms": [], "closed_rooms": 2}
    Calls(monkeypatch, {"sys.status": st})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "  rooms   none open\n" in out and out.endswith("  closed  2 room(s) (switchboard rooms --closed)\n")
    st["closed_rooms"] = 0
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "  rooms   none yet (create one in the web UI)\n" in out and "closed" not in out
