"""``draw_mermaid`` is converted in the broker, with no board page open (§56.9, #725).

An agent's whole diagram is one change: one stored version, one room notice, one hub delta, under the
same rate limit and membership check as the other actions. What it drew is editable with ``move`` and
``edit`` and is in the snapshot of a page that opens later."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from conftest import FakeClock, InProcBroker, cookie_of, ws_connect
from fakes.fake_agent import FakeAgent
from test_board_tool import broker, q, recv_until  # noqa: F401

FLOW = (
    "flowchart TD\nA[Plan] --> B[Build]\nB --> C{Ship?}\nC -->|yes| D[Release]\nsubgraph team [Team]\nB\nend"
)


def stored(b: InProcBroker) -> list[dict]:
    return json.loads(q(b, "SELECT elements FROM room_boards")[0]["elements"])


async def test_a_diagram_is_one_change_with_no_page_open_and_a_later_page_gets_it(broker: InProcBroker):  # noqa: F811
    """With nobody looking at the board one call stores the whole drawing as one version, one notice
    and, for a page that is subscribed, one delta; a page that subscribes afterwards finds it in its
    snapshot. Fails if conversion still waited for a page, or wrote the shapes one by one."""
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        made = await drawer.call("board", room="#build", action="draw_mermaid", text=FLOW)
        assert made["ok"] and made["changed"] is True, made
        assert "drew 4 shapes and 3 arrows in 1 frames" in made["text"]
    rows = stored(broker)
    assert (
        len(rows) == len(made["ids"]) == 4 * 2 + 3 + 1 + 1
    )  # shapes with labels, arrows, an arrow label, a frame
    assert q(broker, "SELECT seq FROM room_boards")[0]["seq"] == 1
    notes = [r["text"] for r in q(broker, "SELECT text FROM messages") if "changed the board" in r["text"]]
    assert notes == ["claude-1 changed the board"]
    web = broker.web_client()
    ws = ws_connect(broker, cookie_of(web))
    try:
        ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {}}))
        snap = recv_until(ws, lambda f: f.get("t") == "board" and f.get("full"))
        assert snap["seq"] == 1 and {e["id"] for e in snap["elements"]} == set(made["ids"])
    finally:
        ws.close()
        web.close()


async def test_a_subscribed_page_hears_one_delta(broker: InProcBroker):  # noqa: F811
    """A page that is subscribed (not drawing, only listening) gets the diagram in one frame."""
    web = broker.web_client()
    ws = ws_connect(broker, cookie_of(web))
    try:
        ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {}}))
        recv_until(ws, lambda f: f.get("t") == "board" and f.get("full"))
        async with FakeAgent(broker.home, "drawer") as drawer:
            await drawer.join("#build", "claude-1")
            made = await drawer.call("board", room="#build", action="draw_mermaid", text=FLOW)
        delta = recv_until(ws, lambda f: f.get("t") == "board" and f.get("seq") == 1 and not f.get("full"))
        assert {e["id"] for e in delta["elements"]} == set(made["ids"]) and delta["actor"] == "claude-1"
    finally:
        ws.close()
        web.close()


async def test_a_converted_shape_can_be_moved_and_edited(broker: InProcBroker, clock: FakeClock):  # noqa: F811
    """What was drawn is ordinary board content: ``move`` and ``edit`` work on its ids."""
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        made = await drawer.call("board", room="#build", action="draw_mermaid", text=FLOW)
        shape = next(e for e in stored(broker) if e["type"] == "rectangle" and e["id"] in made["ids"])
        clock.advance(11)
        moved = await drawer.call("board", room="#build", action="move", id=shape["id"], x=5000, y=6000)
        assert moved["ok"] and moved["changed"], moved
        clock.advance(11)
        edited = await drawer.call("board", room="#build", action="edit", id=shape["id"], text="Renamed")
        assert edited["ok"] and edited["changed"], edited
    now = {e["id"]: e for e in stored(broker)}
    assert now[shape["id"]]["x"] == 5000 and now[shape["id"]]["version"] == 2
    label = next(e for e in now.values() if e.get("containerId") == shape["id"])
    assert label["text"] == "Renamed" and label["x"] > 4900


async def test_the_say_limit_and_membership_still_apply(broker: InProcBroker, clock: FakeClock):  # noqa: F811
    """A diagram shares the ten-second say limit and needs a member: after a post it is refused and
    stores nothing; a stranger is refused; once the window passes it draws."""
    async with FakeAgent(broker.home, "drawer") as drawer, FakeAgent(broker.home, "stranger") as stranger:
        await drawer.join("#build", "claude-1")
        refused = await stranger.call("board", room="#build", action="draw_mermaid", text=FLOW)
        assert refused["ok"] is False and refused["code"] == "not_member"
        await drawer.say("#build", "starting")
        limited = await drawer.call("board", room="#build", action="draw_mermaid", text=FLOW)
        assert limited["changed"] is False and limited["reason"] == "rate_limited"
        assert q(broker, "SELECT count(*) AS n FROM room_boards")[0]["n"] == 0
        clock.advance(11)
        ok = await drawer.call("board", room="#build", action="draw_mermaid", text=FLOW)
        assert ok["changed"] is True
        again = await drawer.call("board", room="#build", action="draw_mermaid", text=FLOW)
        assert again["reason"] == "rate_limited"
    assert q(broker, "SELECT seq FROM room_boards")[0]["seq"] == 1


async def test_a_bad_diagram_is_refused_with_the_line_and_stores_nothing(broker: InProcBroker):  # noqa: F811
    """An unreadable line names its number; one element over the cap says how many it would make;
    the guard's refusals stand. None of them costs the agent its rate-limit window or writes a row."""
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        bad = await drawer.call(
            "board", room="#build", action="draw_mermaid", text="flowchart TD\nA --> B\nA -> B"
        )
        assert bad["ok"] is False and bad["code"] == "bad_request" and "line 3:" in bad["error"]
        big = "flowchart LR\n" + "\n".join(f"n{i}[N{i}]" for i in range(76))
        over = await drawer.call("board", room="#build", action="draw_mermaid", text=big)
        assert over["ok"] is False and "would make 152 elements" in over["error"]
        styled = await drawer.call(
            "board", room="#build", action="draw_mermaid", text="flowchart TD\nstyle A fill:red"
        )
        assert styled["ok"] is False
        assert q(broker, "SELECT count(*) AS n FROM room_boards")[0]["n"] == 0
        fine = await drawer.call("board", room="#build", action="draw_mermaid", text="flowchart TD\nA --> B")
        assert fine["changed"] is True


async def test_a_150_element_diagram_is_stored_whole(broker: InProcBroker):  # noqa: F811
    """The largest diagram the cap allows goes through the merge and the byte limit in one change."""
    lines = ["flowchart TD", *(f"s{i}[S{i}] --> s{i + 1}" for i in range(49)), "s0 --> s49"]
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        made = await drawer.call("board", room="#build", action="draw_mermaid", text="\n".join(lines))
        assert made["changed"] is True and len(made["ids"]) == 150
    assert len(stored(broker)) == 150


async def test_arrange_is_one_change_over_the_tool(broker: InProcBroker, clock: FakeClock):  # noqa: F811
    """`arrange` on a frame moves its shapes, keeps the ids, and is one stored version and one notice."""
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        made = await drawer.call("board", room="#build", action="draw_mermaid", text=FLOW)
        frame = next(e["id"] for e in stored(broker) if e["type"] == "frame")
        before = {e["id"] for e in stored(broker)}
        clock.advance(11)
        done = await drawer.call("board", room="#build", action="arrange", id=frame)
        assert done["ok"] and done["changed"] and done["text"] == f"arranged {frame}", done
        clock.advance(11)
        refused = await drawer.call("board", room="#build", action="arrange", id=made["ids"][-1])
        assert refused["ok"] is False
    assert {e["id"] for e in stored(broker)} == before
    assert q(broker, "SELECT seq FROM room_boards")[0]["seq"] == 2
    assert len([r for r in q(broker, "SELECT text FROM messages") if "changed the board" in r["text"]]) == 2


@pytest.mark.parametrize("text", ["flowchart LR\nA-->B", "graph TB\nA & B --> C"])
async def test_no_pending_conversion_state_is_left_behind(broker: InProcBroker, text: str):  # noqa: F811
    """There is no browser round trip to leak, and the one conversion slot is given back."""
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        await drawer.call("board", room="#build", action="draw_mermaid", text=text)
    assert broker.on_loop(lambda: len(broker.state.room_board._pending)) == 0
    assert broker.on_loop(lambda: len(broker.state.room_board._converting)) == 0


class Gate:
    """Holds the first conversion on a worker thread until released: the loop must be free meanwhile."""

    def __init__(self, monkeypatch):
        from switchboard import board_flow

        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = 0
        real = board_flow.convert

        def convert(source):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                assert self.release.wait(10)
            return real(source)

        monkeypatch.setattr(board_flow, "convert", convert)


async def test_the_broker_answers_other_requests_while_a_diagram_is_being_converted(
    broker: InProcBroker,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    """Conversion runs off the event loop: a say and a read from other members are answered while one
    conversion is held, and a second drawing from the same agent is refused as busy. Fails if the work
    ran on the loop (the say would never return), or if one agent could queue many."""
    gate = Gate(monkeypatch)
    async with FakeAgent(broker.home, "drawer") as drawer, FakeAgent(broker.home, "other") as other:
        await drawer.join("#build", "claude-1")
        await other.join("#build", "codex-1")
        pending = asyncio.create_task(drawer.call("board", room="#build", action="draw_mermaid", text=FLOW))
        assert await asyncio.to_thread(gate.started.wait, 10)
        said = await asyncio.wait_for(other.say("#build", "still here"), 5)
        assert said["ok"]
        seen = await asyncio.wait_for(other.call("board", room="#build", action="read"), 5)
        assert seen["ok"] and seen["seq"] == 0
        again = await asyncio.wait_for(
            drawer.call("board", room="#build", action="draw_mermaid", text=FLOW), 5
        )
        assert again["ok"] is False and again["code"] == "busy"
        gate.release.set()
        made = await asyncio.wait_for(pending, 10)
        assert made["changed"] is True
    assert broker.on_loop(lambda: len(broker.state.room_board._converting)) == 0


async def test_membership_and_the_rate_limit_are_checked_again_after_the_conversion(
    broker: InProcBroker,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    """An agent that leaves, or posts, while its diagram is being made stores nothing."""
    for intervene in ("leave", "say"):
        gate = Gate(monkeypatch)
        async with FakeAgent(broker.home, f"drawer-{intervene}") as drawer:
            await drawer.join("#build", f"claude-{intervene}")
            pending = asyncio.create_task(
                drawer.call("board", room="#build", action="draw_mermaid", text=FLOW)
            )
            assert await asyncio.to_thread(gate.started.wait, 10)
            if intervene == "leave":
                await drawer.leave("#build")
            else:
                await drawer.say("#build", "a post while converting")
            gate.release.set()
            got = await asyncio.wait_for(pending, 10)
            assert not got.get("changed"), got
    assert q(broker, "SELECT count(*) AS n FROM room_boards")[0]["n"] == 0


async def test_placement_uses_the_board_as_it_is_after_the_conversion(
    broker: InProcBroker,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    """A drawing that landed while another was being converted is respected: the second is placed to
    the right of it inside the transaction, not on top of the board as it was when the work began."""
    gate = Gate(monkeypatch)
    async with FakeAgent(broker.home, "slow") as slow, FakeAgent(broker.home, "fast") as fast:
        await slow.join("#build", "claude-1")
        await fast.join("#build", "codex-1")
        pending = asyncio.create_task(slow.call("board", room="#build", action="draw_mermaid", text=FLOW))
        assert await asyncio.to_thread(gate.started.wait, 10)
        first = await fast.call("board", room="#build", action="draw_mermaid", text=FLOW)
        assert first["changed"] is True
        gate.release.set()
        second = await asyncio.wait_for(pending, 10)
        assert second["changed"] is True
    rows = {e["id"]: e for e in stored(broker)}
    mine = [rows[i] for i in first["ids"]]
    theirs = [rows[i] for i in second["ids"]]
    assert min(e["x"] for e in theirs) >= max(e["x"] + e["width"] for e in mine if e["type"] != "arrow")
    assert q(broker, "SELECT seq FROM room_boards")[0]["seq"] == 2
