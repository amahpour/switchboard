"""Rate limit on agent say() (DESIGN.md §8.4): at most one
say() per 10 s per agent, unless it replies to the human or to an @mention of the
sender (or answers priority items it has in context). A refused say is not queued:
the agent gets ``rate_limited`` and ``retry_after_s`` and may try again."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.config import Config
from switchboard.delivery import rules


def test_check_rate_limit() -> None:
    assert rules.check_rate_limit(None, 100.0, 10.0, False) is None
    assert rules.check_rate_limit(95.0, 100.0, 10.0, False) == 5.0
    assert rules.check_rate_limit(90.0, 100.0, 10.0, False) is None
    assert rules.check_rate_limit(99.0, 100.0, 10.0, True) is None
    assert rules.check_rate_limit(99.0, 100.0, 0.0, False) is None


def test_exemptions() -> None:
    ex = rules.rate_limit_exempt
    assert ex(reply_to_kind="human", reply_to_mentions=(), sender_name="bot", unhandled_priority=0)
    assert ex(reply_to_kind="agent", reply_to_mentions=("BOT",), sender_name="bot", unhandled_priority=0)
    assert ex(reply_to_kind=None, reply_to_mentions=(), sender_name="bot", unhandled_priority=1)
    assert not ex(reply_to_kind="agent", reply_to_mentions=("amy",), sender_name="bot", unhandled_priority=0)
    assert not ex(reply_to_kind=None, reply_to_mentions=(), sender_name="bot", unhandled_priority=0)


# ------------------------------------------------------- engine.check_say
@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def said(w: World, p: Any) -> None:
    """A say() went out: the service records when."""
    w.store.update_participant(p.id, last_say_at=w.clock.now())


def test_one_say_per_ten_seconds_per_agent(w: World, clock: FakeClock) -> None:
    p, m = w.agent("bot")
    q, mq = w.agent("amy")
    assert w.engine.check_say(w.p(p), w.m(m), None) is None  # the first one
    said(w, p)
    clock.advance(4)
    assert w.engine.check_say(w.p(p), w.m(m), None) == 6.0
    [ev] = w.store.recent_events(kinds=("rate_limited",))
    assert ev.membership_id == m.id and ev.data["retry_after_s"] == 6.0
    assert w.engine.check_say(w.p(q), w.m(mq), None) is None  # per agent, not per room
    clock.advance(6)
    assert w.engine.check_say(w.p(p), w.m(m), None) is None
    assert w.store.count_events("rate_limited") == 1


def test_replying_to_the_human_or_to_an_at_mention_is_exempt(w: World, clock: FakeClock) -> None:
    p, m = w.agent("bot")
    _q, amy = w.agent("amy")
    human = w.human("what do you think?")
    to_bot = w.agent_says(amy, "@bot and you?", mentions=("bot",))
    to_other = w.agent_says(amy, "@amy note to self", mentions=("amy",))
    said(w, p)
    clock.advance(1)
    assert w.engine.check_say(w.p(p), w.m(m), human) is None
    assert w.engine.check_say(w.p(p), w.m(m), to_bot) is None
    assert w.engine.check_say(w.p(p), w.m(m), to_other) == 9.0  # an agent message not addressed to it
    assert w.engine.check_say(w.p(p), w.m(m), None) == 9.0


def test_answering_priority_items_in_context_is_exempt(w: World, clock: FakeClock) -> None:
    p, m = w.agent("bot", hooks=True)
    said(w, p)
    w.human("@bot quick question", mentions=("bot",))
    out = w.hook(p, "PostToolUse", ok=True)  # it reached the model mid-task
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    clock.advance(1)
    assert w.engine.check_say(w.p(p), w.m(m), None) is None  # unanswered priority in context
    w.store.mark_handled(m.id)  # the say answers it
    said(w, p)
    clock.advance(1)
    assert w.engine.check_say(w.p(p), w.m(m), None) == 9.0


def test_a_refusal_queues_nothing(w: World, clock: FakeClock) -> None:
    """A refused say is not held for later: no message, no delivery to anyone, the
    hop counter and the agent's 10 s untouched, and what it was owed stays pending
    (it comes back with the refusal, as a say() pull)."""
    p, m = w.agent("bot")
    _q, mq = w.agent("amy")
    said(w, p)
    owed = w.human("unread for bot")
    w.agent_says(mq, "amy's chatter")

    def snapshot() -> tuple[Any, ...]:
        con = w.store.con
        return (
            con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM batches").fetchone()[0],
            w.store.room_by_id(w.room.id).hop_count,
            w.p(p).last_say_at,
            w.states(m),
            w.states(mq),
        )

    before = snapshot()
    clock.advance(3)
    assert w.engine.check_say(w.p(p), w.m(m), None) == 7.0
    assert snapshot() == before
    clock.advance(2)
    assert w.engine.check_say(w.p(p), w.m(m), None) == 5.0  # the refusal didn't restart its 10 s
    assert snapshot() == before and w.states(m)[owed.id] == "pending"
    assert w.store.count_events("rate_limited") == 2


def test_rate_limit_zero_is_off(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock, Config().with_delivery(rate_limit_s=0.0))
    p, m = w.agent("bot")
    said(w, p)
    assert w.engine.check_say(w.p(p), w.m(m), None) is None
