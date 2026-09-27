"""The live driver's screen matchers (tests/live/harness/screens.py), without a live CLI."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "live"))

from harness import screens  # noqa: E402

DEVIN_READY = "Ask Devin to do anything\n  swe-1-6-slow · accept edits · {} remaining\n"
DEVIN_SELECTOR = ("Run `cd .worktrees/devin-1 && git log`?\n❭ 1 Yes  (Approve once)\n  2 Yes, and allow always\n"
                  "  6 Edit command\n↵ confirm · esc cancel\n")


def test_devin_quota_is_exhausted_only_at_zero() -> None:
    for pct in ("10%", "20%", "50%", "70%", "42%", "100%", "0.5%"):
        assert not screens.devin_quota_exhausted(DEVIN_READY.format(pct)), pct
    assert screens.devin_quota_left(DEVIN_READY.format("100%")) == 100.0
    assert screens.devin_quota_left(DEVIN_READY.format("42%")) == 42.0
    assert screens.devin_quota_exhausted(DEVIN_READY.format("0%"))
    assert screens.devin_quota_exhausted("usage limit reached for this week")
    assert screens.devin_quota_left("Ask Devin to do anything") is None


def test_claude_model_errors() -> None:
    assert screens.claude_model_error("There's an issue with the selected model (sonnet). It may not exist or you"
                                      " may not have access to it. Run /model to pick a different model.")
    assert screens.claude_model_error("Error: model not found: sonnet-9")
    assert not screens.claude_model_error("? for shortcuts · accept edits on")
    assert not screens.claude_model_error("> join #build as claude-1 and stay in the room")


def test_devin_selector_blocks_typing_and_enter() -> None:
    assert screens.devin_selector_open(DEVIN_SELECTOR)
    assert screens.devin_selector_open("Allow once   Allow always   Deny")
    assert not screens.devin_selector_open(DEVIN_READY.format("42%"))


def test_the_scratch_repo_goes_in_a_private_temp_dir() -> None:
    d = screens.private_tmp()
    st = os.stat(d)
    if st.st_uid == os.getuid() and not (st.st_mode & 0o077):
        assert stat.S_ISDIR(st.st_mode)
    else:  # no private temp dir on this machine: the system one
        import tempfile

        assert d == tempfile.gettempdir()
    assert d == os.path.realpath(d) or d == tempfile.gettempdir()
