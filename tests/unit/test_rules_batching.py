"""Batching (DESIGN.md §8.2): everything that isn't the
human's or an @mention is delivered when the agent is idle and the room has been
quiet for 3 s, as one batch; the maximum hold is 60 s (continuous chatter never
goes quiet); a batch is capped (20 messages, 6,000 rendered characters, and each
harness's own context limit). What doesn't fit stays pending for the next batch."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock
from engine_world import World
from test_codex_adapter import attach, codex
from switchboard.config import Config
from switchboard.models import Push

QUIET, HOLD = 3.0, 60.0


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)  # the real defaults: quiet 3 s, max hold 60 s, 20 messages, 6,000 chars


def listen(w: World, p: Any, m: Any, wid: str) -> Any:
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), wid, 3600)
    w.actions += acts
    return sink


def result(w: World, sink: Any) -> dict[str, Any] | None:
    s = w.engine.sinks.get(sink.id)
    return None if s is None or s.open else s.result


def ids(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"^- id=(\d+) ", text, re.M)]


def tick(w: World, dt: float) -> None:
    w.clock.advance(dt)
    w.actions += w.engine.tick()


def test_defaults() -> None:
    d = Config().delivery
    assert (d.quiet_s, d.max_hold_s, d.batch_max_msgs, d.batch_max_chars) == (3.0, 60.0, 20, 6000)


def test_chatter_waits_for_three_quiet_seconds_then_goes_as_one_batch(w: World) -> None:
    p, m = w.agent("bot")
    _pp, peer = w.agent("peer")
    s = listen(w, p, m, "w1")
    a = w.agent_says(peer, "one")
    tick(w, 1.0)
    b = w.agent_says(peer, "two")  # the quiet period starts over
    tick(w, QUIET - 0.01)
    assert result(w, s) is None
    tick(w, 0.01)
    res = result(w, s)
    assert res is not None and ids(res["text"]) == [a.id, b.id]  # one batch, in order
    batch = w.store.get_batch(res["batch_id"])
    assert batch.wake_reason == "chatter" and batch.budget_counted


def test_the_max_hold_releases_chatter_that_never_goes_quiet(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock, Config().with_delivery(hop_limit=0))  # 30 agent messages in a row
    p, m = w.agent("bot")
    _pp, peer = w.agent("peer")
    s = listen(w, p, m, "w1")
    first = w.agent_says(peer, "chatter 0")
    t0 = w.clock.now()
    i = 1
    while w.clock.now() - t0 < HOLD - 2:
        tick(w, 2.0)  # a message every 2 s: never 3 s of quiet
        w.agent_says(peer, f"chatter {i}")
        i += 1
        assert result(w, s) is None
    tick(w, 2.0)
    res = result(w, s)
    assert res is not None and ids(res["text"])[0] == first.id
    assert w.clock.now() - t0 >= HOLD


def test_a_human_message_or_mention_is_not_held_by_the_quiet_period(w: World) -> None:
    p, m = w.agent("bot")
    q, mq = w.agent("amy")
    _pp, peer = w.agent("peer")
    s, sq = listen(w, p, m, "w1"), listen(w, q, mq, "w2")
    chat = w.agent_says(peer, "chatter first")
    ment = w.agent_says(peer, "@bot now", mentions=("bot",))
    res = result(w, s)
    assert res is not None and ids(res["text"]) == [ment.id, chat.id]  # the chatter rides along
    assert result(w, sq) is None  # amy only has chatter: still quiet-held
    hum = w.human("right away")
    res = result(w, sq)
    assert res is not None and ids(res["text"]) == [hum.id, chat.id, ment.id]


def test_the_batch_cap_in_messages(w: World) -> None:
    p, m = w.agent("bot")
    msgs = [w.human(f"m{i}") for i in range(25)]
    s = listen(w, p, m, "w1")
    res = result(w, s)
    assert ids(res["text"]) == [x.id for x in msgs[:20]]
    w.actions += w.engine.before_call(w.p(p))
    s = listen(w, p, m, "w2")
    assert ids(result(w, s)["text"]) == [x.id for x in msgs[20:]]  # the rest, next


def test_the_batch_cap_in_rendered_characters(w: World) -> None:
    p, m = w.agent("bot")
    msgs = [w.human("x" * 1400) for _ in range(6)]
    s = listen(w, p, m, "w1")
    res = result(w, s)
    got = ids(res["text"])
    assert 1 <= len(got) < 6 and got == [x.id for x in msgs[: len(got)]]
    assert len(res["text"]) <= w.cfg.delivery.batch_max_chars
    left = [d["message_id"] for d in w.store.deliveries(m.id) if d["state"] == "pending"]
    assert left == [x.id for x in msgs[len(got):]]


def test_the_cap_follows_each_harness_context_limit(w: World) -> None:
    """Codex keeps at most 5,000 characters of context: a steer batch fits in it."""
    p, mx = codex(w, status="busy")
    attach(w, view="busy")
    msgs = [w.human("y" * 1400) for _ in range(6)]
    [push] = [a for a in w.take() if isinstance(a, Push)]
    assert push.path == "steer" and len(push.text) <= w.cfg.codex.ctx_max_chars
    assert 1 <= len(ids(push.text)) <= 3
    pending = [d["message_id"] for d in w.store.deliveries(mx.id) if d["state"] == "pending"]
    assert pending == [x.id for x in msgs[len(ids(push.text)):]]


def test_one_huge_message_is_never_stuck(w: World) -> None:
    p, m = w.agent("bot")
    big = w.human("z" * 3900)
    s = listen(w, p, m, "w1")
    res = result(w, s)
    assert ids(res["text"]) == [big.id] and "z" * 3900 in res["text"]  # wait() shows it whole
