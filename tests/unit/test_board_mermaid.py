"""Mermaid input and browser conversion are bounded claims, never edits to existing shapes (§56.9)."""

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
    """The agent's 40-element and type caps apply before any change is stored."""
    for raw in (
        [],
        [shape()] * 41,
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
