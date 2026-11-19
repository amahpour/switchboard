"""How reviews go (DESIGN.md §61.5, #863): the usage page's review figures, with no I/O.

A read of what the review boards (§37.3) already store: each board's row, its items, and its
``review`` events. Nothing is written and no record is added. The caller passes the rooms the viewer
may see (§44) and the rows of the boards that moved in the window; a board whose room was not passed
is dropped, so this module cannot widen what the caller decided was visible.

What a number is, with the window each one belongs to (a duration counts in the window where it ended):

- **Boards.** *Opened*: ``opened_at`` is in the window. *Settled*: the moment the board last became
  settled (:func:`settled_at`) is in the window. *Time to settle*: from opened to settled, the median.
  A board counts for the agent that holds it (``reopened_by`` or ``opened_by``, §37.14); a board held by
  anyone who is not a member of the room (a person, or ``switchboard`` itself) counts in the room only.
- **Findings.** A finding counts in the window it was raised in, in the state it is in now, for the
  agent that raised it (the first raiser, when a takeover moved it, §37.2). *Confirmed*: fixed and
  confirmed. *Dropped*: dropped and not deferred to a ticket (a deferral is settled, not withdrawn).
  *Contested*: it was ever contested. The share confirmed is confirmed over confirmed plus dropped, the
  findings whose end is known: a rough measure of a reviewer's precision, and ``None`` until one ends.
- **Fixes.** *Reopened*: a ``reopened`` event in the window, for the agent whose fix it was (the
  finding's owner). *Heads*: the distinct commits a board went through up to the moment it settled,
  the median over the boards that settled in the window.
- **Stages.** Opened to the first finding, the first finding (or opening, on a board of questions only)
  to settled, settled to merged: each the median over the boards whose stage ended in the window.
  *Merged* is an agent's report (§37.11, #862): the first ``report`` event that says merged. A board
  settled in the window and not reported merged is *unknown*.

Every stored value is a claim: a time that is not a finite number, or an event that is not shaped as a
board writes it, is left out and never raises.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from switchboard import reviews
from switchboard.models import Room, room_slug

WINDOWS = ("today", "week")
# the moves that settle an item, as an event's ``to`` (§37.2): a person's fix counts too
_SETTLING = ("answered", "confirmed", "dropped", "deferred")
_CONTESTS = ("contested", "insisted")

Event = tuple[float, Mapping[str, Any]]  # (ts, data) of one ``review`` event, oldest first


def _t(value: object) -> float | None:
    """A stored time, or None when it is not a finite number (a bool is not a time)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def settled_at(items: Sequence[reviews.Item], events: Sequence[Event]) -> float | None:
    """When a board that is settled now last became settled, or None if it is not settled (or the
    moves that settled it were not recorded). The latest settling event of each item, the latest of
    those: a fix confirmed, an answer, a drop or a deferral, or a person ruling a finding fixed."""
    if not items or not reviews.settled(list(items)):
        return None
    times: dict[Any, float] = {}
    for ts, data in events:
        at = _t(ts)
        if at is None:
            continue
        to = data.get("to")
        if to in _SETTLING or (to == "fixed" and data.get("person")):
            times[data.get("item")] = at
    if all(item.label in times for item in items):
        return max(times[item.label] for item in items)
    return None


def merged_at(review: Mapping[str, Any], events: Sequence[Event]) -> float | None:
    """When an agent first reported the pull request merged, from the board's ``report`` events; else
    the time of its last report when the board says merged and no event survives (an older board)."""
    for ts, data in events:
        report = data.get("report")
        at = _t(ts)
        if at is not None and isinstance(report, Mapping) and report.get("pr_state") == "merged":
            return at
    if review.get("pr_state") == "merged":
        return _t(review.get("reported_at"))
    return None


def head_count(opened: Sequence[Event], until: float | None) -> int:
    """The distinct commits a board was opened or moved to (``open`` events), up to ``until``."""
    seen: set[str] = set()
    for ts, data in opened:
        at = _t(ts)
        head = data.get("head")
        if at is None or (until is not None and at > until):
            continue
        if data.get("open") and isinstance(head, str) and head:
            seen.add(head)
    return len(seen)


def _median(values: Iterable[float]) -> float | None:
    seen = [v for v in values if v >= 0]
    return float(statistics.median(seen)) if seen else None


class _Acc:
    """One window of one row: the counts and the durations the medians come from."""

    __slots__ = (
        "opened", "settled", "merged", "unknown", "raised", "confirmed", "contested", "dropped",
        "reopened", "to_settle", "to_first", "to_settled", "to_merged", "heads",
    )  # fmt: skip

    def __init__(self) -> None:
        self.opened = self.settled = self.merged = self.unknown = 0
        self.raised = self.confirmed = self.contested = self.dropped = self.reopened = 0
        self.to_settle: list[float] = []
        self.to_first: list[float] = []
        self.to_settled: list[float] = []
        self.to_merged: list[float] = []
        self.heads: list[float] = []

    def busy(self) -> bool:
        return bool(
            self.opened or self.settled or self.merged or self.unknown or self.raised or self.reopened
        )

    def out(self) -> dict[str, Any]:
        ended = self.confirmed + self.dropped
        return {
            "opened": self.opened,
            "settled": self.settled,
            "settle_s": _median(self.to_settle),
            "raised": self.raised,
            "confirmed": self.confirmed,
            "contested": self.contested,
            "dropped": self.dropped,
            "share_confirmed": (self.confirmed / ended) if ended else None,
            "reopened": self.reopened,
            "heads": _median(self.heads),
            "first_finding_s": _median(self.to_first),
            "to_settled_s": _median(self.to_settled),
            "to_merged_s": _median(self.to_merged),
            "merged": self.merged,
            "unknown": self.unknown,
        }


class _Row:
    def __init__(self, name: str, room_id: int) -> None:
        self.name = name
        self.room_id = room_id
        self.win = {w: _Acc() for w in WINDOWS}


def shape(
    rooms: Sequence[Room | None],
    boards: Sequence[Mapping[str, Any]],
    items: Sequence[reviews.Item],
    item_boards: Mapping[int, int],
    events: Sequence[tuple[int, float, Mapping[str, Any]]],
    members: Mapping[tuple[int, str], tuple[str, str]],
    *,
    now: float,
    today_start: float,
    week_start: float,
) -> dict[str, Any]:
    """The review figures: a room row for every room with review activity in the 7 days, and an agent
    row for every member of those rooms with some.

    ``boards`` are the stored review rows, ``items`` their items with ``item_boards`` giving each
    item id's board id, ``events`` the boards' ``review`` events as ``(board, ts, data)``, and
    ``members`` the harness and host of each ``(room id, lower-cased name)`` that is an agent in
    the room. Rooms and agents are busiest first (findings raised in the week, then boards opened).
    """
    known = {r.id: r for r in rooms if isinstance(r, Room)}
    start = {"today": float(today_start), "week": float(week_start)}
    until = float(now)

    def within(ts: float | None, window: str) -> bool:
        return ts is not None and start[window] <= ts <= until

    by_board: dict[int, list[reviews.Item]] = {}
    for item in items:
        by_board.setdefault(item_boards.get(item.id, -1), []).append(item)
    moves: dict[int, list[Event]] = {}
    for of_board, ts, data in events:
        at = _t(ts)
        if at is not None and isinstance(data, Mapping):
            moves.setdefault(of_board, []).append((at, data))
    for each in moves.values():
        each.sort(key=lambda e: e[0])

    rows: dict[tuple[int, str], _Row] = {}
    room_rows: dict[int, _Row] = {}

    def room_row(room_id: int) -> _Row:
        if room_id not in room_rows:
            room_rows[room_id] = _Row("", room_id)
        return room_rows[room_id]

    def agent_row(room_id: int, name: str) -> _Row | None:
        if not name or (room_id, name.casefold()) not in members:
            return None  # a person, or switchboard itself: counted in the room, not as an agent
        key = (room_id, name.casefold())
        if key not in rows:
            rows[key] = _Row(name, room_id)
        return rows[key]

    def bump(attr: str) -> Callable[[_Acc], None]:
        return lambda a: setattr(a, attr, getattr(a, attr) + 1)

    def push(attr: str, value: float) -> Callable[[_Acc], None]:
        return lambda a: getattr(a, attr).append(value)

    def credit(room_id: int, name: str, window: str, apply: Callable[[_Acc], None]) -> None:
        apply(room_row(room_id).win[window])
        agent = agent_row(room_id, name)
        if agent is not None:
            apply(agent.win[window])

    for rv in boards:
        room_id = rv.get("room_id")
        board = rv.get("id")
        if isinstance(room_id, bool) or not isinstance(room_id, int) or room_id not in known:
            continue
        if isinstance(board, bool) or not isinstance(board, int):
            continue
        opened = _t(rv.get("opened_at"))
        if opened is None:
            continue
        mine = by_board.get(board, [])
        made = moves.get(board, [])
        holder = reviews.holder(dict(rv))
        findings = [i for i in mine if i.kind == "finding"]
        first = min((i.created_at for i in findings if _t(i.created_at) is not None), default=None)
        settled = settled_at(mine, made)
        merged = merged_at(rv, made)

        heads = head_count(made, settled)
        for window in WINDOWS:
            if within(opened, window):
                credit(room_id, holder, window, bump("opened"))
            if first is not None and within(first, window):
                credit(room_id, holder, window, push("to_first", first - opened))
            if settled is not None and within(settled, window):
                credit(room_id, holder, window, bump("settled"))
                credit(room_id, holder, window, push("to_settle", settled - opened))
                credit(
                    room_id,
                    holder,
                    window,
                    push("to_settled", settled - (opened if first is None else first)),
                )
                credit(room_id, holder, window, push("heads", float(heads)))
                if merged is None:
                    credit(room_id, holder, window, bump("unknown"))
            if merged is not None and within(merged, window):
                credit(room_id, holder, window, bump("merged"))
                if settled is not None:
                    credit(room_id, holder, window, push("to_merged", max(0.0, merged - settled)))

        # findings: in the window they were raised, by the raiser they were first raised by
        first_raiser: dict[str, str] = {}
        contested: set[str] = set()
        for _, data in made:
            label, to = data.get("item"), data.get("to")
            if not isinstance(label, str):
                continue
            if to == "taken" and isinstance(data.get("previous_raiser"), str):
                first_raiser.setdefault(label, data["previous_raiser"])
            if to in _CONTESTS:
                contested.add(label)
        owners = {i.label: i.owner for i in findings}
        for item in findings:
            at = _t(item.created_at)
            raiser = first_raiser.get(item.label) or item.raised_by
            ever = item.label in contested or item.state == "contested"
            for window in WINDOWS:
                if not within(at, window):
                    continue

                def count(a: _Acc, item: reviews.Item = item, ever: bool = ever) -> None:
                    a.raised += 1
                    a.contested += 1 if ever else 0
                    if item.state == "fixed" and item.confirmed_by:
                        a.confirmed += 1
                    elif item.state == "dropped" and not item.deferred_to:
                        a.dropped += 1

                credit(room_id, raiser, window, count)
        for ts, data in made:
            if data.get("to") != "reopened":
                continue
            label = data.get("item")
            owner = owners.get(label, "") if isinstance(label, str) else ""
            for window in WINDOWS:
                if within(ts, window):
                    credit(room_id, owner, window, bump("reopened"))

    def key_of(row: _Row) -> tuple[Any, ...]:
        wk = row.win["week"]
        return (-wk.raised, -wk.opened, -wk.settled)

    out_rooms = []
    for room_id, row in sorted(
        room_rows.items(), key=lambda kv: (*key_of(kv[1]), known[kv[0]].name.casefold())
    ):
        if not any(a.busy() for a in row.win.values()) or not row.win["week"].busy():
            continue
        room = known[room_id]
        out_rooms.append(
            {"name": room.name, "slug": room_slug(room.name)} | {w: row.win[w].out() for w in WINDOWS}
        )
    out_agents = []
    for (room_id, _), row in sorted(
        rows.items(), key=lambda kv: (*key_of(kv[1]), kv[1].name.casefold(), known[kv[0][0]].name.casefold())
    ):
        if not row.win["week"].busy():
            continue
        harness, host = members.get((room_id, row.name.casefold()), ("", ""))
        room = known[room_id]
        out_agents.append(
            {
                "name": row.name,
                "harness": harness,
                "host": host,
                "room": room.name,
                "slug": room_slug(room.name),
            }
            | {w: row.win[w].out() for w in WINDOWS}
        )
    return {"rooms": out_rooms, "agents": out_agents}
