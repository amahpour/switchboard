"""One view of what every agent is doing, across rooms and machines (DESIGN.md §55.9, #815).

A read of rows the broker already keeps (memberships, participants, their status and their last sign of life),
in one query for every room (``Store.agent_sessions``); it is never a new kind of record and it changes
nothing. An agent session appears once, with the rooms the person may see it in. The rule of §44 is the only
one: a room the person cannot see is neither listed nor counted, and a session that is only in such rooms is
not there at all. The page draws it on Home and refreshes it over the same frames as the rest of Home.

The text an agent wrote (its status line) is cut and cleaned here and drawn with ``textContent``.
"""

from __future__ import annotations

from typing import Any

from switchboard.delivery import rules
from switchboard.envelope import clean
from switchboard.models import Room
from switchboard.store import STATUS_LINE_STALE_S

STUCK_S = (
    STATUS_LINE_STALE_S  # the age at which who() calls a status line stale, and a worker "possibly stuck"
)
LINE_MAX = 200
AGENTS_MAX = 100
# what the page shows first: what needs a person, then what may be stuck, then the rest
STATE_ORDER = {"waiting": 0, "working": 1, "idle": 2, "offline": 3, "not_connected": 4}


def state_of(status: str) -> str:
    """The four states a person is shown, from the engine's own (``busy``, ``idle``, ``starting``,
    ``waiting-approval``, ``offline``). A status this code does not know is ``idle``, never ``working``: a
    value from outside must not make an agent look busy or stuck."""
    if status == "busy":
        return "working"
    if status == "waiting-approval":
        return "waiting"
    if status == "offline":
        return "offline"
    return "idle"


def possibly_stuck(state: str, last_active: float | None, now: float) -> bool:
    """Working, and nothing heard of it (a status report, a tool call, a say) for ``STUCK_S``. Pure: the
    clock is an argument. It clears on the next sign of life, because that moves ``last_active`` forward."""
    return state == "working" and last_active is not None and now - last_active >= STUCK_S


def last_active(times: Any, status_line_at: float | None) -> float | None:
    """The newest sign of life the rows hold for a session, or None when it has left none."""
    known = [
        x
        for x in (
            times.get("last_seen"),
            times.get("last_say_at"),
            times.get("hooks_seen_at"),
            times.get("status_at"),
            status_line_at,
        )
        if isinstance(x, (int, float))
    ]
    return max(known) if known else None


def build(state: Any, rooms: list[Room], now: float) -> dict[str, Any]:
    """Every agent session in ``rooms`` (the rooms the viewer may see: the caller applied §44), once each."""
    by_id = {r.id: r for r in rooms}
    held: dict[int, list[tuple[Room, Any]]] = {}
    for row in state.store.agent_sessions():  # one query for every room
        room = by_id.get(row["room_id"])
        if room is not None:
            held.setdefault(row["participant_id"], []).append((room, row))
    delivery = state.service.delivery
    owners: dict[tuple[str, int], str | None] = {}
    agents: list[dict[str, Any]] = []
    for pid, in_rooms in held.items():
        room, first = in_rooms[0]
        key = (first["host"], room.id if room.private else 0)  # a private room's people decide the name shown
        if key not in owners:
            owners[key] = state.service.agent_owner(first["host"], room)
        st = state_of(first["status"])
        active = last_active(dict(first), first["status_line_at"])
        gone = None
        if delivery is not None:  # the same call the Members pane's rows make (§49.14), for this session
            gone = delivery.not_connected_for(
                pid,
                first["status"],
                rules.heard_at(first["last_seen"], first["last_say_at"]),
            )
        if gone is not None:
            st = "not_connected"  # a session that vanished keeps its last state; it is shown as not there
        agents.append(
            {
                "id": pid,
                "name": first["screen_name"],
                "harness": first["harness"],
                "owner": owners[key],
                "host": first["host"],
                "state": st,
                "since": gone if gone is not None else first["status_at"],  # not connected: last heard
                "line": clean(first["status_line"] or "")[:LINE_MAX],
                "line_at": first["status_line_at"],
                "active_at": active,
                "stuck": possibly_stuck(st, active, now),
                "rooms": [
                    {
                        "room": r.name,
                        "slug": r.slug,
                        **({"as": m["screen_name"]} if m["screen_name"] != first["screen_name"] else {}),
                    }
                    for r, m in in_rooms
                ],
            }
        )
    agents.sort(
        key=lambda a: (STATE_ORDER[a["state"]], not a["stuck"], -(a["since"] or 0.0), a["name"], a["id"])
    )
    counts = {s: sum(1 for a in agents if a["state"] == s) for s in STATE_ORDER}
    counts["stuck"] = sum(1 for a in agents if a["stuck"])
    return {"agents": agents[:AGENTS_MAX], "agents_total": len(agents), "agent_counts": counts}
