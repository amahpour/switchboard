"""The runner (DESIGN.md §8.2, §8.7): wait() futures, snapshots of an unknown room, a failing
engine tick, and stop() (every open wait() answered)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeClock
from engine_agents import listener
from engine_world import World

from switchboard.config import Config
from switchboard.delivery.runner import Runner
from switchboard.models import ResolveSink, Snapshot


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


class RecHub:
    """The hub calls a runner makes, recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def members_changed(self, room: str) -> None:
        self.calls.append(("members", room))

    def settings_changed(self, room: str) -> None:
        self.calls.append(("settings", room))

    def notice(self, room: str | None, level: str, text: str) -> None:
        self.calls.append(("notice", (room, level, text)))


def runner_for(w: World, hub: RecHub | None = None, **kw: Any) -> Runner:
    state = SimpleNamespace(store=w.store, engine=w.engine, clock=w.clock, hub=hub or RecHub())
    return Runner(state, **kw)  # type: ignore[arg-type]


def test_snapshots_are_sent_once_per_known_room(w: World) -> None:
    hub = RecHub()
    runner_for(w, hub).execute([Snapshot(w.room.id + 1000), Snapshot(w.room.id), Snapshot(w.room.id)])
    assert hub.calls == [("members", "#build"), ("settings", "#build")]


def test_a_failing_engine_tick_is_logged_and_the_loop_goes_on(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[int] = []

    async def go() -> None:
        second = asyncio.Event()

        class Engine:
            def tick(self) -> list[Any]:
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("boom")
                second.set()
                return []

        r = Runner(SimpleNamespace(engine=Engine()), tick_s=0)  # type: ignore[arg-type]
        task = asyncio.create_task(r.run())
        await asyncio.wait_for(second.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.ERROR, logger="switchboard.runner"):
        asyncio.run(go())
    assert len(calls) >= 2
    [rec] = [r for r in caplog.records if r.getMessage() == "engine tick failed"]
    assert rec.exc_info is not None and isinstance(rec.exc_info[1], RuntimeError)


def test_stop_answers_every_open_wait(w: World) -> None:
    q, qm = w.agent("bot")
    sink, _acts = w.engine.open_wait(w.p(q), w.m(qm), "w1", 50)
    r = runner_for(w)

    async def go() -> asyncio.Future[dict[str, Any]]:
        fut = r.future_for(sink.id)
        assert not fut.done()
        await r.stop()
        return fut

    fut = asyncio.run(go())
    assert fut.result() == {"status": "cancelled"} and r.futures == {}


def test_wait_futures_resolve_once_and_a_dropped_one_is_never_answered(w: World) -> None:
    p, m = w.agent("bot")
    q, qm = w.agent("bot2")
    s1, _ = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    s2, _ = w.engine.open_wait(w.p(q), w.m(qm), "w1", 50)
    r = runner_for(w)

    async def go() -> tuple[Any, Any]:
        f1, f2 = r.future_for(s1.id), r.future_for(s2.id)
        r.drop_future(s2.id)  # its wait() RPC went away
        r.execute(
            [
                ResolveSink(s1.id, {"status": "messages"}),
                ResolveSink(s2.id, {"status": "messages"}),
                ResolveSink(s1.id, {"status": "timeout"}),
            ]
        )
        return f1, f2

    f1, f2 = asyncio.run(go())
    assert f1.result() == {"status": "messages"}  # the first answer wins
    assert not f2.done() and r.futures == {}


def test_a_wait_future_for_an_already_closed_sink_is_done_at_once(w: World) -> None:
    p, m = w.agent("bot")
    w.human("hi")
    filled, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)  # filled on open
    [res] = [a.result for a in acts if isinstance(a, ResolveSink)]
    pc, _mc, parked = listener(w, "cursor-1", "cursor")
    park = w.engine.sinks.get(parked.sink_id)
    w.engine.release_parks(pc.id, "superseded")  # resolved with no continuation (result None)
    r = runner_for(w)

    async def go() -> tuple[Any, Any]:
        return r.future_for(filled.id), r.future_for(park.id)

    f_filled, f_park = asyncio.run(go())
    assert f_filled.result() == res and res["status"] == "messages"
    assert f_park.result() == {"status": "cancelled"}
    assert r.futures == {}
