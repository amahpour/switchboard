"""A run's record: the one thing a grader reads (DESIGN.md §12.7). Test-only.

The rows of the broker's database that say what happened in the room (``messages``, ``deliveries``,
``events``), the ``switchboard report`` of the room (``report.build``, the same dict ``switchboard report
--json`` prints), and the tokens the harness's own session files recorded. The database is opened
read-only, through the same connection ``switchboard report`` uses. A grader never reads what an agent
meant, only who posted, who passed and what was delivered to whom.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from switchboard import report


@dataclass(frozen=True)
class Tokens:
    """What a session's own file says it used. ``cost_usd`` is priced at the published rates of its model
    (``None`` for a model with no price here); cache reads are most of ``total`` and cost a tenth."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0
    cost_usd: float | None = 0.0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def __add__(self, o: Tokens) -> Tokens:
        cost = None if self.cost_usd is None or o.cost_usd is None else self.cost_usd + o.cost_usd
        return Tokens(
            self.input + o.input,
            self.output + o.output,
            self.cache_read + o.cache_read,
            self.cache_write + o.cache_write,
            self.calls + o.calls,
            cost,
        )

    def __sub__(self, o: Tokens) -> Tokens:
        cost = None if self.cost_usd is None or o.cost_usd is None else self.cost_usd - o.cost_usd
        return Tokens(
            self.input - o.input,
            self.output - o.output,
            self.cache_read - o.cache_read,
            self.cache_write - o.cache_write,
            self.calls - o.calls,
            cost,
        )

    def as_dict(self) -> dict[str, Any]:
        d = {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "calls": self.calls,
            "total": self.total,
        }
        d["cost_usd"] = None if self.cost_usd is None else round(self.cost_usd, 5)
        return d


def sum_tokens(items: Any) -> Tokens:
    out = Tokens()
    for t in items:
        out = out + t
    return out


@dataclass(frozen=True)
class Message:
    id: int
    ts: float
    sender: str
    sender_kind: str  # human | agent | system
    kind: str  # chat | join | leave | notice
    text: str
    reply_to: int | None
    mentions: tuple[str, ...]


@dataclass(frozen=True)
class Delivery:
    message_id: int
    agent: str
    prio: int  # 2 a person's, 1 an @mention, 0 chatter
    state: str  # pending | offered | in_context | handled | revoked
    redelivered: int


@dataclass(frozen=True)
class Event:
    id: int
    ts: float
    kind: str
    agent: str | None
    data: dict[str, Any]


@dataclass(frozen=True)
class RunRecord:
    room: str
    agents: tuple[str, ...]
    human: str
    messages: tuple[Message, ...]
    deliveries: tuple[Delivery, ...]
    events: tuple[Event, ...]
    report: dict[str, Any]
    tokens: dict[str, Tokens] = field(default_factory=dict)
    asked: tuple[int, ...] = ()  # the id of each message the person posted, in the order of the steps

    def ts_of(self, message_id: int) -> float:
        return next(m.ts for m in self.messages if m.id == message_id)

    def posts_after(self, message_id: int) -> list[Message]:
        """What the agents posted in the room after message ``message_id``: chat from an agent."""
        return [
            m for m in self.messages if m.id > message_id and m.kind == "chat" and m.sender_kind == "agent"
        ]

    def posters_after(self, message_id: int) -> list[str]:
        """The agents that posted after message ``message_id``, each once, in order of their first post."""
        seen: list[str] = []
        for m in self.posts_after(message_id):
            if m.sender not in seen:
                seen.append(m.sender)
        return seen

    def passes_after(self, message_id: int) -> list[str]:
        """The agents that called ``pass()`` after message ``message_id``, one entry per call."""
        ts = self.ts_of(message_id)
        return [e.agent for e in self.events if e.kind == "pass" and e.agent and e.ts >= ts]


def _json(raw: Any) -> dict[str, Any]:
    try:
        d = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def read_record(
    db_path: str | Path,
    room: str,
    agents: tuple[str, ...],
    human: str,
    asked: tuple[int, ...],
    tokens: dict[str, Tokens],
) -> RunRecord:
    """The record of ``room`` in the broker's database at ``db_path``."""
    con = report.open_ro(db_path)
    try:
        rid = _room_id(con, room)
        messages = tuple(
            Message(
                int(r["id"]),
                float(r["ts"]),
                r["sender_name"],
                r["sender_kind"],
                r["kind"],
                r["text"],
                r["reply_to"],
                tuple(x for x in _json_list(r["mentions"])),
            )
            for r in con.execute(
                "SELECT id, ts, sender_name, sender_kind, kind, text, reply_to, mentions"
                " FROM messages WHERE room_id = ? ORDER BY id",
                (rid,),
            )
        )
        deliveries = tuple(
            Delivery(
                int(r["message_id"]), r["screen_name"], int(r["prio"]), r["state"], int(r["redelivered"])
            )
            for r in con.execute(
                "SELECT d.message_id, m.screen_name, d.prio, d.state, d.redelivered FROM deliveries d"
                " JOIN memberships m ON m.id = d.membership_id WHERE m.room_id = ?"
                " ORDER BY d.message_id, m.id",
                (rid,),
            )
        )
        events = tuple(
            Event(int(r["id"]), float(r["ts"]), r["kind"], r["screen_name"], _json(r["data"]))
            for r in con.execute(
                "SELECT e.id, e.ts, e.kind, e.data, m.screen_name FROM events e"
                " LEFT JOIN memberships m ON m.id = e.membership_id WHERE e.room_id = ? ORDER BY e.id",
                (rid,),
            )
        )
        rep = report.build(con, room)
    finally:
        con.close()
    return RunRecord(room, agents, human, messages, deliveries, events, rep, tokens, asked)


def _json_list(raw: Any) -> list[str]:
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


def _room_id(con: sqlite3.Connection, room: str) -> int:
    row = con.execute("SELECT id FROM rooms WHERE name = ?", (room,)).fetchone()
    if row is None:
        raise LookupError(f"no room {room} in the database")
    return int(row["id"])


@dataclass(frozen=True)
class Quiet:
    """The room as the settle check sees it, from the database alone."""

    mark: tuple[int, int, int]  # the newest message, event and batch: nothing has moved while it is the same
    open_priority: (
        int  # priority deliveries (a person's, an @mention) still pending or offered, and offered batches
    )
    paused: str | None  # why the room is paused (the loop guard), or None


def quiet_state(db_path: str | Path, room: str) -> Quiet:
    """Whether the room has anything left to deliver. Chatter that addresses nobody is not counted: the
    engine holds it for ``unaddressed_hold_s`` (270 s by default, §8.2), far longer than a trial, so it
    is left out of 'settled' and out of the wakes."""
    con = report.open_ro(db_path)
    try:
        rid = _room_id(con, room)

        def one(sql: str, *args: Any) -> int:
            return int(con.execute(sql, args).fetchone()[0])

        mark = (
            one("SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id = ?", rid),
            one("SELECT COALESCE(MAX(id), 0) FROM events WHERE room_id = ?", rid),
            one(
                "SELECT COALESCE(MAX(b.id), 0) FROM batches b JOIN memberships m ON m.id = b.membership_id"
                " WHERE m.room_id = ?",
                rid,
            ),
        )
        open_priority = one(
            "SELECT COUNT(*) FROM deliveries d JOIN memberships m ON m.id = d.membership_id"
            " WHERE m.room_id = ? AND m.left_at IS NULL AND d.prio >= 1"
            " AND d.state IN ('pending', 'offered')",
            rid,
        ) + one(
            "SELECT COUNT(*) FROM batches b JOIN memberships m ON m.id = b.membership_id"
            " WHERE m.room_id = ? AND m.left_at IS NULL AND b.state = 'offered'",
            rid,
        )
        row = con.execute("SELECT paused, paused_reason FROM rooms WHERE id = ?", (rid,)).fetchone()
        paused = (row["paused_reason"] or "paused") if row["paused"] else None
        return Quiet(mark, open_priority, paused)
    finally:
        con.close()
