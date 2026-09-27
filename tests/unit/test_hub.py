"""hub: per-room fan-out, slow consumers, debounced member snapshots."""

from __future__ import annotations

import asyncio

from switchboard.broker import hub as H
from switchboard.broker.hub import Hub, WsSubscriber


def test_publish_only_to_subscribed_rooms() -> None:
    h = Hub()
    a, b = WsSubscriber(), WsSubscriber()
    a.rooms, b.rooms = {"#a"}, {"#b"}
    h.add(a)
    h.add(b)
    assert h.message("#a", {"id": 1}) == 1
    assert a.q.get_nowait() == {"t": "msg", "room": "#a", "msg": {"id": 1}}
    assert b.q.empty()
    assert h.notice(None, "warn", "x") == 2  # room-less notices go to everyone
    assert h.rooms_changed(["#a", "#b"]) == 2
    assert b.q.get_nowait() == {"t": "notice", "room": None, "level": "warn", "text": "x"}
    assert b.q.get_nowait() == {"t": "rooms", "rooms": ["#a", "#b"]}


def test_slow_consumer_is_dropped(monkeypatch) -> None:
    monkeypatch.setattr(H, "WS_QUEUE_MAX", 3)
    h = Hub()
    s = WsSubscriber()
    s.rooms = {"#a"}
    h.add(s)
    for i in range(3):
        h.message("#a", {"id": i})
    assert not s.closed
    h.message("#a", {"id": 99})
    assert s.closed
    assert h.message("#a", {"id": 100}) == 0


def test_close_sessions_targets_one_session() -> None:
    h = Hub()
    a, b = WsSubscriber(), WsSubscriber()
    a.sid_hash, b.sid_hash = "s1", "s2"
    h.add(a)
    h.add(b)
    assert h.close_sessions("s1") == 1 and a.closed and not b.closed
    assert h.close_sessions(None) == 1 and b.closed


def test_members_snapshot_is_debounced() -> None:
    async def run() -> list:
        h = Hub()
        calls = []
        h.set_members_source(lambda room: calls.append(room) or [{"name": "x"}])
        s = WsSubscriber()
        s.rooms = {"#a"}
        h.add(s)
        for _ in range(5):
            h.members_changed("#a")
        await asyncio.sleep(H.MEMBERS_DEBOUNCE_S + 0.1)
        items = []
        while not s.q.empty():
            items.append(s.q.get_nowait())
        return [calls, items]

    calls, items = asyncio.run(run())
    assert calls == ["#a"]
    assert items == [{"t": "members", "room": "#a", "members": [{"name": "x"}]}]


def test_sender_stops_on_close_sentinel() -> None:
    async def run() -> list:
        s = WsSubscriber()
        sent, closed = [], []

        async def send_text(t: str) -> None:
            sent.append(t)

        async def close(code: int) -> None:
            closed.append(code)

        task = asyncio.create_task(s.run_sender(send_text, close))
        s.offer({"t": "pong"})
        s.close()
        await asyncio.wait_for(task, 2)
        return [sent, closed]

    sent, closed = asyncio.run(run())
    assert sent == ['{"t": "pong"}'] and closed == [1008]
