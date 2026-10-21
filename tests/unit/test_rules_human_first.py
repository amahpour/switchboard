"""Human first (DESIGN.md §8.2): agent chatter never jumps
ahead of the human's pending messages, and at most one peer batch is released per
turn boundary (human messages and @mentions are not limited by it)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_agents import STOP_MAX_WAIT_S, fills, listener, result, stop
from engine_world import World
from test_rules_release import item

from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import HookEvent


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0, unaddressed_hold_s=0.0))


def ids(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"^- id=(\d+) ", text, re.M)]


def listen(w: World, p: Any, m: Any, wid: str) -> Any:
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), wid, 3600)
    w.actions += acts
    return sink


def test_order_is_human_then_mentions_then_chatter_each_by_id() -> None:
    items = [item(5, 0), item(1, 1), item(9, 2), item(3, 2), item(2, 0), item(4, 1)]
    assert [i.message_id for i in rules.human_first(items)] == [3, 9, 1, 4, 2, 5]


def test_older_chatter_never_crowds_out_a_later_human_message(tmp_path: Path, clock: FakeClock) -> None:
    w = World(
        tmp_path,
        clock,
        Config().with_delivery(
            quiet_s=0.0, max_hold_s=0.0, unaddressed_hold_s=0.0, batch_max_msgs=3, hop_limit=0
        ),
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
    """A listening Claude Code: a chatter wake (the answer to its Stop hook) starts a turn; more
    chatter waits for that turn to end (its next Stop), then goes as the next batch."""
    p, m, park = listener(w)
    _pp, peer = w.agent("peer")
    a = w.agent_says(peer, "chatter A")
    [res] = fills(w, park)
    assert ids(res["text"]) == [a.id]
    w.engine.on_hook_ack(res["batch_id"], res["ack"])
    w.hook(p, "UserPromptSubmit", gen="g1")  # the follow-up started a turn
    b = w.agent_says(peer, "chatter B")
    c = w.agent_says(peer, "chatter C")
    assert not fills(w)
    assert w.hook(p, "PostToolUse", ok=True) is None  # never chatter mid-task
    w.store.mark_handled(m.id)
    second = stop(w, p)  # the turn boundary: the new Stop parks, and the batch is ready at once
    assert ids(result(w, second)["text"]) == [b.id, c.id]


def test_a_human_message_is_not_limited_by_the_peer_batch(w: World) -> None:
    p, m, park = listener(w)
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "chatter A")
    [res] = fills(w, park)
    w.engine.on_hook_ack(res["batch_id"], res["ack"])
    w.hook(p, "UserPromptSubmit", gen="g1")
    w.store.mark_handled(m.id)
    # the member is idle again and listening with no new boundary: a human message still wakes it,
    # and fresh chatter waits for the next boundary
    w.store.set_status(p.id, "idle", "test")
    ev = HookEvent(harness=p.harness, event="Stop", max_wait_s=STOP_MAX_WAIT_S)
    park2, acts = w.engine.open_park(w.p(p), ev, 3600)
    w.actions += acts
    chat = w.agent_says(peer, "chatter B")
    assert not fills(w)
    hum = w.human("alice here")
    [res] = fills(w, park2)
    assert ids(res["text"]) == [hum.id] and chat.id not in ids(res["text"])


def test_an_expired_peer_batch_does_not_use_up_the_boundary(w: World, clock: FakeClock) -> None:
    """A chatter follow-up the harness never acknowledged goes back to pending, and goes again at
    the next Stop without waiting for a turn boundary (never lose a wake)."""
    p, m, park = listener(w)
    _pp, peer = w.agent("peer")
    a = w.agent_says(peer, "chatter A")
    [res] = fills(w, park)
    clock.advance(w.cfg.delivery.hook_ack_s + 0.1)
    w.actions += w.engine.tick()
    assert w.store.get_batch(res["batch_id"]).expire_reason == "no_ack"
    again = stop(w, p)
    assert ids(result(w, again)["text"]) == [a.id]
