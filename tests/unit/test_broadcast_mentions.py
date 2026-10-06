"""rules.parse_broadcast and rules.broadcast_targets: @here/@everyone (DESIGN.md §8.1,
issue #111). Both are pure: the caller (service.human_say) decides whether a broadcast may
happen at all (only a person's); these decide only who it reaches once it does.

The engine-level tests below show the other half of DESIGN.md §8.1's rule: "a broadcast must
never deliver where an @name wouldn't". Since a broadcast reaches engine.on_message as an
ordinary human message with an expanded ``mentions`` list (service.human_say does the
expanding; delivery/engine.py and delivery/rules.py's wake logic are never touched by this
issue), every existing delivery guard already applies unchanged -- these tests are the proof,
not a new code path."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World
from test_claude_adapter import claude

from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import Push


def test_parse_broadcast_finds_here_or_everyone_or_neither() -> None:
    assert rules.parse_broadcast("@here, status check") == "here"
    assert rules.parse_broadcast("@everyone, ship it") == "everyone"
    assert rules.parse_broadcast("no broadcast in this one") is None
    assert rules.parse_broadcast("") is None


def test_parse_broadcast_is_case_insensitive_and_word_bounded() -> None:
    assert rules.parse_broadcast("@Everyone please look") == "everyone"
    assert rules.parse_broadcast("@HERE") == "here"
    # the same word-boundary rule as MENTION_RE: a mid-word match doesn't count
    assert rules.parse_broadcast("foo@here.com") is None
    assert rules.parse_broadcast("nowhere to be found") is None


def test_parse_broadcast_prefers_everyone_when_both_appear() -> None:
    """everyone is the wider reach, so it wins rather than some arbitrary first-match order."""
    assert rules.parse_broadcast("@here and @everyone both, come see") == "everyone"
    assert rules.parse_broadcast("@everyone and @here both, come see") == "everyone"


def test_parse_broadcast_is_not_at_all_or_at_channel() -> None:
    """Reserved (RESERVED_NAMES) so nobody can claim them, but neither is a broadcast keyword:
    there is no @all, and @channel matches nothing either."""
    assert rules.parse_broadcast("@all hands on deck") is None
    assert rules.parse_broadcast("@channel, listen up") is None


MEMBERS = [("alpha", "idle"), ("bravo", "busy"), ("carol", "offline"), ("devin-1", "waiting-approval")]


def test_everyone_reaches_every_member_offline_included() -> None:
    assert rules.broadcast_targets("everyone", MEMBERS) == ["alpha", "bravo", "carol", "devin-1"]


def test_here_excludes_only_the_offline_ones() -> None:
    """'active right now' (the issue) is read as: whatever who() calls online, i.e. not the
    'offline' status -- busy, waiting-approval and starting are all still 'here'."""
    assert rules.broadcast_targets("here", MEMBERS) == ["alpha", "bravo", "devin-1"]


def test_an_unknown_kind_reaches_nobody() -> None:
    """Defensive: only parse_broadcast's two return values are ever passed in, but a caller
    typo should reach no one rather than everyone (fail closed)."""
    assert rules.broadcast_targets("", MEMBERS) == []
    assert rules.broadcast_targets("all", MEMBERS) == []


def test_everyone_and_here_with_no_members_reach_nobody() -> None:
    assert rules.broadcast_targets("everyone", []) == []
    assert rules.broadcast_targets("here", []) == []


# ---------------------------------------------------- engine-level guard parity
@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def pushes(w: World) -> list[Push]:
    return [a for a in w.take() if isinstance(a, Push)]


def test_an_everyone_broadcast_waiting_on_an_approval_prompt_gets_nothing(w: World) -> None:
    """The exact guard of test_rules_mid_task.py::test_waiting_on_an_approval_prompt_gets_nothing,
    with the member @everyone's own mentioned=1 instead of an ordinary chatter message: being
    the broadcast's target changes nothing about the approval hold."""
    p, m, _c = claude(w, status="waiting-approval", attached=False, registry=None)
    w.human("@everyone held while the prompt is open", mentions=("claude-1", "everyone"))
    assert w.hook(p, "PostToolUse", ok=True) is None
    assert pushes(w) == []


def test_an_everyone_broadcast_in_a_paused_room_delivers_nothing(w: World) -> None:
    w.store.set_paused(w.room.id, True, "paused by alice")
    w.actions += w.engine.on_command(w.room.id, "pause")
    p, m = w.agent("bot")
    s, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 30)
    w.actions += acts
    w.human("@everyone resume when you can", mentions=("bot", "everyone"))
    assert s.open and w.resolved(s.id) == []  # no wake while paused, mentioned or not


def test_an_everyone_broadcast_mid_task_is_priority_only_never_chatter(w: World) -> None:
    """Mirrors test_rules_mid_task.py's busy = priority-only rule: a broadcast is still just a
    human message (prio=2), so a busy member gets it through hook context like any other, not a
    push, and it is never held back as if it were unaddressed chatter."""
    p, m, _conn = claude(w, status="busy", mode="prompting", registry="busy")
    msg = w.human("@everyone status check", mentions=("claude-1", "everyone"))
    assert pushes(w) == []  # never a frame that could straddle a prompt, same as any human message
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and f"id={msg.id} " in out.text
