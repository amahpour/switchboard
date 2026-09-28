"""The runner (DESIGN.md §8.2, §8.7): what happens to a push after its transport
reports (a failure goes back to pending as a counted push failure, a re-route
without counting, a frame confirmed meanwhile is left alone, the post time is
bounded), wait() futures, snapshots of an unknown room, a failing engine tick,
and stop() (frames in flight cancelled, every open wait() answered)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import FakeClock
from engine_world import World
from test_claude_adapter import claude
from test_cursor_adapter import cursor
from switchboard.adapters.base import SendError
from switchboard.config import Config
from switchboard.delivery.runner import Runner
from switchboard.models import HookEvent, Push, ResolveSink, Snapshot


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


def claude_push(w: World) -> tuple[Any, Any, Any, Push, Any]:
    p, m, conn = claude(w)
    w.engine.adapters["claude"].clock = w.clock  # its send backoff runs on the FakeClock
    msg = w.human("wake up")
    [push] = [a for a in w.take() if isinstance(a, Push)]
    return p, m, conn, push, msg


class Transport:
    """A scripted ``adapter.send``: each call takes the next outcome, an exception
    to raise or the ``t_post`` to report (a callable is run first, with the batch)."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[int] = []

    async def __call__(self, p: Any, b: Any, text: str, **meta: Any) -> float | None:
        self.sent.append(b.id)
        o = self.outcomes.pop(0)
        if callable(o) and not isinstance(o, BaseException):
            o = o(b)
        if isinstance(o, BaseException):
            raise o
        return o


def test_a_push_the_transport_could_not_send_goes_back_to_pending_as_a_failure(
        w: World, caplog: pytest.LogCaptureFixture) -> None:
    p, m, conn, push, msg = claude_push(w)
    w.engine.adapters["claude"].detach(conn)  # the session's MCP server went away first
    hub = RecHub()
    with caplog.at_level(logging.WARNING, logger="switchboard.runner"):
        asyncio.run(runner_for(w, hub)._push(push))
    b = w.store.get_batch(push.batch_id)
    assert (b.state, b.expire_reason) == ("expired", "send_error")
    d = w.delivery(m, msg)
    assert (d["state"], d["attempts"]) == ("pending", 1)
    assert w.p(p).push_expiries == 1  # counted, unlike a re-route
    assert f"push of batch {push.batch_id} failed: no inbox channel" in caplog.text
    assert ("members", "#build") in hub.calls  # the re-evaluation's snapshot went out
    assert "idle and not listening" in (w.engine.parked_reason(m.id) or "")  # no second frame


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


def test_stop_cancels_frames_in_flight_and_answers_open_waits(w: World) -> None:
    _p, _m, conn, push, _msg = claude_push(w)
    q, qm = w.agent("bot")  # joined after the message: nothing pending for it
    sink, _acts = w.engine.open_wait(w.p(q), w.m(qm), "w1", 50)
    r = runner_for(w)

    async def go() -> tuple[asyncio.Future[dict[str, Any]], asyncio.Task[Any]]:
        r.execute([push])  # the frame waits for the session's mcp.posted
        for _ in range(50):
            if conn.pushes:
                break
            await asyncio.sleep(0)
        [task] = list(r._tasks)
        fut = r.future_for(sink.id)
        assert not fut.done()
        await r.stop()
        return fut, task

    fut, task = asyncio.run(go())
    assert [k for k, _d in conn.pushes] == ["deliver"]  # handed over, then cancelled
    assert task.cancelled() and not r._tasks
    assert fut.result() == {"status": "cancelled"} and r.futures == {}
    b = w.store.get_batch(push.batch_id)
    assert b.state == "offered" and b.posted_at is not None  # posted: left to the adapter's expiry
    assert push.batch_id not in w.engine.adapters["claude"].pending_posts


def test_a_rerouted_push_is_offered_again_at_once_without_counting_a_failure(
        w: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    p, m, _conn, push, msg = claude_push(w)
    send = Transport(SendError("turn ended", counted=False), None)
    monkeypatch.setattr(w.engine.adapters["claude"], "send", send)
    r = runner_for(w)

    async def go() -> None:
        await r._push(push)
        await asyncio.gather(*list(r._tasks))  # the frame the re-evaluation queued

    with caplog.at_level(logging.INFO, logger="switchboard.runner"):
        asyncio.run(go())
    first = w.store.get_batch(push.batch_id)
    assert (first.state, first.expire_reason) == ("expired", "reroute")
    assert w.p(p).push_expiries == 0
    assert len(send.sent) == 2 and send.sent[0] == push.batch_id
    second = w.store.get_batch(send.sent[1])
    assert second.state == "offered" and second.posted_at is not None
    assert w.delivery(m, msg)["batch_id"] == second.id
    assert f"push of batch {push.batch_id} re-routed: turn ended" in caplog.text


def test_a_push_confirmed_before_its_transport_reported_is_left_alone(
        w: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    p, m, _conn, push, msg = claude_push(w)

    def landed(b: Any) -> SendError:
        # the frame landed and its UserPromptSubmit confirmed it before mcp.posted came back
        w.engine.on_confirm(b.id, "hook:UserPromptSubmit")
        return SendError("no mcp.posted")

    monkeypatch.setattr(w.engine.adapters["claude"], "send", Transport(landed))
    with caplog.at_level(logging.INFO, logger="switchboard.runner"):
        asyncio.run(runner_for(w)._push(push))
    assert w.store.get_batch(push.batch_id).state == "confirmed"
    assert w.delivery(m, msg)["state"] == "in_context"
    assert w.p(p).push_expiries == 0  # nothing to take back, no failure counted
    assert f"push of batch {push.batch_id}: no post report (no mcp.posted); batch already confirmed" in caplog.text


@pytest.mark.parametrize(("t_post", "posted"), [
    (1000.0, 6.0),  # a far-future clock reading is capped at now + 1 s
    (5.5, 5.5),  # a later reading refines the post time
    (-1000.0, 5.0),  # never before the batch; not earlier than the hand-over either
])
def test_the_transports_post_time_is_bounded(w: World, clock: FakeClock, monkeypatch: pytest.MonkeyPatch,
                                              t_post: float, posted: float) -> None:
    _p, _m, _conn, push, _msg = claude_push(w)
    t0 = w.store.get_batch(push.batch_id).created_at
    clock.advance(5.0)  # handed to the transport 5 s after the batch was made
    monkeypatch.setattr(w.engine.adapters["claude"], "send", Transport(t0 + t_post))
    asyncio.run(runner_for(w)._push(push))
    b = w.store.get_batch(push.batch_id)
    assert b.state == "offered" and b.posted_at == pytest.approx(t0 + posted)


def test_wait_futures_resolve_once_and_a_dropped_one_is_never_answered(w: World) -> None:
    p, m = w.agent("bot")
    q, qm = w.agent("bot2")
    s1, _ = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    s2, _ = w.engine.open_wait(w.p(q), w.m(qm), "w1", 50)
    r = runner_for(w)

    async def go() -> tuple[Any, Any]:
        f1, f2 = r.future_for(s1.id), r.future_for(s2.id)
        r.drop_future(s2.id)  # its wait() RPC went away
        r.execute([ResolveSink(s1.id, {"status": "messages"}), ResolveSink(s2.id, {"status": "messages"}),
                   ResolveSink(s1.id, {"status": "timeout"})])
        return f1, f2

    f1, f2 = asyncio.run(go())
    assert f1.result() == {"status": "messages"}  # the first answer wins
    assert not f2.done() and r.futures == {}


def test_a_wait_future_for_an_already_closed_sink_is_done_at_once(w: World) -> None:
    p, m = w.agent("bot")
    w.human("hi")
    filled, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)  # filled on open
    [res] = [a.result for a in acts if isinstance(a, ResolveSink)]
    pc, _mc = cursor(w, status="idle")
    park, _ = w.engine.open_park(w.p(pc), HookEvent(harness="cursor", event="stop", loop_count=0), 60)
    w.engine.release_parks(pc.id, "superseded")  # resolved with no continuation (result None)
    r = runner_for(w)

    async def go() -> tuple[Any, Any]:
        return r.future_for(filled.id), r.future_for(park.id)

    f_filled, f_park = asyncio.run(go())
    assert f_filled.result() == res and res["status"] == "messages"
    assert f_park.result() == {"status": "cancelled"}
    assert r.futures == {}
