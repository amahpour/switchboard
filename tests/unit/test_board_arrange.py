"""``arrange``: a frame's shapes laid out in layers by their arrows, the frame sized to fit, the arrows
rerouted as elbows, all as one change that keeps every id (DESIGN.md §56.9, #728)."""

from __future__ import annotations

from typing import Any

import pytest
from test_board_flow import box, overlap, segment_hits

from switchboard import board


def run(
    elements: list[dict[str, Any]], action: str, **params: Any
) -> tuple[list[dict[str, Any]], board.ActionResult]:
    out = board.apply_action(elements, action, params, now=1.0)
    assert not out.error, out.error
    cap = board.MAX_DIAGRAM if action == "arrange" else None
    merged = board.merge(elements, list(out.incoming), agent=True, cap=cap)
    assert not merged.rejected, merged.rejected
    return list(merged.elements), out


def make(shapes: int = 8, arrows: int = 10, labels: bool = True):
    els: list[dict[str, Any]] = []
    els, out = run(els, "frame", text="Plan", x=0, y=0)
    frame = out.added[0]
    ids = []
    for i in range(shapes):  # placed in a heap, as an agent without coordinates would
        els, out = run(els, "box", text=f"Step {i}", to=frame, x=20 + 10 * (i % 2), y=40 + 20 * i)
        ids.append(out.added[0])
    pairs = [(0, 1), (1, 2), (2, 3), (0, 4), (4, 5), (5, 3), (3, 6), (6, 7), (1, 6), (7, 0)][:arrows]
    for n, (a, b) in enumerate(pairs):
        els, _ = run(els, "connect", id=ids[a], to=ids[b], text=f"go {n}" if labels else "")
    return els, frame, ids


def live(els, *types):
    return [e for e in els if e["type"] in types and not e.get("isDeleted")]


def test_arrange_gives_a_frame_with_no_overlaps_no_stray_shapes_and_no_arrow_through_a_shape():
    """An 8-shape, 10-arrow frame comes out laid out: no overlapping shapes, every shape inside the
    resized frame, every arrow orthogonal and clear of the shapes it does not join, labels apart."""
    els, frame, ids = make()
    before = {e["id"] for e in els}
    els, out = run(els, "arrange", id=frame)
    assert {e["id"] for e in els} == before and out.added == (frame,)  # moves and reroutes; ids stay
    shapes = live(els, "rectangle")
    f = next(e for e in els if e["id"] == frame)
    for i, a in enumerate(shapes):
        assert box(a)[0] >= f["x"] and box(a)[1] >= f["y"]
        assert a["x"] + a["width"] <= f["x"] + f["width"] and a["y"] + a["height"] <= f["y"] + f["height"]
        for b in shapes[i + 1 :]:
            assert not overlap(box(a), box(b))
    for a in live(els, "arrow"):
        assert a["elbowed"] is True and "fixedPoint" in a["startBinding"] and "fixedPoint" in a["endBinding"]
        ends = {a["startBinding"]["elementId"], a["endBinding"]["elementId"]}
        pts = [(a["x"] + px, a["y"] + py) for px, py in a["points"]]
        for p, q in zip(pts, pts[1:], strict=False):
            assert abs(p[0] - q[0]) < 1e-6 or abs(p[1] - q[1]) < 1e-6
            for s in shapes:
                assert s["id"] in ends or not segment_hits(p, q, box(s))
    labels = [e for e in live(els, "text") if e.get("containerId") and e["containerId"].startswith("a")]
    assert len(labels) == 10
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            assert not overlap(box(a), box(b))
    for s in shapes:  # a bound label moved with its shape
        t = next(e for e in els if e.get("containerId") == s["id"])
        assert s["x"] <= t["x"] and t["x"] + t["width"] <= s["x"] + s["width"]
        assert s["y"] <= t["y"] and t["y"] + t["height"] <= s["y"] + s["height"]


def test_arrange_is_one_change_through_the_broker_with_one_notice(tmp_path):
    """Merged as one action the arrange is a single version bump per element, under the per-call cap of 40
    (an 8-shape, 10-arrow frame is 37 elements)."""
    els, frame, ids = make(shapes=8, arrows=10)
    out = board.apply_action(els, "arrange", {"id": frame}, now=1.0)
    assert board.MAX_CALL // 2 < len(out.incoming) <= board.MAX_CALL
    merged = board.merge(els, list(out.incoming), agent=True)
    assert len(merged.taken) == len(out.incoming) and not merged.rejected
    by_old = {e["id"]: e for e in els}
    assert all(e["version"] == by_old[e["id"]]["version"] + 1 for e in merged.taken)


def test_shapes_outside_the_frame_and_arrows_that_leave_it_keep_their_places_and_bindings():
    """Only the frame's own shapes move. An outside shape stays exactly where it was and an arrow from
    inside to it stays bound to both ends, rerouted to the shape's new place."""
    els, frame, ids = make(shapes=4, arrows=3, labels=False)
    els, out = run(els, "box", text="Outside", x=1500, y=900)
    outside = out.added[0]
    els, out = run(els, "connect", id=ids[1], to=outside, text="")
    leaving = out.added[0]
    old_outside = next(e for e in els if e["id"] == outside)
    els, _ = run(els, "arrange", id=frame)
    got = next(e for e in els if e["id"] == outside)
    assert (got["x"], got["y"], got["version"]) == (
        old_outside["x"],
        old_outside["y"],
        old_outside["version"],
    )
    arrow = next(e for e in els if e["id"] == leaving)
    assert arrow["startBinding"]["elementId"] == ids[1] and arrow["endBinding"]["elementId"] == outside
    assert arrow["elbowed"] is True
    end = (arrow["x"] + arrow["points"][-1][0], arrow["y"] + arrow["points"][-1][1])
    assert (
        abs(end[0] - got["x"]) < 1
        or abs(end[1] - got["y"]) < 1
        or got["x"] < end[0] < got["x"] + got["width"]
    )


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"id": "nope"}, "unknown id"),
        ({}, "unknown id"),
        ({"id": "SHAPE"}, "arrange takes a frame id"),
        ({"id": "EMPTY"}, "that frame has no shapes"),
    ],
)
def test_arrange_refuses_what_is_not_a_frame_with_shapes_and_changes_nothing(params, reason):
    """An id that is not a frame, an unknown id and an empty frame are refused with no elements."""
    els, frame, ids = make(shapes=2, arrows=1)
    els, out = run(els, "frame", text="Empty", x=2000, y=0)
    empty = out.added[0]
    params = {k: {"SHAPE": ids[0], "EMPTY": empty}.get(v, v) for k, v in params.items()}
    got = board.apply_action(els, "arrange", params, now=1.0)
    assert got.error == reason and got.incoming == ()


def big_frame(arrows: int):
    """A frame of 35 shapes (70 elements), the frame (1) and ``arrows`` unlabelled arrows."""
    els, frame, ids = make(shapes=35, arrows=0)
    for n in range(arrows):
        els, _ = run(
            els,
            "connect",
            id=ids[n % 35],
            to=ids[(n * 7 + 3) % 35 if (n * 7 + 3) % 35 != n % 35 else (n + 1) % 35],
            text="",
        )
    return els, frame


def test_arrange_may_change_150_elements_and_refuses_151():
    """The maintainer allowed arrange the diagram cap: 150 changed elements arrange (merged under 150), 151
    are refused with the count and nothing is stored."""
    els, frame = big_frame(79)
    out = board.apply_action(els, "arrange", {"id": frame}, now=1.0)
    assert not out.error and len(out.incoming) == 150, out.error
    merged = board.merge(els, list(out.incoming), agent=True, cap=board.MAX_DIAGRAM)
    assert len(merged.taken) == 150 and not merged.rejected
    els, frame = big_frame(80)
    got = board.apply_action(els, "arrange", {"id": frame}, now=1.0)
    assert got.incoming == () and "would change 151 elements" in got.error and "150 at most" in got.error


def test_arrange_refuses_a_shape_with_more_than_40_bound_elements_naming_it():
    """A hub with a label and 40 arrows carries 41 bound elements, more than a shape holds."""
    els, frame, ids = make(shapes=3, arrows=0)
    for i in range(40):
        els, _ = run(els, "connect", id=ids[0], to=ids[1 + i % 2], text="")
        if i % 3 == 2:  # stay clear of the rate window the real tool has: only the geometry is under test
            pass
    got = board.apply_action(els, "arrange", {"id": frame}, now=1.0)
    assert got.incoming == () and '"Step 0"' in got.error and "bound elements" in got.error
