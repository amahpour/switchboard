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
