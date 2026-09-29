"""Coloured CLI output (issue #28): each command is plain when piped, with NO_COLOR,
with ``--color never`` and with ``--json``, and coloured with ``--color always`` or on
a terminal. Colour only ever adds switchboard's own codes: stripping them gives back
the plain output exactly, and escape codes inside room text stay stripped."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock
from engine_world import World
from test_cli_unit import Calls
from switchboard import cli, report
from switchboard.colors import Paint
from switchboard.remote.pairing import Finding, print_findings

SGR = re.compile(r"\x1b\[[0-9;]*m")
# every code switchboard itself emits: reset, bold, dim, the six colours, bold yellow
OURS = re.compile(r"\x1b\[(?:0|1|2|3[1-6]|1;33)m")


def only_our_codes(text: str) -> bool:
    return "\x1b" not in OURS.sub("", text)


MESSAGES: list[dict[str, Any]] = [
    {"id": 1, "ts": 0, "from": "alice", "sender_kind": "human", "via": "web", "kind": "chat",
     "text": "@claude-1 ship it"},
    {"id": 2, "ts": 0, "from": "claude-1", "sender_kind": "agent", "via": "mcp", "kind": "chat",
     "text": "done\x1b]0;pwned\x07\x1b[2J\x1b[31m*** fake warning\x1b[0m\nsecond line"},
    {"id": 3, "ts": 0, "from": "codex-1", "sender_kind": "agent", "via": "mcp", "kind": "join", "text": "joined"},
    {"id": 4, "ts": 0, "from": "switchboard", "sender_kind": "system", "via": "system", "kind": "notice",
     "text": "codex-1 runs with approvals off", "level": "warn"},
    {"id": 5, "ts": 0, "from": "switchboard", "sender_kind": "system", "via": "system", "kind": "notice",
     "text": "devin-1 is parked"},
    {"id": 6, "ts": 0, "from": "alice", "sender_kind": "human", "via": "cli", "kind": "chat", "text": "from cli"},
]


class FakeStream:
    pushes_to_send: list[dict[str, Any]] = []

    def __init__(self, sock: Any) -> None:
        pass

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"messages": MESSAGES, "more": False}

    def pushes(self) -> Any:
        return iter(self.pushes_to_send)


STATUS = {
    "version": "0.4.0", "pid": 7, "uptime_s": 3700, "url": "http://switchboard.localhost:7419/", "web_clients": 1,
    "home": "/h", "hooks": "ok (1 copy)", "codex_link": "up since 10:25:16 (0 thread(s) loaded)", "test_mode": True,
    "rooms": [
        {"name": "#build", "paused": False, "paused_reason": None, "members": 3, "budget_remaining": 47,
         "budget_per_hour": 60, "hop_count": 3, "hop_limit": 30},
        {"name": "#lab", "paused": True, "paused_reason": "loop guard", "members": 1, "budget_remaining": 60,
         "budget_per_hour": 60, "hop_count": 6, "hop_limit": 0},
    ],
    "closed_rooms": 1,
    "remotes": [
        {"name": "fpga-pi", "state": "up", "rtt_ms": 2.1, "version": "0.4.0", "members": ["bench"]},
        {"name": "lab", "state": "blocked", "reason": "host_key", "detail": "up \x1b[31mfake\x1b[0m"},
        {"name": "old", "state": "down", "reason": "unreachable", "retry_in_s": 8},
        {"name": "new", "state": "disabled", "reason": "not_enabled"},
    ],
}

WHO = {"room": "#build", "human": "alice", "members": [
    {"name": "claude-1", "harness": "claude", "status": "idle", "tier": "claude:inbox", "tier_note": None,
     "approval_mode": "prompting", "env_leak": False, "held": False, "queued": 0, "parked": None,
     "session": "3f2a-c91e", "away": None, "host": None},
    {"name": "codex-1", "harness": "codex", "status": "busy", "tier": "codex:daemon", "tier_note": None,
     "approval_mode": "bypass", "env_leak": True, "held": True, "queued": 2, "parked": None, "away": "lunch\x1b[2J"},
    {"name": "devin-1", "harness": "devin", "status": "offline", "tier": "devin:wait-loop", "tier_note": None,
     "approval_mode": "unknown", "env_leak": False, "held": False, "queued": 4,
     "parked": "its turn ended without wait()", "host": "fpga-pi"},
]}


# ---------------------------------------------------------------- per command
def spec_status(mp: pytest.MonkeyPatch, tmp: Path, clock: FakeClock) -> tuple[list[str], bool]:
    Calls(mp, {"sys.status": STATUS})
    return ["status"], True


def spec_who(mp: pytest.MonkeyPatch, tmp: Path, clock: FakeClock) -> tuple[list[str], bool]:
    Calls(mp, {"room.who": WHO})
    return ["who", "#build"], True


def spec_remote_status(mp: pytest.MonkeyPatch, tmp: Path, clock: FakeClock) -> tuple[list[str], bool]:
    Calls(mp, {"remote.status": {"remotes": STATUS["remotes"], "config_error": "bad\x1b[2Jentry"}})
    return ["remote", "status"], True


def spec_tail(mp: pytest.MonkeyPatch, tmp: Path, clock: FakeClock) -> tuple[list[str], bool]:
    mp.setattr("switchboard.mcp.client.Stream", FakeStream)
    mp.setattr(FakeStream, "pushes_to_send", [
        {"push": "notice", "data": {"level": "warn", "text": "loop guard paused #build\x1b[2J"}},
        {"push": "notice", "data": {"level": "info", "text": "claude-1 joined"}},
    ])
    return ["tail", "#build", "--home", str(tmp)], True


def spec_report(mp: pytest.MonkeyPatch, tmp: Path, clock: FakeClock) -> tuple[list[str], bool]:
    home = tmp / "rhome"
    home.mkdir()
    w = World(home, clock)
    w.store.con.close()
    (home / "y.db").rename(home / "switchboard.db")
    return ["report", "--home", str(home), "--room", "#build"], True


def _user_home(tmp: Path) -> Path:
    uh = tmp / "uh"
    (uh / ".claude").mkdir(parents=True, exist_ok=True)
    (uh / ".claude" / "settings.json").write_text('{"model": "x\\u001b[2Jy"}\n')
    return uh


def spec_install(mp: pytest.MonkeyPatch, tmp: Path, clock: FakeClock) -> tuple[list[str], bool]:
    return ["install", "claude", "--dry-run", "--home", str(tmp / "sbh"), "--user-home", str(_user_home(tmp))], False


def spec_uninstall(mp: pytest.MonkeyPatch, tmp: Path, clock: FakeClock) -> tuple[list[str], bool]:
    return ["uninstall", "all", "--dry-run", "--home", str(tmp / "sbh"), "--user-home", str(_user_home(tmp))], False


SPECS: dict[str, Callable[..., tuple[list[str], bool]]] = {
    "status": spec_status, "who": spec_who, "remote status": spec_remote_status, "tail": spec_tail,
    "report": spec_report, "install": spec_install, "uninstall": spec_uninstall,
}


@pytest.mark.parametrize("cmd", list(SPECS))
def test_colour_only_where_it_belongs(cmd: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                      clock: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:
    argv, has_json = SPECS[cmd](monkeypatch, tmp_path, clock)

    def run(*extra: str, tty: bool = False, env: dict[str, str] | None = None) -> str:
        with monkeypatch.context() as m:
            if tty:
                m.setattr(sys.stdout, "isatty", lambda: True, raising=False)
            for k, v in (env or {}).items():
                m.setenv(k, v)
            cli.main([*argv, *extra])
        return capsys.readouterr().out

    plain = run()  # a pipe
    assert plain.strip() and "\x1b" not in plain
    forced = run("--color", "always")
    assert "\x1b[" in forced and only_our_codes(forced)
    assert SGR.sub("", forced) == plain  # colour adds codes and never changes the text
    assert "\x1b[" in run(tty=True)  # auto on a terminal
    assert run(tty=True, env={"NO_COLOR": "1"}) == plain
    assert run("--color", "never", tty=True) == plain
    assert run(tty=True, env={"TERM": "dumb"}) == plain
    assert SGR.sub("", run(env={"FORCE_COLOR": "1"})) == plain
    if has_json:
        out = run("--json", "--color", "always", tty=True)
        assert "\x1b[" not in out.replace("\\u001b", "")
        for line in out.splitlines() if cmd == "tail" else [out]:
            json.loads(line)


def test_color_flag_goes_before_or_after_the_verb() -> None:
    p = cli.build_parser()
    assert p.parse_args(["--color", "always", "status"]).color == "always"
    assert p.parse_args(["status", "--color", "never"]).color == "never"
    assert not hasattr(p.parse_args(["status"]), "color")  # colors.for_args reads that as auto
    with pytest.raises(SystemExit):
        p.parse_args(["status", "--color", "rainbow"])


# ---------------------------------------------------------------- what is coloured
def test_tail_colours_the_framing_and_keeps_room_text_inert(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                             clock: FakeClock,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    argv, _ = spec_tail(monkeypatch, tmp_path, clock)
    cli.main([*argv, "--color", "always"])
    out = capsys.readouterr().out
    assert only_our_codes(out)
    # the injected OSC title and screen clear are gone; the fake colour is inert text
    assert "\x07" not in out and "]0;pwned" in out and "[31m*** fake warning[0m" in out
    assert "<\x1b[1malice\x1b[0m>" in out           # you, bold
    assert "<\x1b[34mclaude-1\x1b[0m>" in out       # an agent, its own colour
    assert "\x1b[2m* codex-1 joined\x1b[0m" in out  # join lines dim
    assert "\x1b[31m-!- codex-1 runs with approvals off\x1b[0m" in out  # a warning red
    assert "\x1b[2m-!- devin-1 is parked\x1b[0m" in out                 # other notices dim
    assert "\x1b[2m (via cli)\x1b[0m" in out
    assert "\x1b[31m-!- loop guard paused #build[2J\x1b[0m" in out     # a pushed warning
    assert "\x1b[2m-!- claude-1 joined\x1b[0m" in out
    assert "\n" + " " * 11 + "second line" in out  # continuation lines keep their indent


def test_status_and_remote_lines_colour_state_words_only() -> None:
    p = Paint(True)
    line = cli._remote_line(p, {"name": "lab", "state": "blocked", "reason": "auth", "detail": "up down"})
    assert line.startswith("lab: \x1b[31mblocked\x1b[0m: auth") and line.endswith("[up down]")
    assert cli._remote_line(p, {"name": "a", "state": "up", "rtt_ms": 3}).startswith("a: \x1b[32mup\x1b[0m 3.0 ms")
    assert cli._remote_line(p, {"name": "a", "state": "disabled", "reason": "config_changed"}).startswith(
        "a: \x1b[33mneeds enable\x1b[0m (config changed)")
    assert cli._remote_line(p, {"name": "a", "state": "disabled", "reason": "disabled"}).startswith(
        "a: \x1b[2mdisabled\x1b[0m (")
    assert cli._remote_line(p, {"name": "a", "state": "connecting"}) == "a: \x1b[33mconnecting\x1b[0m"
    # a name holding control characters is cleaned in the line and in the lookup alike:
    # only switchboard's own codes reach the terminal
    line = cli._remote_line(p, {"name": "a\x1b[2J", "state": "up", "rtt_ms": 1})
    assert line.startswith("a[2J: \x1b[32mup\x1b[0m") and only_our_codes(line)
    assert cli._remote_line(p, {"name": "a", "state": ""}) == "a: "  # no state word: nothing coloured
    assert cli._lead(p, "ok (1 copy)") == "\x1b[32mok\x1b[0m (1 copy)"
    assert cli._lead(p, "off") == "\x1b[2moff\x1b[0m"
    assert cli._lead(p, "down: no daemon") == "\x1b[33mdown:\x1b[0m no daemon"
    assert cli._state(p, "failed") == "\x1b[31mfailed\x1b[0m"


def test_report_headings_and_fired_rules(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock)
    rep = report.build(w.store.con, "#build", now=clock.now())
    rep["rules"]["kick"] = 2
    md = report.render_markdown(rep, paint=Paint(True))
    assert md.startswith("\x1b[1m# switchboard report: #build\x1b[0m")
    assert "\x1b[1m## Rules that fired\x1b[0m" in md
    assert "| \x1b[33m/kick\x1b[0m | 2 |" in md and "| /pause | 0 |" in md
    assert SGR.sub("", md) == report.render_markdown(rep)


def test_report_strips_control_characters_from_database_strings() -> None:
    assert report._safe("gpt\x1b[2J-5‮") == "gpt[2J-5"


def test_doctor_levels(capsys: pytest.CaptureFixture[str]) -> None:
    import io

    buf = io.StringIO()
    rc = print_findings([Finding("ok", "a"), Finding("note", "b"), Finding("WARN", "c"), Finding("FAIL", "d")], buf,
                        paint=Paint(True))
    out = buf.getvalue()
    assert rc == 1
    assert "  \x1b[32mok\x1b[0m    a" in out and "  \x1b[2mnote\x1b[0m  b" in out
    assert "  \x1b[33mWARN\x1b[0m  c" in out and "  \x1b[31mFAIL\x1b[0m  d" in out
    assert out.endswith("\x1b[31m1 warning(s), 1 failure(s)\x1b[0m\n")
    buf = io.StringIO()
    assert print_findings([Finding("ok", "a")], buf, paint=Paint(True)) == 0
    assert buf.getvalue().endswith("\x1b[32mclean\x1b[0m\n")
    buf = io.StringIO()
    print_findings([Finding("WARN", "c")], buf, paint=Paint(True))
    assert buf.getvalue().endswith("\x1b[33m1 warning(s), 0 failure(s)\x1b[0m\n")
    buf = io.StringIO()
    print_findings([Finding("WARN", "c")], buf)
    assert buf.getvalue() == "  WARN  c\n1 warning(s), 0 failure(s)\n"  # plain is unchanged


def test_install_diff_lines_are_made_safe_before_they_are_coloured() -> None:
    from switchboard.install.common import _diff_line

    p = Paint(True)
    assert _diff_line(p, "  + a\x1b[2Jb") == "\x1b[32m  + a\\x1b[2Jb\x1b[0m"
    assert _diff_line(p, "  - x") == "\x1b[31m  - x\x1b[0m"
    assert _diff_line(p, "  ! hooks moved") == "\x1b[33m  ! hooks moved\x1b[0m"
    assert _diff_line(p, "  ~ 2 line(s)") == "\x1b[33m  ~ 2 line(s)\x1b[0m"
    assert _diff_line(p, "context") == "context"


def test_install_run_statuses() -> None:
    from switchboard.install.common import _status

    p = Paint(True)
    assert _status(p, "error: bad JSON") == "\x1b[31merror: bad JSON\x1b[0m"
    assert _status(p, "skipped (`devin` not on PATH)") == "\x1b[2mskipped (`devin` not on PATH)\x1b[0m"
    assert _status(p, "no changes") == "no changes"
