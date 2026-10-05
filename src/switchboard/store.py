"""Store: every SQL statement switchboard runs (DESIGN.md §4). Nothing else touches SQL.

The broker is the only writer. It calls the store synchronously on its event
loop; every write is wrapped in ``db.tx`` (``BEGIN IMMEDIATE``).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from switchboard import db
from switchboard.clock import Clock, SystemClock
from switchboard.models import (
    LOCAL_HOST,
    ROOM_RE,
    WATCHDOG_DONE,
    Batch,
    Event,
    Item,
    MachineRow,
    Member,
    Membership,
    Message,
    Participant,
    PasskeyRow,
    PersonRow,
    RemoteRow,
    Room,
    room_ref,
    split_closed,
    valid_host,
)
from switchboard.models import session_key as make_session_key

OPEN_DELIVERY_STATES = ("pending", "offered", "in_context")
# What deleting a room removes (DESIGN.md §28.6): the keys of ``room_delete_counts`` and
# of ``delete_room``'s result. Participants are never deleted.
ROOM_DELETE_TABLES = ("rooms", "messages", "memberships", "deliveries", "batches", "events")
CONFIG_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class StoreError(Exception):
    pass


class Conflict(StoreError):
    pass


class NotFound(StoreError):
    pass


class Ambiguous(StoreError):
    """A room reference that names several closed rooms (``resolve_room``): ``names``,
    newest first."""

    def __init__(self, message: str, names: list[str]):
        super().__init__(message)
        self.names = names


class Store:
    def __init__(self, con: sqlite3.Connection, clock: Clock | None = None):
        self.con = con
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ rooms
    def create_room(
        self, name: str, created_by: str, budget_per_hour: int, hop_limit: int, rules_text: str = ""
    ) -> Room:
        now = self.clock.now()
        with db.tx(self.con):
            if self.get_room(name) is not None:
                raise Conflict(f"{name} already exists")
            cur = self.con.execute(
                "INSERT INTO rooms(name, created_at, created_by, budget_per_hour,"
                " budget_remaining, budget_window_start, hop_limit, rules_text)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (name, now, created_by, budget_per_hour, budget_per_hour, now, hop_limit, rules_text),
            )
            rid = cur.lastrowid
        assert rid is not None
        room = self.room_by_id(rid)
        assert room is not None
        return room

    def get_room(self, name: str) -> Room | None:
        r = self.con.execute("SELECT * FROM rooms WHERE name=?", (name,)).fetchone()
        return Room.from_row(r) if r else None

    def set_room_rules(self, room_id: int, text: str) -> Room:
        if not isinstance(text, str) or len(text) > 2000:
            raise ValueError("room rules must be at most 2000 characters")
        with db.tx(self.con):
            self.con.execute(
                "UPDATE rooms SET rules_text=?, rules_version=rules_version+1 WHERE id=? AND rules_text<>?",
                (text, room_id, text),
            )
        room = self.room_by_id(room_id)
        assert room is not None
        return room

    def room_by_id(self, room_id: int) -> Room | None:
        r = self.con.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        return Room.from_row(r) if r else None

    def list_rooms(self, *, closed: bool = False) -> list[Room]:
        """The open rooms by name; with ``closed``, the closed ones instead, newest first
        (DESIGN.md §28.2: only a closed room's name contains '~')."""
        if closed:
            rows = self.con.execute(
                "SELECT * FROM rooms WHERE instr(name, '~')>0 ORDER BY id DESC"
            ).fetchall()
        else:
            rows = self.con.execute("SELECT * FROM rooms WHERE instr(name, '~')=0 ORDER BY name").fetchall()
        return [Room.from_row(r) for r in rows]

    def count_closed_rooms(self) -> int:
        return int(self.con.execute("SELECT COUNT(*) FROM rooms WHERE instr(name, '~')>0").fetchone()[0])

    def closed_rooms(self, base: str) -> list[Room]:
        """The closed rooms that were ``base`` (an open room's name), newest first. ``substr``,
        not LIKE: '_' in a room name is a LIKE wildcard."""
        prefix = base + "~closed-"
        rows = self.con.execute(
            "SELECT * FROM rooms WHERE substr(name, 1, ?)=? ORDER BY id DESC", (len(prefix), prefix)
        ).fetchall()
        out = [Room.from_row(r) for r in rows]
        return [r for r in out if r.closed and r.display_name == base]

    def resolve_room(self, ref: str) -> Room:
        """A room the human named (``report --room``, ``rooms delete``; DESIGN.md §28.5): a
        closed room's full name is that room; else an open room of that name wins; else
        the one closed room that had it. Several: Ambiguous. Raises InvalidName, NotFound."""
        n = room_ref(ref)
        room = self.get_room(n)
        if room is not None:
            return room  # a closed room's full name, or an open room (which wins)
        if split_closed(n) is None:
            closed = self.closed_rooms(n)
            if len(closed) == 1:
                return closed[0]
            if closed:
                names = [r.name for r in closed]
                raise Ambiguous(f"{n} names {len(names)} closed rooms: {', '.join(names)}", names)
        raise NotFound(f"no such room: {n}")

    def rename_room(self, room_id: int, new_name: str, *, expect: str) -> Room:
        """Close (``#x`` -> ``#x~closed-<id>``) or reopen a room, if it is still named
        ``expect``: else Conflict (as when ``new_name`` is taken)."""
        if not (ROOM_RE.fullmatch(new_name) or split_closed(new_name)):
            raise ValueError(f"not a room name: {new_name!r}")
        with db.tx(self.con):
            try:
                n = self.con.execute(
                    "UPDATE rooms SET name=? WHERE id=? AND name=?", (new_name, room_id, expect)
                ).rowcount
            except sqlite3.IntegrityError:
                raise Conflict(f"{new_name} already exists") from None
            if n != 1:
                raise Conflict(f"room {room_id} is not {expect}")
            return self._room_or_raise(room_id)

    def reopen_room(self, room_id: int) -> Room:
        """A closed room gets its name back (DESIGN.md §28.4), and the credentials /close kept
        (for its error text only) are cleared. NotFound unless the room exists and is closed;
        Conflict while an open room holds the name. Nobody is re-added."""
        with db.tx(self.con):
            room = self.room_by_id(room_id)
            if room is None or not room.closed:
                raise NotFound(f"no closed room with id {room_id}")
            if self.get_room(room.display_name) is not None:
                raise Conflict(f"{room.display_name} is taken by an open room")
            got = self.rename_room(room_id, room.display_name, expect=room.name)
            self.con.execute(
                "UPDATE memberships SET cred_hash=NULL WHERE room_id=? AND left_reason='closed'", (room_id,)
            )
        return got

    def latest_close_event(self, room_id: int) -> Event | None:
        """The last ``room_close`` of this room: who closed it and when."""
        evs = self.recent_events(room_id=room_id, kinds=["room_close"], limit=1)
        return evs[0] if evs else None

    # The rows deleting a room removes (DESIGN.md §28.6), in delete order: children first.
    # ``deliveries`` and ``events`` have no foreign key, so nothing else would catch an orphan.
    _ROOM_MEMBERSHIPS = "SELECT id FROM memberships WHERE room_id=?"
    _ROOM_ROWS = (
        (
            "deliveries",
            f"deliveries WHERE membership_id IN ({_ROOM_MEMBERSHIPS})"
            " OR message_id IN (SELECT id FROM messages WHERE room_id=?)",
            2,
        ),
        ("batches", f"batches WHERE membership_id IN ({_ROOM_MEMBERSHIPS})", 1),
        ("events", f"events WHERE room_id=? OR membership_id IN ({_ROOM_MEMBERSHIPS})", 2),
        ("messages", "messages WHERE room_id=?", 1),
        ("memberships", "memberships WHERE room_id=?", 1),
        ("rooms", "rooms WHERE id=?", 1),
    )

    def room_delete_counts(self, room_id: int) -> dict[str, int]:
        """What ``delete_room`` would remove now, per table (``ROOM_DELETE_TABLES``)."""
        got = {
            t: int(self.con.execute(f"SELECT COUNT(*) FROM {where}", (room_id,) * n).fetchone()[0])
            for t, where, n in self._ROOM_ROWS
        }
        return {t: got[t] for t in ROOM_DELETE_TABLES}

    def delete_room(
        self,
        room_id: int,
        *,
        name: str,
        created_at: float,
        expect_counts: dict[str, int],
        event: dict[str, Any],
    ) -> dict[str, int]:
        """Delete a room and every row that names it, in one transaction (DESIGN.md §28.6):
        only while it is still ``name`` and ``created_at`` (ids may be reused, so a room
        re-created after a delete can have the same id and name), has no active
        membership (on any host, online or
        not) and the database still has the row counts its backup was checked against
        (``expect_counts``, every table). Then the foreign keys and every table's count
        are checked again; any failure rolls it all back. Records ``room_delete`` with
        ``room_id`` NULL (ids may be reused). Returns the rows removed per table."""
        with db.tx(self.con):
            room = self.room_by_id(room_id)
            if room is None or room.name != name or room.created_at != created_at:
                raise Conflict(f"{name} changed since the plan")
            members = self.room_memberships(room_id)
            if members:
                raise Conflict(f"{name} has {len(members)} agent(s)")
            before = db.row_counts(self.con, db.TABLES)
            if before != expect_counts:
                raise Conflict("the database changed while its backup was made")
            removed: dict[str, int] = {}
            for t, where, n in self._ROOM_ROWS:
                removed[t] = self.con.execute(f"DELETE FROM {where}", (room_id,) * n).rowcount
            if self.con.execute("PRAGMA foreign_key_check").fetchall():
                raise StoreError(f"deleting {name} would leave rows that point at it")
            after = db.row_counts(self.con, db.TABLES)
            want = {t: before[t] - removed.get(t, 0) for t in db.TABLES}
            if after != want:
                raise StoreError(f"deleting {name} changed other rows ({after} != {want})")
            removed = {t: removed[t] for t in ROOM_DELETE_TABLES}
            self.add_event("room_delete", room_id=None, data={**event, "removed": removed})
        return removed

    def _room_or_raise(self, room_id: int) -> Room:
        room = self.room_by_id(room_id)
        if room is None:
            raise NotFound(f"room {room_id} not found")
        return room

    def set_paused(self, room_id: int, paused: bool, reason: str | None = None) -> Room:
        with db.tx(self.con):
            if paused:
                self.con.execute("UPDATE rooms SET paused=1, paused_reason=? WHERE id=?", (reason, room_id))
            else:
                self.con.execute(
                    "UPDATE rooms SET paused=0, paused_reason=NULL, hop_count=0 WHERE id=?",
                    (room_id,),
                )
        return self._room_or_raise(room_id)

    def set_budget(self, room_id: int, remaining: int) -> Room:
        if remaining < 0:
            raise ValueError("budget must be >= 0")
        with db.tx(self.con):
            self.con.execute("UPDATE rooms SET budget_remaining=? WHERE id=?", (remaining, room_id))
        return self._room_or_raise(room_id)

    def set_hop_limit(self, room_id: int, limit: int) -> Room:
        """The room's loop-guard limit (``/hops n``); 0 turns the guard off. The count and
        the paused state are left alone: a lower limit trips on the next agent message."""
        if limit < 0:
            raise ValueError("hop limit must be >= 0")
        with db.tx(self.con):
            self.con.execute("UPDATE rooms SET hop_limit=? WHERE id=?", (limit, room_id))
        return self._room_or_raise(room_id)

    def spend_budget(self, room_id: int) -> Room:
        """One wake that has no batch of its own (a Devin Stop re-arm): remaining - 1, floor 0."""
        with db.tx(self.con):
            self.con.execute(
                "UPDATE rooms SET budget_remaining=MAX(budget_remaining-1, 0) WHERE id=?", (room_id,)
            )
        return self._room_or_raise(room_id)

    def refill_budget(self, room_id: int, now: float | None = None) -> Room:
        """Fixed hourly window: once the window has passed, refill to budget_per_hour."""
        now = self.clock.now() if now is None else now
        with db.tx(self.con):
            room = self._room_or_raise(room_id)
            if now >= room.budget_window_start + 3600.0:
                elapsed = int((now - room.budget_window_start) // 3600.0)
                self.con.execute(
                    "UPDATE rooms SET budget_remaining=budget_per_hour,"
                    " budget_window_start=budget_window_start + ? WHERE id=?",
                    (elapsed * 3600.0, room_id),
                )
        return self._room_or_raise(room_id)

    # --------------------------------------------------------------- messages
    def insert_message(
        self,
        room_id: int,
        *,
        sender_name: str,
        sender_kind: str,
        via: str,
        text: str,
        kind: str = "chat",
        sender_membership_id: int | None = None,
        sender_harness: str | None = None,
        reply_to: int | None = None,
        mentions: Iterable[str] = (),
        skip_memberships: Iterable[int] = (),
        sender_host: str | None = None,
        sender_person_id: int | None = None,
    ) -> Message:
        """Persist a message and its per-recipient delivery rows in one transaction.

        Classification (§8.1): for every active membership other than the sender,
        prio 2 for a human message, 1 when the member is @mentioned, else 0.
        join/leave/notice messages are never delivered to agents. ``skip_memberships``
        get no delivery row either (the subjects of a ``/catchup`` request, §26).
        ``sender_host`` is a remote agent sender's host (§27.6); None on this machine.
        ``sender_person_id`` is a human sender other than the owner (§32.2); every person
        is a human to every agent, so it changes nothing about the deliveries.
        """
        if sender_host is not None and not valid_host(sender_host):
            raise ValueError(f"not a host name: {sender_host!r}")
        skip = {int(x) for x in skip_memberships}
        now = self.clock.now()
        mentions_l = sorted({m.lower() for m in mentions})
        with db.tx(self.con):
            cur = self.con.execute(
                "INSERT INTO messages(room_id, ts, sender_membership_id, sender_name,"
                " sender_harness, sender_kind, via, kind, text, reply_to, mentions, sender_host,"
                " sender_person_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    room_id,
                    now,
                    sender_membership_id,
                    sender_name,
                    sender_harness,
                    sender_kind,
                    via,
                    kind,
                    text,
                    reply_to,
                    json.dumps(mentions_l),
                    sender_host,
                    sender_person_id,
                ),
            )
            mid = cur.lastrowid
            if kind == "chat":
                if sender_kind == "human":
                    self.con.execute(
                        "UPDATE rooms SET last_msg_at=?, hop_count=0 WHERE id=?",
                        (now, room_id),
                    )
                elif sender_kind == "agent":
                    self.con.execute(
                        "UPDATE rooms SET last_msg_at=?, hop_count=hop_count+1 WHERE id=?",
                        (now, room_id),
                    )
                else:
                    self.con.execute("UPDATE rooms SET last_msg_at=? WHERE id=?", (now, room_id))
                rows = self.con.execute(
                    "SELECT id, screen_name FROM memberships WHERE room_id=? AND left_at IS NULL",
                    (room_id,),
                ).fetchall()
                for r in rows:
                    if (sender_membership_id is not None and r["id"] == sender_membership_id) or r[
                        "id"
                    ] in skip:
                        continue
                    mentioned = r["screen_name"].lower() in mentions_l
                    prio = 2 if sender_kind == "human" else (1 if mentioned else 0)
                    self.con.execute(
                        "INSERT INTO deliveries(membership_id, message_id, prio, mentioned) VALUES(?,?,?,?)",
                        (r["id"], mid, prio, int(mentioned)),
                    )
        assert mid is not None
        msg = self.get_message(mid)
        assert msg is not None
        return msg

    def get_message(self, message_id: int) -> Message | None:
        r = self.con.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return Message.from_row(r) if r else None

    def history(self, room_id: int, after: int | None = None, limit: int = 100) -> list[Message]:
        """Messages in id order: after ``after`` (oldest first), or the last ``limit``."""
        limit = max(1, min(int(limit), 1000))
        if after is None:
            rows = self.con.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (room_id, limit),
            ).fetchall()
        else:
            rows = self.con.execute(
                "SELECT * FROM messages WHERE room_id=? AND id>? ORDER BY id LIMIT ?",
                (room_id, int(after), limit),
            ).fetchall()
        return [Message.from_row(r) for r in rows]

    def count_messages(self, room_id: int, after: int = 0, before: int | None = None) -> int:
        """Messages in one room with ``after < id < before`` (ids are global, so count rows)."""
        sql = "SELECT COUNT(*) FROM messages WHERE room_id=? AND id>?"
        args: list[int] = [room_id, int(after)]
        if before is not None:
            sql += " AND id<?"
            args.append(int(before))
        return int(self.con.execute(sql, args).fetchone()[0])

    def last_message_id(self, room_id: int) -> int:
        r = self.con.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id=?", (room_id,)
        ).fetchone()
        return int(r[0])

    # ---------------------------------------------------- memberships (reads)
    def active_names(self, room_id: int) -> list[str]:
        rows = self.con.execute(
            "SELECT screen_name FROM memberships WHERE room_id=? AND left_at IS NULL ORDER BY joined_at",
            (room_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def members(self, room_id: int) -> list[Member]:
        rows = self.con.execute(
            "SELECT m.id AS membership_id, m.participant_id, m.room_id, m.screen_name,"
            " m.held, m.joined_at, p.harness, p.status, p.tier, p.tier_note, p.away,"
            " p.approval_mode, p.env_leak, p.host,"
            " (SELECT COUNT(*) FROM deliveries d WHERE d.membership_id=m.id"
            "   AND d.state='pending') AS queued,"
            " (SELECT COUNT(*) FROM deliveries d WHERE d.membership_id=m.id"
            "   AND d.state='offered') AS inflight"
            " FROM memberships m JOIN participants p ON p.id=m.participant_id"
            " WHERE m.room_id=? AND m.left_at IS NULL ORDER BY m.joined_at, m.id",
            (room_id,),
        ).fetchall()
        return [
            Member(
                membership_id=r["membership_id"],
                participant_id=r["participant_id"],
                room_id=r["room_id"],
                name=r["screen_name"],
                harness=r["harness"],
                status=r["status"],
                tier=r["tier"],
                tier_note=r["tier_note"],
                away=r["away"],
                approval_mode=r["approval_mode"],
                env_leak=bool(r["env_leak"]),
                held=bool(r["held"]),
                joined_at=r["joined_at"],
                queued=r["queued"],
                inflight=r["inflight"],
                host=r["host"],
            )
            for r in rows
        ]

    def find_member(self, room_id: int, name: str) -> Member | None:
        for m in self.members(room_id):
            if m.name.lower() == name.lower():
                return m
        return None

    def set_held(self, membership_id: int, held: bool) -> None:
        now = self.clock.now()
        with db.tx(self.con):
            self.con.execute(
                "UPDATE memberships SET held=?, held_at=? WHERE id=? AND left_at IS NULL",
                (int(held), now if held else None, membership_id),
            )

    def end_membership(
        self, membership_id: int, reason: str, *, kicked: bool = False, keep_cred: bool = False
    ) -> None:
        """Leave, kick or session end: revoke the credential and open deliveries.
        ``keep_cred`` (``/close``, DESIGN.md §28.2) keeps the hash so the broker can name the
        closed room in the error; it never authorizes again (every lookup that does wants
        ``left_at IS NULL``)."""
        now = self.clock.now()
        with db.tx(self.con):
            self.con.execute(
                "UPDATE memberships SET left_at=?, left_reason=?,"
                " cred_hash=CASE WHEN ? THEN cred_hash ELSE NULL END,"
                " kicked=MAX(kicked, ?) WHERE id=? AND left_at IS NULL",
                (now, reason, int(keep_cred), int(kicked), membership_id),
            )
            self.con.execute(
                "UPDATE deliveries SET state='revoked' WHERE membership_id=?"
                " AND state IN ('pending','offered','in_context')",
                (membership_id,),
            )
            self.con.execute(
                "UPDATE batches SET state='cancelled', expired_at=?, expire_reason=?"
                " WHERE membership_id=? AND state='offered'",
                (now, reason, membership_id),
            )

    def membership_delivery_counts(self, membership_id: int) -> dict[str, int]:
        rows = self.con.execute(
            "SELECT state, COUNT(*) FROM deliveries WHERE membership_id=? GROUP BY state",
            (membership_id,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    # --------------------------------------------- member detail (the Inspector, §29)
    # Read-only queries behind the web UI's Inspector (GET /api/rooms/{slug}/members/{name}).
    # None of them selects message text: the Inspector shows ids and times, and the browser
    # resolves ids against the history it already holds. RoomService.member_detail whitelists
    # every field before it leaves the broker.
    def member_times(self, membership_id: int) -> dict[str, Any] | None:
        """One membership's clock facts: ``joined_at`` and ``held_at`` (the membership), the
        participant's ``status_at``, ``status_src``, ``last_seen`` and ``last_say_at``, and
        ``last_pass_at`` (its newest ``pass`` event in this membership). None if no such row."""
        r = self.con.execute(
            "SELECT m.joined_at, m.held_at, p.status_at, p.status_src, p.last_seen, p.last_say_at,"
            " (SELECT MAX(e.ts) FROM events e WHERE e.kind='pass' AND e.membership_id=m.id)"
            "   AS last_pass_at"
            " FROM memberships m JOIN participants p ON p.id=m.participant_id WHERE m.id=?",
            (membership_id,),
        ).fetchone()
        return {k: r[k] for k in r.keys()} if r is not None else None

    def pending_deliveries(self, membership_id: int, limit: int = 50) -> list[tuple[int, int]]:
        """``(message_id, prio)`` of the member's queued (pending) deliveries, oldest first."""
        rows = self.con.execute(
            "SELECT message_id, prio FROM deliveries WHERE membership_id=? AND state='pending'"
            " ORDER BY message_id LIMIT ?",
            (membership_id, limit),
        ).fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]

    # The events the Inspector's delivery timeline shows (the delivery engine's and pass()).
    TIMELINE_EVENT_KINDS = (
        "offer",
        "expire",
        "cancel",
        "parked",
        "unparked",
        "rearm",
        "requeue",
        "watchdog_remind",
        "watchdog_escalate",
        "pass",
    )

    def member_timeline(self, membership_id: int, since: float, limit: int = 6) -> list[dict[str, Any]]:
        """The newest ``limit`` delivery events of a membership since ``since``, plus its own
        chat messages as ``said`` entries (id and time only, never text), oldest first.
        Each entry: ``{"ts", "kind", "data": dict, "id": message id for 'said' else None}``.
        The caller whitelists ``data``: it is engine-internal and never leaves as is."""
        kinds = self.TIMELINE_EVENT_KINDS
        rows = self.con.execute(
            "SELECT ts, kind, data, NULL AS mid, id AS ord FROM events"
            f" WHERE membership_id=? AND ts>=? AND kind IN ({','.join('?' * len(kinds))})"
            " UNION ALL"
            " SELECT ts, 'said', '{}', id, id FROM messages"
            " WHERE room_id=(SELECT room_id FROM memberships WHERE id=?)"
            "   AND sender_membership_id=? AND kind='chat' AND ts>=?"
            " ORDER BY ts DESC, ord DESC LIMIT ?",
            (membership_id, since, *kinds, membership_id, membership_id, since, limit),
        ).fetchall()
        out = []
        for r in reversed(rows):
            try:
                data = json.loads(r["data"]) if r["data"] else {}
            except ValueError:  # pragma: no cover - the broker writes events as JSON only
                data = {}
            out.append(
                {
                    "ts": r["ts"],
                    "kind": r["kind"],
                    "data": data if isinstance(data, dict) else {},
                    "id": r["mid"],
                }
            )
        return out

    def offer_senders(
        self, membership_id: int, message_ids: Sequence[int]
    ) -> list[tuple[int, str, str | None]]:
        """``(prio, sender_name, sender_host)`` of the member's deliveries of these messages,
        in id order (the Inspector's "from" list of an offer). At most 20 ids are looked up."""
        ids = [int(i) for i in message_ids][:20]
        if not ids:
            return []
        rows = self.con.execute(
            "SELECT d.prio, m.sender_name, m.sender_host FROM deliveries d"
            " JOIN messages m ON m.id=d.message_id"
            f" WHERE d.membership_id=? AND d.message_id IN ({','.join('?' * len(ids))})"
            " ORDER BY d.message_id",
            (membership_id, *ids),
        ).fetchall()
        return [(int(r[0]), r[1], r[2]) for r in rows]

    def batch_expiry_counts(self, membership_id: int) -> dict[str, int]:
        rows = self.con.execute(
            "SELECT COALESCE(expire_reason, '?'), COUNT(*) FROM batches"
            " WHERE membership_id=? AND state='expired' GROUP BY expire_reason",
            (membership_id,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def count_active_members(self) -> int:
        r = self.con.execute("SELECT COUNT(*) FROM memberships WHERE left_at IS NULL").fetchone()
        return int(r[0])

    # ----------------------------------------------------------------- events
    def add_event(
        self,
        kind: str,
        *,
        room_id: int | None = None,
        membership_id: int | None = None,
        participant_id: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        now = self.clock.now()
        with db.tx(self.con):
            cur = self.con.execute(
                "INSERT INTO events(ts, room_id, membership_id, participant_id, kind, data)"
                " VALUES(?,?,?,?,?,?)",
                (now, room_id, membership_id, participant_id, kind, json.dumps(data or {})),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def recent_events(
        self,
        *,
        room_id: int | None = None,
        kinds: Iterable[str] | None = None,
        limit: int = 10,
    ) -> list[Event]:
        sql = "SELECT * FROM events WHERE 1=1"
        args: list[Any] = []
        if room_id is not None:
            sql += " AND room_id=?"
            args.append(room_id)
        if kinds is not None:
            kinds = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [Event.from_row(r) for r in self.con.execute(sql, args).fetchall()]

    # ------------------------------------------------------------ web sessions
    def web_session_create(
        self, id_hash: str, ttl_s: float, via: str | None = None, person_id: int | None = None
    ) -> None:
        """``via`` (schema 3, DESIGN.md §31.2): how the session was made, ``login-link``,
        ``claim``, ``passkey:<name>`` or ``password``. ``person_id`` (schema 4, §32.2): whose
        it is, None for the owner (and for everyone on a desktop broker)."""
        now = self.clock.now()
        with db.tx(self.con):
            self.con.execute(
                "INSERT INTO web_sessions(id_hash, created_at, last_seen, expires_at, via, person_id)"
                " VALUES(?,?,?,?,?,?)",
                (id_hash, now, now, now + ttl_s, via, person_id),
            )

    def web_session_person(self, id_hash: str) -> int | None:
        """Whose session this is: a person's id, or None for the owner."""
        r = self.con.execute("SELECT person_id FROM web_sessions WHERE id_hash=?", (id_hash,)).fetchone()
        return int(r[0]) if r and r[0] is not None else None

    def web_sessions_of(self, person_id: int | None) -> list[str]:
        """The id hashes of one person's sessions (None: the owner's)."""
        if person_id is None:
            rows = self.con.execute("SELECT id_hash FROM web_sessions WHERE person_id IS NULL").fetchall()
        else:
            rows = self.con.execute(
                "SELECT id_hash FROM web_sessions WHERE person_id=?", (person_id,)
            ).fetchall()
        return [str(r[0]) for r in rows]

    def web_session_delete_person(self, person_id: int | None) -> list[str]:
        """Sign one person out everywhere (None: the owner): their sessions' id hashes."""
        with db.tx(self.con):
            gone = self.web_sessions_of(person_id)
            self.con.executemany("DELETE FROM web_sessions WHERE id_hash=?", [(h,) for h in gone])
        return gone

    def web_session_via(self, id_hash: str) -> str | None:
        r = self.con.execute("SELECT via FROM web_sessions WHERE id_hash=?", (id_hash,)).fetchone()
        return r[0] if r else None

    def web_session_touch(self, id_hash: str, ttl_s: float) -> bool:
        """True when the session exists and hasn't expired; slides its expiry."""
        now = self.clock.now()
        with db.tx(self.con):
            r = self.con.execute("SELECT expires_at FROM web_sessions WHERE id_hash=?", (id_hash,)).fetchone()
            if r is None:
                return False
            if r[0] <= now:
                self.con.execute("DELETE FROM web_sessions WHERE id_hash=?", (id_hash,))
                return False
            self.con.execute(
                "UPDATE web_sessions SET last_seen=?, expires_at=? WHERE id_hash=?",
                (now, now + ttl_s, id_hash),
            )
            return True

    def web_session_delete(self, id_hash: str) -> int:
        with db.tx(self.con):
            return self.con.execute("DELETE FROM web_sessions WHERE id_hash=?", (id_hash,)).rowcount

    def web_session_delete_all(self) -> int:
        with db.tx(self.con):
            return self.con.execute("DELETE FROM web_sessions").rowcount

    def web_session_purge(self) -> int:
        now = self.clock.now()
        with db.tx(self.con):
            return self.con.execute("DELETE FROM web_sessions WHERE expires_at<=?", (now,)).rowcount

    def web_session_valid(self, id_hash: str) -> bool:
        """Non-sliding check (used to drop WebSockets of expired or revoked sessions)."""
        r = self.con.execute("SELECT expires_at FROM web_sessions WHERE id_hash=?", (id_hash,)).fetchone()
        return r is not None and r[0] > self.clock.now()

    def web_session_count(self) -> int:
        return int(self.con.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0])

    # ------------------------------------------------------------------- meta
    def meta_get(self, key: str) -> str | None:
        r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(r[0]) if r else None

    def meta_set(self, key: str, value: str) -> None:
        with db.tx(self.con):
            self.con.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET"
                " value=excluded.value",
                (key, value),
            )

    def meta_delete(self, key: str) -> None:
        with db.tx(self.con):
            self.con.execute("DELETE FROM meta WHERE key=?", (key,))

    # ---------------------------------------------------------- the owner (§31)
    # A hosted broker has one owner: whoever claimed it (DESIGN.md §31.3). ``owner_handle``
    # is WebAuthn's user id for that one owner (16 random bytes, hex in ``meta``); the
    # passkeys are the owner's. ``reset_owner`` is SWITCHBOARD_RESET_OWNER, once per value.
    def owner_handle(self) -> bytes | None:
        v = self.meta_get("owner_handle")
        return bytes.fromhex(v) if v else None

    def owner_claimed_at(self) -> float | None:
        v = self.meta_get("owner_claimed_at")
        return float(v) if v else None

    def claim_owner(self, handle: bytes) -> None:
        if len(handle) != 16:
            raise ValueError("an owner handle is 16 bytes")
        now = self.clock.now()
        with db.tx(self.con):
            self.meta_set("owner_handle", handle.hex())
            self.meta_set("owner_claimed_at", repr(now))

    def reset_owner_applied(self) -> str | None:
        return self.meta_get("reset_owner_applied")

    def reset_owner(self, applied: str) -> dict[str, int]:
        """The owner reset (DESIGN.md §31.5), in one transaction: every passkey and web
        session is deleted, the owner is cleared, every paired machine goes back to pending
        (whoever held the broker may have paired one), and ``applied`` (the hash of the
        SWITCHBOARD_RESET_OWNER value acted on) is recorded, so the same value never resets
        again. Returns what it removed or changed."""
        now = self.clock.now()
        with db.tx(self.con):
            n_keys = self.con.execute("DELETE FROM passkeys").rowcount
            n_sess = self.con.execute("DELETE FROM web_sessions").rowcount
            n_mach = self.con.execute(
                "UPDATE link_machines SET approved_at=NULL, approved_via=NULL"
                " WHERE removed_at IS NULL AND approved_at IS NOT NULL"
            ).rowcount
            # everyone else was added by the old owner: the new owner adds them again
            n_people = self.con.execute(
                "UPDATE people SET removed_at=?, password_hash=NULL, must_reset=0,"
                " password_expires_at=NULL WHERE removed_at IS NULL",
                (now,),
            ).rowcount
            self.meta_delete("owner_password")
            self.meta_delete("owner_handle")
            self.meta_delete("owner_claimed_at")
            self.meta_set("reset_owner_applied", applied)
        return {"passkeys": n_keys, "sessions": n_sess, "machines_pending": n_mach, "people": n_people}

    # --------------------------------------------------------------- passkeys
    def passkey_add(
        self,
        credential_id: bytes,
        public_key: bytes,
        name: str,
        aaguid: str | None,
        sign_count: int = 0,
        person_id: int | None = None,
    ) -> PasskeyRow:
        """``person_id``: whose passkey (None: the owner's)."""
        if not credential_id or len(credential_id) > 1023 or not public_key:
            raise ValueError("a passkey needs a credential id and a public key")
        now = self.clock.now()
        with db.tx(self.con):
            self.con.execute(
                "INSERT INTO passkeys(credential_id, public_key, sign_count, name, aaguid,"
                " created_at, person_id)"
                " VALUES(?,?,?,?,?,?,?)",
                (credential_id, public_key, int(sign_count), name, aaguid, now, person_id),
            )
        got = self.passkey(credential_id)
        assert got is not None
        return got

    def passkey(self, credential_id: bytes) -> PasskeyRow | None:
        r = self.con.execute("SELECT * FROM passkeys WHERE credential_id=?", (credential_id,)).fetchone()
        return PasskeyRow.from_row(r) if r else None

    def passkeys(self) -> list[PasskeyRow]:
        return [
            PasskeyRow.from_row(r)
            for r in self.con.execute("SELECT * FROM passkeys ORDER BY created_at, name")
        ]

    def passkey_count(self) -> int:
        """Everyone's: whether anyone can sign in with a passkey here."""
        return int(self.con.execute("SELECT COUNT(*) FROM passkeys").fetchone()[0])

    def passkeys_of(self, person_id: int | None) -> list[PasskeyRow]:
        """One person's passkeys (None: the owner's)."""
        if person_id is None:
            rows = self.con.execute(
                "SELECT * FROM passkeys WHERE person_id IS NULL ORDER BY created_at, name"
            )
        else:
            rows = self.con.execute(
                "SELECT * FROM passkeys WHERE person_id=? ORDER BY created_at, name", (person_id,)
            )
        return [PasskeyRow.from_row(r) for r in rows]

    def passkey_used(self, credential_id: bytes, sign_count: int) -> bool:
        """A sign-in with this passkey succeeded now: its last use and the counter it sent."""
        now = self.clock.now()
        with db.tx(self.con):
            return (
                self.con.execute(
                    "UPDATE passkeys SET sign_count=?, last_used_at=? WHERE credential_id=?",
                    (int(sign_count), now, credential_id),
                ).rowcount
                == 1
            )

    # ----------------------------------------------------------------- people
    # Everyone but the owner (DESIGN.md §32): the owner adds them by name in the admin
    # section, with a one-time password to hand over. A removed person keeps their row, so
    # their messages keep their name, and loses their password, passkeys and sessions at once.
    def person(self, person_id: int) -> PersonRow | None:
        r = self.con.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
        return PersonRow.from_row(r) if r else None

    def person_named(self, name: str) -> PersonRow | None:
        """The active person with this name (any case)."""
        r = self.con.execute("SELECT * FROM people WHERE name=? AND removed_at IS NULL", (name,)).fetchone()
        return PersonRow.from_row(r) if r else None

    def person_by_handle(self, handle: bytes) -> PersonRow | None:
        r = self.con.execute("SELECT * FROM people WHERE handle=?", (handle,)).fetchone()
        return PersonRow.from_row(r) if r else None

    def people(self) -> list[PersonRow]:
        """The active people, in the order they were added."""
        return [
            PersonRow.from_row(r)
            for r in self.con.execute("SELECT * FROM people WHERE removed_at IS NULL ORDER BY created_at, id")
        ]

    def person_add(self, name: str, handle: bytes, one_time_hash: str, ttl_s: float) -> PersonRow:
        """A new person with a one-time password (its hash), good for ``ttl_s``. Raises
        ValueError when an active person has that name already."""
        if len(handle) != 16:
            raise ValueError("a person's handle is 16 bytes")
        now = self.clock.now()
        with db.tx(self.con):
            if self.person_named(name) is not None:
                raise ValueError("that name is taken")
            cur = self.con.execute(
                "INSERT INTO people(name, handle, password_hash, must_reset, password_expires_at, created_at)"
                " VALUES(?,?,?,1,?,?)",
                (name, handle, one_time_hash, now + ttl_s, now),
            )
            pid = int(cur.lastrowid or 0)
        got = self.person(pid)
        assert got is not None
        return got

    def person_one_time_password(self, person_id: int, one_time_hash: str, ttl_s: float) -> list[str] | None:
        """The owner's reset: a new one-time password replaces the person's password, and
        their sessions end (they sign in with it and choose a new one; their passkeys stay).
        Returns the ended sessions' id hashes, or None if there is no such active person."""
        now = self.clock.now()
        with db.tx(self.con):
            if (
                self.con.execute(
                    "UPDATE people SET password_hash=?, must_reset=1, password_expires_at=?"
                    " WHERE id=? AND removed_at IS NULL",
                    (one_time_hash, now + ttl_s, person_id),
                ).rowcount
                != 1
            ):
                return None
            return self.web_session_delete_person(person_id)

    def person_set_password(self, person_id: int, password_hash: str | None) -> bool:
        """The person chose their own password (or, with None, a passkey instead of one):
        the one-time password is gone and nothing is left to reset."""
        with db.tx(self.con):
            return (
                self.con.execute(
                    "UPDATE people SET password_hash=?, must_reset=0, password_expires_at=NULL"
                    " WHERE id=? AND removed_at IS NULL",
                    (password_hash, person_id),
                ).rowcount
                == 1
            )

    def person_remove(self, person_id: int) -> list[str] | None:
        """In one transaction: the person is removed, their password and passkeys deleted and
        their sessions ended. Returns the ended sessions' id hashes, or None if there was no
        such active person."""
        now = self.clock.now()
        with db.tx(self.con):
            if (
                self.con.execute(
                    "UPDATE people SET removed_at=?, password_hash=NULL, must_reset=0,"
                    " password_expires_at=NULL WHERE id=? AND removed_at IS NULL",
                    (now, person_id),
                ).rowcount
                != 1
            ):
                return None
            self.con.execute("DELETE FROM passkeys WHERE person_id=?", (person_id,))
            return self.web_session_delete_person(person_id)

    # ------------------------------------------------------------ preferences
    # 0 is the owner (desktop or hosted); positive ids belong to the people table.
    # Only the web route chooses the id, from its authenticated session.
    def preferences(self, person_id: int | None) -> dict[str, str]:
        key = person_id if person_id is not None else 0
        row = self.con.execute(
            "SELECT theme, text_size, room_rules FROM preferences WHERE person_id=?", (key,)
        ).fetchone()
        return {
            "theme": row["theme"] if row else "system",
            "text_size": row["text_size"] if row else "default",
            "room_rules": row["room_rules"] if row else "",
        }

    def set_preferences(self, person_id: int | None, changes: dict[str, str]) -> dict[str, str]:
        if not changes or set(changes) - {"theme", "text_size", "room_rules"}:
            raise ValueError("invalid preferences")
        if "theme" in changes and changes["theme"] not in ("system", "light", "dark"):
            raise ValueError("invalid theme")
        if "text_size" in changes and changes["text_size"] not in ("small", "default", "large", "larger"):
            raise ValueError("invalid text size")
        if "room_rules" in changes and (
            not isinstance(changes["room_rules"], str) or len(changes["room_rules"]) > 2000
        ):
            raise ValueError("room rules must be at most 2000 characters")
        key = person_id if person_id is not None else 0
        with db.tx(self.con):
            saved = self.preferences(person_id) | changes
            self.con.execute(
                "INSERT INTO preferences(person_id, theme, text_size, room_rules) VALUES(?, ?, ?, ?)"
                " ON CONFLICT(person_id) DO UPDATE SET theme=excluded.theme,"
                " text_size=excluded.text_size, room_rules=excluded.room_rules",
                (key, saved["theme"], saved["text_size"], saved["room_rules"]),
            )
        return self.preferences(person_id)

    # ------------------------------------------------------- the owner's password
    def owner_password_hash(self) -> str | None:
        return self.meta_get("owner_password")

    def set_owner_password(self, password_hash: str | None) -> None:
        with db.tx(self.con):
            if password_hash is None:
                self.meta_delete("owner_password")
            else:
                self.meta_set("owner_password", password_hash)

    # --------------------------------------------------------------- recovery
    def recover_on_start(self, alive: Callable[[int, float | None], bool | None]) -> dict[str, int]:
        """Restart semantics (§4): expire open offers, mark everyone offline,
        end participants whose agent process is gone.

        ``alive`` probes this machine's processes, so only local rows (``host = ''``)
        are probed: a remote row's pids are pids on its own host. Remote rows go
        offline like every row and wait for their link (DESIGN.md §27.5.6)."""
        now = self.clock.now()
        ended: list[tuple[int, int, str]] = []  # (room_id, membership_id, name)
        with db.tx(self.con):
            reverted = self.con.execute(
                "UPDATE deliveries SET state='pending', attempts=attempts+1 WHERE state='offered'"
            ).rowcount
            expired = self.con.execute(
                "UPDATE batches SET state='expired', expired_at=?, expire_reason='restart'"
                " WHERE state='offered'",
                (now,),
            ).rowcount
            offline = self.con.execute(
                "UPDATE participants SET status='offline', status_at=?, status_src='restart'"
                " WHERE ended_at IS NULL",
                (now,),
            ).rowcount
            parts = self.con.execute(
                "SELECT id, agent_pid, agent_start FROM participants"
                " WHERE ended_at IS NULL AND agent_pid IS NOT NULL AND host=?",
                (LOCAL_HOST,),
            ).fetchall()
            n_ended = 0
            for p in parts:
                if alive(p["agent_pid"], p["agent_start"]) is not False:
                    continue  # alive, or can't tell (None): never ended on a guess
                n_ended += 1
                self.con.execute("UPDATE participants SET ended_at=? WHERE id=?", (now, p["id"]))
                for m in self.con.execute(
                    "SELECT id, room_id, screen_name FROM memberships"
                    " WHERE participant_id=? AND left_at IS NULL",
                    (p["id"],),
                ).fetchall():
                    self.end_membership(m["id"], "session_end")
                    ended.append((m["room_id"], m["id"], m["screen_name"]))
            for room_id, membership_id, name in ended:
                self.insert_message(
                    room_id,
                    sender_name=name,
                    sender_kind="agent",
                    via="system",
                    kind="leave",
                    text="left (session ended)",
                    sender_membership_id=membership_id,
                )
        return {
            "batches_expired": expired,
            "deliveries_reverted": reverted,
            "participants_offline": offline,
            "participants_ended": n_ended,
        }

    # ---------------------------------------------------------------- remotes
    # One row per remote that was ever enabled (DESIGN.md §27.6, §27.5.8): the broker
    # dials a remote only when its row holds an enabled_at for the current config_hash
    # and no block (blocked_at is NULL): ``RemoteRow.may_dial`` is the one gate. A
    # block (host key, auth, replaced) holds until the next enable clears it.
    def remote_row(self, name: str) -> RemoteRow | None:
        r = self.con.execute("SELECT * FROM remotes WHERE name=?", (name,)).fetchone()
        return RemoteRow.from_row(r) if r else None

    def remote_rows(self) -> list[RemoteRow]:
        return [RemoteRow.from_row(r) for r in self.con.execute("SELECT * FROM remotes ORDER BY name")]

    @staticmethod
    def _remote_name(name: str) -> str:
        if not valid_host(name):
            raise ValueError(f"not a remote name: {name!r}")
        return name

    def set_remote_enabled(self, name: str, config_hash: str, via: str) -> RemoteRow:
        """The human enabled this remote for exactly ``config_hash`` (``via`` cli or web).
        Enabling also clears a block (§27.4.7)."""
        self._remote_name(name)
        if not isinstance(config_hash, str) or not CONFIG_HASH_RE.match(config_hash):
            raise ValueError("config_hash must be a sha256 hex digest")
        if via not in ("cli", "web"):
            raise ValueError(f"enabled via {via!r}")
        now = self.clock.now()
        with db.tx(self.con):
            self.con.execute(
                "INSERT INTO remotes(name, config_hash, enabled_at, enabled_via, blocked_at, blocked_reason)"
                " VALUES(?,?,?,?,NULL,NULL) ON CONFLICT(name) DO UPDATE SET config_hash=excluded.config_hash,"
                " enabled_at=excluded.enabled_at, enabled_via=excluded.enabled_via,"
                " blocked_at=NULL, blocked_reason=NULL",
                (name, config_hash, now, via),
            )
        got = self.remote_row(name)
        assert got is not None
        return got

    def set_remote_disabled(self, name: str) -> bool:
        """``remote disable``: forget the consent (the row and its hash stay). False if no row."""
        self._remote_name(name)
        with db.tx(self.con):
            return (
                self.con.execute(
                    "UPDATE remotes SET enabled_at=NULL, enabled_via=NULL WHERE name=?", (name,)
                ).rowcount
                == 1
            )

    def set_remote_blocked(self, name: str, reason: str) -> bool:
        """The link needs the owner (``host_key``, ``auth``, ``replaced``, ...): no retry
        until the next enable. False if the remote has no row."""
        self._remote_name(name)
        if not isinstance(reason, str) or not REASON_RE.match(reason):
            raise ValueError(f"blocked reason {reason!r}")
        now = self.clock.now()
        with db.tx(self.con):
            return (
                self.con.execute(
                    "UPDATE remotes SET blocked_at=?, blocked_reason=? WHERE name=?", (now, reason, name)
                ).rowcount
                == 1
            )

    def touch_remote_up(self, name: str) -> bool:
        """The link came up (a handshake completed) now."""
        self._remote_name(name)
        now = self.clock.now()
        with db.tx(self.con):
            return self.con.execute("UPDATE remotes SET last_up_at=? WHERE name=?", (now, name)).rowcount == 1

    def clear_remote(self, name: str) -> bool:
        """``remote remove``: drop the row (consent, block and history). False if none."""
        self._remote_name(name)
        with db.tx(self.con):
            return self.con.execute("DELETE FROM remotes WHERE name=?", (name,)).rowcount == 1

    # ------------------------------------------------------ machines (§31.7)
    # A machine that dials in: pending from its pairing until the owner approves it, approved
    # until Remove (which forgets its key; the row stays, so a name keeps its history). A code
    # made for a removed machine's name pairs a new key under it, pending again.
    def machine(self, name: str) -> MachineRow | None:
        r = self.con.execute("SELECT * FROM link_machines WHERE name=?", (name,)).fetchone()
        return MachineRow.from_row(r) if r else None

    def machines(self, *, removed: bool = False) -> list[MachineRow]:
        sql = (
            "SELECT * FROM link_machines"
            + ("" if removed else " WHERE removed_at IS NULL")
            + " ORDER BY name"
        )
        return [MachineRow.from_row(r) for r in self.con.execute(sql)]

    def machine_pair(self, name: str, key: bytes, key_fp: str, facts: dict[str, Any]) -> MachineRow:
        """A machine paired with a code: a new pending row, or a removed one's name again."""
        if not valid_host(name) or len(key) != 32:
            raise ValueError("a machine needs a name like work-laptop and a 32-byte key")
        now = self.clock.now()
        with db.tx(self.con):
            old = self.machine(name)
            if old is not None and not old.removed:
                raise Conflict(f"{name} is paired already: remove it first")
            self.con.execute("DELETE FROM link_machines WHERE name=?", (name,))
            self.con.execute(
                "INSERT INTO link_machines(name, key, key_fp, facts, created_at) VALUES(?,?,?,?,?)",
                (name, key, key_fp, json.dumps(facts, sort_keys=True), now),
            )
        got = self.machine(name)
        assert got is not None
        return got

    def machine_approve(self, name: str, via: str) -> bool:
        """The owner approved a pending machine. False if it isn't pending."""
        if via not in ("cli", "web"):
            raise ValueError(f"approved via {via!r}")
        now = self.clock.now()
        with db.tx(self.con):
            return (
                self.con.execute(
                    "UPDATE link_machines SET approved_at=?, approved_via=? WHERE name=?"
                    " AND approved_at IS NULL"
                    " AND removed_at IS NULL",
                    (now, via, name),
                ).rowcount
                == 1
            )

    def machine_remove(self, name: str) -> bool:
        """Remove: the key is forgotten (a connection with it is refused from now on)."""
        now = self.clock.now()
        with db.tx(self.con):
            return (
                self.con.execute(
                    "UPDATE link_machines SET removed_at=?, key=X'', key_fp='' WHERE name=?"
                    " AND removed_at IS NULL",
                    (now, name),
                ).rowcount
                == 1
            )

    def machine_seen(self, name: str, t: float) -> None:
        with db.tx(self.con):
            self.con.execute(
                "UPDATE link_machines SET last_seen_at=? WHERE name=? AND removed_at IS NULL", (t, name)
            )

    # ============================================================ M2: agents
    # ----------------------------------------------------------- participants
    _PARTICIPANT_COLS = frozenset(
        {
            "session_id",
            "agent_pid",
            "agent_start",
            "mcp_pid",
            "mcp_start",
            "claude_socket",
            "bind_state",
            "bind_nonce",
            "thread_proof",
            "status",
            "status_at",
            "status_src",
            "tier",
            "tier_note",
            "approval_mode",
            "env_leak",
            "away",
            "boundary_seq",
            "gen",
            "gen_tainted",
            "rearms_in_gen",
            "last_loop_count",
            "unconfirmed_followups",
            "push_expiries",
            "hooks_seen_at",
            "last_say_at",
            "last_seen",
            "ended_at",
            "session_key",
            "host",
        }
    )

    def joins_since_thread_proof(self, participant_id: int) -> set[int]:
        """The memberships this participant joined with a "verifying..." join line (``join``
        events with ``verifying: true``) after its last passed Codex thread proof (a ``bind``
        event with ``what: thread_proof, ok: true``): the joins no "is verified" notice followed
        yet. A proof whose notice was held for the first look for a TUI (``notice: held``)
        doesn't count until the notice went out (``what: verified_notice``). Its
        ``thread_proof`` column is reset by a join from another MCP process; this history
        isn't (DESIGN.md §9.3)."""
        rows = self.con.execute(
            "SELECT * FROM events WHERE kind IN ('join', 'bind') AND participant_id=? ORDER BY id",
            (participant_id,),
        ).fetchall()
        out: set[int] = set()
        for e in (Event.from_row(r) for r in rows):
            if e.kind == "join":
                if e.data.get("verifying") is True and e.membership_id is not None:
                    out.add(e.membership_id)
            elif (
                e.data.get("what") == "thread_proof"
                and e.data.get("ok") is True
                and e.data.get("notice") != "held"
            ) or e.data.get("what") == "verified_notice":
                out.clear()
        return out

    def get_participant(self, participant_id: int) -> Participant | None:
        r = self.con.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
        return Participant.from_row(r) if r else None

    def find_participant(self, harness: str, session_key: str) -> Participant | None:
        r = self.con.execute(
            "SELECT * FROM participants WHERE harness=? AND session_key=?", (harness, session_key)
        ).fetchone()
        return Participant.from_row(r) if r else None

    def upsert_participant(self, harness: str, session_key: str, **fields: Any) -> Participant:
        """Create the participant, or refresh an existing one (and re-activate it)."""
        bad = set(fields) - self._PARTICIPANT_COLS
        if bad:
            raise ValueError(f"unknown participant fields: {sorted(bad)}")
        host = fields.pop("host", LOCAL_HOST)
        if host != LOCAL_HOST and not valid_host(host):
            raise ValueError(f"not a host name: {host!r}")
        now = self.clock.now()
        with db.tx(self.con):
            cur = self.find_participant(harness, session_key)
            if cur is None:
                # a session key names its host (§27.5.4): '<h>:...' here, '<h>@<host>:...' remote
                if not session_key.startswith(make_session_key(harness, host, "")):
                    raise ValueError(f"session key {session_key!r} is not a {harness} key of host {host!r}")
                cols = ["harness", "session_key", "created_at", "last_seen", "host", *fields]
                vals = [harness, session_key, now, now, host, *fields.values()]
                c = self.con.execute(
                    f"INSERT INTO participants({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                    vals,
                )
                assert c.lastrowid is not None
                pid = c.lastrowid
            else:
                if cur.host != host:
                    raise ValueError("a session never moves between hosts")
                pid = cur.id
                upd = {**fields, "ended_at": None, "last_seen": now}
                self._update_participant(pid, upd)
        got = self.get_participant(pid)
        assert got is not None
        return got

    def _update_participant(self, participant_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        bad = set(fields) - self._PARTICIPANT_COLS
        if bad:
            raise ValueError(f"unknown participant fields: {sorted(bad)}")
        if "host" in fields:
            raise ValueError("a participant's host is set when it is created and never changes")
        sets = ", ".join(f"{k}=?" for k in fields)
        self.con.execute(f"UPDATE participants SET {sets} WHERE id=?", [*fields.values(), participant_id])

    def update_participant(self, participant_id: int, **fields: Any) -> Participant:
        with db.tx(self.con):
            self._update_participant(participant_id, fields)
        got = self.get_participant(participant_id)
        assert got is not None
        return got

    def set_status(
        self, participant_id: int, status: str, src: str, *, bump_boundary: bool = False
    ) -> tuple[Participant, Participant]:
        """Set the observed status; ``bump_boundary`` increments boundary_seq (busy->idle)."""
        now = self.clock.now()
        with db.tx(self.con):
            before = self.get_participant(participant_id)
            if before is None:
                raise NotFound(f"participant {participant_id} not found")
            self.con.execute(
                "UPDATE participants SET status=?, status_at=?, status_src=?,"
                " boundary_seq=boundary_seq+? WHERE id=?",
                (status, now, src, 1 if bump_boundary else 0, participant_id),
            )
        after = self.get_participant(participant_id)
        assert after is not None
        return before, after

    def bump_boundary(self, participant_id: int) -> None:
        """A turn boundary that isn't a status change (a new wait() call)."""
        with db.tx(self.con):
            self.con.execute(
                "UPDATE participants SET boundary_seq=boundary_seq+1 WHERE id=?", (participant_id,)
            )

    def active_participants(self) -> list[Participant]:
        rows = self.con.execute("SELECT * FROM participants WHERE ended_at IS NULL ORDER BY id").fetchall()
        return [Participant.from_row(r) for r in rows]

    def joined_participants(self) -> list[Participant]:
        """Active participants with at least one active membership."""
        rows = self.con.execute(
            "SELECT * FROM participants p WHERE p.ended_at IS NULL AND EXISTS("
            " SELECT 1 FROM memberships m WHERE m.participant_id=p.id AND m.left_at IS NULL)"
            " ORDER BY p.id"
        ).fetchall()
        return [Participant.from_row(r) for r in rows]

    def active_participants_by_agent(self, harness: str, host: str, agent_pid: int) -> list[Participant]:
        """Active participants of ``harness`` on ``host`` whose agent process is
        ``agent_pid`` (a pid on that host), newest first."""
        rows = self.con.execute(
            "SELECT * FROM participants WHERE harness=? AND host=? AND agent_pid=? AND ended_at IS NULL"
            " ORDER BY id DESC",
            (harness, host, agent_pid),
        ).fetchall()
        return [Participant.from_row(r) for r in rows]

    def participants_by_mcp(self, host: str, mcp_pid: int, mcp_start: float | None) -> list[Participant]:
        """Active participants served by the MCP process ``(mcp_pid, mcp_start)`` on ``host``."""
        rows = self.con.execute(
            "SELECT * FROM participants WHERE ended_at IS NULL AND host=? AND mcp_pid=?", (host, mcp_pid)
        ).fetchall()
        out = [Participant.from_row(r) for r in rows]
        return [
            p
            for p in out
            if p.mcp_start is not None and mcp_start is not None and abs(p.mcp_start - mcp_start) < 0.011
        ]

    def end_participant(self, participant_id: int, reason: str = "session_end") -> list[Membership]:
        """End a session: leave every room, revoke creds and deliveries. Returns the ended memberships."""
        now = self.clock.now()
        ended: list[Membership] = []
        with db.tx(self.con):
            for m in self.participant_memberships(participant_id):
                self.end_membership(m.id, reason)
                ended.append(m)
            self.con.execute(
                "UPDATE participants SET ended_at=?, status='offline', status_at=?, status_src=? WHERE id=?",
                (now, now, reason, participant_id),
            )
        return ended

    # ------------------------------------------------------------ memberships
    def create_membership(
        self, room_id: int, participant_id: int, screen_name: str, cred_hash: str
    ) -> Membership:
        now = self.clock.now()
        with db.tx(self.con):
            last = self.last_message_id(room_id)
            cur = self.con.execute(
                "INSERT INTO memberships(room_id, participant_id, screen_name, cred_hash,"
                " joined_at, join_msg_id) VALUES(?,?,?,?,?,?)",
                (room_id, participant_id, screen_name, cred_hash, now, last),
            )
            assert cur.lastrowid is not None
            mid = cur.lastrowid
        got = self.get_membership(mid)
        assert got is not None
        return got

    def get_membership(self, membership_id: int) -> Membership | None:
        r = self.con.execute("SELECT * FROM memberships WHERE id=?", (membership_id,)).fetchone()
        return Membership.from_row(r) if r else None

    def mark_rules_seen(self, membership_id: int, version: int) -> None:
        """Record only the version actually included in a join or delivery frame."""
        with db.tx(self.con):
            self.con.execute(
                "UPDATE memberships SET rules_seen=MAX(rules_seen, ?) WHERE id=? AND left_at IS NULL",
                (version, membership_id),
            )

    def active_membership(self, room_id: int, participant_id: int) -> Membership | None:
        r = self.con.execute(
            "SELECT * FROM memberships WHERE room_id=? AND participant_id=? AND left_at IS NULL",
            (room_id, participant_id),
        ).fetchone()
        return Membership.from_row(r) if r else None

    def active_membership_by_name(self, room_id: int, name: str) -> Membership | None:
        r = self.con.execute(
            "SELECT * FROM memberships WHERE room_id=? AND screen_name=? AND left_at IS NULL",
            (room_id, name),
        ).fetchone()
        return Membership.from_row(r) if r else None

    def membership_by_cred(self, cred_hash: str) -> Membership | None:
        r = self.con.execute(
            "SELECT * FROM memberships WHERE cred_hash=? AND left_at IS NULL", (cred_hash,)
        ).fetchone()
        return Membership.from_row(r) if r else None

    def closed_membership_by_cred(self, cred_hash: str) -> tuple[Membership, Room] | None:
        """The membership ``/close`` ended with this credential, and its room while that is
        still closed: only to name the room in the error (it authorizes nothing)."""
        r = self.con.execute(
            "SELECT * FROM memberships WHERE cred_hash=? AND left_at IS NOT NULL AND left_reason='closed'"
            " ORDER BY id DESC LIMIT 1",
            (cred_hash,),
        ).fetchone()
        if r is None:
            return None
        m = Membership.from_row(r)
        room = self.room_by_id(m.room_id)
        return (m, room) if room is not None and room.closed else None

    def participant_memberships(self, participant_id: int) -> list[Membership]:
        rows = self.con.execute(
            "SELECT * FROM memberships WHERE participant_id=? AND left_at IS NULL ORDER BY id",
            (participant_id,),
        ).fetchall()
        return [Membership.from_row(r) for r in rows]

    def room_memberships(self, room_id: int) -> list[Membership]:
        rows = self.con.execute(
            "SELECT * FROM memberships WHERE room_id=? AND left_at IS NULL ORDER BY id",
            (room_id,),
        ).fetchall()
        return [Membership.from_row(r) for r in rows]

    def all_active_memberships(self) -> list[Membership]:
        rows = self.con.execute("SELECT * FROM memberships WHERE left_at IS NULL ORDER BY id").fetchall()
        return [Membership.from_row(r) for r in rows]

    def rotate_cred(self, membership_id: int, cred_hash: str) -> None:
        with db.tx(self.con):
            self.con.execute(
                "UPDATE memberships SET cred_hash=? WHERE id=? AND left_at IS NULL",
                (cred_hash, membership_id),
            )

    def name_used_by_other(self, room_id: int, name: str, participant_id: int, since: float) -> bool:
        """True if another participant held ``name`` in this room at any time since ``since``."""
        r = self.con.execute(
            "SELECT 1 FROM memberships WHERE room_id=? AND screen_name=? AND participant_id<>?"
            " AND (left_at IS NULL OR left_at>=?) LIMIT 1",
            (room_id, name, participant_id, since),
        ).fetchone()
        return r is not None

    def was_kicked(self, room_id: int, participant_id: int) -> bool:
        r = self.con.execute(
            "SELECT 1 FROM memberships WHERE room_id=? AND participant_id=? AND kicked=1 LIMIT 1",
            (room_id, participant_id),
        ).fetchone()
        return r is not None

    def was_kicked_session(self, room_id: int, harness: str, session_key: str, *, exclude: int) -> bool:
        """True if an earlier participant of this session (the key itself, or an ended
        one renamed ``<key>#ended-<n>``) other than ``exclude`` was kicked from the room."""
        prefix = f"{session_key}#ended-"
        r = self.con.execute(
            "SELECT 1 FROM memberships m JOIN participants p ON p.id=m.participant_id"
            " WHERE m.room_id=? AND m.kicked=1 AND p.harness=? AND p.id<>?"
            " AND (p.session_key=? OR substr(p.session_key, 1, ?)=?) LIMIT 1",
            (room_id, harness, exclude, session_key, len(prefix), prefix),
        ).fetchone()
        return r is not None

    # ------------------------------------------------------------- deliveries
    _ITEM_SQL = (
        "SELECT d.membership_id, d.message_id, d.prio, d.mentioned, d.state, d.batch_id,"
        " d.attempts, d.notified_at, d.redelivered, d.reminders, d.in_context_at,"
        " m.ts, m.sender_name, m.sender_kind,"
        " m.sender_harness, m.text, m.reply_to, m.mentions, m.sender_host"
        " FROM deliveries d JOIN messages m ON m.id=d.message_id"
    )

    def pending_items(self, membership_id: int) -> list[Item]:
        rows = self.con.execute(
            self._ITEM_SQL + " WHERE d.membership_id=? AND d.state='pending' ORDER BY d.message_id",
            (membership_id,),
        ).fetchall()
        return [Item.from_row(r) for r in rows]

    def batch_items(self, batch_id: int) -> list[Item]:
        rows = self.con.execute(
            self._ITEM_SQL + " WHERE d.batch_id=? ORDER BY d.message_id", (batch_id,)
        ).fetchall()
        return [Item.from_row(r) for r in rows]

    def delivery_state(self, membership_id: int, message_id: int) -> str | None:
        r = self.con.execute(
            "SELECT state FROM deliveries WHERE membership_id=? AND message_id=?",
            (membership_id, message_id),
        ).fetchone()
        return r[0] if r else None

    def deliveries(self, membership_id: int) -> list[dict[str, Any]]:
        rows = self.con.execute(
            "SELECT * FROM deliveries WHERE membership_id=? ORDER BY message_id", (membership_id,)
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def unhandled_priority(self, membership_id: int) -> int:
        r = self.con.execute(
            "SELECT COUNT(*) FROM deliveries WHERE membership_id=? AND state='in_context' AND prio>=1",
            (membership_id,),
        ).fetchone()
        return int(r[0])

    def mark_handled(self, membership_id: int) -> int:
        """say/pass: every in_context delivery of this member becomes handled.

        @mentions the member was only notified of (a "call read()" stub) stay
        pending for read(), but count as answered for the watchdog (§8.5):
        ``reminders = WATCHDOG_DONE``, also for one already escalated (so it no
        longer shows ``reminder=yes``)."""
        now = self.clock.now()
        with db.tx(self.con):
            n = self.con.execute(
                "UPDATE deliveries SET state='handled', handled_at=? WHERE membership_id=?"
                " AND state='in_context'",
                (now, membership_id),
            ).rowcount
            self.con.execute(
                "UPDATE deliveries SET reminders=? WHERE membership_id=? AND state='pending'"
                " AND notified_at IS NOT NULL AND mentioned=1 AND reminders<>?",
                (WATCHDOG_DONE, membership_id, WATCHDOG_DONE),
            )
            self._advance_cursor(membership_id)
        return n

    def in_context_items(self, membership_id: int) -> list[Item]:
        """Deliveries that reached this member's context and aren't handled yet."""
        rows = self.con.execute(
            self._ITEM_SQL + " WHERE d.membership_id=? AND d.state='in_context' ORDER BY d.message_id",
            (membership_id,),
        ).fetchall()
        return [Item.from_row(r) for r in rows]

    def requeue(self, membership_id: int, message_ids: Iterable[int], *, redeliver: bool = False) -> int:
        """Put in-context deliveries back to pending for another wake (DESIGN.md §8.5).

        ``redeliver`` marks them re-delivered (the once-only re-delivery after a
        turn ended without say()/pass()); it is independent of ``attempts``.
        """
        ids = list(message_ids)
        if not ids:
            return 0
        with db.tx(self.con):
            n = 0
            for mid in ids:
                n += self.con.execute(
                    "UPDATE deliveries SET state='pending', batch_id=NULL, notified_at=NULL,"
                    " redelivered=CASE WHEN ? THEN 1 ELSE redelivered END"
                    " WHERE membership_id=? AND message_id=? AND state='in_context'",
                    (int(redeliver), membership_id, mid),
                ).rowcount
        return n

    def memberships_with_pending(self) -> list[int]:
        rows = self.con.execute(
            "SELECT DISTINCT d.membership_id FROM deliveries d JOIN memberships m"
            " ON m.id=d.membership_id WHERE d.state='pending' AND m.left_at IS NULL"
        ).fetchall()
        return [r[0] for r in rows]

    # ---------------------------------------------------------------- watchdog
    def watch_items(self, before: float) -> list[Item]:
        """@mentions of active members that reached the model (in context) or
        were notified as a stub at or before ``before``, and that the watchdog
        isn't done with yet (DESIGN.md §8.5)."""
        # active members first, then their open deliveries by index: this runs every tick
        cols = self._ITEM_SQL.split(" FROM ")[0]
        rows = self.con.execute(
            cols + " FROM memberships mm CROSS JOIN deliveries d INDEXED BY deliveries_open"
            " ON d.membership_id=mm.id AND d.state IN ('in_context','pending')"
            " JOIN messages m ON m.id=d.message_id"
            " WHERE mm.left_at IS NULL AND d.mentioned=1 AND d.reminders<?"
            " AND ((d.state='in_context' AND d.in_context_at<=?)"
            "   OR (d.state='pending' AND d.notified_at IS NOT NULL AND d.notified_at<=?))"
            " ORDER BY d.membership_id, d.message_id",
            (WATCHDOG_DONE, before, before),
        ).fetchall()
        return [Item.from_row(r) for r in rows]

    def last_answer_at(self, membership_id: int) -> float | None:
        """When this member last said something in its room, or passed (say/pass)."""
        r = self.con.execute(
            "SELECT MAX(t) FROM ("
            " SELECT MAX(ts) AS t FROM messages WHERE sender_membership_id=? AND kind='chat'"
            " UNION ALL"
            " SELECT MAX(ts) AS t FROM events WHERE kind='pass' AND membership_id=?)",
            (membership_id, membership_id),
        ).fetchone()
        return r[0] if r and r[0] is not None else None

    def watchdog_requeue(self, membership_id: int, message_ids: Iterable[int]) -> int:
        """Bring unanswered @mentions back for another wake (a watchdog reminder):
        in-context or notified-stub deliveries go back to pending, wake-eligible
        again (notified_at cleared), with ``reminders + 1``."""
        ids = list(message_ids)
        n = 0
        with db.tx(self.con):
            for mid in ids:
                n += self.con.execute(
                    "UPDATE deliveries SET state='pending', batch_id=NULL, notified_at=NULL,"
                    " reminders=reminders+1 WHERE membership_id=? AND message_id=? AND reminders<?"
                    " AND (state='in_context' OR (state='pending' AND notified_at IS NOT NULL))",
                    (membership_id, mid, WATCHDOG_DONE - 1),
                ).rowcount
        return n

    def watchdog_done(self, membership_id: int, message_ids: Iterable[int], *, keep_count: bool) -> int:
        """The watchdog is done with these @mentions: ``reminders += WATCHDOG_DONE``
        (escalated: ``keep_count``, the reminders sent still show) or
        ``= WATCHDOG_DONE`` (answered). An in-context item goes back to pending as
        a notified stub: read() still shows it, but it never wakes again. An
        escalated one is also marked ``redelivered``, so a later read() and an
        unanswered turn don't bring it back through the re-deliver-once rule."""
        ids = list(message_ids)
        now = self.clock.now()
        n = 0
        with db.tx(self.con):
            for mid in ids:
                n += self.con.execute(
                    "UPDATE deliveries SET reminders=CASE WHEN ? THEN reminders+? ELSE ? END,"
                    " redelivered=CASE WHEN ? THEN 1 ELSE redelivered END,"
                    " notified_at=CASE WHEN state='in_context' THEN ? ELSE notified_at END,"
                    " batch_id=CASE WHEN state='in_context' THEN NULL ELSE batch_id END,"
                    " state=CASE WHEN state='in_context' THEN 'pending' ELSE state END"
                    " WHERE membership_id=? AND message_id=? AND reminders<?"
                    " AND (state='in_context' OR (state='pending' AND notified_at IS NOT NULL))",
                    (
                        int(keep_count),
                        WATCHDOG_DONE,
                        WATCHDOG_DONE,
                        int(keep_count),
                        now,
                        membership_id,
                        mid,
                        WATCHDOG_DONE,
                    ),
                ).rowcount
        return n

    # ---------------------------------------------------------------- batches
    def create_batch(
        self,
        membership_id: int,
        *,
        path: str,
        kind: str,
        items: Sequence[tuple[int, int]],
        wake_kind: str | None = None,
        wake_reason: str | None = None,
        counted: bool = False,
        peer_boundary: int | None = None,
    ) -> Batch:
        """One transaction: batch row, deliveries offered, budget decrement,
        peer_batch_boundary when chatter is included (DESIGN.md §8.2)."""
        now = self.clock.now()
        with db.tx(self.con):
            m = self.get_membership(membership_id)
            if m is None or m.left_at is not None:
                raise NotFound(f"membership {membership_id} is not active")
            cur = self.con.execute(
                "INSERT INTO batches(membership_id, path, kind, wake_kind, wake_reason,"
                " budget_counted, created_at) VALUES(?,?,?,?,?,?,?)",
                (membership_id, path, kind, wake_kind, wake_reason, int(counted), now),
            )
            assert cur.lastrowid is not None
            bid = cur.lastrowid
            for message_id, inline in items:
                n = self.con.execute(
                    "UPDATE deliveries SET state='offered', batch_id=?, offered_inline=?"
                    " WHERE membership_id=? AND message_id=? AND state='pending'",
                    (bid, int(inline), membership_id, message_id),
                ).rowcount
                if n != 1:
                    raise StoreError(f"delivery {membership_id}/{message_id} is not pending")
            if counted:
                self.con.execute(
                    "UPDATE rooms SET budget_remaining=MAX(budget_remaining-1, 0) WHERE id=?",
                    (m.room_id,),
                )
            if peer_boundary is not None:
                self.con.execute(
                    "UPDATE memberships SET peer_batch_boundary=? WHERE id=?",
                    (peer_boundary, membership_id),
                )
        got = self.get_batch(bid)
        assert got is not None
        return got

    def mark_peer_batch(self, membership_id: int, boundary: int) -> None:
        """A peer (chatter) batch made at turn boundary ``boundary`` was confirmed:
        the next one waits for a later boundary (DESIGN.md §8.2)."""
        with db.tx(self.con):
            self.con.execute(
                "UPDATE memberships SET peer_batch_boundary=MAX(peer_batch_boundary, ?) WHERE id=?",
                (int(boundary), membership_id),
            )

    def get_batch(self, batch_id: int) -> Batch | None:
        r = self.con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return Batch.from_row(r) if r else None

    def offered_batches(
        self, membership_id: int | None = None, paths: Iterable[str] | None = None
    ) -> list[Batch]:
        sql = "SELECT * FROM batches WHERE state='offered'"
        args: list[Any] = []
        if membership_id is not None:
            sql += " AND membership_id=?"
            args.append(membership_id)
        if paths is not None:
            ps = list(paths)
            sql += f" AND path IN ({','.join('?' * len(ps))})"
            args.extend(ps)
        sql += " ORDER BY id"
        return [Batch.from_row(r) for r in self.con.execute(sql, args).fetchall()]

    def inflight_offer(self, membership_id: int) -> bool:
        r = self.con.execute(
            "SELECT 1 FROM batches WHERE membership_id=? AND state='offered' LIMIT 1",
            (membership_id,),
        ).fetchone()
        return r is not None

    def offered_batch_exists(self, participant_id: int, exclude_paths: Iterable[str] = ()) -> bool:
        """Any offered batch of this participant (any room) on a path not excluded."""
        ex = list(exclude_paths)
        sql = (
            "SELECT 1 FROM batches b JOIN memberships m ON m.id=b.membership_id"
            " WHERE m.participant_id=? AND b.state='offered'"
        )
        if ex:
            sql += f" AND b.path NOT IN ({','.join('?' * len(ex))})"
        r = self.con.execute(sql + " LIMIT 1", (participant_id, *ex)).fetchone()
        return r is not None

    def mark_posted(self, batch_id: int, t: float | None = None) -> None:
        with db.tx(self.con):
            self.con.execute(
                "UPDATE batches SET posted_at=? WHERE id=? AND posted_at IS NULL",
                (self.clock.now() if t is None else t, batch_id),
            )

    def refine_posted(self, batch_id: int, t: float) -> None:
        """The transport's own post time (e.g. the MCP server's ``t_post``), if later."""
        with db.tx(self.con):
            self.con.execute(
                "UPDATE batches SET posted_at=? WHERE id=? AND (posted_at IS NULL OR posted_at<?)",
                (t, batch_id, t),
            )

    def set_batch_times(self, batch_id: int, **fields: float) -> None:
        allowed = {"turn_start_at", "first_action_at", "posted_at"}
        if set(fields) - allowed:
            raise ValueError("bad batch time field")
        with db.tx(self.con):
            for k, v in fields.items():
                self.con.execute(f"UPDATE batches SET {k}=? WHERE id=? AND {k} IS NULL", (v, batch_id))

    def confirm_batch(self, batch_id: int, evidence: str) -> Batch | None:
        """The only writer of cursor_id (DESIGN.md §8.7). None if the batch isn't offered."""
        now = self.clock.now()
        with db.tx(self.con):
            b = self.get_batch(batch_id)
            if b is None or b.state != "offered":
                return None
            self.con.execute(
                "UPDATE batches SET state='confirmed', confirmed_at=?, evidence=? WHERE id=?",
                (now, evidence, batch_id),
            )
            # inline chatter: straight to handled; other inline items: in context
            self.con.execute(
                "UPDATE deliveries SET state='handled', in_context_at=?, handled_at=?"
                " WHERE batch_id=? AND state='offered' AND offered_inline=1 AND prio=0",
                (now, now, batch_id),
            )
            self.con.execute(
                "UPDATE deliveries SET state='in_context', in_context_at=?"
                " WHERE batch_id=? AND state='offered' AND offered_inline=1",
                (now, batch_id),
            )
            # texts cut to fit: shown in part, so in context too; pending, so read() shows the rest
            self.con.execute(
                "UPDATE deliveries SET state='pending', notified_at=?, in_context_at=?, batch_id=NULL"
                " WHERE batch_id=? AND state='offered' AND offered_inline=2",
                (now, now, batch_id),
            )
            # stubs: the agent was told to read(); they stay pending, pull-only
            self.con.execute(
                "UPDATE deliveries SET state='pending', notified_at=?, batch_id=NULL"
                " WHERE batch_id=? AND state='offered'",
                (now, batch_id),
            )
            self._advance_cursor(b.membership_id)
            self.con.execute(
                "UPDATE participants SET push_expiries=0 WHERE id="
                "(SELECT participant_id FROM memberships WHERE id=?)",
                (b.membership_id,),
            )
        return self.get_batch(batch_id)

    def expire_batch(
        self, batch_id: int, reason: str, *, state: str = "expired", push: bool = False, refund: bool = False
    ) -> Batch | None:
        """Offer failed (or was cancelled): its deliveries go back to pending. ``refund``: a
        wake that reached nobody (a push re-route) gives its room's budget unit back, at most
        up to the hourly budget, and no longer counts as a counted wake."""
        if state not in ("expired", "cancelled"):
            raise ValueError(state)
        now = self.clock.now()
        with db.tx(self.con):
            b = self.get_batch(batch_id)
            if b is None or b.state != "offered":
                return None
            self.con.execute(
                "UPDATE batches SET state=?, expired_at=?, expire_reason=? WHERE id=?",
                (state, now, reason, batch_id),
            )
            self.con.execute(
                "UPDATE deliveries SET state='pending', batch_id=NULL, attempts=attempts+?"
                " WHERE batch_id=? AND state='offered'",
                (1 if state == "expired" else 0, batch_id),
            )
            if push and state == "expired":
                self.con.execute(
                    "UPDATE participants SET push_expiries=push_expiries+1 WHERE id="
                    "(SELECT participant_id FROM memberships WHERE id=?)",
                    (b.membership_id,),
                )
            if refund and b.budget_counted:
                self.con.execute("UPDATE batches SET budget_counted=0 WHERE id=?", (batch_id,))
                self.con.execute(
                    "UPDATE rooms SET budget_remaining=MIN(budget_remaining+1, budget_per_hour) WHERE id="
                    "(SELECT room_id FROM memberships WHERE id=?)",
                    (b.membership_id,),
                )
        return self.get_batch(batch_id)

    def _advance_cursor(self, membership_id: int) -> None:
        """cursor_id = highest message id with every delivery <= it confirmed."""
        r = self.con.execute(
            "SELECT MIN(message_id) FROM deliveries WHERE membership_id=? AND state IN ('pending','offered')",
            (membership_id,),
        ).fetchone()
        first_open = r[0]
        if first_open is None:
            r = self.con.execute(
                "SELECT MAX(message_id) FROM deliveries WHERE membership_id=?", (membership_id,)
            ).fetchone()
        else:
            r = self.con.execute(
                "SELECT MAX(message_id) FROM deliveries WHERE membership_id=? AND message_id<?",
                (membership_id, first_open),
            ).fetchone()
        top = r[0]
        if top is not None:
            self.con.execute(
                "UPDATE memberships SET cursor_id=MAX(cursor_id, ?) WHERE id=?",
                (top, membership_id),
            )

    # ------------------------------------------------------------------ rooms
    def mark_budget_notice(self, room_id: int, window: float) -> bool:
        """Record that this budget window was announced as exhausted. False if it already was."""
        with db.tx(self.con):
            room = self._room_or_raise(room_id)
            if room.budget_notice_window == window:
                return False
            self.con.execute("UPDATE rooms SET budget_notice_window=? WHERE id=?", (window, room_id))
        return True

    def count_events(
        self, kind: str, *, room_id: int | None = None, participant_id: int | None = None
    ) -> int:
        sql = "SELECT COUNT(*) FROM events WHERE kind=?"
        args: list[Any] = [kind]
        if room_id is not None:
            sql += " AND room_id=?"
            args.append(room_id)
        if participant_id is not None:
            sql += " AND participant_id=?"
            args.append(participant_id)
        return int(self.con.execute(sql, args).fetchone()[0])
