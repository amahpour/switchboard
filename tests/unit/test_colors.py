"""switchboard.colors (issue #28): when the CLI colours, and how."""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from switchboard import colors
from switchboard.colors import PLAIN, Paint, enabled, for_args, nick_colour


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(("mode", "env", "tty", "want"), [
    ("always", {"NO_COLOR": "1"}, False, True),      # an explicit flag beats everything
    ("never", {"FORCE_COLOR": "1"}, True, False),
    ("auto", {}, True, True),                        # a terminal
    ("auto", {}, False, False),                      # a pipe or a file
    ("auto", {"NO_COLOR": "1"}, True, False),        # https://no-color.org
    ("auto", {"NO_COLOR": ""}, True, True),          # ... "present and not empty"
    ("auto", {"NO_COLOR": "1", "FORCE_COLOR": "1"}, True, False),  # NO_COLOR wins
    ("auto", {"FORCE_COLOR": "1"}, False, True),
    ("auto", {"FORCE_COLOR": "0"}, False, False),    # 0 / false turn forcing off
    ("auto", {"FORCE_COLOR": "false"}, False, False),
    ("auto", {"CLICOLOR_FORCE": "1"}, False, True),
    ("auto", {"TERM": "dumb"}, True, False),
])
def test_when_to_colour(mode: str, env: dict[str, str], tty: bool, want: bool) -> None:
    stream = Tty() if tty else io.StringIO()
    assert enabled(mode, stream, env) is want


def test_a_stream_that_cannot_answer_is_not_a_terminal() -> None:
    class NoIsatty:
        pass

    class Closed(io.StringIO):
        def isatty(self) -> bool:
            raise ValueError("I/O operation on closed file")

    assert enabled("auto", NoIsatty(), {}) is False
    assert enabled("auto", Closed(), {}) is False


def test_default_stream_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", Tty())
    assert enabled() is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert enabled() is False


def test_paint_off_is_the_identity() -> None:
    for role in ("bold", "dim", "ok", "warn", "bad", "link", "added", "removed", "heading", "prompt"):
        assert getattr(PLAIN, role)("x\ny") == "x\ny"
    assert PLAIN.nick("claude-1") == "claude-1" and PLAIN.nick("alice", "human") == "alice"


def test_paint_wraps_each_line_and_leaves_empty_text_alone() -> None:
    p = Paint(True)
    assert p.bad("a\n\nb") == "\x1b[31ma\x1b[0m\n\n\x1b[31mb\x1b[0m"  # no colour bleeding past a newline
    assert p.prompt("Apply?") == "\x1b[1;33mApply?\x1b[0m"
    assert p.ok("") == ""
    assert p.style("x") == "x"  # no style named


def test_nicks_are_stable_and_the_human_and_system_stand_apart() -> None:
    p = Paint(True)
    # crc32, not hash(): the same colour in every process and on every run
    assert [nick_colour(n) for n in ("claude-1", "codex-1", "devin-1", "bench")] == ["blue", "cyan", "magenta",
                                                                                    "yellow"]
    out = subprocess.run([sys.executable, "-c", "from switchboard.colors import nick_colour as n; print(n('claude-1'))"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "blue"
    assert p.nick("codex-1") == "\x1b[36mcodex-1\x1b[0m"
    assert p.nick("alice", "human") == "\x1b[1malice\x1b[0m"
    assert p.nick("switchboard", "system") == "\x1b[2mswitchboard\x1b[0m"
    assert "red" not in colors._NICK_COLOURS  # red means a warning


def test_for_args_never_paints_json_and_reads_a_missing_flag_as_auto() -> None:
    class A:
        json = True
        color = "always"

    class B:
        pass

    assert for_args(A()) is PLAIN
    assert for_args(B(), io.StringIO()).on is False
    assert for_args(B(), Tty()).on is True
