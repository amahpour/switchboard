"""rules.mentions_humans: @humans (DESIGN.md §8.1, issue #138). Pure: the callers
(service.human_say and agents.py's say()) decide whether to add the literal word "humans" to
a message's own mentions; this decides only whether @humans appears in the text at all.

The engine-level tests below are the other half of the design: a message whose mentions
already include "humans" (what either caller produces) changes no agent's delivery at all.
"humans" is reserved (models.RESERVED_NAMES), so it can never be a real agent's screen name,
and store.insert_message's per-recipient check (screen_name in mentions) can therefore never
match it -- no agent is ever mentioned, woken or given a higher priority by it, whether a
person or an agent sent it. Unlike @here/@everyone (issue #111), that holds the same for both
senders: there is no separate "from an agent it's plain text" case to prove here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.config import Config
from switchboard.delivery import rules


def test_mentions_humans_finds_the_word_or_not() -> None:
    assert rules.mentions_humans("@humans, can someone take a look?") is True
    assert rules.mentions_humans("no mention in this one") is False
    assert rules.mentions_humans("") is False


def test_mentions_humans_is_case_insensitive_and_word_bounded() -> None:
    assert rules.mentions_humans("@Humans please look") is True
    assert rules.mentions_humans("@HUMANS") is True
    # the same word-boundary rule as MENTION_RE: a mid-word match doesn't count
    assert rules.mentions_humans("foo@humans.example") is False
    assert rules.mentions_humans("subhumans are not addressed") is False


# ---------------------------------------------------- engine-level guard parity
@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def test_an_agents_humans_mention_never_mentions_or_raises_priority_for_another_agent(w: World) -> None:
    """The primary case (issue #138): an agent's own @humans, with the literal word already in
    its own mentions (what agents.py's say() produces, the same way agents.py adds it when the
    text matches mentions_humans), is chatter to every other agent member -- never mentioned,
    never a priority bump -- exactly as if "humans" had never been added at all."""
    _p1, m1 = w.agent("alpha")
    _p2, m2 = w.agent("bravo")
    msg = w.agent_says(m1, "@humans need a decision: ship tonight or wait?", mentions=("humans",))
    row = w.delivery(m2, msg)
    assert row["mentioned"] == 0
    assert row["prio"] == 0  # chatter, same as any other unaddressed agent message


def test_a_persons_humans_mention_still_never_mentions_any_agent(w: World) -> None:
    """A person's @humans reaches every agent at prio=2 regardless (DESIGN §8.1: every human
    message already does, mentioned or not), but never sets mentioned -- the mention style and
    the watchdog never fire for it, the same as any other unaddressed chatter from the human."""
    _p, m = w.agent("bravo")
    msg = w.human("@humans status check when you can", mentions=("humans",))
    row = w.delivery(m, msg)
    assert row["mentioned"] == 0
    assert row["prio"] == 2  # every human message is prio=2 regardless of mentions
