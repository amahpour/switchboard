"""The drawing controls an agent has: ``style``, ``w`` and ``h``, ``resize``, ``text`` (DESIGN.md §56.3).

Style is Excalidraw's own words and fails closed. A shape sizes itself to its words and a frame to
what is put in it. The layout checks run from geometry, and a write's result names the problems of
what it touched. Each test would fail if the control were missing, loose or silent."""

from __future__ import annotations

from typing import Any

import pytest

from switchboard import board, board_layout, board_style


def ids() -> Any:
    n: dict[str, int] = {}

    def new(prefix: str) -> str:
        n[prefix] = n.get(prefix, 0) + 1
        return f"{prefix}{n[prefix]}"

    return new


class World:
    """Elements as the broker would hold them, after each action is merged."""

    def __init__(self) -> None:
        self.elements: list[dict[str, Any]] = []
        self.new = ids()
        self.nonce = 100

    def do(self, action: str, **params: Any) -> board.ActionResult:
        def nonce() -> int:
            self.nonce += 1
            return self.nonce

        got = board.apply_action(self.elements, action, params, now=10, new_id=self.new, new_nonce=nonce)
        if not got.error:
            merged = board.merge(self.elements, list(got.incoming), agent=True)
            assert not merged.rejected, merged.rejected
            self.elements = list(merged.elements)
        return got

    def get(self, eid: str) -> dict[str, Any]:
        return next(e for e in self.elements if e["id"] == eid)

    def label(self, eid: str) -> dict[str, Any]:
        return next(e for e in self.elements if e.get("containerId") == eid)


def test_every_style_key_and_value_maps_to_what_excalidraws_panel_stores() -> None:
    """The words are the style panel's, so a person sees what the agent set."""
    el, label = board_style.fields(
        board_style.parse_style(
            "stroke=red background=blue fill=hachure stroke-width=extra-bold stroke-style=dotted "
            "sloppiness=cartoonist edges=sharp opacity=40 font-size=xl align=right"
        ),
        "shape",
    )
    assert el == {
        "strokeColor": "#e03131",
        "backgroundColor": "#a5d8ff",
        "fillStyle": "hachure",
        "strokeWidth": 4,
        "strokeStyle": "dotted",
        "roughness": 2,
        "roundness": None,
        "opacity": 40,
    }
    assert label == {"fontSize": 36, "textAlign": "right"}
    arrow, _ = board_style.fields(
        board_style.parse_style("arrow-type=elbow start=dot end=none stroke-width=thin"), "arrow"
    )
    assert arrow == {
        "elbowed": True,
        "roundness": None,
        "startArrowhead": "dot",
        "endArrowhead": None,
        "strokeWidth": 1,
    }
    curved, _ = board_style.fields({"arrow-type": "curved"}, "arrow")
    assert curved == {"elbowed": False, "roundness": {"type": 2}}


@pytest.mark.parametrize(
    "bad",
    [
        "stroke=#ff0000",  # raw hex
        "stroke=Red",  # wrong case
        "stroke=indigo",  # not in the picker's names
        "fill=striped",
        "opacity=5",
        "opacity=101",
        "opacity=1e2",
        "opacity=²",  # str.isdigit() is True for it and int() then raises
        "opacity=١٠٠",  # Arabic-Indic digits are not 100
        "opacity=\n50",
        "font-size=huge",
        "colour=red",  # unknown key
        "stroke",  # no value
        "stroke=red stroke=blue",  # twice
        "stroke=red " * 13,  # too many
        "x" * 400,
    ],
)
def test_a_bad_style_is_refused_with_the_accepted_list(bad: str) -> None:
    with pytest.raises(board_style.StyleError) as e:
        board_style.fields(board_style.parse_style(bad), "shape")
    assert e.value.reason


def test_style_keys_that_do_not_fit_the_target_are_refused() -> None:
    """A frame is drawn in Excalidraw's fixed frame style whatever it stores, so it takes none; an
    arrow has no background; a loose text has no edges."""
    for target, key in (("frame", "stroke=red"), ("arrow", "background=red"), ("text", "edges=round")):
        with pytest.raises(board_style.StyleError):
            board_style.fields(board_style.parse_style(key), target)
    with pytest.raises(board_style.StyleError):
        board_style.parse_style(5)


def test_a_bad_style_through_an_action_stores_nothing() -> None:
    w = World()
    for text in ("stroke=#fff", "nonsense=1", "stroke=red stroke=red"):
        got = w.do("box", text="a", style=text)
        assert got.error and not got.incoming
    assert w.elements == []
    assert w.do("frame", text="f", style="stroke=red").error


def test_box_note_and_connect_store_the_style() -> None:
    w = World()
    w.do(
        "box", text="gate", style="stroke=red background=yellow fill=solid edges=sharp font-size=l", x=0, y=0
    )
    box = w.get("b1")
    assert box["strokeColor"] == "#e03131" and box["backgroundColor"] == "#ffec99"
    assert "roundness" not in box  # sharp
    assert w.label("b1")["fontSize"] == 28
    w.do("note", text="n", style="background=green", x=400, y=0)
    assert w.get("n1")["backgroundColor"] == "#b2f2bb"
    w.do("connect", id="b1", to="n1", text="go", style="arrow-type=elbow stroke=blue stroke-style=dashed")
    arrow = w.get("a1")
    assert arrow["elbowed"] is True and arrow["strokeColor"] == "#1971c2" and arrow["strokeStyle"] == "dashed"
    plain = World()
    plain.do("note", text="n")
    plain.do("box", text="b")
    assert plain.get("n1")["backgroundColor"] == "#ffec99" and plain.get("b1")["backgroundColor"] == "#ffffff"
    assert plain.get("b1")["strokeColor"] == "#1e1e1e"  # defaults are unchanged without a style


def test_a_box_grows_in_height_to_fit_its_wrapped_label() -> None:
    w = World()
    w.do("box", text="short", x=0, y=0)
    w.do("box", text="a label that is much longer than a box this wide can hold on one line " * 2, x=400, y=0)
    assert w.get("b1")["height"] == 80
    tall = w.get("b2")
    assert tall["height"] > 80
    label = w.label("b2")
    assert label["height"] <= tall["height"] - 20
    assert abs(label["y"] + label["height"] / 2 - tall["y"] - tall["height"] / 2) < 1
    assert board_layout.warnings(w.elements) == []


def test_w_and_h_size_a_shape_and_bad_sizes_are_refused() -> None:
    w = World()
    w.do("box", text="wide", w=400, h=200, x=0, y=0)
    assert (w.get("b1")["width"], w.get("b1")["height"]) == (400, 200)
    for bad in (5, -3, 99999, "big", float("nan")):
        assert w.do("box", text="x", w=bad).error
    assert w.do("frame", text="f", w=900, h=500).incoming[0]["width"] == 900


def test_a_frame_grows_to_hold_a_shape_added_with_to() -> None:
    w = World()
    w.do("frame", text="Plan", x=0, y=0)
    for i in range(6):
        w.do("box", text=f"step {i}", to="f1")
    frame = w.get("f1")
    assert frame["height"] > 360 and frame["version"] > 1
    assert [x for x in board_layout.warnings(w.elements) if "frame" in x] == []
    for i in range(1, 7):
        s = w.get(f"b{i}")
        assert s["frameId"] == "f1" and s["y"] + s["height"] <= frame["y"] + frame["height"]


def test_resize_keeps_the_label_centred_and_resends_bound_arrows() -> None:
    w = World()
    w.do("box", text="left", x=0, y=0)
    w.do("box", text="right", x=400, y=0)
    w.do("connect", id="b1", to="b2")
    before = w.get("a1")["version"]
    got = w.do("resize", id="b1", w=300, h=200)
    assert not got.error and (w.get("b1")["width"], w.get("b1")["height"]) == (300, 200)
    label, shape = w.label("b1"), w.get("b1")
    assert abs(label["x"] + label["width"] / 2 - shape["x"] - 150) < 1
    assert abs(label["y"] + label["height"] / 2 - shape["y"] - 100) < 1
    assert w.get("a1")["version"] > before  # the arrow is sent again so the pane re-attaches it
    only_w = w.do("resize", id="b2", w=200)
    assert w.get("b2")["height"] == 80 and not only_w.error
    for params in ({"id": "b1"}, {"id": "a1", "w": 50}, {"id": "nope", "w": 50}, {"id": "b1", "h": 5}):
        assert w.do("resize", **params).error


def test_the_text_action_draws_a_caption_with_no_box() -> None:
    w = World()
    w.do("frame", text="Plan", x=0, y=0)
    got = w.do("text", text="Heading", style="font-size=xl stroke=violet align=center", to="f1")
    t = w.get(got.added[0])
    assert t["type"] == "text" and not t.get("containerId")
    assert t["fontSize"] == 36 and t["strokeColor"] == "#6741d9" and t["textAlign"] == "center"
    assert t["frameId"] == "f1"
    assert w.do("text", text="").error and w.do("text", text="x", style="fill=solid").error


def test_edit_restyles_with_and_without_new_words_and_regrows_the_shape() -> None:
    w = World()
    w.do("box", text="start", x=0, y=0)
    w.do("edit", id="b1", style="background=red stroke=red")
    assert w.get("b1")["backgroundColor"] == "#ffc9c9" and w.label("b1")["text"] == "start"
    w.do("edit", id="b1", text="renamed", style="font-size=l")
    assert w.label("b1")["text"] == "renamed" and w.label("b1")["fontSize"] == 28
    w.do("edit", id="b1", text="renamed " * 12)
    assert w.get("b1")["height"] > 80 and board_layout.warnings(w.elements) == []
    assert w.do("edit", id="b1").error  # neither text nor style
    assert w.do("edit", id="b1", style="stroke=#fff").error
    w.do("frame", text="F", x=0, y=500)
    assert w.do("edit", id="f1", style="stroke=red").error
    assert w.do("edit", id="f1", text="Renamed").incoming[0]["name"] == "Renamed"


def test_each_layout_warning_is_found_from_geometry() -> None:
    w = World()
    w.do("frame", text="F", x=0, y=0)
    w.do("box", text="a", x=20, y=60, to="f1")
    w.do("box", text="b", x=60, y=80, to="f1")  # overlaps a
    w.do("box", text="c", x=900, y=60, to="f1", w=100, h=80)
    text = " ".join(board_layout.warnings(w.elements))
    assert "overlap: b1 and b2" in text
    w2 = World()
    w2.do("box", text="left", x=0, y=0)
    w2.do("box", text="mid", x=300, y=0)
    w2.do("box", text="right", x=600, y=0)
    w2.do("connect", id="b1", to="b3", text="long label here")
    w2.do("connect", id="b3", to="b1", text="long label here")
    found = board_layout.warnings(w2.elements)
    assert "arrow a1 passes through b2" in found
    assert any(x.startswith("arrow labels overlap") for x in found)
    w3 = World()
    w3.do("box", text="Supercalifragilisticexpialidocious", w=40, h=30, x=0, y=0)
    assert any("label may overflow: b1" in x for x in board_layout.warnings(w3.elements))
    w4 = World()
    w4.do("frame", text="F", x=0, y=0)
    w4.do("box", text="in", x=20, y=60, to="f1")
    w4.do("move", id="b1", x=500, y=300)
    assert any("straddles its frame f1" in x for x in board_layout.warnings(w4.elements))
    w4.do("move", id="b1", x=2000, y=2000)
    assert any("is outside its frame" in x for x in board_layout.warnings(w4.elements))


def test_a_write_returns_only_the_warnings_of_what_it_touched_and_read_returns_all() -> None:
    w = World()
    w.do("box", text="a", x=0, y=0)
    w.do("box", text="b", x=20, y=10)  # overlaps a
    w.do("box", text="c", x=2000, y=2000)  # clean
    w.do("box", text="d", x=2500, y=2000)
    # a write about an untouched, clean pair says nothing; one that touches the overlap does
    assert w.do("move", id="b3", x=2100, y=2300).warnings == ()
    got = w.do("move", id="b2", x=30, y=20)
    assert got.warnings == ("overlap: b1 and b2",)
    assert "layout warnings:\n- overlap: b1 and b2" in board.summary(w.elements, 4)
    clean = World()
    clean.do("box", text="a", x=0, y=0)
    assert "layout warnings" not in board.summary(clean.elements, 1)
    for action, params in (
        ("box", {"text": "z", "x": 10, "y": 10}),
        ("note", {"text": "z", "x": 10, "y": 10}),
        ("edit", {"id": "b2", "text": "z " * 60}),
        ("resize", {"id": "b1", "w": 400, "h": 400}),
    ):
        assert w.do(action, **params).warnings, action  # each write names what it caused
    w2 = World()
    w2.do("box", text="a", x=0, y=0)
    w2.do("box", text="b", x=300, y=0)
    w2.do("box", text="c", x=600, y=0)
    assert w2.do("connect", id="b1", to="b3").warnings == ("arrow a1 passes through b2",)


def test_warnings_are_bounded() -> None:
    elements = [
        {"id": f"s{i}", "type": "rectangle", "x": 0, "y": 0, "width": 50, "height": 50, "isDeleted": False}
        for i in range(12)
    ]
    found = board_layout.warnings(elements)
    assert len(found) == board_layout.MAX_WARNINGS + 1 and found[-1].startswith("... and")


def test_an_elbow_arrow_starts_and_ends_on_the_facing_edges_not_at_the_target_centre() -> None:
    """The pane routes an elbow from its two stored end points, so an end at the target's centre
    would run the arrow into the shape. The end stops a gap short of the target's edge."""
    w = World()
    w.do("box", text="a", x=0, y=0)
    w.do("box", text="b", x=0, y=300)
    w.do("connect", id="b1", to="b2", style="arrow-type=elbow")
    a = w.get("a1")
    assert (a["x"], a["y"]) == (70, 80)  # bottom edge, middle of the first box
    assert a["points"][1] == [0, 212]  # the second box's top (300) less the 8 gap
    straight = World()
    straight.do("box", text="a", x=0, y=0)
    straight.do("box", text="b", x=0, y=300)
    straight.do("connect", id="b1", to="b2")
    assert straight.get("a1")["points"][1] == [-70, 300]  # unchanged: aimed at the centre


def three_with_a_blocker() -> World:
    """b1 top left, b3 far lower right, and b2 on the straight line between them but off an elbow's."""
    w = World()
    w.do("box", text="a", x=0, y=0)
    w.do("box", text="blocker", x=180, y=120)
    w.do("box", text="c", x=600, y=300)
    return w


def test_a_straight_arrow_through_a_blocker_warns_and_an_elbow_or_bent_one_around_it_does_not() -> None:
    """Warnings follow the arrow's own route, not a line between the centres: an elbow that goes
    round a shape, and an arrow stored with several points, are clean; the straight one is not."""
    w = three_with_a_blocker()
    straight = w.do("connect", id="b1", to="b3")
    assert straight.warnings == ("arrow a1 passes through b2",)
    w = three_with_a_blocker()
    assert w.do("connect", id="b1", to="b3", style="arrow-type=elbow").warnings == ()
    assert board_layout.warnings(w.elements) == []
    bent = three_with_a_blocker()
    bent.do("connect", id="b1", to="b3")
    a = bent.get("a1")
    around = {**a, "points": [[0, 0], [226, 0], [226, 300], [452, 300]]}
    others = [e for e in bent.elements if e["id"] != "a1"]
    assert board_layout.warnings([*others, around]) == []
    through = {**a, "points": [[0, 0], [200, 120], [452, 300]]}
    assert any("passes through b2" in x for x in board_layout.warnings([*others, through]))


def test_an_arrow_label_sits_at_the_middle_of_the_route() -> None:
    """Two labelled straight arrows crossing at their middles collide; the middle of a bent route
    is half-way along its length, not half-way between its ends."""
    straight = World()
    for i, (x, y) in enumerate([(0, 0), (600, 400), (0, 400), (600, 0)]):
        straight.do("box", text=f"s{i}", x=x, y=y)
    straight.do("connect", id="b1", to="b2", text="one")
    straight.do("connect", id="b3", to="b4", text="two")
    assert any(x.startswith("arrow labels overlap") for x in board_layout.warnings(straight.elements))
    mid = board_layout._midpoint([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)])
    assert mid == (10.0, 0.0)


def test_edit_font_size_on_a_loose_text_resizes_its_box_to_the_words() -> None:
    w = World()
    t = w.do("text", text="A caption that is fairly long", style="font-size=s").added[0]
    small = (w.get(t)["width"], w.get(t)["height"])
    w.do("edit", id=t, style="font-size=xl")
    big = w.get(t)
    assert big["fontSize"] == 36 and big["width"] > small[0] and big["height"] >= small[1]
    w.do("edit", id=t, text="x " * 80)
    assert w.get(t)["height"] > big["height"]  # wrapped: the box is taller, not just wider


def test_a_label_style_with_no_words_is_refused_not_dropped() -> None:
    w = World()
    assert w.do("box", style="font-size=l").error == "that shape has no text"
    assert w.do("box", style="align=left").error and not w.elements
    w.do("box", text="a", x=0, y=0)
    w.do("box", text="b", x=300, y=0)
    assert w.do("connect", id="b1", to="b2", style="font-size=l").error == "that arrow has no text"
    assert not w.do("box", style="stroke=red").error  # a plain style on an empty box is fine


def test_a_frame_grows_in_every_direction_for_a_shape_placed_outside_it() -> None:
    w = World()
    w.do("frame", text="F", x=100, y=100)
    w.do("box", text="left and up", to="f1", x=-50, y=0)
    f = w.get("f1")
    assert (f["x"], f["y"]) == (-70, -20)
    s = w.get("b1")
    assert f["x"] <= s["x"] - 20 and f["y"] <= s["y"] - 20
    assert f["x"] + f["width"] >= 700 and f["y"] + f["height"] >= 460  # the old right and bottom kept
    assert [x for x in board_layout.warnings(w.elements) if "frame" in x] == []
    w.do("box", text="right and down", to="f1", x=900, y=700)
    f = w.get("f1")
    s = w.get("b2")
    assert (
        f["x"] + f["width"] >= s["x"] + s["width"] + 20 and f["y"] + f["height"] >= s["y"] + s["height"] + 20
    )
