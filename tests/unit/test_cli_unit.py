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
