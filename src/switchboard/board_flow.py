"""Draw a Mermaid flowchart on the whiteboard in the broker, and arrange a frame (DESIGN.md §56.9).

Pure. ``convert`` reads the flowcharts ``board.mermaid_source`` accepts, lays them out with
``board_layout`` and returns the same elements ``box``, ``connect`` and ``frame`` make; the broker
then sends them through ``board.mermaid_elements`` (the cleaner, the element cap, new ids, placement)
and stores them as one change. ``arrange`` lays out a frame's existing shapes the same way and
reroutes their arrows as elbows. Nothing here needs a browser.

The parser is a per-statement dispatch (``_statement``): a subgraph line, ``end``, ``direction``, a
``class`` line, or a chain of nodes and links. Nodes keep their label and shape kind in ``_Node``, so a
fill or a stroke can be added to a node, and a style line to the dispatch, without touching the layout.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from switchboard import board, board_checks, board_layout, board_style
from switchboard.board import MAX_DIAGRAM, MAX_TEXT, Reject
from switchboard.board_layout import Group, Item, Link

DIRECTIONS = {"TD": "TD", "TB": "TD", "BT": "BT", "LR": "LR", "RL": "RL"}
GLYPH = board_checks.GLYPH  # the one text measure, shared with the shape actions and read's warnings
LINE = board_checks.LINE
NOTHING = "write A[label] --> B[label]"
_HEADER = re.compile(r"(?:flowchart|graph)\s+(TD|TB|BT|RL|LR)\b")
_ID = re.compile(r"\w+(?:-\w+)*")
_CLASS = re.compile(r"class\s+([\w-]+(?:\s*,\s*[\w-]+)*)\s+([\w-]+)")
_STYLE = re.compile(r"(style|classDef)\s+([\w-]+(?:\s*,\s*[\w-]+)*)\s+(\S.*)", re.DOTALL)
_HEADS = {"<": "arrow", ">": "arrow", "x": "bar", "o": "dot"}
# opener, the closers that can end it, the shape it makes, whether a missing closer may fall back
_OPENERS: tuple[tuple[str, tuple[str, ...], str, bool], ...] = (
    ("(((", (")))",), "circle", False),
    ("((", ("))",), "circle", False),
    ("([", ("])",), "stadium", False),
    ("(", (")",), "round", False),
    ("[[", ("]]",), "rect", False),
    ("[(", (")]",), "rect", False),
    ("[/", ("/]", "\\]"), "rect", True),
    ("[\\", ("\\]", "/]"), "rect", True),
    ("[", ("]",), "rect", False),
    ("{{", ("}}",), "diamond", False),
    ("{", ("}",), "diamond", False),
    (">", ("]",), "rect", False),
)
_MAX_LINKS = 400
MAX_BOUND = board.MAX_CALL  # what one shape's ``boundElements`` may hold: its label and its arrows


class _Bad(Exception):
    """A statement the parser cannot read; ``why`` is None for the general message."""

    def __init__(self, why: str | None = None) -> None:
        super().__init__(why or "")
        self.why = why


@dataclass
class _Node:
    id: str
    label: str
    kind: str
    group: str | None
    order: int
    named: bool = False  # a label or shape was written for it
    classes: list[str] = field(default_factory=list)  # ``A:::name`` and ``class A name``
    style: dict[str, str] = field(default_factory=dict)  # ``style A fill:...``: the palette's hex


@dataclass
class _Sub:
    id: str
    title: str
    group: str | None
    direction: str | None
    order: int
    line: int


@dataclass
class _Edge:
    src: str
    dst: str
    style: str  # normal, dotted, thick or invisible
    head_a: str | None
    head_b: str | None
    label: str


@dataclass
class _Ref:
    id: str
    kind: str | None = None
    label: str | None = None
    klass: str | None = None


@dataclass
class _Flow:
    direction: str
    nodes: dict[str, _Node] = field(default_factory=dict)
    subs: dict[str, _Sub] = field(default_factory=dict)
    edges: list[_Edge] = field(default_factory=list)
    visible: int = 0  # arrows the diagram draws, counted even when they are not kept
    labeled: int = 0
    hidden: int = 0
    counter: int = 0
    classdefs: dict[str, dict[str, str]] = field(default_factory=dict)
    pending: list[tuple[int, str, str, dict[str, str]]] = field(
        default_factory=list
    )  # line, ids, kind, props

    def order(self) -> int:
        self.counter += 1
        return self.counter


@dataclass(frozen=True)
class Converted:
    elements: list[dict[str, Any]]
    summary: str


# --------------------------------------------------------------------------------- reading


def _label(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":  # a Markdown string: the words, not the marks
        s = s[1:-1]
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)

    def entity(m: re.Match[str]) -> str:
        name = m.group(1)
        got = html.unescape(f"&#{name};" if name.isdigit() else f"&{name};")
        return m.group(0) if got.startswith("&") or got == "" else got

    s = re.sub(r"#(\w+);", entity, s)
    s = "\n".join(line.strip() for line in s.split("\n")).strip()
    return board.clean(s)[:MAX_TEXT]


def _statements(text: str) -> list[tuple[int, str]]:
    """Statements with the line they start on. A newline or ``;`` ends one outside quotes; a comment
    runs to the end of its line; a quoted label may span lines."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    line = 1
    start = 1
    quote_line = 0
    in_quote = False
    i = 0

    def flush() -> None:
        s = "".join(buf).strip()
        if s:
            out.append((start, s))
        buf.clear()

    while i < len(text):
        ch = text[i]
        if ch == '"':
            in_quote = not in_quote
            quote_line = line if in_quote else quote_line
            if not "".join(buf).strip():
                start = line
            buf.append(ch)
        elif in_quote:
            buf.append(ch)
            line += ch == "\n"
        elif ch == "\n":
            flush()
            line += 1
        elif (
            ch == ";"
            and not re.match(r"\s*(?:style|classDef)\b", "".join(buf[:10]))
            and re.search(r"#\w+$", "".join(buf[-12:]))
        ):  # `#quot;` is a character, but a colour's `#f9f;` in a style line ends it
            buf.append(ch)
        elif ch == ";":
            flush()
        elif ch == "%" and text.startswith("%%", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        else:
            if not ch.isspace() and not "".join(buf).strip():
                start = line
            buf.append(ch)
        i += 1
    if in_quote:
        raise Reject(f'line {quote_line}: a quote is never closed; close it with a second "')
    flush()
    return out


class _Reader:
    def __init__(self, s: str) -> None:
        self.s = s
        self.i = 0

    def skip(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\n":
            self.i += 1

    def done(self) -> bool:
        self.skip()
        return self.i >= len(self.s)

    def node(self) -> _Ref:
        self.skip()
        m = _ID.match(self.s, self.i)
        if m is None:
            raise _Bad()
        self.i = m.end()
        ref = _Ref(m.group(0))
        for opener, closers, kind, fallback in _OPENERS:
            if not self.s.startswith(opener, self.i):
                continue
            got = self._label(self.i + len(opener), closers)
            if got is None:
                if fallback:
                    continue
                raise _Bad()
            text, self.i = got
            ref.kind, ref.label = kind, text
            break
        m2 = re.compile(r":::([\w-]+)").match(self.s, self.i)  # a class name, for ``classDef`` to colour
        if m2:
            self.i = m2.end()
            ref.klass = m2.group(1)
        return ref

    def _label(self, j: int, closers: tuple[str, ...]) -> tuple[str, int] | None:
        s = self.s
        k = j
        while k < len(s) and s[k] in " \t":
            k += 1
        if k < len(s) and s[k] == '"':
            end = s.find('"', k + 1)
            if end < 0:
                return None
            k2 = end + 1
            while k2 < len(s) and s[k2] in " \t":
                k2 += 1
            for c in closers:
                if s.startswith(c, k2):
                    return s[k + 1 : end], k2 + len(c)
            return None
        best: tuple[int, str] | None = None
        for c in closers:
            p = s.find(c, j)
            if p >= 0 and (best is None or p < best[0]):
                best = (p, c)
        return None if best is None else (s[j : best[0]], best[0] + len(best[1]))

    def group(self) -> list[_Ref]:
        refs = [self.node()]
        while True:
            self.skip()
            if self.i < len(self.s) and self.s[self.i] == "&":
                self.i += 1
                self.skip()
                refs.append(self.node())
            else:
                return refs

    def link(self) -> tuple[str, str | None, str | None, str] | None:
        """One link: its style, the head at each end and its label. None when no link starts here."""
        self.skip()
        s, i = self.s, self.i
        if s.startswith("~~~", i):
            m = re.compile(r"~{3,}").match(s, i)
            assert m
            self.i = m.end()
            return "invisible", None, None, self._pipe()
        for style, pat, label_pat, close_pat in (
            ("dotted", r"([xo<])?-(\.+)-([>xo])?", r"([xo<])?-\.(?=\s)", r"\.+-[>xo]?"),
            ("thick", r"([xo<])?(={2,})([>xo])?", r"([xo<])?==(?=\s)", r"={3,}|={2,}[>xo]"),
            ("normal", r"([xo<])?(-{2,})([>xo])?", r"([xo<])?--(?=\s)", r"-{3,}|-{2,}[>xo]"),
        ):
            m = re.compile(pat).match(s, i)
            if m:
                body = m.group(2)
                plain = style == "dotted" or m.group(3) is not None or len(body) >= 3
                if plain:
                    self.i = m.end()
                    return style, _HEADS.get(m.group(1) or ""), _HEADS.get(m.group(3) or ""), self._pipe()
            m = re.compile(label_pat).match(s, i)
            if m:
                found = re.compile(close_pat).search(s, m.end())
                if found is None:
                    raise _Bad()
                text = s[m.end() : found.start()]
                tail = found.group(0)[-1]
                self.i = found.end()
                return (
                    style,
                    _HEADS.get(m.group(1) or ""),
                    _HEADS.get(tail if tail in ">xo" else ""),
                    _label(text),
                )
        return None

    def _pipe(self) -> str:
        self.skip()
        if self.i < len(self.s) and self.s[self.i] == "|":
            end = self.s.find("|", self.i + 1)
            if end < 0:
                raise _Bad()
            text = self.s[self.i + 1 : end].strip()
            self.i = end + 1
            if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
                text = text[1:-1]
            return _label(text)
        return ""


def _unquote(text: str) -> str:
    t = text.strip()
    return t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t


def _statement(flow: _Flow, stack: list[str], line: int, stmt: str) -> None:
    sub = re.match(r"subgraph(?=\s|$)(.*)", stmt, re.DOTALL)
    if sub:
        rest = sub.group(1).strip()
        named = re.fullmatch(rf"({_ID.pattern})\s*\[(.*)\]", rest, re.DOTALL)
        if named:
            sid, title = named.group(1), _label(_unquote(named.group(2)))
        elif rest:
            sid, title = _unquote(rest), _label(_unquote(rest))
        else:
            raise _Bad("a subgraph needs a name; write subgraph Name")
        if sid in flow.subs:
            raise _Bad(f"subgraph {sid} is written twice")
        old = flow.nodes.get(sid)
        if old is not None:
            if old.named:
                raise _Bad(f"{sid} is a node and a subgraph; give one of them another id")
            del flow.nodes[sid]  # only mentioned by a link so far: the link means the subgraph
        flow.subs[sid] = _Sub(sid, title or sid, stack[-1] if stack else None, None, flow.order(), line)
        stack.append(sid)
        return
    if stmt == "end":
        if not stack:
            raise _Bad("end has no subgraph; remove it")
        stack.pop()
        return
    d = re.fullmatch(r"direction\s+(TD|TB|BT|LR|RL)", stmt)
    if d:
        if not stack:
            raise _Bad("direction goes inside a subgraph")
        sub_ = flow.subs[stack[-1]]
        sub_.direction = DIRECTIONS[d.group(1)]
        return
    cls = _CLASS.fullmatch(stmt)
    if cls:
        flow.pending.append((line, cls.group(1), "class", {"class": cls.group(2)}))
        return
    st = _STYLE.fullmatch(stmt)
    if st:
        props = _paint(st.group(3))
        if st.group(1) == "classDef":
            for name in re.split(r"\s*,\s*", st.group(2)):
                flow.classdefs.setdefault(name, {}).update(props)
        else:
            flow.pending.append((line, st.group(2), "style", props))
        return
    if re.match(r"(?:style|classDef|linkStyle|click)\b", stmt):
        raise _Bad(
            "write style A fill:blue,stroke:red (a palette name or a hex colour; only fill and stroke); "
            "linkStyle and clicks are not drawn"
        )
    r = _Reader(stmt)
    groups = [r.group()]
    links: list[tuple[str, str | None, str | None, str]] = []
    while not r.done():
        got = r.link()
        if got is None:
            raise _Bad()
        links.append(got)
        groups.append(r.group())
    for refs in groups:
        for ref in refs:
            _register(flow, stack, ref)
    for (style, a, b, text), left, right in zip(links, groups[:-1], groups[1:], strict=True):
        count = len(left) * len(right)
        if style == "invisible":
            flow.hidden += count
            if flow.hidden > _MAX_LINKS:
                raise _Bad("too many ~~~ links; keep the diagram smaller")
        else:
            flow.visible += count
            flow.labeled += count if text else 0
        if flow.visible > _MAX_LINKS and style != "invisible":
            continue  # counted for the size message, and the diagram is refused for it
        for u in left:
            for v in right:
                flow.edges.append(_Edge(u.id, v.id, style, a, b, text))


def _paint(text: str) -> dict[str, str]:
    """``fill:red,stroke:#c0392b`` as the palette's hex for each. Only fill and stroke; each value is a
    palette name or a ``#rgb``/``#rrggbb`` hex (mapped to the nearest palette colour). Everything else,
    fonts, widths, dashes, ``url(``, is refused rather than dropped."""
    out: dict[str, str] = {}
    for part in text.split(","):
        key, colon, value = part.partition(":")
        key, value = key.strip(), value.strip()
        if not colon or not key or not value:
            raise _Bad(f"can't read {board.clean(part)[:40]!r}; write fill:blue,stroke:red")
        if key not in ("fill", "stroke"):
            raise _Bad(f"only fill and stroke are drawn; remove {board.clean(key)[:30]!r}")
        try:
            out[key] = board_style.mermaid_color(value, key)
        except board_style.StyleError:
            raise _Bad(
                f"{key}:{board.clean(value)[:30]!r} is not a colour the board draws; use a palette name "
                f"({', '.join(board_style.COLOR_NAMES)}) or a #rrggbb hex"
            ) from None
    return out


def _register(flow: _Flow, stack: list[str], ref: _Ref) -> None:
    if ref.id in flow.subs:
        if ref.kind or ref.label is not None:
            raise _Bad(f"{ref.id} is a subgraph; give the node another id")
        return
    node = flow.nodes.get(ref.id)
    here = stack[-1] if stack else None
    if node is None:
        node = flow.nodes[ref.id] = _Node(ref.id, ref.id, "rect", here, flow.order())
    elif here is not None and node.group is None:
        node.group = here  # listed inside a subgraph: it belongs there
    if ref.kind:
        node.kind = ref.kind
        node.named = True
    if ref.label is not None:
        node.label = _label(ref.label) or node.id
        node.named = True
    if ref.klass:
        node.classes.append(ref.klass)


def _parse(text: str) -> _Flow:
    head = _HEADER.match(text)
    if head is None:  # the guard checked this already
        raise Reject("use a flowchart or graph without configuration directives")
    flow = _Flow(DIRECTIONS[head.group(1)])
    stack: list[str] = []
    for line, stmt in _statements(text[head.end() :]):
        try:
            _statement(flow, stack, line, stmt)
        except _Bad as e:
            snippet = board.clean(stmt)[:60]
            why = e.why or f'can\'t read "{snippet}"; {NOTHING}'
            raise Reject(f"line {line}: {why}") from None
    if stack:
        s = flow.subs[stack[-1]]
        raise Reject(f"line {s.line}: subgraph {s.id} is never closed; add end after its nodes")
    for line, ids, kind, props in flow.pending:  # a style may come before or after the node it names
        for key in re.split(r"\s*,\s*", ids):
            if key in flow.subs:
                raise Reject(f"line {line}: {key} is a subgraph; the board draws every frame the same")
            node = flow.nodes.get(key)
            if node is None:
                raise Reject(f"line {line}: {key} is not a node of this diagram; name a node you drew")
            if kind == "class":
                node.classes.append(props["class"])
            else:
                node.style.update(props)
    return flow


# -------------------------------------------------------------------------------- the drawing


_wrap = board_checks.wrap
Fit = board_checks.Fit
fit = board_checks.fit


def _count(flow: _Flow) -> int:
    frames = sum(1 for s in flow.subs.values() if s.group is None)
    return 2 * len(flow.nodes) + flow.visible + flow.labeled + frames


def convert(value: Any) -> Converted:
    """The elements for a whole flowchart, ready for ``board.mermaid_elements``."""
    flow = _parse(board.mermaid_source(value))
    if not flow.nodes and not flow.subs:
        raise Reject(f"line 1: nothing to draw; {NOTHING}")
    total = _count(flow)
    if total > MAX_DIAGRAM:
        raise Reject(
            f"this diagram would make {total} elements and one call takes {MAX_DIAGRAM}: "
            "split it into two calls, or drop the edge labels"
        )
    fits = {n.id: fit(n.label, n.kind) for n in flow.nodes.values()}
    parts: list[board_layout.Node] = []
    ordered: list[_Node | _Sub] = sorted([*flow.nodes.values(), *flow.subs.values()], key=lambda x: x.order)
    for n in ordered:
        if isinstance(n, _Sub):
            parts.append(Group(n.id, n.group, n.direction, 40.0 if n.group is None else 20.0))
        else:
            f = fits[n.id]
            parts.append(Item(n.id, f.w, f.h, n.group))
    known = {*flow.nodes, *flow.subs}
    drawn = [e for e in flow.edges if e.src in known and e.dst in known]
    bound = {k: 1 if k in flow.nodes else 0 for k in known}  # a node's label is bound to it
    for e in drawn:
        if e.style != "invisible":
            for end in {e.src, e.dst}:
                bound[end] += 1
    for k, count in bound.items():
        if count > MAX_BOUND:
            name = flow.nodes[k].label if k in flow.nodes else flow.subs[k].title
            raise Reject(
                f'"{name}" would carry {count} bound elements (its label and its arrows) and a shape holds '
                f"{MAX_BOUND}: split its links across two nodes, or draw it in two calls"
            )
    sizes = {}
    for i, e in enumerate(drawn):
        if e.label:
            lines = _wrap(e.label, 18)
            sizes[i] = (min(220.0, GLYPH * max(len(x) for x in lines) + 10), LINE * len(lines))
    links = [Link(e.src, e.dst, sizes.get(i), e.style == "invisible") for i, e in enumerate(drawn)]
    horizontal = flow.direction in ("LR", "RL")
    boxes = board_layout.place(parts, links, flow.direction)
    # the boxes arrows must not cross: nodes, not the frames around them
    obstacles = {k: boxes[k] for k in flow.nodes}
    routes = board_layout.route(boxes, obstacles, links, horizontal=horizontal)
    return _elements(flow, fits, drawn, sizes, boxes, routes)


def _top(flow: _Flow, key: str) -> str | None:
    """The top-level subgraph (the one frame) that holds ``key``, or None."""
    g = flow.nodes[key].group if key in flow.nodes else flow.subs[key].group
    top = None
    while g is not None:
        top = g
        g = flow.subs[g].group
    return top


def _elements(
    flow: _Flow,
    fits: dict[str, Fit],
    drawn: list[_Edge],
    sizes: dict[int, tuple[float, float]],
    boxes: dict[str, board_layout.Rect],
    routes: dict[int, board_layout.Route],
) -> Converted:
    now, ids = 0.0, iter(range(1, 10**6))

    def vn() -> int:
        return next(ids)

    out: list[dict[str, Any]] = []
    el_of: dict[str, str] = {}
    frame_of: dict[str, str] = {}
    for s in flow.subs.values():
        if s.group is None:
            x, y, w, h = boxes[s.id]
            f = board._base("frame", f"f{len(el_of)}", x, y, w, h, now, vn())
            f["name"] = s.title
            el_of[s.id] = frame_of[s.id] = f["id"]
            out.append(f)
    shape_el: dict[str, dict[str, Any]] = {}
    for n in flow.nodes.values():
        x, y, w, h = boxes[n.id]
        shape = board._base(
            {"circle": "ellipse", "diamond": "diamond"}.get(n.kind, "rectangle"),
            f"n{len(el_of)}",
            x,
            y,
            w,
            h,
            now,
            vn(),
        )
        shape["backgroundColor"] = "#ffffff"
        look: dict[str, str] = dict(flow.classdefs.get("default", {}))
        for name in n.classes:
            look.update(flow.classdefs.get(name, {}))
        look.update(n.style)  # a node's own style wins over its classes
        if "fill" in look:
            shape["backgroundColor"] = look["fill"]
        if "stroke" in look:
            shape["strokeColor"] = look["stroke"]
        if n.kind in ("round", "stadium"):
            shape["roundness"] = {"type": 3}
        top = _top(flow, n.id)
        if top:
            shape["frameId"] = frame_of[top]
        nf = fits[n.id]
        tid = f"t{len(el_of)}"
        tw = min(w - 10, nf.text_w + 8)
        text = board._text_el(
            tid,
            shape["id"],
            "\n".join(nf.lines),
            x + (w - tw) / 2,
            y + (h - nf.text_h) / 2,
            tw,
            now,
            vn(),
            frame_of.get(top, "") if top else "",
        )
        text["originalText"] = n.label
        text["height"] = nf.text_h
        shape["boundElements"] = [{"id": tid, "type": "text"}]
        el_of[n.id] = shape["id"]
        shape_el[n.id] = shape
        out.extend([shape, text])
    arrows = 0
    for i, e in enumerate(drawn):
        route = routes.get(i)
        if route is None:
            continue
        top_a, top_b = _top(flow, e.src), _top(flow, e.dst)
        frame = top_a if top_a == top_b and e.src not in frame_of and e.dst not in frame_of else None
        arrow = _arrow(f"a{len(el_of) + i}", route, now, vn())
        arrow["strokeWidth"] = 4 if e.style == "thick" else 2
        if e.style == "dotted":
            arrow["strokeStyle"] = "dashed"
        arrow["startArrowhead"], arrow["endArrowhead"] = e.head_a, e.head_b
        if frame:
            arrow["frameId"] = frame_of[frame]
        for end, key, at in (("startBinding", e.src, route.src_at), ("endBinding", e.dst, route.dst_at)):
            if key in el_of:  # a nested subgraph has no element of its own to bind to
                arrow[end] = {"elementId": el_of[key], "focus": 0.0, "gap": 0.0, "fixedPoint": list(at)}
                target = shape_el.get(key) or next(o for o in out if o["id"] == el_of[key])
                ref = {"id": arrow["id"], "type": "arrow"}
                if ref not in target.setdefault("boundElements", []):  # a loop binds both ends to one shape
                    target["boundElements"].append(ref)
        out.append(arrow)
        arrows += 1
        if e.label and route.label is not None:
            lw, lh = sizes[i]
            lines = _wrap(e.label, 18)
            lt = board._text_el(
                f"l{len(el_of) + i}",
                arrow["id"],
                "\n".join(lines),
                route.label[0],
                route.label[1],
                lw,
                now,
                vn(),
                frame_of[frame] if frame else "",
            )
            lt["originalText"] = e.label
            lt["height"] = lh
            lt["textAlign"] = "center"
            arrow["boundElements"] = [{"id": lt["id"], "type": "text"}]
            out.append(lt)
    nested = any(s.group is not None for s in flow.subs.values())
    frames = len(frame_of)
    summary = f"drew {len(shape_el)} shapes and {arrows} arrows" + (f" in {frames} frames" if frames else "")
    if nested:
        summary += "; frames cannot nest, so a nested subgraph is a group inside its outer frame"
    return Converted(out, summary)


def _arrow(eid: str, route: board_layout.Route, now: float, vn: int) -> dict[str, Any]:
    pts = route.points
    x0, y0 = pts[0]
    rel = [[px - x0, py - y0] for px, py in pts]
    xs, ys = [p[0] for p in rel], [p[1] for p in rel]
    arrow = board._base("arrow", eid, x0, y0, max(xs) - min(xs), max(ys) - min(ys), now, vn)
    arrow["points"] = rel
    arrow["elbowed"] = True
    return arrow


# ----------------------------------------------------------------------------------- arrange


def arrange(
    elements: list[dict[str, Any]], params: dict[str, Any], *, now: float, new_nonce: Callable[[], int]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Lay out a frame's shapes in layers by the arrows between them, size the frame to fit and route
    the arrows as elbows. Only moves, resizes and reroutes: ids and text stay."""
    fid = params.get("id")
    if not isinstance(fid, str):
        raise Reject("unknown id")
    frame = board._find(elements, fid)
    if frame is None:
        raise Reject("unknown id")
    if frame["type"] != "frame":
        raise Reject("arrange takes a frame id")
    live = [e for e in elements if not e.get("isDeleted")]
    members = [
        e
        for e in live
        if e.get("frameId") == fid
        and (e["type"] in board.BOX_SHAPES or (e["type"] == "text" and not e.get("containerId")))
    ]
    if not members:
        raise Reject("that frame has no shapes")
    by_id = {e["id"]: e for e in live}
    inside = {e["id"] for e in members}
    texts: dict[str, dict[str, Any]] = {
        e["containerId"]: e for e in live if e["type"] == "text" and e.get("containerId") in by_id
    }
    arrows: list[tuple[dict[str, Any], str, str]] = []
    for a in live:
        if a["type"] != "arrow":
            continue
        s = (a.get("startBinding") or {}).get("elementId")
        d = (a.get("endBinding") or {}).get("elementId")
        if (
            isinstance(s, str)
            and isinstance(d, str)
            and s in by_id
            and d in by_id
            and (s in inside or d in inside)
        ):
            if by_id[s]["type"] in ("frame", "arrow") or by_id[d]["type"] in ("frame", "arrow"):
                continue
            arrows.append((a, s, d))
    for e in members:
        carried = (1 if e["id"] in texts else 0) + sum(
            1
            for a in live
            if a["type"] == "arrow"
            and e["id"]
            in ((a.get("startBinding") or {}).get("elementId"), (a.get("endBinding") or {}).get("elementId"))
        )
        if carried > MAX_BOUND:
            name = texts[e["id"]]["text"] if e["id"] in texts else e["id"]
            raise Reject(
                f'"{name}" carries {carried} bound elements (its label and its arrows) and a shape holds '
                f"{MAX_BOUND}: split its links across two shapes"
            )
    nodes: list[board_layout.Node] = [Item(e["id"], e["width"], e["height"]) for e in members]
    sizes: dict[int, tuple[float, float]] = {}
    links = []
    for i, (a, s, d) in enumerate(arrows):
        label = texts.get(a["id"])
        if label is not None:
            sizes[i] = (max(20.0, label["width"]), max(10.0, label["height"]))
        links.append(Link(s, d, sizes.get(i)))
    placed = board_layout.place(nodes, [ln for ln in links if ln.src in inside and ln.dst in inside], "LR")
    pad = 40.0
    right = max(x + w for x, _y, w, _h in placed.values())
    bottom = max(y + h for _x, y, _w, h in placed.values())
    new_w, new_h = max(200.0, right + 2 * pad), max(120.0, bottom + 2 * pad)
    ox, oy = frame["x"] + pad, frame["y"] + pad
    boxes: dict[str, board_layout.Rect] = {k: (ox + x, oy + y, w, h) for k, (x, y, w, h) in placed.items()}
    obstacles = dict(boxes)
    for _a, s, d in arrows:
        for k in (s, d):
            if k not in boxes:
                e = by_id[k]
                boxes[k] = (e["x"], e["y"], e["width"], e["height"])
    for e in live:
        if e["type"] in board.BOX_SHAPES and e["id"] not in boxes and e["id"] != fid:
            obstacles[e["id"]] = (e["x"], e["y"], e["width"], e["height"])
    for k in boxes:
        obstacles.setdefault(k, boxes[k])
    routes = board_layout.route(boxes, obstacles, links, horizontal=True)
    out: list[dict[str, Any]] = [board._bump(frame, now, new_nonce(), width=new_w, height=new_h)]
    for e in members:
        x, y, _w, _h = boxes[e["id"]]
        dx, dy = x - e["x"], y - e["y"]
        out.append(board._bump(e, now, new_nonce(), x=x, y=y))
        t = texts.get(e["id"])
        if t is not None and e["type"] != "text":
            out.append(board._bump(t, now, new_nonce(), x=t["x"] + dx, y=t["y"] + dy))
    for i, (a, _s, _d) in enumerate(arrows):
        route = routes.get(i)
        if route is None:
            continue
        arrow = _arrow(a["id"], route, now, 0)
        change: dict[str, Any] = {k: arrow[k] for k in ("x", "y", "width", "height", "points", "elbowed")}
        change["roundness"] = None
        for end, at in (("startBinding", route.src_at), ("endBinding", route.dst_at)):
            change[end] = {**(a.get(end) or {}), "fixedPoint": list(at)}
        out.append(board._bump(a, now, new_nonce(), **change))
        label = texts.get(a["id"])
        if label is not None and route.label is not None:
            out.append(board._bump(label, now, new_nonce(), x=route.label[0], y=route.label[1]))
    if len(out) > MAX_DIAGRAM:
        raise Reject(
            f"this frame would change {len(out)} elements and arrange changes {MAX_DIAGRAM} at most: "
            "split the frame in two"
        )
    return out, [fid]
