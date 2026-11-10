"""Which Kokoro voice an agent speaks with on a call (DESIGN.md §52.6, #713)."""

from __future__ import annotations

import pytest

from switchboard.meeting import voices


def test_the_default_list_is_short_and_every_name_has_the_shape_kokoro_accepted() -> None:
    """Names of the shape that was heard live (``am_adam``, ``bf_emma``). A longer list means more names
    that may not exist; the shape check keeps a typo in it from reaching joinly."""
    assert 2 <= len(voices.DEFAULT_VOICES) <= 6
    assert len(set(voices.DEFAULT_VOICES)) == len(voices.DEFAULT_VOICES)
    assert {"am_adam", "bf_emma"} <= set(voices.DEFAULT_VOICES)
    assert all(voices.valid(v) for v in voices.DEFAULT_VOICES)


def test_a_screen_name_always_gets_the_same_default_whatever_its_case() -> None:
    """No setting, no storage: the voice is a function of the name. Fails if it depends on the process
    (Python's per-run string hash) or the name's case."""
    assert voices.default_for("claude-1") == voices.default_for("Claude-1") == voices.default_for("CLAUDE-1")
    assert voices.default_for("claude-1") in voices.DEFAULT_VOICES


def test_default_voices_are_the_same_in_a_second_process() -> None:
    """The mapping is a hash of the name, not Python's randomised ``hash()``. Fails if it changes per run."""
    import subprocess
    import sys

    code = (
        "from switchboard.meeting import voices;"
        " print(voices.default_for('codex-1'), voices.default_for('x'))"
    )
    outs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout for _ in range(3)
    }
    assert len(outs) == 1 and outs != {""}
    assert outs == {f"{voices.default_for('codex-1')} {voices.default_for('x')}\n"}


def test_names_spread_over_the_whole_list() -> None:
    """Several agents in one room should not all get one voice. Fails if the choice ignores the name."""
    got = {voices.default_for(f"agent-{i}") for i in range(40)}
    assert got == set(voices.DEFAULT_VOICES)


def test_a_pinned_voice_wins_and_a_bad_one_is_no_voice() -> None:
    assert voices.effective("bf_emma", "claude-1") == "bf_emma"
    assert voices.effective(None, "claude-1") == voices.default_for("claude-1")
    for bad in ("", "Bad", "a b", "../x", "x" * 33, "voice\n"):
        assert voices.effective(bad, "claude-1") == voices.default_for("claude-1"), bad


@pytest.mark.parametrize("ok", ["a", "am_adam", "x" * 32, "a1_b2"])
def test_valid_accepts_the_kokoro_shape(ok: str) -> None:
    assert voices.valid(ok)


@pytest.mark.parametrize(
    "bad", ["", "x" * 33, "Am_adam", "am-adam", "am adam", "../x", "a/b", "é", "a\n", None, 7]
)
def test_valid_refuses_everything_else(bad: object) -> None:
    """The check is a whole-string match: a newline at the end, a path or a capital never passes."""
    assert not voices.valid(bad)
