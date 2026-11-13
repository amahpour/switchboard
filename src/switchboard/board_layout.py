"""Place boxes in layers and route elbow arrows between them (DESIGN.md §56.9).

Pure geometry: no board elements, no I/O, deterministic for the same input. ``place`` is a layered
(Sugiyama-style) layout: cycles are broken for ranking only, ranks are the longest path, a few
barycenter sweeps cut crossings, and a group (a subgraph, or an arranged frame) is laid out as a block
and placed as one box in its parent, so a group never overlaps what is outside it. ``route`` draws each
link as an orthogonal path on a grid of the free corridors between the boxes, so no arrow crosses a box
that is not one of its ends, and places each label clear of the others. ``board_flow`` turns a Mermaid
flowchart into elements with it, and ``arrange`` re-places an existing frame with it.
"""

from __future__ import annotations

import heapq
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Union

Rect = tuple[float, float, float, float]  # x, y, width, height
Point = tuple[float, float]

NODE_GAP = 60.0  # between two boxes in one layer
DUMMY_GAP = 30.0  # between a box and the room kept for a long arrow
DUMMY_W = 10.0
RANK_GAP = 100.0  # between layers, more when many arrows cross the gap
MARGIN = 16.0  # how far an arrow stays from a box
LANE = 14.0  # spacing of the parallel lines arrows can use inside a corridor
TURN = 30.0  # what a bend costs in the route search, in pixels
SHARED = 14.0  # what an already used unit of line costs
SWEEPS = 8


@dataclass(frozen=True)
class Item:
    key: str
    w: float
    h: float
    group: str | None = None


@dataclass(frozen=True)
class Group:
    """A block of items: its frame is sized to the block plus ``pad`` on every side."""

    key: str
    group: str | None = None
    direction: str | None = None
    pad: float = 40.0
    min_w: float = 200.0
    min_h: float = 120.0


@dataclass(frozen=True)
class Link:
    src: str
    dst: str
    label: tuple[float, float] | None = None  # the label's width and height
    hidden: bool = False  # shapes layout only: no arrow is drawn


@dataclass(frozen=True)
class Route:
    points: tuple[Point, ...]
    src_at: Point  # where the arrow leaves its first box, as a fraction of that box
    dst_at: Point
    label: Point | None  # top-left of the label


Node = Union[Item, Group]


# ----------------------------------------------------------------------------------- layers


def _spread(desired: list[float], widths: list[float], gaps: list[float]) -> list[float]:
    """Left edges, in order, closest to the desired centers while each stays clear of the one before.

    Isotonic regression on the offsets (pool adjacent violators): the least total squared move."""
    n = len(desired)
    off = [0.0] * n
    for i in range(1, n):
        off[i] = off[i - 1] + widths[i - 1] + gaps[i - 1]
    target = [desired[i] - widths[i] / 2 - off[i] for i in range(n)]
    blocks: list[list[float]] = []
    for v in target:
        blocks.append([v, 1.0])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            (m2, c2), (m1, c1) = blocks.pop(), blocks.pop()
            blocks.append([(m1 * c1 + m2 * c2) / (c1 + c2), c1 + c2])
    level: list[float] = []
    for mean, count in blocks:
        level.extend([mean] * int(count))
    return [level[i] + off[i] for i in range(n)]


def _layered(
    sizes: list[tuple[float, float]], pairs: list[tuple[int, int]], label_ext: list[float] | None = None
) -> tuple[list[float], list[float], float, float]:
    """Cross and layer positions (left/top edges) of each item, and the whole extent.

    ``sizes`` are (cross, layer) extents. ``pairs`` are directed links between item indexes."""
    n = len(sizes)
    if n == 0:
        return [], [], 0.0, 0.0
    uniq: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for u, v in pairs:
        if u != v and (u, v) not in seen:
            seen.add((u, v))
            uniq.append((u, v))
    adj: list[list[int]] = [[] for _ in range(n)]
    for u, v in uniq:
        adj[u].append(v)
    state = [0] * n
    back: set[tuple[int, int]] = set()

    def visit(u: int) -> None:
        state[u] = 1
        for v in adj[u]:
            if state[v] == 0:
                visit(v)
            elif state[v] == 1:
                back.add((u, v))
        state[u] = 2

    for u in range(n):
        if state[u] == 0:
            visit(u)
    dag: list[tuple[int, int]] = []
    dseen: set[tuple[int, int]] = set()
    for u, v in uniq:
        e = (v, u) if (u, v) in back else (u, v)
        if e not in dseen:
            dseen.add(e)
            dag.append(e)
    indeg = [0] * n
    out: list[list[int]] = [[] for _ in range(n)]
    for u, v in dag:
        out[u].append(v)
        indeg[v] += 1
    rank = [0] * n
    queue = [i for i in range(n) if indeg[i] == 0]
    head = 0
    while head < len(queue):
        u = queue[head]
        head += 1
        for v in out[u]:
            rank[v] = max(rank[v], rank[u] + 1)
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    # room for the long arrows: one thin placeholder per layer they cross
    rk = list(rank)
    ab: list[tuple[float, float]] = list(sizes)
    segs: list[tuple[int, int]] = []
    for u, v in dag:
        prev = u
        for r in range(rank[u] + 1, rank[v]):
            rk.append(r)
            ab.append((DUMMY_W, 0.0))
            segs.append((prev, len(rk) - 1))
            prev = len(rk) - 1
        segs.append((prev, v))
    total = len(rk)
    top = max(rk) + 1
    layers: list[list[int]] = [[] for _ in range(top)]
    for i in range(total):
        layers[rk[i]].append(i)
    up: list[list[int]] = [[] for _ in range(total)]
    down: list[list[int]] = [[] for _ in range(total)]
    by_layer: list[list[tuple[int, int]]] = [[] for _ in range(top)]
    for u, v in segs:
        down[u].append(v)
        up[v].append(u)
        by_layer[rk[u]].append((u, v))
    pos: dict[int, int] = {}

    def index() -> None:
        for layer in layers:
            for i, x in enumerate(layer):
                pos[x] = i

    def crossings() -> int:
        index()
        count = 0
        for r in range(top - 1):
            s = [(pos[u], pos[v]) for u, v in by_layer[r]]
            for i in range(len(s)):
                for j in range(i + 1, len(s)):
                    if (s[i][0] - s[j][0]) * (s[i][1] - s[j][1]) < 0:
                        count += 1
        return count

    def reorder(r: int, nbr: list[list[int]]) -> None:
        index()
        layers[r].sort(
            key=lambda x: (sum(pos[y] for y in nbr[x]) / len(nbr[x]) if nbr[x] else pos[x], pos[x])
        )

    best = [list(layer) for layer in layers]
    best_count = crossings()
    for _ in range(SWEEPS):
        if best_count == 0:
            break
        for r in range(1, top):
            reorder(r, up)
        for r in range(top - 2, -1, -1):
            reorder(r, down)
        count = crossings()
        if count < best_count:
            best, best_count = [list(layer) for layer in layers], count
    layers = best

    def gap_after(a: int, b: int) -> float:
        return NODE_GAP if a < n and b < n else DUMMY_GAP

    cen: dict[int, float] = {}
    for layer in layers:
        x = 0.0
        for i, item in enumerate(layer):
            cen[item] = x + ab[item][0] / 2
            x += ab[item][0] + (gap_after(item, layer[i + 1]) if i + 1 < len(layer) else 0.0)

    def settle(r: int, nbr: list[list[int]]) -> None:
        layer = layers[r]
        if not layer:
            return
        desired = [sum(cen[y] for y in nbr[x]) / len(nbr[x]) if nbr[x] else cen[x] for x in layer]
        widths = [ab[x][0] for x in layer]
        gaps = [gap_after(layer[i], layer[i + 1]) for i in range(len(layer) - 1)] + [0.0]
        for x, left in zip(layer, _spread(desired, widths, gaps), strict=True):
            cen[x] = left + ab[x][0] / 2

    for _ in range(SWEEPS):
        for r in range(1, top):
            settle(r, up)
        for r in range(top - 2, -1, -1):
            settle(r, down)
    shift = min(cen[x] - ab[x][0] / 2 for x in range(total))
    for x in cen:
        cen[x] -= shift
    width = max(cen[x] + ab[x][0] / 2 for x in range(total))
    band = [max((ab[x][1] for x in layer), default=0.0) for layer in layers]
    labelled = [0.0] * (top + 1)
    for (u, v), ext in zip(pairs, label_ext or [0.0] * len(pairs), strict=False):
        if u != v and ext:
            labelled[min(rank[u], rank[v])] += ext + 6
    start: list[float] = []
    y = 0.0
    for r in range(top):
        start.append(y)
        room = 4 * MARGIN + 20 + labelled[r]  # the labels of the links that start here stack in its gap
        y += band[r] + (max(RANK_GAP, room, 40.0 + 16.0 * min(len(by_layer[r]), 40)) if r + 1 < top else 0.0)
    cross = [cen[i] - sizes[i][0] / 2 for i in range(n)]
    layer_at = [start[rank[i]] + (band[rank[i]] - sizes[i][1]) / 2 for i in range(n)]
    return cross, layer_at, width, y


# ------------------------------------------------------------------------------------ groups


def place(nodes: list[Node], links: list[Link], direction: str = "TD") -> dict[str, Rect]:
    """Every item's and group's box. ``direction`` is TD, BT, LR or RL; a group may turn its own.

    Links between two boxes in one block shape that block's layers; a link between boxes in different
    blocks shapes the layers of the smallest block holding both, between the blocks themselves."""
    by_key = {n.key: n for n in nodes}
    order = {n.key: i for i, n in enumerate(nodes)}
    children: dict[str | None, list[str]] = {None: []}
    for n in nodes:
        children.setdefault(n.group if n.group in by_key else None, []).append(n.key)
        if isinstance(n, Group):
            children.setdefault(n.key, [])

    def chain(key: str) -> list[str]:
        out = [key]
        g = by_key[key].group
        while g in by_key and g not in out:
            out.append(g)
            g = by_key[g].group
        return out[::-1]

    lifted: dict[str | None, list[tuple[str, str, tuple[float, float] | None]]] = {}
    for link in links:
        if link.src not in by_key or link.dst not in by_key:
            continue
        a, b = chain(link.src), chain(link.dst)
        k = 0
        while k < min(len(a), len(b)) and a[k] == b[k]:
            k += 1
        if k == len(a) or k == len(b):
            continue  # the same box, or a box and the block holding it: no layers to shape
        where = a[k - 1] if k else None
        lifted.setdefault(where, []).append((a[k], b[k], link.label))

    levels: dict[str | None, tuple[float, float, dict[str, Point]]] = {}

    def level(container: str | None, inherited: str) -> None:
        node = by_key.get(container) if container is not None else None
        d = (node.direction if isinstance(node, Group) and node.direction else None) or inherited
        keys = sorted(children.get(container, []), key=order.__getitem__)
        sizes: list[tuple[float, float]] = []
        for k in keys:
            n = by_key[k]
            if isinstance(n, Group):
                level(k, d)
                lw, lh, _ = levels[k]
                sizes.append((max(n.min_w, lw + 2 * n.pad), max(n.min_h, lh + 2 * n.pad)))
            else:
                sizes.append((n.w, n.h))
        horizontal = d in ("LR", "RL")
        canon = [(h, w) if horizontal else (w, h) for w, h in sizes]
        at = {k: i for i, k in enumerate(keys)}
        pairs = [(at[a], at[b]) for a, b, _x in lifted.get(container, []) if a in at and b in at]
        exts = [
            0.0 if x[2] is None else (x[2][0] if horizontal else x[2][1])
            for x in lifted.get(container, [])
            if x[0] in at and x[1] in at
        ]
        cross, layer_at, width, length = _layered(canon, pairs, exts)
        w_all, h_all = (length, width) if horizontal else (width, length)
        pos: dict[str, Point] = {}
        for i, k in enumerate(keys):
            c, ly = cross[i], layer_at[i]
            ext = canon[i][1]
            if d == "TD":
                pos[k] = (c, ly)
            elif d == "BT":
                pos[k] = (c, length - ly - ext)
            elif d == "LR":
                pos[k] = (ly, c)
            else:
                pos[k] = (length - ly - ext, c)
        levels[container] = (w_all, h_all, pos)
        sized[container] = {k: sizes[i] for i, k in enumerate(keys)}

    sized: dict[str | None, dict[str, tuple[float, float]]] = {}
    root = direction if direction in ("TD", "BT", "LR", "RL") else "TD"
    level(None, root)
    boxes: dict[str, Rect] = {}

    def settle(container: str | None, ox: float, oy: float) -> None:
        lw, lh, pos = levels[container]
        for k, (x, y) in pos.items():
            w, h = sized[container][k]
            boxes[k] = (ox + x, oy + y, w, h)
            if isinstance(by_key[k], Group):
                iw, ih, _ = levels[k]
                settle(k, ox + x + (w - iw) / 2, oy + y + (h - ih) / 2)

    settle(None, 0.0, 0.0)
    return boxes


# ------------------------------------------------------------------------------------ routes

_OUT = {"t": (0, -1), "b": (0, 1), "l": (-1, 0), "r": (1, 0)}


def _sides(a: Rect, b: Rect, horizontal: bool) -> tuple[str, str]:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b

    def vert() -> tuple[str, str] | None:
        if ay + ah <= by:
            return "b", "t"
        if by + bh <= ay:
            return "t", "b"
        return None

    def side() -> tuple[str, str] | None:
        if ax + aw <= bx:
            return "r", "l"
        if bx + bw <= ax:
            return "l", "r"
        return None

    for pick in (side, vert) if horizontal else (vert, side):
        got = pick()
        if got:
            return got
    return "t", "t"  # one holds the other, or they overlap: leave by the top


def _overlap(a: Rect, b: Rect, pad: float = 0.0) -> bool:
    return (
        a[0] < b[0] + b[2] + pad
        and b[0] < a[0] + a[2] + pad
        and a[1] < b[1] + b[3] + pad
        and b[1] < a[1] + a[3] + pad
    )


def _simplify(points: list[Point]) -> list[Point]:
    out: list[Point] = []
    for p in points:
        if out and out[-1] == p:
            continue
        if len(out) >= 2 and ((out[-2][0] == out[-1][0] == p[0]) or (out[-2][1] == out[-1][1] == p[1])):
            out[-1] = p
            continue
        out.append(p)
    return out


class _Grid:
    """The free corridors: lines beside every box, at every port, and spaced across wide corridors."""

    def __init__(self, obstacles: list[Rect], ports: list[Point]) -> None:
        xs: set[float] = set()
        ys: set[float] = set()
        for x, y, w, h in obstacles:
            xs.update((x - MARGIN, x + w + MARGIN))
            ys.update((y - MARGIN, y + h + MARGIN))
        for px, py in ports:
            xs.add(px)
            ys.add(py)
        self.xs = self._fill(sorted(xs))
        self.ys = self._fill(sorted(ys))
        self.hblock: set[tuple[int, int]] = set()  # (ix, iy): the segment from xs[ix] to xs[ix+1] at ys[iy]
        self.vblock: set[tuple[int, int]] = set()
        for x, y, w, h in obstacles:
            # a segment is blocked when it runs through the inside of a box: along a line strictly
            # inside it, over any stretch that overlaps the box's own span
            ix_lo, ix_hi = bisect_right(self.xs, x) - 1, bisect_left(self.xs, x + w)
            iy_lo, iy_hi = bisect_right(self.ys, y) - 1, bisect_left(self.ys, y + h)
            for iy, line in enumerate(self.ys):
                if y < line < y + h:
                    self.hblock.update((ix, iy) for ix in range(max(ix_lo, 0), ix_hi))
            for ix, line in enumerate(self.xs):
                if x < line < x + w:
                    self.vblock.update((ix, iy) for iy in range(max(iy_lo, 0), iy_hi))

    @staticmethod
    def _fill(vals: list[float]) -> list[float]:
        out: list[float] = []
        for i, v in enumerate(vals):
            out.append(v)
            if i + 1 < len(vals):
                room = vals[i + 1] - v
                if room >= 2 * LANE:
                    n = min(int(room // LANE) - 1, 8)
                    step = room / (n + 1)
                    out.extend(v + step * j for j in range(1, n + 1))
        return out


def _search(
    grid: _Grid,
    start: Point,
    goal: Point,
    first: int,
    last: int,
    used: set[tuple[str, int, int]],
) -> list[Point] | None:
    xs, ys = grid.xs, grid.ys
    if start[0] not in xs or goal[0] not in xs:
        return None
    six, siy = xs.index(start[0]), ys.index(start[1])
    gix, giy = xs.index(goal[0]), ys.index(goal[1])
    dirs = ((0, -1), (1, 0), (0, 1), (-1, 0))  # up, right, down, left
    heap: list[tuple[float, float, int, int, int, int]] = []
    heapq.heappush(heap, (0.0, 0.0, six, siy, first, 0))
    best: dict[tuple[int, int, int], float] = {(six, siy, first): 0.0}
    came: dict[tuple[int, int, int], tuple[int, int, int] | None] = {(six, siy, first): None}
    tick = 0
    while heap:
        _f, cost, ix, iy, d, _t = heapq.heappop(heap)
        if best.get((ix, iy, d), 1e18) < cost:
            continue
        if (ix, iy) == (gix, giy):
            pts = []
            cur: tuple[int, int, int] | None = (ix, iy, d)
            while cur is not None:
                pts.append((xs[cur[0]], ys[cur[1]]))
                cur = came[cur]
            return pts[::-1]
        for nd, (dx, dy) in enumerate(dirs):
            if nd == (d + 2) % 4:
                continue
            nx, ny = ix + dx, iy + dy
            if not (0 <= nx < len(xs) and 0 <= ny < len(ys)):
                continue
            if dx:
                key = (min(ix, nx), iy)
                if key in grid.hblock:
                    continue
                length = abs(xs[nx] - xs[ix])
                seg = ("h", key[0], iy)
            else:
                key = (ix, min(iy, ny))
                if key in grid.vblock:
                    continue
                length = abs(ys[ny] - ys[iy])
                seg = ("v", ix, key[1])
            step = length + (TURN if nd != d else 0.0) + (SHARED if seg in used else 0.0)
            if (nx, ny) == (gix, giy) and nd != last:
                step += TURN
            ncost = cost + step
            if ncost < best.get((nx, ny, nd), 1e18):
                best[(nx, ny, nd)] = ncost
                came[(nx, ny, nd)] = (ix, iy, d)
                tick += 1
                h = abs(xs[nx] - xs[gix]) + abs(ys[ny] - ys[giy])
                heapq.heappush(heap, (ncost + h, ncost, nx, ny, nd, tick))
    return None


def _mark(used: set[tuple[str, int, int]], grid: _Grid, pts: list[Point]) -> None:
    for (ax, ay), (bx, by) in zip(pts, pts[1:], strict=False):
        if ax == bx:
            ix, lo, hi = grid.xs.index(ax), sorted((ay, by))[0], sorted((ay, by))[1]
            for iy in range(grid.ys.index(lo), grid.ys.index(hi)):
                used.add(("v", ix, iy))
        elif ay == by:
            iy, lo, hi = grid.ys.index(ay), sorted((ax, bx))[0], sorted((ax, bx))[1]
            for ix in range(grid.xs.index(lo), grid.xs.index(hi)):
                used.add(("h", ix, iy))


def _loop(box: Rect) -> tuple[list[Point], Point, Point]:
    """An arrow from a box to itself: out of the right side, around the top-right corner, in at the top."""
    x, y, w, h = box
    sy, ex = y + h * 0.7, x + w * 0.7
    pts = [(x + w, sy), (x + w + 36, sy), (x + w + 36, y - 30), (ex, y - 30), (ex, y)]
    return pts, (1.0, 0.7), (0.7, 0.0)


def _seg_rect_hit(a: Point, b: Point, r: Rect) -> bool:
    """Whether an axis-parallel segment passes through the inside of a rect."""
    x, y, w, h = r
    if a[0] == b[0]:
        return x < a[0] < x + w and min(a[1], b[1]) < y + h and max(a[1], b[1]) > y
    return y < a[1] < y + h and min(a[0], b[0]) < x + w and max(a[0], b[0]) > x


def path_hits(points: tuple[Point, ...] | list[Point], rect: Rect) -> bool:
    return any(_seg_rect_hit(a, b, rect) for a, b in zip(points, points[1:], strict=False))


def route(
    boxes: dict[str, Rect],
    obstacles: dict[str, Rect],
    links: list[Link],
    *,
    horizontal: bool = False,
) -> dict[int, Route]:
    """An orthogonal route for each link that is drawn, keyed by its index.

    ``boxes`` holds every link end (items, and groups that arrows may end on); ``obstacles`` the boxes
    an arrow must not cross. Ends sharing a side are spread along it so each arrow has its own port."""
    drawn = [i for i, link in enumerate(links) if not link.hidden and link.src in boxes and link.dst in boxes]
    sides: dict[tuple[int, str], str] = {}
    attach: dict[tuple[str, str], list[tuple[float, int, str]]] = {}
    for i in drawn:
        link = links[i]
        if link.src == link.dst:
            continue
        a, b = boxes[link.src], boxes[link.dst]
        sa, sb = _sides(a, b, horizontal)
        sides[(i, "src")], sides[(i, "dst")] = sa, sb
        ca = (b[0] + b[2] / 2) if sa in "tb" else (b[1] + b[3] / 2)
        cb = (a[0] + a[2] / 2) if sb in "tb" else (a[1] + a[3] / 2)
        attach.setdefault((link.src, sa), []).append((ca, i, "src"))
        attach.setdefault((link.dst, sb), []).append((cb, i, "dst"))
    port: dict[tuple[int, str], Point] = {}
    ratio: dict[tuple[int, str], Point] = {}
    for (key, side), ends in attach.items():
        x, y, w, h = boxes[key]
        ends.sort()
        for n, (_c, i, end) in enumerate(ends):
            f = (n + 1) / (len(ends) + 1)
            if side == "t":
                p, r = (x + w * f, y), (f, 0.0)
            elif side == "b":
                p, r = (x + w * f, y + h), (f, 1.0)
            elif side == "l":
                p, r = (x, y + h * f), (0.0, f)
            else:
                p, r = (x + w, y + h * f), (1.0, f)
            port[(i, end)], ratio[(i, end)] = p, r
    stubs: dict[tuple[int, str], Point] = {}
    for (i, end), (px, py) in port.items():
        dx, dy = _OUT[sides[(i, end)]]
        stubs[(i, end)] = (px + dx * MARGIN, py + dy * MARGIN)
    rects = list(obstacles.values())
    grid = _Grid(rects, [*port.values(), *stubs.values()])
    used: set[tuple[str, int, int]] = set()
    paths: dict[int, tuple[list[Point], Point, Point]] = {}
    for i in drawn:
        link = links[i]
        if link.src == link.dst:
            paths[i] = _loop(boxes[link.src])
            continue
        s_side, d_side = sides[(i, "src")], sides[(i, "dst")]
        s_out, d_out = _OUT[s_side], _OUT[d_side]
        first = {(0, -1): 0, (1, 0): 1, (0, 1): 2, (-1, 0): 3}[s_out]
        last = {(0, -1): 2, (1, 0): 3, (0, 1): 0, (-1, 0): 1}[d_out]  # moving into the box
        s, t = stubs[(i, "src")], stubs[(i, "dst")]
        mid = _search(grid, s, t, first, last, used)
        if mid is None:  # boxed in: a plain Z, which may cross a box
            if s_side in "tb":
                my = (s[1] + t[1]) / 2
                mid = [s, (s[0], my), (t[0], my), t]
            else:
                mx = (s[0] + t[0]) / 2
                mid = [s, (mx, s[1]), (mx, t[1]), t]
        else:
            _mark(used, grid, mid)
        pts = _simplify([port[(i, "src")], *mid, port[(i, "dst")]])
        paths[i] = (pts, ratio[(i, "src")], ratio[(i, "dst")])
    # labels last, once every path is known: each takes the first spot along its path that is clear
    placed: list[Rect] = []
    out: dict[int, Route] = {}
    for i in drawn:
        pts, ra, rb = paths[i]
        link = links[i]
        spot: Point | None = None
        if link.label is not None:
            spot = _label_spot(pts, link.label, placed, rects)
            placed.append((spot[0] - 3, spot[1] - 3, link.label[0] + 6, link.label[1] + 6))
        out[i] = Route(tuple(pts), ra, rb, spot)
    return out


def _label_spot(pts: list[Point], size: tuple[float, float], placed: list[Rect], boxes: list[Rect]) -> Point:
    """The first spot along the path where the label touches no box and no other label: on the line,
    then beside it. When nothing is free the first spot on the line is used."""
    w, h = size
    segs = sorted(
        zip(pts, pts[1:], strict=False), key=lambda s: -(abs(s[0][0] - s[1][0]) + abs(s[0][1] - s[1][1]))
    )
    cands: list[Point] = []
    steps = sorted((i / 10 for i in range(-6, 17)), key=lambda t: (abs(t - 0.5), t))
    for t in steps:  # along the line first, then past its ends where the corridor goes on
        for a, b in segs:
            mx, my = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            cands.append((mx - w / 2, my - h / 2))
            if a[0] == b[0]:
                cands += [(mx + 6, my - h / 2), (mx - w - 6, my - h / 2)]
            else:
                cands += [(mx - w / 2, my - h - 4), (mx - w / 2, my + 4)]

    for a, b in segs[:3]:  # still nothing: further out, in rings around the line
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        for ring in range(1, 9):
            for along in (0.0, -w * 0.6, w * 0.6, -w * 1.2, w * 1.2):
                for side in (1, -1):
                    if a[0] == b[0]:
                        cands.append(
                            (mx + side * 30 * ring - (w if side < 0 else 0), my + along * h / w - h / 2)
                        )
                    else:
                        cands.append((mx + along - w / 2, my + side * 30 * ring - (h if side < 0 else 0)))

    def free(c: Point, others: bool) -> bool:
        r = (c[0] - 3, c[1] - 3, w + 6, h + 6)
        return not any(_overlap(r, p) for p in placed) and (
            not others or not any(_overlap(r, b) for b in boxes)
        )

    for c in cands:
        if free(c, True):
            return c
    for c in cands:
        if free(c, False):
            return c
    return cands[0] if cands else pts[0]
