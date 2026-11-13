"""The broker draws a Mermaid flowchart itself, and lays a frame out (DESIGN.md §56.9).

Each test says what it checks and the bug that would fail it. The geometric checks are the bar
issue #728 set: no overlapping shapes, no label spilling out of its shape, no shape outside its frame,
no arrow through a shape that is not one of its ends, no overlapping arrow labels."""

from __future__ import annotations

import random
from typing import Any

import pytest

from switchboard import board, board_flow

SHAPE_TYPES = ("rectangle", "ellipse", "diamond")


def draw(source: str, current: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    flow = board_flow.convert(source)
    return board.mermaid_elements(current or [], flow.elements, now=10)


def kinds(els: list[dict[str, Any]], *types: str) -> list[dict[str, Any]]:
    return [e for e in els if e["type"] in types]


def label_of(els: list[dict[str, Any]], shape: dict[str, Any]) -> str:
    return next(e["text"] for e in els if e.get("containerId") == shape["id"])


def named(els: list[dict[str, Any]], text: str) -> dict[str, Any]:
    return next(s for s in kinds(els, *SHAPE_TYPES) if label_of(els, s).replace("\n", " ") == text)


def box(e: dict[str, Any]) -> tuple[float, float, float, float]:
    return e["x"], e["y"], e["width"], e["height"]


def overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def inside(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return (
        a[0] >= b[0] - 0.01
        and a[1] >= b[1] - 0.01
        and a[0] + a[2] <= b[0] + b[2] + 0.01
        and a[1] + a[3] <= b[1] + b[3] + 0.01
    )


def absolute(a: dict[str, Any]) -> list[tuple[float, float]]:
    return [(a["x"] + px, a["y"] + py) for px, py in a["points"]]


def segment_hits(
    p: tuple[float, float], q: tuple[float, float], r: tuple[float, float, float, float]
) -> bool:
    x, y, w, h = r
    e = 0.5  # float noise: lying along an edge is not crossing
    if abs(p[0] - q[0]) < 1e-6:
        return x + e < p[0] < x + w - e and min(p[1], q[1]) < y + h - e and max(p[1], q[1]) > y + e
    return y + e < p[1] < y + h - e and min(p[0], q[0]) < x + w - e and max(p[0], q[0]) > x + e


def check_bar(els: list[dict[str, Any]]) -> None:
    """The #728 bar, on any converted diagram."""
    by_id = {e["id"]: e for e in els}
    shapes = kinds(els, *SHAPE_TYPES)
    for i, a in enumerate(shapes):
        for b in shapes[i + 1 :]:
            assert not overlap(box(a), box(b)), (label_of(els, a), label_of(els, b))
    for s in shapes:  # a label fits inside what its shape can show
        t = next(e for e in els if e.get("containerId") == s["id"])
        shrink = {"rectangle": 1.0, "ellipse": 0.72, "diamond": 0.52}[s["type"]]
        cx, cy = s["x"] + s["width"] / 2, s["y"] + s["height"] / 2
        room = (
            cx - s["width"] * shrink / 2,
            cy - s["height"] * shrink / 2,
            s["width"] * shrink,
            s["height"] * shrink,
        )
        assert inside(box(t), room), (s["type"], t["text"])
        if s.get("frameId"):
            assert inside(box(s), box(by_id[s["frameId"]])), t["text"]
    frames = kinds(els, "frame")
    for i, a in enumerate(frames):
        for b in frames[i + 1 :]:
            assert not overlap(box(a), box(b))
        for s in shapes:
            if s.get("frameId") != a["id"]:
                assert not overlap(box(a), box(s)), "a frame holds a shape that is not its own"
    labels = []
    for a in kinds(els, "arrow"):
        pts = absolute(a)
        assert a["elbowed"] is True
        for p, q in zip(pts, pts[1:], strict=False):
            assert p[0] == q[0] or p[1] == q[1], "elbow arrows turn at right angles"
        ends = {(a.get("startBinding") or {}).get("elementId"), (a.get("endBinding") or {}).get("elementId")}
        for s in shapes:
            if s["id"] in ends:
                continue
            for p, q in zip(pts, pts[1:], strict=False):
                assert not segment_hits(p, q, box(s)), ("an arrow passes through", label_of(els, s))
        labels += [e for e in els if e.get("containerId") == a["id"]]
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            assert not overlap(box(a), box(b)), (a["text"], b["text"])
        for s in shapes:
            assert not overlap(box(a), box(s)), (a["text"], label_of(els, s))
    for e in els:
        board.clean_element(e, agent=True)


DEPS = """flowchart TD
  subgraph Web
    ui[UI] -->|calls| api[API]
    api --> auth{Auth ok?}
    auth -->|no| login[Login]
  end
  subgraph Core
    svc[Service] --> repo[(Repository)]
    svc --> cache([Cache])
    repo --> db[(Database)]
    svc --> queue>Queue]
  end
  subgraph Workers
    w1[Worker one] --> w2[Worker two]
    w2 --> mail[Mailer]
  end
  api --> svc
  auth -->|yes| svc
  queue --> w1
  mail -.->|retry| queue
  login --> ui
  cache -.-> db
  w2 ==> db
  svc --> metrics((Metrics))
  metrics --> log[Logger]
  api --> log
  mail --> log
  w1 --> log
"""


def test_shapes_become_the_kinds_the_board_has_and_labels_are_their_words():
    """Every node form maps onto a rectangle, an ellipse or a diamond with its label bound inside; a
    parser that dropped the label, or made every shape a rectangle, fails."""
    els = draw(
        "graph LR\n"
        "a[Rect] --> b(Round) --> c([Stadium]) --> d[[Sub]] --> e[(Db)] --> f((Circle)) --> g(((Double)))\n"
        "h{Choice} --> i{{Hex}} --> j>Flag] --> k[/Slant/] --> l[\\Back\\] --> m[/Trap\\] --> n[\\Inv/]\n"
        'o["quoted [text]"] --> p["`markdown`"] --> q[line one<br/>line two<br>three]\n'
        "q --> r[a #quot;b#quot; #35;]\n"
        "bare\n"
    )
    types = {label_of(els, s): s["type"] for s in kinds(els, *SHAPE_TYPES)}
    assert types["Rect"] == "rectangle" and types["Circle"] == "ellipse" and types["Double"] == "ellipse"
    assert types["Choice"] == "diamond" and types["Hex"] == "diamond"
    assert (
        types["Round"] == "rectangle"
        and next(s for s in els if s["type"] == "rectangle" and label_of(els, s) == "Round")["roundness"]
    )
    assert label_of(els, named(els, "Rect")) == "Rect" and "bare" in types
    for word in ("Stadium", "Sub", "Db", "Flag", "Slant", "Back", "Trap", "Inv", "quoted [text]", "markdown"):
        assert word in types, word
    assert "line one\nline two\nthree" in types
    assert 'a "b" #' in types
    check_bar(els)


def test_every_edge_form_draws_the_right_line_and_heads():
    """Dotted is dashed, thick is wider, an open link has no head, `o` and `x` ends and `<-->` have
    theirs, `~~~` draws nothing, and labels come in all three spellings. A wrong head or a drawn
    invisible link fails."""
    src = (
        "flowchart TD\nA --> B\nA --- C\nA -.-> D\nA ==> E\nA --o F\nA --x G\nA <--> H\nA o--o I\nA x--x J\n"
        "A ---> K\nA -..-> L\nA ===> M\nA ~~~ N\nA -- one --> O\nA -->|two| P\n"
        "A -. three .-> Q\nA == four ==> R\n"
    )
    els = draw(src)
    arrows = kinds(els, "arrow")
    assert len(arrows) == 16  # N is only a layout link
    by_end = {
        label_of(els, next(s for s in els if s["id"] == a["endBinding"]["elementId"])): a for a in arrows
    }
    assert by_end["B"]["endArrowhead"] == "arrow" and by_end["B"]["startArrowhead"] is None
    assert by_end["C"]["endArrowhead"] is None and by_end["C"]["startArrowhead"] is None
    assert by_end["D"]["strokeStyle"] == "dashed" and by_end["L"]["strokeStyle"] == "dashed"
    assert by_end["M"]["strokeWidth"] == 4 and by_end["K"]["endArrowhead"] == "arrow"
    assert by_end["E"]["strokeWidth"] == 4 and by_end["B"]["strokeWidth"] == 2
    assert by_end["F"]["endArrowhead"] == "dot" and by_end["G"]["endArrowhead"] == "bar"
    assert by_end["H"]["startArrowhead"] == "arrow" and by_end["H"]["endArrowhead"] == "arrow"
    assert by_end["I"]["startArrowhead"] == "dot" and by_end["I"]["endArrowhead"] == "dot"
    assert by_end["J"]["startArrowhead"] == "bar" and by_end["J"]["endArrowhead"] == "bar"
    for end, words in (("O", "one"), ("P", "two"), ("Q", "three"), ("R", "four")):
        a = by_end[end]
        assert next(e for e in els if e.get("containerId") == a["id"])["text"] == words
    assert "N" in {label_of(els, s) for s in kinds(els, *SHAPE_TYPES)}  # the node is drawn, the link is not
    check_bar(els)


def test_chains_ampersands_semicolons_and_comments():
    """`A --> B --> C`, `A & B --> C & D` (four arrows), `;` separators and `%%` comments read as
    Mermaid reads them; a parser that treats `&` as text or keeps comments fails."""
    els = draw("flowchart LR; A --> B --> C; %% a comment\nD & E --> F & G %% trailing\n")
    assert len(kinds(els, "arrow")) == 2 + 4
    assert {label_of(els, s) for s in kinds(els, *SHAPE_TYPES)} == set("ABCDEFG")


@pytest.mark.parametrize("direction", ["TD", "TB", "BT", "LR", "RL"])
def test_each_direction_runs_the_way_it_says(direction):
    """In TD and TB the flow runs down, in BT up, in LR right, in RL left: the coordinates say so, not
    just the header."""
    els = draw(f"flowchart {direction}\nA --> B --> C\nA --> D")
    a, c = named(els, "A"), named(els, "C")
    dx, dy = c["x"] - a["x"], c["y"] - a["y"]
    if direction in ("TD", "TB"):
        assert dy > 100 and abs(dx) < dy
    elif direction == "BT":
        assert dy < -100
    elif direction == "LR":
        assert dx > 100 and abs(dy) < dx
    else:
        assert dx < -100
    check_bar(els)


def test_a_subgraph_is_one_frame_that_holds_its_members_and_nothing_else():
    """Frames hold exactly their members, shapes outside stay clear of them, and a link to a subgraph
    id binds to the frame. A frame that overlapped an outside node, or a link to the id that went
    nowhere, fails."""
    src = (
        'flowchart TD\nsubgraph one [First]\nA --> B\nend\nsubgraph "Second one"\nC\nend\n'
        "subgraph three\nE\nend\nX --> one\none --> C\nB --> E\n"
    )
    els = draw(src)
    frames = {f["name"]: f for f in kinds(els, "frame")}
    assert set(frames) == {"First", "Second one", "three"}
    members = {
        label_of(els, s) for s in kinds(els, *SHAPE_TYPES) if s.get("frameId") == frames["First"]["id"]
    }
    assert members == {"A", "B"}
    assert named(els, "X").get("frameId") is None
    bound = [
        a
        for a in kinds(els, "arrow")
        if frames["First"]["id"] in (a["startBinding"]["elementId"], a["endBinding"]["elementId"])
    ]
    assert len(bound) == 2
    check_bar(els)


def test_nested_subgraphs_stay_inside_the_outer_frame_and_the_reply_says_so():
    """Excalidraw frames do not nest: the inner group has no frame, its members are in the outer one, and
    the reply says that in a clause. Two frames, or no clause, fails."""
    flow = board_flow.convert(
        "flowchart LR\nsubgraph outer\nsubgraph inner\nA --> B\nend\nB --> C\nend\nC --> D"
    )
    els = board.mermaid_elements([], flow.elements, now=1)
    assert [f["name"] for f in kinds(els, "frame")] == ["outer"]
    outer = kinds(els, "frame")[0]
    assert {label_of(els, s) for s in kinds(els, *SHAPE_TYPES) if s.get("frameId") == outer["id"]} == {
        "A",
        "B",
        "C",
    }
    assert "cannot nest" in flow.summary
    check_bar(els)


def test_cycles_self_loops_and_a_node_used_before_it_is_defined():
    """A cycle still draws every arrow the way it was written; a self-loop leaves and returns to its
    shape; a node first named by a link and labelled later takes the label. A hang, a dropped arrow or a
    lost label fails."""
    els = draw("flowchart TD\nA --> B --> C --> A\nC --> C\nB --> Z\nZ[Zed]\nZ --> A")
    arrows = kinds(els, "arrow")
    assert len(arrows) == 6
    ids = {label_of(els, s): s["id"] for s in kinds(els, *SHAPE_TYPES)}
    assert "Zed" in ids and "Z" not in ids
    loop = next(a for a in arrows if a["startBinding"]["elementId"] == a["endBinding"]["elementId"])
    assert loop["startBinding"]["elementId"] == ids["C"] and len(loop["points"]) >= 4
    back = next(
        a
        for a in arrows
        if a["endBinding"]["elementId"] == ids["A"] and a["startBinding"]["elementId"] == ids["C"]
    )
    assert back["endArrowhead"] == "arrow"
    check_bar(els)


def test_placement_is_clear_of_what_is_already_drawn():
    """A converted diagram goes right of the live board, the way every other converted one does."""
    existing = [board._base("rectangle", "keep", 0, 0, 300, 100, 0, 1)]
    els = draw("flowchart LR\nA --> B", existing)
    assert min(e["x"] for e in els) >= 380


def test_the_cap_counts_elements_and_says_how_many_it_would_make():
    """150 elements is the most; 151 is refused whole with the count, and a hint to split it. A shape is
    2 elements, an arrow 1, a labelled arrow 2, a frame 1."""
    exact = "flowchart LR\n" + "\n".join(f"n{i}[N{i}]" for i in range(75))  # 75 shapes x 2
    assert len(board_flow.convert(exact).elements) == 150
    over = exact + "\nextra"  # 151... 152
    with pytest.raises(board.Reject, match=r"would make 152 elements.*split it into two calls.*edge labels"):
        board_flow.convert(over)
    one_over = (
        "flowchart LR\n" + "\n".join(f"n{i}[N{i}]" for i in range(74)) + "\na --> b"
    )  # 148 + 2 shapes...
    with pytest.raises(board.Reject, match=r"would make 15\d elements"):
        board_flow.convert(one_over)
    edges = "flowchart LR\n" + "\n".join(f"A --> B{i}" for i in range(80))  # 81 shapes + 80 arrows
    with pytest.raises(board.Reject, match="would make 242 elements"):
        board_flow.convert(edges)


def test_a_diagram_of_exactly_150_elements_is_drawn_and_151_is_not():
    """The limit is inclusive: 50 shapes (100) and 50 arrows make 150 and draw whole, through the cleaner
    and the placement; one more arrow is 151 and is refused with the count."""
    lines = ["flowchart TD"] + [f"s{i}[S{i}] --> s{i + 1}" for i in range(49)]  # 50 shapes, 49 arrows
    assert len(board_flow.convert("\n".join(lines)).elements) == 149
    exact = "\n".join([*lines, "s0 --> s49"])
    els = board.mermaid_elements([], board_flow.convert(exact).elements, now=1)
    assert len(els) == 150
    with pytest.raises(board.Reject, match="would make 151 elements"):
        board_flow.convert(exact + "\ns1 --> s49")


def test_hostile_labels_stay_inert_text_cut_to_200():
    """Tags, `javascript:` text, entities, control and bidi characters and a 10,000-character label are
    only words on the canvas, cut to 200 characters, with no link or image element."""
    long = "w" * 150 + " " + "x" * 200
    src = (
        'flowchart TD\nA["<script>alert(1)</script>"] --> B["javascript:alert(1)"]\n'
        'B --> C["&lt;img src=x onerror=alert(1)&gt; #lt;b#gt; ‮evil\u0007​"]\n'
        f'C --> D["{long}"]\nD -->|"<b>x</b>"| A\n'
    )
    els = draw(src)
    texts = [e["text"] for e in els if e["type"] == "text"]
    assert any("<script>alert(1)</script>" in t for t in texts)
    assert all(len(t) <= 200 for t in texts) and all("‮" not in t and "\u0007" not in t for t in texts)
    assert all(e["type"] != "image" and e["link"] is None for e in els)
    for e in els:
        for key in ("text", "originalText"):
            assert len(e.get(key, "")) <= 200
    check_bar(els)
    huge = 'flowchart TD\nA["' + "q" * 7900 + '"] --> B'
    got = draw(huge)
    assert all(len(e.get("text", "")) <= 200 for e in got)


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("flowchart TD\nA --> B\nA -> B", 3),
        ("flowchart TD\nA --> B\n\nA[unclosed --> B", 4),
        ("flowchart TD\nsubgraph x\nA --> B", 2),
        ("flowchart TD\nA --> B\nend", 3),
        ("flowchart TD\nA --> B; B --> ", 2),
        ("flowchart TD\nA --> B\nA --> B C", 3),
        ('flowchart TD\nA["never closed --> B', 2),
        ("flowchart TD\nA --> B; style A color:red", 2),
        ("flowchart TD\nsubgraph\nA\nend", 2),
    ],
)
def test_an_unreadable_line_is_refused_with_its_number_and_the_form(source, line):
    """Nothing is silently dropped: the error names the line and says what is accepted."""
    with pytest.raises(board.Reject) as caught:
        board_flow.convert(source)
    assert f"line {line}:" in caught.value.reason, caught.value.reason
    assert (
        "write A[label] --> B[label]" in caught.value.reason
        or "subgraph" in caught.value.reason
        or "end" in caught.value.reason
        or "quote" in caught.value.reason
        or "style" in caught.value.reason
        or "fill and stroke" in caught.value.reason
    )


def test_the_guard_still_refuses_what_mermaid_source_refused():
    """The converter reads only what ``mermaid_source`` accepts: configuration, linkStyle, clicks inline,
    other diagram types and oversize stay refused (``style`` and ``classDef`` are read for fill and stroke
    only: tests/unit/test_board_flow_style.py)."""
    for source in (
        "sequenceDiagram\nA->>B: x",
        "%%{init: {}}%%\nflowchart TD\nA-->B",
        "flowchart TD\nA-->B\nlinkStyle 0 stroke:red",
        "flowchart TD;click A cb",
        "x" * 8001,
    ):
        with pytest.raises(board.Reject):
            board_flow.convert(source)
    assert len(draw('flowchart TD\nA-->B\nclick A href "https://example.com"')) == 5


def test_a_class_line_and_a_class_suffix_with_no_definition_change_nothing():
    """`class A x` and `A:::x` with no `classDef x` colour nothing, and are not drawn as text."""
    els = draw("flowchart TD\nA:::hot --> B\nclass A,B x")
    assert {label_of(els, s) for s in kinds(els, *SHAPE_TYPES)} == {"A", "B"}


def test_a_twenty_node_dependency_graph_meets_the_bar_and_is_deterministic():
    """About 20 nodes and 25 arrows, with three subgraphs and long edges, draw with no overlaps, no
    label overflow, no shape outside its frame, no arrow through a shape and no overlapping labels, and
    the same source lays out the same way twice."""
    first = board_flow.convert(DEPS).elements
    again = board_flow.convert(DEPS).elements
    strip = lambda els: [(e["type"], e["x"], e["y"], e.get("points")) for e in els]  # noqa: E731
    assert strip(first) == strip(again)
    els = board.mermaid_elements([], first, now=1)
    assert len(kinds(els, *SHAPE_TYPES)) == 14 and len(kinds(els, "arrow")) == 21
    check_bar(els)


def random_flow(seed: int) -> str:
    rng = random.Random(seed)
    n = rng.randint(2, 12)
    words = ["alpha", "beta service", "gamma gamma gamma gamma gamma", "d", "epsilon data store", "zeta"]
    shapes = ["[{}]", "({})", "{{{}}}", "(({}))", "([{}])", "[({})]"]
    out = [f"flowchart {rng.choice(['TD', 'LR', 'BT', 'RL'])}"]
    names = [f"n{i}" for i in range(n)]
    groups: dict[str, list[str]] = {}
    for name in names:
        if rng.random() < 0.4:
            groups.setdefault(f"g{rng.randint(0, 2)}", []).append(name)
    inner = {m for ms in groups.values() for m in ms}
    for name in names:
        if name not in inner:
            out.append(name + rng.choice(shapes).format(rng.choice(words)))
    for g, members in groups.items():
        out.append(f"subgraph {g} [Group {g}]")
        out.extend(m + rng.choice(shapes).format(rng.choice(words)) for m in members)
        out.append("end")
    for _ in range(rng.randint(1, 2 * n)):
        a, b = rng.choice(names), rng.choice(names)
        link = rng.choice(["-->", "-.->", "==>", "---", "<-->"])
        label = rng.choice(["", "", "|go|", "|a longer label here|"])
        out.append(f"{a} {link}{label} {b}")
    return "\n".join(out)


@pytest.mark.parametrize("seed", range(120))
def test_random_graphs_meet_the_bar(seed):
    """Seeded random graphs (cycles, self-loops, groups, labels, every direction): no two node boxes
    overlap, every element passes the cleaner, and the geometric bar holds."""
    src = random_flow(seed)
    try:
        flow = board_flow.convert(src)
    except board.Reject as e:
        assert "would make" in e.reason, (src, e.reason)
        return
    els = board.mermaid_elements([], flow.elements, now=1)
    check_bar(els)


def test_every_elbow_arrow_carries_the_fixed_point_where_its_ends_sit():
    """Excalidraw keeps an elbow arrow attached by `fixedPoint`, the fraction of the bound box where the end
    sits. Each end's fraction, applied to its shape's box, must land exactly on the arrow's first and last
    points, so a dragged shape moves the end from where it really is. Fails if the ratio is missing,
    outside the box, or not the point the router drew to."""
    els = draw(DEPS)
    by_id = {e["id"]: e for e in els}
    arrows = kinds(els, "arrow")
    assert arrows
    for a in arrows:
        pts = absolute(a)
        for key, point in (("startBinding", pts[0]), ("endBinding", pts[-1])):
            bind = a[key]
            fx, fy = bind["fixedPoint"]
            assert 0 <= fx <= 1 and 0 <= fy <= 1
            t = by_id[bind["elementId"]]
            assert abs(t["x"] + fx * t["width"] - point[0]) < 0.01
            assert abs(t["y"] + fy * t["height"] - point[1]) < 0.01


def test_a_node_with_more_than_40_bound_elements_is_refused_naming_it():
    """A shape holds 40 bound elements: its label and 39 arrows. A hub with 40 links is refused whole,
    naming the node and saying to split it; 39 are drawn. Else its list would be dropped by the guard."""
    hub = "flowchart LR\n" + "\n".join(f"Hub[Hub] --> n{i}" for i in range(40))
    with pytest.raises(board.Reject, match=r'"Hub" would carry 41 bound elements.*split'):
        board_flow.convert(hub)
    # 39 arrows and the label are 40, which fits, but the diagram is 2*40 + 39 = 119 elements
    ok = board_flow.convert("flowchart LR\n" + "\n".join(f"Hub[Hub] --> n{i}" for i in range(39)))
    hub_el = next(e for e in ok.elements if e["type"] == "rectangle" and len(e["boundElements"]) == 40)
    assert hub_el["boundElements"][0]["type"] == "text"


def test_the_densest_diagram_the_cap_allows_is_bounded_work(monkeypatch):
    """16 nodes and 118 links (150 elements) took over four seconds in the router. The search now has a
    step budget, shared by the whole call, after which an arrow is a plain elbow: the steps are bounded
    exactly and the time is a loose guard, not a stopwatch."""
    import time

    from switchboard import board_layout

    spent = []
    original = board_layout._search

    def spy(*args):
        before = args[-1][0]
        got = original(*args)
        spent.append(before - max(args[-1][0], 0))
        return got

    monkeypatch.setattr(board_layout, "_search", spy)
    pairs = [(i, j) for i in range(16) for j in range(i + 1, 16)][:118]
    src = "flowchart LR\n" + "\n".join(f"n{i}-->n{j}" for i, j in pairs)
    started = time.process_time()
    flow = board_flow.convert(src)
    assert len(flow.elements) == 150
    assert sum(spent) <= board_layout.TOTAL_BUDGET + len(spent)
    assert time.process_time() - started < 3.0
