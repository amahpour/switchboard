"""The board's colour palette and how `read` reports it (DESIGN.md §56, #726).

Colours are Excalidraw's own names, so what an agent sets and what a person's picker shows agree.
Raw hex and unknown words are refused (fail closed), and the summary names a colour that isn't
the default, using the nearest palette name for one a person picked by hand.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from switchboard import board, board_style

NAMES = ("red", "pink", "grape", "violet", "blue", "cyan", "teal", "green", "yellow", "orange", "gray")
BUNDLE = Path(board.__file__).parent / "web/static/vendor/board/chunks/chunk-42LTX363.js"


def shape(**kw: Any) -> dict[str, Any]:
    base = {"id": "n1", "type": "rectangle", "x": 0, "y": 0, "width": 80, "height": 40}
    return {**base, **kw}


def test_every_palette_name_is_the_pinned_excalidraw_colour() -> None:
    """The values come from the vendored build's colour table (fill: shade 2, stroke: shade 8), not
    from memory: a drifted hex would make an agent's colour differ from the picker's."""
    table = re.search(r"Jt=\{white:.*?\};", BUNDLE.read_text()).group(0)
    assert set(board_style.PALETTE) == set(NAMES)
    for name in NAMES:
        shades = re.search(name + r":\[(.*?)\]", table).group(1).replace('"', "").split(",")
        assert board_style.color_hex(name, "fill") == shades[2]
        assert board_style.color_hex(name, "stroke") == shades[8]
    assert board_style.color_hex("yellow", "fill") == "#ffec99"  # the note's long-standing fill
    assert board_style.color_hex("black", "stroke") == board_style.DEFAULT_STROKE
    assert board_style.color_hex("white", "fill") == "#ffffff"
    assert board_style.color_hex("transparent", "fill") == "transparent"


@pytest.mark.parametrize("bad", ["#ff0000", "ff0000", "Red", "RED", "purple", "", " red", 5, None, ["red"]])
def test_raw_hex_unknown_names_and_non_strings_are_refused_with_the_list(bad: Any) -> None:
    with pytest.raises(board_style.StyleError) as e:
        board_style.color_hex(bad, "fill")
    assert all(n in e.value.reason for n in (*NAMES, "black", "white", "transparent"))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("role", ["fill", "stroke"])
def test_a_palette_colour_maps_back_to_its_own_name(name: str, role: str) -> None:
    assert board_style.nearest_name(board_style.color_hex(name, role), role) == name


def test_a_hand_picked_colour_gets_the_nearest_name() -> None:
    assert board_style.nearest_name("#ff0000", "stroke") == "red"
    assert board_style.nearest_name("#a0d0ff", "fill") == "blue"
    assert board_style.nearest_name("#fff", "fill") == "white"
    assert board_style.nearest_name("transparent", "fill") == "transparent"
    assert board_style.nearest_name("not a colour", "fill") == "transparent"


def test_the_summary_names_non_default_colours_only() -> None:
    """A plain box adds nothing (the format is unchanged); a coloured box and arrow say so."""
    plain = shape()
    white = shape(id="n2", backgroundColor="#ffffff", strokeColor=board_style.DEFAULT_STROKE)
    red = shape(id="n3", backgroundColor=board_style.color_hex("red", "fill"), strokeColor="#ff0001")
    arrow = {"id": "a1", "type": "arrow", "x": 0, "y": 0, "width": 10, "height": 0, "strokeColor": "#1971c2"}
    lines = board.summary([plain, white, red, arrow], 1).splitlines()
    assert lines[1] == 'rectangle id=n1 "" at (0,0) 80x40'
    assert lines[2] == 'rectangle id=n2 "" at (0,0) 80x40'
    assert lines[3].endswith("80x40 fill=red stroke=red")
    assert lines[4] == 'arrow id=a1 (free) -> (free) "" stroke=blue'
