"""hub: closed subscribers, the close sentinel, a sender whose client went away, the
settings debounce and shutdown (DESIGN.md §5.5)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from switchboard.broker import hub as H
from switchboard.broker.hub import Hub, Subscriber, WsSubscriber


def drain(s: Subscriber) -> list[Any]:
    out = []
    while not s.q.empty():
        out.append(s.q.get_nowait())
    return out


def test_a_subscriber_wants_only_its_kinds_and_nothing_once_closed() -> None:
    s = WsSubscriber()
    s.rooms = {"#a"}
    assert s.wants("msg", "#a") and s.wants("notice", None)
    assert not s.wants("msg", "#b")
    assert not s.wants("typing", "#a")  # not a kind a WebSocket takes
    s.close()
    assert not s.wants("msg", "#a") and not s.wants("notice", None)


def test_offer_after_close_is_refused_and_queues_nothing() -> None:
    s = WsSubscriber()
    s.close()
    assert drain(s) == [None]  # the close sentinel
    assert s.offer({"t": "pong"}) is False
    assert s.q.empty()
    s.close()  # closing twice queues no second sentinel
    assert s.q.empty()


def test_close_on_a_full_queue_makes_room_for_the_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(H, "WS_QUEUE_MAX", 2)
    s = WsSubscriber()
    assert s.offer(1) and s.offer(2)
    s.close()
    assert s.closed
    assert drain(s) == [2, None]  # the oldest item made way for the sentinel


class _NoRoomQueue(asyncio.Queue):
    """A queue that never takes another item, even after one is taken off."""

    def put_nowait(self, item: Any) -> None:
        raise asyncio.QueueFull


def test_close_never_raises_when_the_sentinel_cannot_be_queued() -> None:
    # an item is taken off to make room, but the queue still refuses the sentinel
    s = WsSubscriber()
    s.q = _NoRoomQueue(maxsize=1)
    asyncio.Queue.put_nowait(s.q, {"t": "msg"})
    s.close()
    assert s.closed and s.q.empty()
    assert s.offer({"t": "pong"}) is False
    # an empty queue that refuses items: the get fails first, still no raise
    s2 = WsSubscriber()
    s2.q = _NoRoomQueue(maxsize=1)
    s2.close()
    assert s2.closed and s2.q.empty()


def test_a_sender_whose_client_went_away_marks_the_subscriber_closed() -> None:
    async def run() -> tuple[list[str], list[int], bool]:
        s = WsSubscriber()
        sent: list[str] = []
        closed: list[int] = []

        async def send_text(t: str) -> None:
            if sent:
                raise ConnectionResetError("client went away")
            sent.append(t)

        async def close(code: int) -> None:
            closed.append(code)

        task = asyncio.create_task(s.run_sender(send_text, close))
        s.offer({"t": "pong"})
        s.offer({"t": "pong", "n": 2})
        await asyncio.wait_for(task, 2)  # returns: no exception escapes the sender
        return sent, closed, s.closed

    sent, closed, is_closed = asyncio.run(run())
    assert sent == ['{"t": "pong"}'] and closed == [] and is_closed


def test_a_cancelled_sender_stays_cancelled() -> None:
    async def run() -> tuple[bool, bool]:
        s = WsSubscriber()
        calls: list[Any] = []

        async def send_text(t: str) -> None:
            calls.append(t)

        async def close(code: int) -> None:
            calls.append(code)

        task = asyncio.create_task(s.run_sender(send_text, close))
        await asyncio.sleep(0)  # the sender is now waiting on its queue
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == []  # the queue stayed empty: nothing sent, no close
        return task.cancelled(), s.closed

    cancelled, closed = asyncio.run(run())
    assert cancelled and not closed


def test_flushes_without_a_source_publish_nothing() -> None:
    h = Hub()
    s = WsSubscriber()
    s.rooms = {"#a"}
    h.add(s)
    h.members_changed("#a")  # no event loop: flushes at once, but there is no members source
    h.settings_changed("#a")  # likewise for settings
    assert s.q.empty()
    assert h._members_pending == {} and h._settings_pending == {}


def test_settings_without_a_loop_are_published_at_once() -> None:
    h = Hub()
    s = WsSubscriber()
    s.rooms = {"#a"}
    h.add(s)
    calls: list[str] = []

    def source(room: str) -> dict[str, Any] | None:
        calls.append(room)
        return {"paused": True} if room == "#a" else None

    h.set_settings_source(source)
    h.settings_changed("#a")
    h.settings_changed("#gone")  # the source knows no such room: no frame
    assert calls == ["#a", "#gone"]
    assert drain(s) == [{"t": "room", "room": "#a", "settings": {"paused": True}}]


def test_settings_frames_are_debounced_on_a_loop() -> None:
    async def run() -> tuple[list[str], list[Any]]:
        h = Hub()
        calls: list[str] = []
        h.set_settings_source(lambda room: calls.append(room) or {"budget": 3})
        s = WsSubscriber()
        s.rooms = {"#a"}
        h.add(s)
        for _ in range(4):
            h.settings_changed("#a")
        assert list(h._settings_pending) == ["#a"]
        await asyncio.sleep(H.MEMBERS_DEBOUNCE_S + 0.05)
        return calls, drain(s)

    calls, items = asyncio.run(run())
    assert calls == ["#a"]
    assert items == [{"t": "room", "room": "#a", "settings": {"budget": 3}}]


def test_close_all_cancels_pending_flushes_and_closes_every_subscriber() -> None:
    async def run() -> tuple[list[str], Hub, list[WsSubscriber]]:
        h = Hub()
        calls: list[str] = []
        h.set_members_source(lambda room: calls.append(room) or [])
        h.set_settings_source(lambda room: calls.append(room) or {})
        subs = [WsSubscriber(), WsSubscriber()]
        for s in subs:
            s.rooms = {"#a"}
            h.add(s)
        h.members_changed("#a")
        h.settings_changed("#a")
        handles = [*h._members_pending.values(), *h._settings_pending.values()]
        assert len(handles) == 2
        h.close_all()
        assert all(t.cancelled() for t in handles)  # the debounced flushes never run
        return calls, h, subs

    calls, h, subs = asyncio.run(run())
    assert calls == []
    assert h.subs == set() and h._members_pending == {} and h._settings_pending == {}
    assert all(s.closed for s in subs)
    assert all(drain(s) == [None] for s in subs)
    assert h.ws_count() == 0
