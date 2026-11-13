"""Mermaid ``style``, ``classDef``, ``class`` and ``:::`` colour a drawn node (DESIGN.md §56.9, #726).

Only ``fill`` and ``stroke`` are read, and each value is a palette name or a hex mapped to the nearest
palette colour (``board_style.mermaid_color``), so a converted diagram uses the colours the style panel
shows. Everything else is refused with the line number rather than dropped: fonts, widths, dashes,
``url(``, ``linkStyle``. Each test fails if the form is still refused, mis-mapped, or a hostile value
gets through."""

from __future__ import annotations

from typing import Any

import pytest

from switchboard import board, board_flow, board_style


def draw(source: str) -> list[dict[str, Any]]:
    return board.mermaid_elements([], board_flow.convert(source).elements, now=10)


def node(els: list[dict[str, Any]], text: str) -> dict[str, Any]:
    for s in els:
        if s["type"] in ("rectangle", "ellipse", "diamond"):
            label = next(e for e in els if e.get("containerId") == s["id"])
            if label["originalText"] == text:
                return s
    raise AssertionError(text)


FILL = board_style.color_hex
RED_FILL, RED_STROKE = FILL("red", "fill"), FILL("red", "stroke")


def test_a_style_line_sets_fill_and_stroke_by_palette_name() -> None:
    els = draw("flowchart TD\nA[One] --> B[Two]\nstyle A fill:red,stroke:blue\nstyle B fill:green")
    a, b = node(els, "One"), node(els, "Two")
    assert (a["backgroundColor"], a["strokeColor"]) == (RED_FILL, FILL("blue", "stroke"))
    assert b["backgroundColor"] == FILL("green", "fill") and b["strokeColor"] == "#1e1e1e"


def test_a_style_line_may_name_several_nodes_and_come_before_them() -> None:
    els = draw("flowchart TD\nstyle A,B fill:yellow\nA[One] --> B[Two] --> C[Three]")
    assert (
        node(els, "One")["backgroundColor"] == node(els, "Two")["backgroundColor"] == FILL("yellow", "fill")
    )
    assert node(els, "Three")["backgroundColor"] == "#ffffff"  # unstyled stays as it was


def test_a_hex_colour_is_mapped_to_the_nearest_palette_colour() -> None:
    """Mermaid diagrams are written with hex. #ff0000 as a fill is the palette's light red, as a stroke
    its dark red; #f9f (a Mermaid docs favourite) and a six-digit hex both work."""
    els = draw("flowchart TD\nA[One] --> B[Two]\nstyle A fill:#ff0000,stroke:#ff0000\nstyle B fill:#ffcccc;")
    a, b = node(els, "One"), node(els, "Two")
    assert (a["backgroundColor"], a["strokeColor"]) == (RED_FILL, RED_STROKE)
    assert b["backgroundColor"] == RED_FILL  # a trailing `;` ends the statement, it is not an entity
    short = draw("flowchart TD\nA[One]\nstyle A fill:#f9f")
    assert node(short, "One")["backgroundColor"] in {FILL(n, "fill") for n in board_style.PALETTE}
    assert board_style.mermaid_color("#fff", "fill") == "#ffffff"
    assert board_style.mermaid_color("#000", "stroke") == "#1e1e1e"


def test_classdef_class_and_the_triple_colon_suffix_colour_the_nodes_that_use_them() -> None:
    els = draw(
        "flowchart LR\nclassDef risk fill:red,stroke:red\nclassDef ok fill:green\n"
        "A[One]:::risk --> B[Two] --> C[Three]\nclass B,C ok"
    )
    assert node(els, "One")["backgroundColor"] == RED_FILL
    assert node(els, "One")["strokeColor"] == RED_STROKE
    assert (
        node(els, "Two")["backgroundColor"] == node(els, "Three")["backgroundColor"] == FILL("green", "fill")
    )


def test_a_class_defined_after_its_use_and_for_several_names_applies() -> None:
    els = draw("flowchart TD\nA[One]:::a --> B[Two]:::b\nclassDef a,b fill:violet")
    assert (
        node(els, "One")["backgroundColor"] == node(els, "Two")["backgroundColor"] == FILL("violet", "fill")
    )


def test_a_nodes_own_style_wins_over_its_class_and_default_is_the_base() -> None:
    els = draw(
        "flowchart TD\nclassDef default fill:gray\nclassDef hot fill:red\n"
        "A[One]:::hot --> B[Two] --> C[Three]\n"
        "style A fill:blue\nstyle C stroke:red"
    )
    assert node(els, "One")["backgroundColor"] == FILL("blue", "fill")
    assert node(els, "Two")["backgroundColor"] == FILL("gray", "fill")  # the default class
    assert node(els, "Three")["strokeColor"] == RED_STROKE


def test_converted_shapes_keep_the_fills_through_the_merge_and_the_summary_names_them() -> None:
    els = draw("flowchart TD\nA[One] --> B[Two]\nstyle A fill:red")
    merged = board.merge([], els, agent=True, cap=board.MAX_DIAGRAM).elements
    one = next(e for e in merged if e["type"] == "rectangle" and e["backgroundColor"] == RED_FILL)
    assert one["strokeColor"] == "#1e1e1e"
    assert "fill=red" in board.summary(list(merged), 1)


@pytest.mark.parametrize(
    "line",
    [
        "style A stroke-width:4px",
        "style A fill:red,font-size:20px",
        "style A color:#fff",
        "style A stroke-dasharray:5 5",
        "style A fill:url(#x)",
        "style A fill:url(https://example.com/x)",
        "style A fill:rgb(255,0,0)",
        "style A fill:none",
        "style A fill:crimson",
        "style A fill:#ff00",
        "style A fill:#ggg",
        "style A fill:red;stroke-width:9px",
        "style A fill:expression(alert(1))",
        "style A fill",
        "style A fill:",
        "style A",
        "classDef x stroke-width:3px",
        "classDef x fill:javascript:alert(1)",
        "class A",
    ],
)
def test_everything_but_fill_and_stroke_by_name_or_hex_is_refused_with_its_line(line: str) -> None:
    with pytest.raises(board.Reject) as caught:
        board_flow.convert(f"flowchart TD\nA[One] --> B[Two]\n{line}")
    assert "line 3:" in caught.value.reason, caught.value.reason


def test_linkstyle_a_subgraph_and_an_unknown_node_are_refused() -> None:
    with pytest.raises(board.Reject):
        board.mermaid_source("flowchart TD\nA-->B\nlinkStyle 0 stroke:red")
    with pytest.raises(board.Reject) as caught:
        board_flow.convert("flowchart TD\nsubgraph s [Group]\nA[One]\nend\nstyle s fill:red")
    assert "line 5:" in caught.value.reason and "frame" in caught.value.reason
    with pytest.raises(board.Reject) as caught:
        board_flow.convert("flowchart TD\nA[One]\nstyle Z fill:red")
    assert "line 3:" in caught.value.reason and "not a node" in caught.value.reason


def test_hostile_values_never_reach_an_element() -> None:
    """A colour is only ever one of the palette's hex strings, whatever was written."""
    hostile = ["red", "#FF0000", "#abc", "BLUE", "Red", "teal ", "‮red", "rеd", "#ａｂｃ"]
    palette = {v for pair in board_style.PALETTE.values() for v in pair} | {
        "#1e1e1e",
        "#ffffff",
        "transparent",
    }
    for value in hostile:
        try:
            els = draw(f"flowchart TD\nA[One]\nstyle A fill:{value},stroke:{value}")
        except board.Reject:
            continue
        shape = node(els, "One")
        assert shape["backgroundColor"] in palette and shape["strokeColor"] in palette, value


def test_the_skills_mermaid_recipe_converts_and_is_clean() -> None:
    """The example in ``reference.md`` is what agents will copy: it must be accepted, coloured and free
    of layout warnings."""
    import re
    from pathlib import Path

    from switchboard import board_checks

    text = (Path(board.__file__).parents[2] / "plugins/claude/skills/board/reference.md").read_text()
    source = re.search(r"```\n(flowchart LR.*?)```", text, re.S).group(1)
    els = draw(source)
    assert node(els, "Spec")["backgroundColor"] == FILL("blue", "fill")
    assert node(els, "Risk: rework")["strokeColor"] == RED_STROKE
    assert board_checks.warnings(els) == []


@pytest.mark.parametrize("indent", [0, 4, 10, 12, 40])
def test_an_indented_style_line_ending_in_a_semicolon_is_read_whatever_the_indent(indent: int) -> None:
    """`#f9f;` ends a statement in a style line, however far it is indented: the check looks at the
    statement's first word, not at a fixed number of leading characters."""
    pad = " " * indent
    els = draw(
        f"flowchart TD\nA[One] --> B[Two]\n{pad}style A fill:#f9f;\n{pad}classDef k fill:#ffcccc;\nB:::k"
    )
    assert node(els, "One")["backgroundColor"] in {FILL(n, "fill") for n in board_style.PALETTE}
    assert node(els, "Two")["backgroundColor"] == RED_FILL
