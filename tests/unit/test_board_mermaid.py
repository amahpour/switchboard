"""Mermaid input is a bounded claim and a conversion never edits existing shapes (§56.9)."""

from __future__ import annotations

import pytest

from switchboard import board


def shape(**over):
    return {
        "id": "a",
        "type": "rectangle",
        "x": 0,
        "y": 0,
        "width": 100,
        "height": 50,
        "version": 1,
        "versionNonce": 1,
        **over,
    }


def test_mermaid_source_rejects_directives_oversize_and_non_flow_diagrams_and_removes_clicks():
    """No diagram-supplied configuration or links reach the renderer."""
    assert (
        board.mermaid_source('flowchart LR\nA-->B\nclick A href "https://example.com"')
        == "flowchart LR\nA-->B"
    )
    for source in (
        "x" * 8001,
        "%%{init: {}}%%\nflowchart LR\nA-->B",
        "sequenceDiagram\nA->>B: hi",
        "flowchart LR;click A callback",
    ):
        with pytest.raises(board.Reject):
            board.mermaid_source(source)


def test_converted_shapes_are_reminted_clear_of_the_live_board_and_links_are_removed():
    """A compromised converter cannot overwrite a person’s shape or bind to it."""
    existing = [shape(x=1000, y=100)]
    raw = [
        shape(link="https://example.com"),
        shape(id="b", type="text", text="<script>hi</script>", containerId="a"),
    ]
    placed = board.mermaid_elements(existing, raw, now=10)
    assert len(placed) == 2 and {e["id"] for e in placed}.isdisjoint({"a", "b"})
    assert min(e["x"] for e in placed) >= 1180
    assert all(e["link"] is None and e["version"] == 1 for e in placed)
    assert placed[1]["containerId"] == placed[0]["id"]
    assert placed[1]["text"] == "<script>hi</script>"
    assert existing == [shape(x=1000, y=100)]


def test_a_conversion_is_rejected_whole_if_it_has_bad_shapes_or_no_free_space():
    """The diagram cap (150) and the type allowlist apply before any change is stored."""
    for raw in (
        [],
        [shape()] * (board.MAX_DIAGRAM + 1),
        [shape(), shape(id="b", type="image")],
        [shape(), shape()],
        [shape(frameId="outside")],
    ):
        with pytest.raises(board.Reject):
            board.mermaid_elements([], raw, now=10)
    with pytest.raises(board.Reject):
        board.mermaid_elements(
            [shape(x=20000, width=10000, y=20000, height=10000)], [shape(width=10000, height=10000)], now=10
        )


def test_free_space_counts_rotated_shapes_and_points_outside_a_strokes_declared_box():
    """A tall rotated box and a long point list must not hide their actual right edge from placement."""
    import math

    current = [shape(x=100, y=100, width=10, height=1000, angle=math.pi / 2)]
    assert board.mermaid_elements(current, [shape()], now=1)[0]["x"] >= 685
    current = [shape(type="freedraw", width=1, height=1, points=[[0, 0], [2000, 10]])]
    assert board.mermaid_elements(current, [shape()], now=1)[0]["x"] >= 2080


def test_the_guard_returns_the_text_as_written_and_normalizes_only_line_breaks():
    """The converter reads what the guard returns, so `A & B` and `#quot;` keep their meaning (no
    fullwidth stand-ins), a CRLF becomes a newline, and the rejections are the ones above."""
    assert board.mermaid_source("flowchart LR\r\nA & B --> C #quot;x#quot;") == (
        "flowchart LR\nA & B --> C #quot;x#quot;"
    )
    with pytest.raises(board.Reject):
        board.mermaid_source("flowchart LR\nstyle A fill:red")


def test_a_bound_list_longer_than_a_shape_call_may_make_is_still_dropped():
    """The element guard is unchanged: 40 bound elements are kept and 41 are dropped, for everyone."""

    def kept(n):
        hub = shape(boundElements=[{"id": f"a{i}", "type": "arrow"} for i in range(n)])
        return board.clean_element(hub, agent=True).get("boundElements")

    assert len(kept(40)) == 40 and kept(41) is None


def test_an_elbow_arrows_binding_keeps_its_fixed_point_only_when_it_is_two_numbers_from_0_to_1():
    """An elbow arrow needs the fraction of the box it leaves from; anything else in that field (a
    string, a long list, a number outside 0 to 1, NaN, a boolean) is dropped and the binding stays plain.
    `board-rules.js` has the same rule (test_board_browser.py compares them)."""

    def bound(fixed):
        arrow = shape(
            type="arrow", points=[[0, 0], [10, 0]], startBinding={"elementId": "b", "fixedPoint": fixed}
        )
        return board.clean_element(arrow, agent=True)["startBinding"]

    assert bound([0.5, 1]) == {"elementId": "b", "focus": 0, "gap": 0, "fixedPoint": [0.5, 1]}
    for bad in ("x", [0.5], [0, 1, 2], [-0.1, 0.5], [0.5, 1.1], [float("nan"), 0], [True, 0], {"a": 1}):
        assert "fixedPoint" not in bound(bad), bad
