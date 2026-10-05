"""Human first (DESIGN.md §8.2): agent chatter never jumps
ahead of the human's pending messages, and at most one peer batch is released per
turn boundary (human messages and @mentions are not limited by it)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import World
from test_claude_adapter import claude, reg, tok
from test_rules_release import item

from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import Push


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def ids(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"^- id=(\d+) ", text, re.M)]


def listen(w: World, p: Any, m: Any, wid: str) -> Any:
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), wid, 3600)
    w.actions += acts
    return sink


def result(w: World, sink: Any) -> dict[str, Any] | None:
    s = w.engine.sinks.get(sink.id)
    return None if s is None or s.open else s.result


def test_order_is_human_then_mentions_then_chatter_each_by_id() -> None:
    items = [item(5, 0), item(1, 1), item(9, 2), item(3, 2), item(2, 0), item(4, 1)]
    assert [i.message_id for i in rules.human_first(items)] == [3, 9, 1, 4, 2, 5]


def test_older_chatter_never_crowds_out_a_later_human_message(tmp_path: Path, clock: FakeClock) -> None:
    w = World(
        tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0, batch_max_msgs=3, hop_limit=0)
    )
    p, m = w.agent("bot")
    _pp, peer = w.agent("peer")
    w.store.set_budget(w.room.id, 0)  # chatter can't go on its own
    chatter = [w.agent_says(peer, f"chatter {i}") for i in range(5)]
    hum = [w.human(f"alice {i}") for i in range(2)]
    s = listen(w, p, m, "w1")
    got = ids(result(w, s)["text"])
    assert got[:2] == [x.id for x in hum] and got[2] == chatter[0].id and len(got) == 3


def test_mid_task_the_human_leads_then_mentions_and_no_chatter(w: World) -> None:
    p, m = w.agent("bot", hooks=True)  # busy
    _pp, peer = w.agent("peer")
    chat = w.agent_says(peer, "chatter")
    ment = w.agent_says(peer, "@bot look", mentions=("bot",))
    hum = w.human("from alice")
    out = w.hook(p, "PostToolUse", ok=True)
    assert ids(out.text) == [hum.id, ment.id] and chat.id not in ids(out.text)


def test_one_peer_batch_per_turn_boundary(w: World, clock: FakeClock) -> None:
    """Claude on the inbox: a chatter wake starts a turn; more chatter waits for
    that turn to end (Stop), then goes as the next batch."""
    p, m, _conn = claude(w)
    _pp, peer = w.agent("peer")
    a = w.agent_says(peer, "chatter A")
    [push] = [x for x in w.take() if isinstance(x, Push)]
    assert ids(push.text) == [a.id]
    w.hook(p, "UserPromptSubmit", tokens=(tok(push.text),), gen="g1")  # the frame started a turn
    b = w.agent_says(peer, "chatter B")
    c = w.agent_says(peer, "chatter C")
    assert not [x for x in w.take() if isinstance(x, Push)]
    assert w.hook(p, "PostToolUse", ok=True) is None  # never chatter mid-task
    w.store.mark_handled(m.id)
    w.hook(p, "Stop")  # the turn boundary
    reg(w, p, "idle")
    w.actions += w.engine.evaluate(m.id)
    [push] = [x for x in w.take() if isinstance(x, Push)]
    assert ids(push.text) == [b.id, c.id]


def test_a_human_message_is_not_limited_by_the_peer_batch(w: World) -> None:
    p, m, _conn = claude(w)
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "chatter A")
    [push] = [x for x in w.take() if isinstance(x, Push)]
    w.hook(p, "UserPromptSubmit", tokens=(tok(push.text),), gen="g1")
    w.store.mark_handled(m.id)
    # the member is idle again with no new boundary (e.g. an Esc the registry saw): a
    # human message still wakes it, and fresh chatter waits for the next boundary
    w.store.set_status(p.id, "idle", "test")
    chat = w.agent_says(peer, "chatter B")
    assert not [x for x in w.take() if isinstance(x, Push)]
    hum = w.human("alice here")
    reg(w, p, "idle")
    w.actions += w.engine.evaluate(m.id)
    [push] = [x for x in w.take() if isinstance(x, Push)]
    assert ids(push.text) == [hum.id] and chat.id not in ids(push.text)


def test_an_expired_peer_batch_does_not_use_up_the_boundary(w: World, clock: FakeClock) -> None:
    """A chatter frame that never started a turn goes back to pending, and goes
    again without waiting for a turn boundary (never lose a wake)."""
    p, m, _conn = claude(w)
    _pp, peer = w.agent("peer")
    a = w.agent_says(peer, "chatter A")
    [push] = [x for x in w.take() if isinstance(x, Push)]
    w.store.mark_posted(push.batch_id)
    clock.advance(w.cfg.claude.inbox_idle_expire_s)
    reg(w, p, "idle")
    w.actions += w.engine.tick()
    assert w.store.get_batch(push.batch_id).expire_reason == "idle_no_token"
    for _ in range(10):  # the unconfirmed-frame backoff, then the retry
        clock.advance(1)
        reg(w, p, "idle")
        w.actions += w.engine.tick()
    again = [x for x in w.take() if isinstance(x, Push)]
    assert again and ids(again[0].text) == [a.id]
