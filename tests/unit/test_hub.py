"""hub: per-room fan-out, slow consumers, debounced member snapshots."""

from __future__ import annotations

import asyncio
import time

from switchboard.broker import hub as H
from switchboard.broker.hub import Hub, WsSubscriber
from switchboard.broker.rpc import TailSubscriber


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


class _Conn:
    """What a TailSubscriber writes to: an RPC connection's push queue."""

    closed = False

    def __init__(self) -> None:
        self.pushed: list[tuple[str, dict]] = []

    def push(self, kind: str, data: dict) -> None:
        self.pushed.append((kind, data))


def test_drop_room_unsubscribes_everyone_from_that_room() -> None:
    """A closed or deleted room (DESIGN.md §28): a room created again under its name never
    streams into an old web page or tail."""
    h = Hub()
    ws, other = WsSubscriber(), WsSubscriber()
    ws.rooms, other.rooms = {"#build", "#lab"}, {"#lab"}
    conn = _Conn()
    tail = TailSubscriber(conn, "#build")  # type: ignore[arg-type]
    for s in (ws, other, tail):
        h.add(s)
    assert h.message("#build", {"id": 1}) == 2
    h.drop_room("#build")
    assert ws.rooms == {"#lab"} and other.rooms == {"#lab"} and tail.rooms == set()
    assert h.message("#build", {"id": 2}) == 0
    assert h.message("#lab", {"id": 3}) == 2
    assert [d["msg"]["id"] for _, d in conn.pushed] == [1]
    assert not ws.closed and not tail.closed  # still connected, only quieter
    assert h.notice(None, "warn", "#build deleted") == 3
    h.drop_room("#nobody")  # unknown: nothing to do


def test_drop_room_cancels_its_pending_frames() -> None:
    async def run() -> tuple[list[str], list[str], dict, dict]:
        h = Hub()
        members: list[str] = []
        settings: list[str] = []
        h.set_members_source(lambda room: members.append(room) or [])
        h.set_settings_source(lambda room: settings.append(room) or {})
        h.members_changed("#build")
        h.settings_changed("#build")
        h.members_changed("#lab")  # scheduled after #build's, so it fires after them
        handles = (h._members_pending["#build"], h._settings_pending["#build"])
        h.drop_room("#build")
        assert all(x.cancelled() for x in handles)
        pending = (dict(h._members_pending), dict(h._settings_pending))
        deadline = time.monotonic() + 5
        while "#lab" not in members and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return members, settings, *pending

    members, settings, mp, sp = asyncio.run(run())
    assert list(mp) == ["#lab"] and sp == {}
    assert members == ["#lab"] and settings == []


def drained(s: WsSubscriber) -> list[dict]:
    out = []
    while not s.q.empty():
        out.append(s.q.get_nowait())
    return out


def test_the_review_board_is_published_again_only_when_who_is_in_the_room_changes() -> None:
    """Whether an option is recommended depends on every agent in the room (DESIGN.md §37.12), so a
    join or a leave sends the page the board again; a status change alone sends the members and
    nothing more. Fails if the board is never refreshed (a newcomer is not waited on), or if every
    busy/idle flip republishes it (a rebuild per flip)."""
    h = Hub()
    members: list[dict] = [{"name": "claude-1", "status": "idle"}]
    boards: list[str] = []

    def board(room: str) -> dict | None:
        boards.append(room)
        return {"url": "u", "n": len(boards)}

    h.set_members_source(lambda room: list(members))
    h.set_board_source(board)
    s = WsSubscriber()
    s.rooms = {"#a"}
    h.add(s)
    h.members_changed("#a")  # no loop here, so this publishes at once
    assert [f["t"] for f in drained(s)] == ["members", "review"]
    members[0]["status"] = "busy"
    h.members_changed("#a")
    assert [f["t"] for f in drained(s)] == ["members"]  # same names: the board stays as it was
    members.append({"name": "codex-1", "status": "idle"})
    h.members_changed("#a")
    frames = drained(s)
    assert [f["t"] for f in frames] == ["members", "review"] and frames[1]["board"]["n"] == 2
    members.pop(0)
    h.members_changed("#a")
    assert [f["t"] for f in drained(s)] == ["members", "review"]
    assert boards == ["#a", "#a", "#a"]


def test_a_room_with_no_board_gets_no_review_frame_and_a_dropped_room_forgets_its_members() -> None:
    h = Hub()
    h.set_members_source(lambda room: [{"name": "claude-1"}])
    h.set_board_source(lambda room: None)  # no board is open in the room
    s = WsSubscriber()
    s.rooms = {"#a"}
    h.add(s)
    h.members_changed("#a")
    assert [f["t"] for f in drained(s)] == ["members"]
    assert h._member_names == {"#a": (("claude-1",), ())}
    h.drop_room("#a")
    assert h._member_names == {}


def test_the_board_is_published_again_when_an_agent_goes_offline_or_comes_back() -> None:
    """A contested finding is a person's once its raiser is offline (DESIGN.md §37.13), so the drop
    to offline, and the return, send the page the board again with no move at all; busy and idle
    still don't. Fails if an agent going offline leaves the finding in the agents' lane."""
    h = Hub()
    members: list[dict] = [{"name": "claude-1", "status": "idle"}, {"name": "codex-1", "status": "idle"}]
    h.set_members_source(lambda room: [dict(m) for m in members])
    h.set_board_source(lambda room: {"url": "u"})
    s = WsSubscriber()
    s.rooms = {"#a"}
    h.add(s)
    h.members_changed("#a")
    assert [f["t"] for f in drained(s)] == ["members", "review"]
    members[1]["status"] = "busy"
    h.members_changed("#a")
    assert [f["t"] for f in drained(s)] == ["members"]
    members[1]["status"] = "offline"
    h.members_changed("#a")
    assert [f["t"] for f in drained(s)] == ["members", "review"]
    members[1]["status"] = "idle"  # back
    h.members_changed("#a")
    assert [f["t"] for f in drained(s)] == ["members", "review"]
