"""A room's whiteboard: Excalidraw elements, the merge, and what an agent may draw (DESIGN.md §56).

Pure. The rows live in ``store.py`` and the broker applies them (``broker/room_board.py``). The
merge is Excalidraw's own: per element, the higher ``version`` wins, a tie goes to the lower
``versionNonce``, and a delete is a tombstone that stays in the list. Shapes from outside are a
claim: known fields only, text cut, coordinates clamped, ``link`` forced null, and an image,
embed or frame-of-a-page never stored.
"""

from __future__ import annotations

import json
import math
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from switchboard import board_checks, board_layout, board_style
from switchboard.board_style import DEFAULT_STROKE, nearest_name
from switchboard.envelope import clean

# The spike's caps (#622, question 7). Text is cut for everyone, so what an agent reads back
# stays bounded; a person drawing by hand meets the same cut.
MAX_TEXT = 200
MAX_CALL = 40  # shapes one agent call may add or change
# Elements one Mermaid diagram, or one arranged frame, may make or change in a single change. The
# maintainer's yes, 2026-11-13: "Raise how many shapes one Mermaid diagram can make, from 40 to 150? ... Yes?"
# "Yes". The per-call cap of the shape actions stays MAX_CALL. `arrange` may change as many: the maintainer's
# "I'll go with your recommendations", 2026-11-13, to "`arrange` moving up to 150 shapes at once ... I
# recommend yes."
MAX_DIAGRAM = 150
MAX_ELEMENTS = 1500  # live and deleted, together
MAX_BYTES = 1_000_000
MAX_CHANGES = 200  # change records kept per room (who, when, which ids)
MAX_POINTS = 500
MAX_SHAPE_BYTES = 32_000
COORD = 20_000
DIM_MAX = 10_000
NOT_PLACED = 1_000_000_000.0  # the tool's default for x and y: place it, don't use this number

AGENT_TYPES = frozenset({"rectangle", "ellipse", "diamond", "arrow", "text", "frame"})
# A person's page can also send a line or a pen stroke. Anything else, including an image, fails closed.
PERSON_TYPES = AGENT_TYPES | frozenset({"line", "freedraw"})
NEVER_TYPES = frozenset({"image", "embeddable", "iframe", "magicframe"})
ACTIONS = (
    "note",
    "box",
    "connect",
    "frame",
    "move",
    "edit",
    "delete",
    "read",
    "draw_mermaid",
    "arrange",
    "resize",
    "text",
)
BOX_SHAPES = frozenset({"rectangle", "ellipse", "diamond"})
_FILL = frozenset({"hachure", "cross-hatch", "solid", "zigzag"})
_STROKE = frozenset({"solid", "dashed", "dotted"})
_ALIGN = frozenset({"left", "center", "right"})
_VALIGN = frozenset({"top", "middle", "bottom"})
_ARROWHEADS = frozenset({"arrow", "bar", "dot", "triangle", "diamond", "triangle_outline"})
_BOUND = frozenset({"arrow", "text"})
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_INDEX = re.compile(r"^[A-Za-z0-9]{1,120}$")


class Reject(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class MergeResult:
    elements: tuple[dict[str, Any], ...]
    taken: tuple[dict[str, Any], ...]
    rejected: tuple[str, ...]


@dataclass(frozen=True)
class ActionResult:
    """What one agent action does to the current elements, before the merge.

    ``incoming`` is the elements to merge (new ones, or the old ones with a bumped version).
    ``error`` set means nothing is merged.
    """

    incoming: tuple[dict[str, Any], ...] = ()
    error: str = ""
    summary: str = ""
    added: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def mint(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(6)}"


def nonce() -> int:
    return secrets.randbits(31)


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    n = float(v)
    if not math.isfinite(n):
        return None
    return n


def _clamp(v: Any, lo: float, hi: float) -> float | None:
    n = _finite(v)
    if n is None:
        return None
    return max(lo, min(hi, n))


def _iint(v: Any, lo: int, hi: int) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    if v < lo or v > hi:
        return None
    return v


def _text(v: Any) -> str:
    if not isinstance(v, str):
        return ""
    return clean(v)[:MAX_TEXT]


def _ident(v: Any) -> str | None:
    if isinstance(v, str) and _ID.fullmatch(v):
        return v
    return None


def _color(v: Any) -> str | None:
    if v == "transparent":
        return v
    if isinstance(v, str) and _COLOR.fullmatch(v):
        return v
    return None


def _paint(el: dict[str, Any]) -> str:
    """`` fill=<name> stroke=<name>`` for the colours that are not the defaults, else empty."""
    out = ""
    bg = el.get("backgroundColor")
    if bg not in (None, "transparent", "#ffffff"):
        out += f" fill={nearest_name(bg, 'fill')}"
    stroke = el.get("strokeColor")
    if stroke not in (None, DEFAULT_STROKE):
        out += f" stroke={nearest_name(stroke, 'stroke')}"
    return out


def _choice(v: Any, allowed: frozenset[str]) -> str | None:
    if isinstance(v, str) and v in allowed:
        return v
    return None


def _points(raw: Any) -> list[list[float]]:
    if not isinstance(raw, list) or len(raw) > MAX_POINTS:
        raise Reject("oversized")
    out: list[list[float]] = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise Reject("bad points")
        x, y = _clamp(p[0], -COORD, COORD), _clamp(p[1], -COORD, COORD)
        if x is None or y is None:
            raise Reject("bad points")
        out.append([x, y])
    return out


def _binding(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    eid = _ident(raw.get("elementId"))
    if eid is None:
        return None
    focus = _clamp(raw.get("focus", 0), -1, 1)
    gap = _clamp(raw.get("gap", 0), 0, 100)
    if focus is None or gap is None:
        return None
    out: dict[str, Any] = {"elementId": eid, "focus": focus, "gap": gap}
    # An elbow arrow's end sits at a fraction of the box it is bound to (Excalidraw's `fixedPoint`): exactly
    # two finite numbers from 0 to 1, or nothing. The maintainer's yes, 2026-11-13, "I'll go with your
    # recommendations", to "Elbow arrows following shapes you drag ... means letting the board store one
    # more arrow field and rebuilding the board page. I recommend yes." `board-rules.js` has the same rule.
    fixed = raw.get("fixedPoint")
    if isinstance(fixed, (list, tuple)) and len(fixed) == 2:
        fx, fy = _finite(fixed[0]), _finite(fixed[1])
        if fx is not None and fy is not None and 0 <= fx <= 1 and 0 <= fy <= 1:
            out["fixedPoint"] = [fx, fy]
    return out


def _bound_list(raw: Any) -> list[dict[str, str]] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_CALL:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        bid, kind = _ident(item.get("id")), _choice(item.get("type"), _BOUND)
        if bid is not None and kind is not None:
            out.append({"id": bid, "type": kind})
    return out


def clean_element(raw: Any, *, agent: bool) -> dict[str, Any]:
    """One element as it may be stored. Raises ``Reject`` when it must not be stored at all."""
    if not isinstance(raw, dict):
        raise Reject("not an element")
    eid = _ident(raw.get("id"))
    if eid is None:
        raise Reject("bad id")
    kind = raw.get("type")
    if not isinstance(kind, str) or kind in NEVER_TYPES:
        raise Reject("image" if kind in NEVER_TYPES else "unknown type")
    allowed = AGENT_TYPES if agent else PERSON_TYPES
    if kind not in allowed:
        raise Reject("agent type" if agent else "unknown type")
    x, y = _clamp(raw.get("x"), -COORD, COORD), _clamp(raw.get("y"), -COORD, COORD)
    w, h = _clamp(raw.get("width"), 0, DIM_MAX), _clamp(raw.get("height"), 0, DIM_MAX)
    if None in (x, y, w, h):
        raise Reject("bad geometry")
    version = _iint(raw.get("version"), 0, 1_000_000_000)
    vn = _iint(raw.get("versionNonce"), -(2**31), 2**31 - 1)
    if version is None or vn is None:
        raise Reject("bad version")
    deleted = raw.get("isDeleted", False)
    if not isinstance(deleted, bool):
        raise Reject("bad version")
    el: dict[str, Any] = {
        "id": eid,
        "type": kind,
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "version": version,
        "versionNonce": vn,
        "isDeleted": deleted,
        "link": None,  # no clickable link, from anyone
    }
    angle = _clamp(raw.get("angle", 0), -100, 100)
    if angle is not None:
        el["angle"] = angle
    for key in ("strokeColor", "backgroundColor"):
        color = _color(raw.get(key))
        if color is not None:
            el[key] = color
    fill, stroke = _choice(raw.get("fillStyle"), _FILL), _choice(raw.get("strokeStyle"), _STROKE)
    if fill is not None:
        el["fillStyle"] = fill
    if stroke is not None:
        el["strokeStyle"] = stroke
    width = _clamp(raw.get("strokeWidth"), 0, 20)
    if width is not None and "strokeWidth" in raw:
        el["strokeWidth"] = width
    rough = _clamp(raw.get("roughness"), 0, 3)
    if rough is not None and "roughness" in raw:
        el["roughness"] = rough
    opacity = _iint(raw.get("opacity"), 0, 100) if "opacity" in raw else None
    if opacity is not None:
        el["opacity"] = opacity
    if isinstance(raw.get("groupIds"), list):
        ids = [i for i in (_ident(g) for g in raw["groupIds"][:16]) if i is not None]
        el["groupIds"] = ids
    frame = raw.get("frameId")
    if frame is None or _ident(frame):
        if "frameId" in raw:
            el["frameId"] = frame if frame is None else _ident(frame)
    if isinstance(raw.get("index"), str) and _INDEX.fullmatch(raw["index"]):
        el["index"] = raw["index"]
    rnd = raw.get("roundness")
    if isinstance(rnd, dict) and _iint(rnd.get("type"), 1, 3) is not None:
        el["roundness"] = {"type": rnd["type"]}
    seed = _iint(raw.get("seed"), 0, 2**31 - 1)
    if seed is not None:
        el["seed"] = seed
    bounds = _bound_list(raw.get("boundElements"))
    if bounds is not None:
        el["boundElements"] = bounds
    updated = _iint(raw.get("updated"), 0, 10**15)
    if updated is not None:
        el["updated"] = updated
    if isinstance(raw.get("locked"), bool):
        el["locked"] = raw["locked"]
    for key in ("text", "originalText", "name"):
        if isinstance(raw.get(key), str):
            el[key] = _text(raw[key])
    size = _iint(raw.get("fontSize"), 8, 48)
    if size is not None:
        el["fontSize"] = size
    family = _iint(raw.get("fontFamily"), 1, 3)
    if family is not None:
        el["fontFamily"] = family
    align, valign = _choice(raw.get("textAlign"), _ALIGN), _choice(raw.get("verticalAlign"), _VALIGN)
    if align is not None:
        el["textAlign"] = align
    if valign is not None:
        el["verticalAlign"] = valign
    container = raw.get("containerId")
    if container is None and "containerId" in raw:
        el["containerId"] = None
    elif _ident(container):
        el["containerId"] = container
    if isinstance(raw.get("autoResize"), bool):
        el["autoResize"] = raw["autoResize"]
    lh = _clamp(raw.get("lineHeight"), 0.5, 3)
    if lh is not None and "lineHeight" in raw:
        el["lineHeight"] = lh
    if kind in ("arrow", "line", "freedraw"):
        el["points"] = _points(raw.get("points"))
    if kind == "freedraw" and isinstance(raw.get("pressures"), list):
        if len(raw["pressures"]) > MAX_POINTS:
            raise Reject("oversized")
        pressures = []
        for p in raw["pressures"]:
            n = _clamp(p, 0, 1)
            if n is None:
                raise Reject("bad points")
            pressures.append(n)
        el["pressures"] = pressures
    if isinstance(raw.get("simulatePressure"), bool):
        el["simulatePressure"] = raw["simulatePressure"]
    for key in ("startBinding", "endBinding"):
        if key in raw and raw[key] is not None:
            bound = _binding(raw[key])
            if bound is not None:
                el[key] = bound
    for key in ("startArrowhead", "endArrowhead"):
        if raw.get(key) is None and key in raw:
            el[key] = None
        else:
            head = _choice(raw.get(key), _ARROWHEADS)
            if head is not None:
                el[key] = head
    if isinstance(raw.get("elbowed"), bool):
        el["elbowed"] = raw["elbowed"]
    if len(json.dumps(el, separators=(",", ":")).encode()) > MAX_SHAPE_BYTES:
        raise Reject("oversized")
    return el


def _keeps(cur: dict[str, Any], inc: dict[str, Any]) -> bool:
    """True when the stored element wins. A tie on version goes to the lower nonce, and an
    equal nonce keeps what is already stored, so the same element sent twice changes nothing."""
    if cur["version"] > inc["version"]:
        return True
    if cur["version"] == inc["version"] and cur["versionNonce"] <= inc["versionNonce"]:
        return True
    return False


def _nbytes(elements: list[dict[str, Any]]) -> int:
    return len(json.dumps(elements, separators=(",", ":"), ensure_ascii=False).encode())


def merge(
    current: list[dict[str, Any]], incoming: list[Any], *, agent: bool, cap: int | None = None
) -> MergeResult:
    """Merge ``incoming`` into ``current``. ``agent`` selects the type allowlist and the per-call cap
    (``cap`` is the larger one a whole diagram or an arranged frame gets)."""
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for e in current:
        if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"] not in by_id:
            by_id[e["id"]] = e
            order.append(e["id"])
    cap = (cap or MAX_CALL) if agent else MAX_ELEMENTS
    taken: list[dict[str, Any]] = []
    rejected: list[str] = []
    for i, raw in enumerate(incoming):
        if i >= cap:
            rejected.append("too many")
            break
        try:
            el = clean_element(raw, agent=agent)
        except Reject as e:
            rejected.append(e.reason)
            continue
        cur = by_id.get(el["id"])
        if cur is not None and _keeps(cur, el):
            continue
        if cur is None and len(by_id) >= MAX_ELEMENTS:
            rejected.append("board full")
            continue
        trial = [by_id[n] for n in order]
        if cur is None:
            trial.append(el)
        else:
            trial = [el if e["id"] == el["id"] else e for e in trial]
        if _nbytes(trial) > MAX_BYTES:
            rejected.append("board full")
            continue
        if cur is None:
            order.append(el["id"])
        by_id[el["id"]] = el
        taken.append(el)
    return MergeResult(tuple(by_id[n] for n in order), tuple(taken), tuple(rejected))


def _live(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in elements if not e.get("isDeleted")]


def _label(elements: list[dict[str, Any]], el: dict[str, Any]) -> str:
    if el["type"] == "text":
        return str(el.get("text") or "")
    for b in el.get("boundElements") or []:
        if not isinstance(b, dict):
            continue
        t = next((e for e in elements if e["id"] == b.get("id")), None)
        if t and t["type"] == "text" and t.get("containerId") == el["id"] and not t.get("isDeleted"):
            return str(t.get("text") or "")
    if el["type"] == "frame":
        return str(el.get("name") or "")
    return ""


def summary(elements: list[dict[str, Any]], seq: int) -> str:
    """The text an agent reads: frames in reading order, shapes with ids, arrows as ``a -> b``.

    The same shape the spike measured. Tombstones are left out. The broker runs the result
    through the envelope before an agent sees it, so this string is data, not instructions.
    """
    live = _live(elements)
    frames = sorted((e for e in live if e["type"] == "frame"), key=lambda e: (round(e["y"] / 100), e["x"]))
    shapes = [e for e in live if e["type"] in ("rectangle", "ellipse", "diamond")]
    loose = [e for e in live if e["type"] == "text" and not e.get("containerId")]
    arrows = [e for e in live if e["type"] == "arrow"]
    lines = [
        f"board rev {seq}: {len(frames)} frames, {len(shapes)} shapes, "
        f"{len(arrows)} arrows, {len(loose)} loose texts"
    ]
    for i, f in enumerate(frames, 1):
        lines.append(
            f'frame {i} id={f["id"]} "{f.get("name") or ""}" at ({round(f["x"])},{round(f["y"])}) '
            f"{round(f['width'])}x{round(f['height'])}"
        )
    for s in shapes:
        where = f" in {s['frameId']}" if s.get("frameId") else ""
        lines.append(
            f'{s["type"]} id={s["id"]} "{_label(elements, s)}" at ({round(s["x"])},{round(s["y"])}) '
            f"{round(s['width'])}x{round(s['height'])}{where}{_paint(s)}"
        )
    for t in loose:
        lines.append(f'text id={t["id"]} "{t.get("text") or ""}" at ({round(t["x"])},{round(t["y"])})')
    for a in arrows:
        start = (a.get("startBinding") or {}).get("elementId")
        end = (a.get("endBinding") or {}).get("elementId")
        lines.append(
            f'arrow id={a["id"]} {start or "(free)"} -> {end or "(free)"} "{_label(elements, a)}"{_paint(a)}'
        )
    problems = board_checks.warnings(live)
    if problems:
        lines.append("layout warnings:")
        lines.extend(f"- {w}" for w in problems)
    return "\n".join(lines)


def _by_id(elements: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in elements if isinstance(e, dict) and isinstance(e.get("id"), str)}


def _base(kind: str, eid: str, x: float, y: float, w: float, h: float, now: float, vn: int) -> dict[str, Any]:
    return {
        "id": eid,
        "type": kind,
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "seed": vn,
        "version": 1,
        "versionNonce": vn,
        "isDeleted": False,
        "link": None,
        "locked": False,
        "updated": int(now * 1000),
    }


def _text_el(
    eid: str, container: str, text: str, x: float, y: float, w: float, now: float, vn: int, frame: str
) -> dict[str, Any]:
    el = _base("text", eid, x, y, max(w, 20), 25, now, vn)
    el.update(
        text=text,
        originalText=text,
        fontSize=16,
        fontFamily=1,
        textAlign="center",
        verticalAlign="middle",
        containerId=container,
        autoResize=True,
        lineHeight=1.25,
    )
    if frame:
        el["frameId"] = frame
    return el


def _bump(el: dict[str, Any], now: float, vn: int, **kw: Any) -> dict[str, Any]:
    return {**el, **kw, "version": int(el["version"]) + 1, "versionNonce": vn, "updated": int(now * 1000)}


def _find(elements: list[dict[str, Any]], eid: str) -> dict[str, Any] | None:
    el = _by_id(elements).get(eid)
    if el is None or el.get("isDeleted"):
        return None
    return el


def _companions(elements: list[dict[str, Any]], el: dict[str, Any], *, arrows: bool) -> list[dict[str, Any]]:
    want = {el["id"]}
    for b in el.get("boundElements") or []:
        if isinstance(b, dict) and isinstance(b.get("id"), str):
            want.add(b["id"])
    out = []
    for e in elements:
        if e.get("isDeleted"):
            continue
        if e.get("containerId") == el["id"] or e["id"] in want:
            out.append(e)
            continue
        if not arrows or e["type"] not in ("arrow", "line"):
            continue
        ends = {
            (e.get("startBinding") or {}).get("elementId"),
            (e.get("endBinding") or {}).get("elementId"),
        }
        if el["id"] in ends:
            out.append(e)
    return out


def _free_slot(
    elements: list[dict[str, Any]], w: float, h: float, frame: dict[str, Any] | None
) -> tuple[float, float]:
    boxes = [
        (e["x"], e["y"], e["width"], e["height"])
        for e in _live(elements)
        if e["type"] not in ("arrow", "line", "text")
        and not e.get("containerId")
        and (frame is None or e["id"] != frame["id"])
    ]
    if frame is not None:
        x0, y0, cols, dx, dy = frame["x"] + 20, frame["y"] + 40, 2, 300, max(140, h + 60)
    else:
        x0, y0, cols, dx, dy = 0, 0, 4, 300, max(140, h + 60)
    for i in range(200):
        x, y = x0 + (i % cols) * dx, y0 + (i // cols) * dy
        hit = any(
            x < bx + bw + 10 and bx < x + w + 10 and y < by + bh + 10 and by < y + h + 10
            for bx, by, bw, bh in boxes
        )
        if not hit:
            return x, y
    return x0, y0


def _free_frame(elements: list[dict[str, Any]], w: float, h: float) -> tuple[float, float]:
    n = len([e for e in _live(elements) if e["type"] == "frame"])
    return (n % 3) * (w + 40), (n // 3) * (h + 40) + 400


def _need_text(params: dict[str, Any]) -> str | None:
    text = params.get("text", "")
    if not isinstance(text, str):
        return None
    return _text(text).strip()


def _coord(params: dict[str, Any], key: str) -> float | None:
    """A placed coordinate, or None when the caller left it out. A bad value raises."""
    if key not in params:
        return None
    v = params[key]
    if v == NOT_PLACED:
        return None
    n = _finite(v)
    if n is None:
        raise Reject("bad geometry")
    return n


def apply_action(
    elements: list[dict[str, Any]],
    action: str,
    params: dict[str, Any],
    *,
    now: float,
    new_id: Callable[[str], str] = mint,
    new_nonce: Callable[[], int] = nonce,
) -> ActionResult:
    """The elements one action wants merged. Unknown actions fail closed."""
    if action not in ACTIONS:
        return ActionResult(error="unknown action")
    if action == "read":
        return ActionResult(summary=summary(elements, 0))
    try:
        incoming, added = _act(elements, action, params, now=now, new_id=new_id, new_nonce=new_nonce)
    except Reject as e:
        return ActionResult(error=e.reason)
    touched = {e["id"] for e in incoming}
    merged = {**_by_id(elements), **{e["id"]: e for e in incoming}}
    problems = board_checks.warnings(list(merged.values()), only=touched)
    return ActionResult(incoming=tuple(incoming), added=tuple(added), warnings=tuple(problems))


def _style(params: dict[str, Any], target: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The element fields and label fields of a call's ``style``, or nothing when it has none."""
    if "style" not in params or params["style"] == "":
        return {}, {}
    try:
        return board_style.fields(board_style.parse_style(params["style"]), target)
    except board_style.StyleError as e:
        raise Reject(e.reason) from e


def _size(params: dict[str, Any], key: str) -> float | None:
    """A requested width or height, or None when left out. A bad value raises."""
    if key not in params or params[key] in (0, 0.0, None, ""):
        return None
    n = _finite(params[key])
    if n is None or not 20 <= n <= 3000:
        raise Reject(f"{key} must be a number from 20 to 3000")
    return n


def _act(
    elements: list[dict[str, Any]],
    action: str,
    params: dict[str, Any],
    *,
    now: float,
    new_id: Callable[[str], str],
    new_nonce: Callable[[], int],
) -> tuple[list[dict[str, Any]], list[str]]:
    if action == "note":
        return _add_box(elements, params, "rectangle", "#ffec99", True, now, new_id, new_nonce)
    if action == "box":
        shape = params.get("shape") or "rectangle"
        if not isinstance(shape, str) or shape not in BOX_SHAPES:
            raise Reject("unknown shape")
        return _add_box(elements, params, shape, "#ffffff", False, now, new_id, new_nonce)
    if action == "frame":
        return _add_frame(elements, params, now, new_id, new_nonce)
    if action == "connect":
        return _connect(elements, params, now, new_id, new_nonce)
    if action == "arrange":
        from switchboard import board_flow

        return board_flow.arrange(elements, params, now=now, new_nonce=new_nonce)
    if action == "move":
        return _move(elements, params, now, new_nonce)
    if action == "edit":
        return _edit(elements, params, now, new_nonce)
    if action == "resize":
        return _resize(elements, params, now, new_nonce)
    if action == "text":
        return _add_text(elements, params, now, new_id, new_nonce)
    if action == "delete":
        return _delete(elements, params, now, new_nonce)
    raise Reject("unknown action")


def _frame_for(elements: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any] | None:
    frame_id = params.get("to") or ""
    if not isinstance(frame_id, str):
        raise Reject("unknown frame")
    frame = _find(elements, frame_id) if frame_id else None
    if frame_id and (frame is None or frame["type"] != "frame"):
        raise Reject("unknown frame")
    return frame


def _grow_frame(
    frame: dict[str, Any] | None, x: float, y: float, w: float, h: float, now: float, vn: int
) -> list[dict[str, Any]]:
    """The frame, bumped and enlarged (in any direction) so a shape at (x, y, w, h) sits inside it."""
    if frame is None:
        return []
    left, top = min(frame["x"], x - 20), min(frame["y"], y - 20)
    right = max(frame["x"] + frame["width"], x + w + 20)
    bottom = max(frame["y"] + frame["height"], y + h + 20)
    if (left, top, right - left, bottom - top) == (frame["x"], frame["y"], frame["width"], frame["height"]):
        return []
    return [
        _bump(
            frame,
            now,
            vn,
            x=max(left, -COORD),
            y=max(top, -COORD),
            width=min(right - left, DIM_MAX),
            height=min(bottom - top, DIM_MAX),
        )
    ]


def _place_label(shape: dict[str, Any], label: dict[str, Any], text: str, fs: float) -> dict[str, Any]:
    """The label's box: as wide as the shape's inside, as tall as its wrapped text, centred."""
    width, th = board_checks.label_box(shape, text, fs)
    return {
        **label,
        "x": shape["x"] + (shape["width"] - width) / 2,
        "y": shape["y"] + (shape["height"] - th) / 2,
        "width": width,
        "height": th,
    }


def _add_box(
    elements: list[dict[str, Any]],
    params: dict[str, Any],
    kind: str,
    fill: str,
    require_text: bool,
    now: float,
    new_id: Callable[[str], str],
    new_nonce: Callable[[], int],
) -> tuple[list[dict[str, Any]], list[str]]:
    text = _need_text(params)
    if text is None:
        raise Reject("bad text")
    if require_text and not text:
        raise Reject("text is required")
    frame = _frame_for(elements, params)
    style, label_style = _style(params, "shape")
    asked_w, asked_h = _size(params, "w"), _size(params, "h")
    if label_style and not text:
        raise Reject("that shape has no text")
    fs = float(label_style.get("fontSize", 16))
    shape_kind = board_checks.KINDS[kind]
    fitted = board_checks.fit(text, shape_kind, fs) if text else None
    w = asked_w or (fitted.w if fitted else 160.0)
    h = asked_h or (board_checks.height_for(shape_kind, w, text, fs) if text else 80.0)
    x, y = _coord(params, "x"), _coord(params, "y")
    if x is None or y is None:
        x, y = _free_slot(elements, w, h, frame)
    sid, tid = new_id("n" if require_text else "b"), new_id("t")
    vn = new_nonce()
    shape = _base(kind, sid, x, y, w, h, now, vn)
    shape["backgroundColor"] = fill
    shape["roundness"] = {"type": 3}
    shape.update(style)
    if shape.get("roundness") is None:
        del shape["roundness"]
    if frame is not None:
        shape["frameId"] = frame["id"]
    out: list[dict[str, Any]] = [shape]
    added = [sid]
    if text:
        label = _text_el(
            tid, sid, text, x + 10, y + 28, w - 20, now, new_nonce(), frame["id"] if frame else ""
        )
        label.update(label_style)
        label = _place_label(shape, label, text, fs)
        shape["boundElements"] = [{"id": tid, "type": "text"}]
        out.append(label)
        added.append(tid)
    out.extend(_grow_frame(frame, x, y, w, h, now, new_nonce()))
    return out, added


def _loose_size(text: str, fs: float) -> tuple[float, float]:
    """The box of a text with no shape around it: one line up to 520 wide, then wrapped."""
    return board_checks.loose_size(text, fs)


def _add_text(
    elements: list[dict[str, Any]],
    params: dict[str, Any],
    now: float,
    new_id: Callable[[str], str],
    new_nonce: Callable[[], int],
) -> tuple[list[dict[str, Any]], list[str]]:
    """A heading or caption with no box."""
    text = _need_text(params)
    if not text:
        raise Reject("text is required")
    frame = _frame_for(elements, params)
    _, own = _style(params, "text")  # for a loose text, every key lands on the text itself
    fs = float(own.get("fontSize", 20))
    own.setdefault("fontSize", 20)
    w, h = _loose_size(text, fs)
    x, y = _coord(params, "x"), _coord(params, "y")
    if x is None or y is None:
        x, y = _free_slot(elements, w, h, frame)
    tid = new_id("t")
    el = _text_el(tid, "", text, x, y, w, now, new_nonce(), frame["id"] if frame else "")
    el.update(own)
    el.update(height=h, containerId=None, textAlign=own.get("textAlign", "left"), verticalAlign="top")
    out = [el, *_grow_frame(frame, x, y, w, h, now, new_nonce())]
    return out, [tid]


def _add_frame(
    elements: list[dict[str, Any]],
    params: dict[str, Any],
    now: float,
    new_id: Callable[[str], str],
    new_nonce: Callable[[], int],
) -> tuple[list[dict[str, Any]], list[str]]:
    title = _need_text(params)
    if title is None:
        raise Reject("bad text")
    _style(params, "frame")
    x, y = _coord(params, "x"), _coord(params, "y")
    w, h = _size(params, "w") or 600, _size(params, "h") or 360
    if x is None or y is None:
        x, y = _free_frame(elements, w, h)
    eid = new_id("f")
    el = _base("frame", eid, x, y, w, h, now, new_nonce())
    el["name"] = title
    return [el], [eid]


def _elbow(elements: list[dict[str, Any]], src: dict[str, Any], dst: dict[str, Any]) -> Any:
    """The router's elbow between two shapes, kept clear of every other shape on the board, or None."""
    if src["id"] == dst["id"]:
        return None

    def rect(e: dict[str, Any]) -> board_layout.Rect:
        return e["x"], e["y"], e["width"], e["height"]

    boxes = {src["id"]: rect(src), dst["id"]: rect(dst)}
    obstacles = {e["id"]: rect(e) for e in _live(elements) if e["type"] in BOX_SHAPES}
    obstacles.update(boxes)
    horizontal = abs(dst["x"] - src["x"]) >= abs(dst["y"] - src["y"])
    got = board_layout.route(
        boxes, obstacles, [board_layout.Link(src["id"], dst["id"])], horizontal=horizontal
    )
    return got.get(0)


def _connect(
    elements: list[dict[str, Any]],
    params: dict[str, Any],
    now: float,
    new_id: Callable[[str], str],
    new_nonce: Callable[[], int],
) -> tuple[list[dict[str, Any]], list[str]]:
    a, b = params.get("id"), params.get("to")
    if not isinstance(a, str) or not isinstance(b, str):
        raise Reject("unknown id")
    src, dst = _find(elements, a), _find(elements, b)
    if src is None or dst is None or src["type"] == "arrow" or dst["type"] == "arrow":
        raise Reject("unknown id")
    label = _need_text(params)
    if label is None:
        raise Reject("bad text")
    style, label_style = _style(params, "arrow")
    if label_style and not label:
        raise Reject("that arrow has no text")
    x = src["x"] + src["width"]
    y = src["y"] + src["height"] / 2
    dx = (dst["x"] + dst["width"] / 2) - x
    dy = (dst["y"] + dst["height"] / 2) - y
    routed = _elbow(elements, src, dst) if style.get("elbowed") else None
    eid = new_id("a")
    vn = new_nonce()
    if routed is not None:
        # The same router ``arrange`` and Mermaid conversion use: right-angled points round every other
        # shape, and a ``fixedPoint`` on each end, which is how Excalidraw keeps an elbow attached.
        x, y = routed.points[0]
        rel = [[px - x, py - y] for px, py in routed.points]
        arrow = _base(
            "arrow",
            eid,
            x,
            y,
            max(p[0] for p in rel) - min(p[0] for p in rel),
            max(p[1] for p in rel) - min(p[1] for p in rel),
            now,
            vn,
        )
        arrow["points"] = rel
        arrow["startBinding"] = {
            "elementId": src["id"],
            "focus": 0.0,
            "gap": 0.0,
            "fixedPoint": list(routed.src_at),
        }
        arrow["endBinding"] = {
            "elementId": dst["id"],
            "focus": 0.0,
            "gap": 0.0,
            "fixedPoint": list(routed.dst_at),
        }
    else:
        arrow = _base("arrow", eid, x, y, abs(dx), abs(dy), now, vn)
        arrow["points"] = [[0.0, 0.0], [dx, dy]]
        arrow["startBinding"] = {"elementId": src["id"], "focus": 0.0, "gap": 8.0}
        arrow["endBinding"] = {"elementId": dst["id"], "focus": 0.0, "gap": 8.0}
    arrow["endArrowhead"] = "arrow"
    arrow.update(style)
    if arrow.get("roundness") is None:
        arrow.pop("roundness", None)
    out = [arrow]
    if routed is not None:
        # Excalidraw moves an elbow with a shape only when the shape lists the arrow (a Mermaid
        # diagram's shapes do), so the two ends are re-sent with it.
        for end_shape in {src["id"]: src, dst["id"]: dst}.values():
            listed = [*(end_shape.get("boundElements") or []), {"id": eid, "type": "arrow"}]
            out.append(_bump(end_shape, now, new_nonce(), boundElements=listed))
    added = [eid]
    if label:
        tid = new_id("t")
        text = _text_el(tid, eid, label, x, y, 80, now, new_nonce(), "")
        text.update(label_style)
        arrow["boundElements"] = [{"id": tid, "type": "text"}]
        out.append(text)
        added.append(tid)
    return out, added


def _move(
    elements: list[dict[str, Any]], params: dict[str, Any], now: float, new_nonce: Callable[[], int]
) -> tuple[list[dict[str, Any]], list[str]]:
    eid = params.get("id")
    if not isinstance(eid, str):
        raise Reject("unknown id")
    el = _find(elements, eid)
    if el is None or el["type"] in ("arrow", "line", "text"):
        raise Reject("unknown id")
    x, y = _coord(params, "x"), _coord(params, "y")
    if x is None or y is None:
        raise Reject("x and y are required")
    dx, dy = x - el["x"], y - el["y"]
    out = [_bump(el, now, new_nonce(), x=x, y=y)]
    for c in _companions(elements, el, arrows=False):
        if c["id"] == el["id"]:
            continue
        if c["type"] == "text":
            out.append(_bump(c, now, new_nonce(), x=c["x"] + dx, y=c["y"] + dy))
    return out, [el["id"]]


def _label_el(elements: list[dict[str, Any]], el: dict[str, Any]) -> dict[str, Any] | None:
    for c in _companions(elements, el, arrows=False):
        if c["type"] == "text" and c.get("containerId") == el["id"]:
            return c
    return None


def _arrows_of(elements: list[dict[str, Any]], el: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in _companions(elements, el, arrows=True) if c["type"] == "arrow"]


def _edit(
    elements: list[dict[str, Any]], params: dict[str, Any], now: float, new_nonce: Callable[[], int]
) -> tuple[list[dict[str, Any]], list[str]]:
    """New words, a new style, or both. A shape grows in height when its new words need it."""
    eid = params.get("id")
    if not isinstance(eid, str):
        raise Reject("unknown id")
    el = _find(elements, eid)
    if el is None:
        raise Reject("unknown id")
    text = _need_text(params)
    has_style = bool(params.get("style"))
    if text is None or (not text and not has_style):
        raise Reject("text or style is required")
    target = {"frame": "frame", "text": "text", "arrow": "arrow"}.get(el["type"], "shape")
    style, label_style = _style(params, target)
    if el["type"] == "frame":
        return [_bump(el, now, new_nonce(), **({"name": text} if text else {}))], [el["id"]]
    if el["type"] == "text":
        change = {**style, **label_style, **({"text": text, "originalText": text} if text else {})}
        words = text or _text_of(el)
        fs = float(change.get("fontSize") or el.get("fontSize") or 20)
        if text or "fontSize" in change:  # the box must match the words, as when it was drawn
            change["width"], change["height"] = _loose_size(words, fs)
        return [_bump(el, now, new_nonce(), **change)], [el["id"]]
    label = _label_el(elements, el)
    if (text or label_style) and label is None:
        raise Reject("that shape has no text")
    out: list[dict[str, Any]] = []
    shape = el
    if style:
        shape = _bump(el, now, new_nonce(), **style)
        if "roundness" in style and style["roundness"] is None:
            shape.pop("roundness", None)
    if label is not None and (text or label_style):
        words = text or _text_of(label)
        new = _bump(label, now, new_nonce(), **label_style, text=words, originalText=words)
        if el["type"] != "arrow":
            fs = float(new.get("fontSize") or 16)
            need = board_checks.height_for(board_checks.KINDS[el["type"]], shape["width"], words, fs)
            if need > shape["height"]:
                shape = _bump(shape, now, new_nonce(), height=need)
            new = _place_label(shape, new, words, fs)
        out.append(new)
    if shape is not el:
        out.insert(0, shape)
        if shape["height"] != el["height"]:
            out.extend(_bump(a, now, new_nonce()) for a in _arrows_of(elements, el))
    return out, [el["id"]]


def _text_of(label: dict[str, Any]) -> str:
    return str(label.get("originalText") or label.get("text") or "")


def _resize(
    elements: list[dict[str, Any]], params: dict[str, Any], now: float, new_nonce: Callable[[], int]
) -> tuple[list[dict[str, Any]], list[str]]:
    """New ``w`` and ``h``. The label stays centred and bound arrows are re-sent so they re-attach."""
    eid = params.get("id")
    if not isinstance(eid, str):
        raise Reject("unknown id")
    el = _find(elements, eid)
    if el is None or el["type"] not in (*BOX_SHAPES, "frame"):
        raise Reject("unknown id")
    w, h = _size(params, "w"), _size(params, "h")
    if w is None and h is None:
        raise Reject("w or h is required")
    shape = _bump(el, now, new_nonce(), width=w or el["width"], height=h or el["height"])
    out = [shape]
    label = _label_el(elements, el)
    if label is not None:
        words = _text_of(label)
        out.append(
            _bump(_place_label(shape, label, words, float(label.get("fontSize") or 16)), now, new_nonce())
        )
    out.extend(_bump(a, now, new_nonce()) for a in _arrows_of(elements, el))
    return out, [el["id"]]


def _delete(
    elements: list[dict[str, Any]], params: dict[str, Any], now: float, new_nonce: Callable[[], int]
) -> tuple[list[dict[str, Any]], list[str]]:
    eid = params.get("id")
    if not isinstance(eid, str):
        raise Reject("unknown id")
    el = _find(elements, eid)
    if el is None:
        raise Reject("unknown id")
    out = [_bump(c, now, new_nonce(), isDeleted=True) for c in _companions(elements, el, arrows=True)]
    return out, [el["id"]]


def mermaid_source(value: Any) -> str:
    """Only bounded fresh flows (§56.9): the source with its click lines removed, or a ``Reject`` for
    anything the board does not draw. This is the guard in front of the converter, which reads the text
    as written, so `A & B` and `#quot;` keep their meaning."""
    if not isinstance(value, str) or len(value) > 8000 or len(value.encode(errors="replace")) > 16000:
        raise Reject("Mermaid source exceeds 8000 characters or 16000 bytes")
    source = value.strip()
    if "%%{" in source or not re.match(r"^(?:flowchart|graph)\s+(?:TD|TB|BT|RL|LR)\b", source):
        raise Reject("use a flowchart or graph without configuration directives")
    source = re.sub(r"(?im)^\s*click\b[^\n]*", "", source).strip()
    if re.search(r"\bclick\b", source, re.IGNORECASE):
        raise Reject("put click directives on separate lines; they are removed")
    if re.search(r"(?im)^\s*linkStyle\b|@\s*\{|!\[", source):
        raise Reject("use plain flow shapes and labels; linkStyle and images are not drawn")
    return source.replace("\r\n", "\n").replace("\r", "\n")


def _drawing_bounds(e: dict[str, Any]) -> tuple[float, float, float, float]:
    """Include rotation and all line/pen points, even an inconsistent claimed bounding box."""
    w, h = e["width"], e["height"]
    cx, cy = e["x"] + w / 2, e["y"] + h / 2
    angle = e.get("angle", 0)
    c, s = math.cos(angle), math.sin(angle)
    points = [(0, 0), (w, 0), (w, h), (0, h), *e.get("points", [])]
    rotated = [
        (cx + c * (x - w / 2) - s * (y - h / 2), cy + s * (x - w / 2) + c * (y - h / 2)) for x, y in points
    ]
    return (
        min(x for x, y in rotated),
        min(y for x, y in rotated),
        max(x for x, y in rotated),
        max(y for x, y in rotated),
    )


def mermaid_elements(current: list[dict[str, Any]], raw: Any, *, now: float) -> list[dict[str, Any]]:
    """Validate a whole conversion, mint its ids, and place it clear of the latest live board."""
    if not isinstance(raw, list) or not 0 < len(raw) <= MAX_DIAGRAM:
        raise Reject(f"a diagram must have 1 to {MAX_DIAGRAM} elements")
    elements = [clean_element(e, agent=True) for e in raw]
    ids = {e["id"]: mint("m") for e in elements}
    if len(ids) != len(elements) or any(e["isDeleted"] for e in elements):
        raise Reject("duplicate or deleted diagram elements")
    bounds = [_drawing_bounds(e) for e in elements]
    left, top = min(b[0] for b in bounds), min(b[1] for b in bounds)
    width, height = max(b[2] for b in bounds) - left, max(b[3] for b in bounds) - top
    if max(width, height) > DIM_MAX:
        raise Reject("diagram exceeds the drawing limits")
    live = _live(current)
    x, y = 0.0, 0.0
    if live:
        occupied = [_drawing_bounds(e) for e in live]
        x = max(b[2] for b in occupied) + 80
        if x + width > COORD:
            x, y = 0.0, max(b[3] for b in occupied) + 80
    if x + width > COORD or y + height > COORD:
        raise Reject("no free space for this diagram")
    groups: dict[str, str] = {}

    def reference(value: str) -> str:
        if value not in ids:
            raise Reject("diagram refers to a shape outside itself")
        return ids[value]

    for e in elements:
        e.update(
            id=ids[e["id"]],
            x=e["x"] - left + x,
            y=e["y"] - top + y,
            version=1,
            versionNonce=nonce(),
            updated=int(now * 1000),
        )
        for key in ("frameId", "containerId"):
            if e.get(key):
                e[key] = reference(e[key])
        for key in ("startBinding", "endBinding"):
            if e.get(key):
                e[key]["elementId"] = reference(e[key]["elementId"])
        for bound in e.get("boundElements") or []:
            bound["id"] = reference(bound["id"])
        e["groupIds"] = [groups.setdefault(g, mint("g")) for g in e.get("groupIds", [])]
    if _nbytes(elements) > MAX_BYTES:
        raise Reject("diagram exceeds the board byte limit")
    return elements
