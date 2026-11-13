"""#728's Done-when, end to end: a diagram that comes out of the tools is clean (§56.5, §56.9).

A dependency map of about 20 parts drawn by ``draw_mermaid``, and 20 shapes with 25 arrows drawn by the
shape actions and then ``arrange``d frame by frame, must give the agent no layout warning in any result
and none in ``read``. The checks and the layouts are written separately (``board_checks`` sizes text the
way ``board_flow`` does, and reads the elbow routes ``board_layout`` stores), so this fails if they ever
disagree: a label the converter sized to fit that the checks call overflowing, or a route the router drew
round a shape that the checks call a crossing."""

from __future__ import annotations

from typing import Any

from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from test_board_tool import broker, q  # noqa: F401

from switchboard import board, board_checks


def dependency_map() -> str:
    """Four subgraphs of five modules, 25 edges between them and inside them, some labelled."""
    groups = {"core": "abcde", "web": "fghij", "agents": "klmno", "store": "pqrst"}
    lines = ["flowchart LR"]
    for name, ids in groups.items():
        lines.append(f"subgraph {name} [{name.title()} package]")
        lines += [f"{i}[{name} module {i.upper()}]" for i in ids]
        lines.append("end")
    edges = (
        "a-->b a-->c b-->d c-->d d-->e f-->g f-->h g-->i h-->i i-->j k-->l k-->m l-->n m-->n n-->o "
        "p-->q p-->r q-->s r-->s s-->t a-->f f-->k k-->p e-->j o-->t"
    ).split()
    for n, e in enumerate(edges):
        a, b = e.split("-->")
        lines.append(f"{a} -->|uses| {b}" if n % 4 == 0 else f"{a} --> {b}")
    return "\n".join(lines)


async def test_a_twenty_node_dependency_map_has_no_warnings_in_its_result_or_in_read(
    broker: InProcBroker,  # noqa: F811
) -> None:
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        made = await drawer.call("board", room="#build", action="draw_mermaid", text=dependency_map())
        assert made["ok"] and made["changed"] is True, made
        assert "layout warnings" not in made["text"], made["text"]
        seen = await drawer.call("board", room="#build", action="read")
        assert "layout warnings" not in seen["text"], seen["text"]
        assert "20 shapes" in seen["text"] and "25 arrows" in seen["text"], seen["text"][:200]


def test_twenty_shapes_and_25_arrows_arranged_by_frame_give_no_warnings_in_any_result(
    broker: InProcBroker,  # noqa: F811
) -> None:
    """Draw into two frames with the shape actions and no coordinates, join them with a frame-to-frame
    arrow, ``arrange`` each frame, then ``read``: no result along the way names a problem."""
    b = broker
    room = b.state.store.get_room("#build")
    results: list[str] = []

    def do(action: str, **params: Any) -> dict[str, Any]:
        got = b.on_loop(lambda: b.state.room_board.agent(room, "drawer", {"action": action, **params}, None))
        results.append(got["text"])
        return got

    # An arranged frame grows to fit its shapes, so each gets a region of its own (`arrange` does not
    # move other frames out of the way, and the skill says to leave room).
    frames = [do("frame", text=f"Part {n}", x=n * 6000, y=0)["ids"][0] for n in (0, 1)]
    shapes: list[list[str]] = [[], []]
    for n, frame in enumerate(frames):
        for i in range(10):
            shapes[n].append(do("box", text=f"Module {n}{i}", to=frame)["ids"][0])
    arrows = 0
    for n in (0, 1):
        s = shapes[n]
        pairs = [
            (0, 1),
            (1, 2),
            (2, 3),
            (3, 4),
            (4, 5),
            (5, 6),
            (6, 7),
            (7, 8),
            (8, 9),
            (0, 5),
            (2, 7),
            (4, 9),
        ]
        for a, c in pairs[:12]:
            do("connect", id=s[a], to=s[c], text="uses" if a % 3 == 0 else "", style="arrow-type=elbow")
            arrows += 1
    do("connect", id=frames[0], to=frames[1], text="feeds", style="arrow-type=elbow")
    arrows += 1
    assert arrows == 25
    for frame in frames:
        do("arrange", id=frame)
    seen = b.on_loop(lambda: b.state.room_board.agent(room, "drawer", {"action": "read"}, None))["text"]
    assert "20 shapes" in seen and "25 arrows" in seen and "2 frames" in seen, seen[:200]
    assert [r for r in results if "layout warnings" in r] == [], [
        r for r in results if "layout warnings" in r
    ]
    assert "layout warnings" not in seen, seen


def test_the_route_check_reads_the_stored_elbow_points_of_the_router() -> None:
    """A routed arrow carries several points and a ``fixedPoint`` on each end. The check follows those
    points: the arrow goes round the blocker and is clean; replace its points with the straight line
    and the same arrow is reported crossing it."""
    elements: list[dict[str, Any]] = []
    new = iter(range(1, 999))

    def act(action: str, **params: Any) -> list[str]:
        nonlocal elements
        out = board.apply_action(
            elements, action, params, now=1.0, new_id=lambda p: f"{p}{next(new)}", new_nonce=lambda: 1
        )
        assert not out.error, out.error
        elements = list(board.merge(elements, list(out.incoming), agent=True).elements)
        return list(out.added)

    one = act("box", text="one", x=0, y=0)[0]
    blocker = act("box", text="blocker", x=180, y=120)[0]
    two = act("box", text="two", x=600, y=300)[0]
    arrow = act("connect", id=one, to=two, style="arrow-type=elbow")[0]
    el = next(e for e in elements if e["id"] == arrow)
    assert len(el["points"]) >= 3 and el["startBinding"]["fixedPoint"] and el["endBinding"]["fixedPoint"]
    assert board_checks.warnings(elements) == []
    start = el["points"][0]
    end = el["points"][-1]
    straight = [{**e, "points": [start, end]} if e["id"] == arrow else e for e in elements]
    assert f"arrow {arrow} passes through {blocker}" in board_checks.warnings(straight)
