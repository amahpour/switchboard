"""Store: every SQL statement switchboard runs (DESIGN.md §4). Nothing else touches SQL.

The broker is the only writer. It calls the store synchronously on its event
loop; every write is wrapped in ``db.tx`` (``BEGIN IMMEDIATE``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from switchboard import db
from switchboard.clock import Clock, SystemClock
from switchboard.models import WATCHDOG_DONE, Batch, Event, Item, Member, Membership, Message, Participant, Room

OPEN_DELIVERY_STATES = ("pending", "offered", "in_context")


class StoreError(Exception):
    pass


class Conflict(StoreError):
    pass


class NotFound(StoreError):
    pass


class Store:
    def __init__(self, con: sqlite3.Connection, clock: Clock | None = None):
        self.con = con
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ rooms
    def create_room(
        self, name: str, created_by: str, budget_per_hour: int, hop_limit: int
    ) -> Room:
        now = self.clock.now()
        with db.tx(self.con):
            if self.get_room(name) is not None:
                raise Conflict(f"{name} already exists")
            cur = self.con.execute(
                "INSERT INTO rooms(name, created_at, created_by, budget_per_hour,"
                " budget_remaining, budget_window_start, hop_limit)"
                " VALUES(?,?,?,?,?,?,?)",
                (name, now, created_by, budget_per_hour, budget_per_hour, now, hop_limit),
            )
            rid = cur.lastrowid
        room = self.room_by_id(rid)
        assert room is not None
        return room

    def get_room(self, name: str) -> Room | None:
        r = self.con.execute("SELECT * FROM rooms WHERE name=?", (name,)).fetchone()
        return Room.from_row(r) if r else None

    def room_by_id(self, room_id: int) -> Room | None:
        r = self.con.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        return Room.from_row(r) if r else None

    def list_rooms(self) -> list[Room]:
        rows = self.con.execute("SELECT * FROM rooms ORDER BY name").fetchall()
        return [Room.from_row(r) for r in rows]

    def _room_or_raise(self, room_id: int) -> Room:
        room = self.room_by_id(room_id)
        if room is None:
            raise NotFound(f"room {room_id} not found")
        return room

    def set_paused(self, room_id: int, paused: bool, reason: str | None = None) -> Room:
        with db.tx(self.con):
            if paused:
                self.con.execute(
                    "UPDATE rooms SET paused=1, paused_reason=? WHERE id=?", (reason, room_id)
                )
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
            self.con.execute(
                "UPDATE rooms SET budget_remaining=? WHERE id=?", (remaining, room_id)
            )
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
    ) -> Message:
        """Persist a message and its per-recipient delivery rows in one transaction.

        Classification (§8.1): for every active membership other than the sender,
        prio 2 for a human message, 1 when the member is @mentioned, else 0.
        join/leave/notice messages are never delivered to agents. ``skip_memberships``
        get no delivery row either (the author named in a ``/review`` request, §26).
        """
        skip = {int(x) for x in skip_memberships}
        now = self.clock.now()
        mentions_l = sorted({m.lower() for m in mentions})
        with db.tx(self.con):
            cur = self.con.execute(
                "INSERT INTO messages(room_id, ts, sender_membership_id, sender_name,"
                " sender_harness, sender_kind, via, kind, text, reply_to, mentions)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                    self.con.execute(
                        "UPDATE rooms SET last_msg_at=? WHERE id=?", (now, room_id)
                    )
                rows = self.con.execute(
                    "SELECT id, screen_name FROM memberships"
                    " WHERE room_id=? AND left_at IS NULL",
                    (room_id,),
                ).fetchall()
                for r in rows:
                    if (sender_membership_id is not None and r["id"] == sender_membership_id) or r["id"] in skip:
                        continue
                    mentioned = r["screen_name"].lower() in mentions_l
                    prio = 2 if sender_kind == "human" else (1 if mentioned else 0)
                    self.con.execute(
                        "INSERT INTO deliveries(membership_id, message_id, prio, mentioned)"
                        " VALUES(?,?,?,?)",
                        (r["id"], mid, prio, int(mentioned)),
                    )
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
                "SELECT * FROM (SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT ?)"
                " ORDER BY id",
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
            "SELECT screen_name FROM memberships WHERE room_id=? AND left_at IS NULL"
            " ORDER BY joined_at",
            (room_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def members(self, room_id: int) -> list[Member]:
        rows = self.con.execute(
            "SELECT m.id AS membership_id, m.participant_id, m.room_id, m.screen_name,"
            " m.held, m.joined_at, p.harness, p.status, p.tier, p.tier_note, p.away,"
            " p.approval_mode, p.env_leak,"
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

    def end_membership(self, membership_id: int, reason: str, *, kicked: bool = False) -> None:
        """Leave, kick or session end: revoke the credential and open deliveries."""
        now = self.clock.now()
        with db.tx(self.con):
            self.con.execute(
                "UPDATE memberships SET left_at=?, left_reason=?, cred_hash=NULL,"
                " kicked=MAX(kicked, ?) WHERE id=? AND left_at IS NULL",
                (now, reason, int(kicked), membership_id),
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

    def batch_expiry_counts(self, membership_id: int) -> dict[str, int]:
        rows = self.con.execute(
            "SELECT COALESCE(expire_reason, '?'), COUNT(*) FROM batches"
            " WHERE membership_id=? AND state='expired' GROUP BY expire_reason",
            (membership_id,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def count_active_members(self) -> int:
        r = self.con.execute(
            "SELECT COUNT(*) FROM memberships WHERE left_at IS NULL"
        ).fetchone()
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
            return int(cur.lastrowid)

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
    def web_session_create(self, id_hash: str, ttl_s: float) -> None:
        now = self.clock.now()
        with db.tx(self.con):
            self.con.execute(
                "INSERT INTO web_sessions(id_hash, created_at, last_seen, expires_at)"
                " VALUES(?,?,?,?)",
                (id_hash, now, now, now + ttl_s),
            )

    def web_session_touch(self, id_hash: str, ttl_s: float) -> bool:
        """True when the session exists and hasn't expired; slides its expiry."""
        now = self.clock.now()
        with db.tx(self.con):
            r = self.con.execute(
                "SELECT expires_at FROM web_sessions WHERE id_hash=?", (id_hash,)
            ).fetchone()
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
            return self.con.execute(
                "DELETE FROM web_sessions WHERE id_hash=?", (id_hash,)
            ).rowcount

    def web_session_delete_all(self) -> int:
        with db.tx(self.con):
            return self.con.execute("DELETE FROM web_sessions").rowcount

    def web_session_purge(self) -> int:
        now = self.clock.now()
        with db.tx(self.con):
            return self.con.execute(
                "DELETE FROM web_sessions WHERE expires_at<=?", (now,)
            ).rowcount

    def web_session_valid(self, id_hash: str) -> bool:
        """Non-sliding check (used to drop WebSockets of expired or revoked sessions)."""
        r = self.con.execute(
            "SELECT expires_at FROM web_sessions WHERE id_hash=?", (id_hash,)
        ).fetchone()
        return r is not None and r[0] > self.clock.now()

    def web_session_count(self) -> int:
        return int(self.con.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0])

    # --------------------------------------------------------------- recovery
    def recover_on_start(
        self, alive: Callable[[int, float | None], bool]
    ) -> dict[str, int]:
        """Restart semantics (§4): expire open offers, mark everyone offline,
        end participants whose agent process is gone."""
        now = self.clock.now()
        ended: list[tuple[int, int, str]] = []  # (room_id, membership_id, name)
        with db.tx(self.con):
            reverted = self.con.execute(
                "UPDATE deliveries SET state='pending', attempts=attempts+1"
                " WHERE state='offered'"
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
                " WHERE ended_at IS NULL AND agent_pid IS NOT NULL"
            ).fetchall()
            n_ended = 0
            for p in parts:
                if alive(p["agent_pid"], p["agent_start"]):
                    continue
                n_ended += 1
                self.con.execute(
                    "UPDATE participants SET ended_at=? WHERE id=?", (now, p["id"])
                )
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


    # ============================================================ M2: agents
    # ----------------------------------------------------------- participants
    _PARTICIPANT_COLS = frozenset(
        {
            "session_id", "agent_pid", "agent_start", "mcp_pid", "mcp_start", "claude_socket",
            "bind_state", "bind_nonce", "thread_proof", "status", "status_at", "status_src",
            "tier", "tier_note", "approval_mode", "env_leak", "away", "boundary_seq", "gen",
            "gen_tainted", "rearms_in_gen", "last_loop_count", "unconfirmed_followups",
            "push_expiries", "hooks_seen_at", "last_say_at", "last_seen", "ended_at",
            "session_key",
        }
    )

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
        now = self.clock.now()
        with db.tx(self.con):
            cur = self.find_participant(harness, session_key)
            if cur is None:
                cols = ["harness", "session_key", "created_at", "last_seen", *fields]
                vals = [harness, session_key, now, now, *fields.values()]
                c = self.con.execute(
                    f"INSERT INTO participants({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                    vals,
                )
                pid = int(c.lastrowid)
            else:
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
        sets = ", ".join(f"{k}=?" for k in fields)
        self.con.execute(
            f"UPDATE participants SET {sets} WHERE id=?", [*fields.values(), participant_id]
        )

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
        rows = self.con.execute(
            "SELECT * FROM participants WHERE ended_at IS NULL ORDER BY id"
        ).fetchall()
        return [Participant.from_row(r) for r in rows]

    def joined_participants(self) -> list[Participant]:
        """Active participants with at least one active membership."""
        rows = self.con.execute(
            "SELECT * FROM participants p WHERE p.ended_at IS NULL AND EXISTS("
            " SELECT 1 FROM memberships m WHERE m.participant_id=p.id AND m.left_at IS NULL)"
            " ORDER BY p.id"
        ).fetchall()
        return [Participant.from_row(r) for r in rows]

    def active_participants_by_agent(self, harness: str, agent_pid: int) -> list[Participant]:
        """Active participants of ``harness`` whose agent process is ``agent_pid``, newest first."""
        rows = self.con.execute(
            "SELECT * FROM participants WHERE harness=? AND agent_pid=? AND ended_at IS NULL ORDER BY id DESC",
            (harness, agent_pid),
        ).fetchall()
        return [Participant.from_row(r) for r in rows]

    def participants_by_mcp(self, mcp_pid: int, mcp_start: float | None) -> list[Participant]:
        rows = self.con.execute(
            "SELECT * FROM participants WHERE ended_at IS NULL AND mcp_pid=?", (mcp_pid,)
        ).fetchall()
        out = [Participant.from_row(r) for r in rows]
        return [p for p in out if p.mcp_start is not None and mcp_start is not None
                and abs(p.mcp_start - mcp_start) < 0.011]

    def end_participant(self, participant_id: int, reason: str = "session_end") -> list[Membership]:
        """End a session: leave every room, revoke creds and deliveries. Returns the ended memberships."""
        now = self.clock.now()
        ended: list[Membership] = []
        with db.tx(self.con):
            for m in self.participant_memberships(participant_id):
                self.end_membership(m.id, reason)
                ended.append(m)
            self.con.execute(
                "UPDATE participants SET ended_at=?, status='offline', status_at=?, status_src=?"
                " WHERE id=?",
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
            mid = int(cur.lastrowid)
        got = self.get_membership(mid)
        assert got is not None
        return got

    def get_membership(self, membership_id: int) -> Membership | None:
        r = self.con.execute("SELECT * FROM memberships WHERE id=?", (membership_id,)).fetchone()
        return Membership.from_row(r) if r else None

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
        rows = self.con.execute(
            "SELECT * FROM memberships WHERE left_at IS NULL ORDER BY id"
        ).fetchall()
        return [Membership.from_row(r) for r in rows]

    def rotate_cred(self, membership_id: int, cred_hash: str) -> None:
        with db.tx(self.con):
            self.con.execute(
                "UPDATE memberships SET cred_hash=? WHERE id=? AND left_at IS NULL",
                (cred_hash, membership_id),
            )

    def name_used_by_other(
        self, room_id: int, name: str, participant_id: int, since: float
    ) -> bool:
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
        " m.sender_harness, m.text, m.reply_to, m.mentions"
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
                    (int(keep_count), WATCHDOG_DONE, WATCHDOG_DONE, int(keep_count), now, membership_id, mid,
                     WATCHDOG_DONE),
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
            bid = int(cur.lastrowid)
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
        sql = ("SELECT 1 FROM batches b JOIN memberships m ON m.id=b.membership_id"
               " WHERE m.participant_id=? AND b.state='offered'")
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
        self, batch_id: int, reason: str, *, state: str = "expired", push: bool = False
    ) -> Batch | None:
        """Offer failed (or was cancelled): its deliveries go back to pending."""
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
        return self.get_batch(batch_id)

    def _advance_cursor(self, membership_id: int) -> None:
        """cursor_id = highest message id with every delivery <= it confirmed."""
        r = self.con.execute(
            "SELECT MIN(message_id) FROM deliveries WHERE membership_id=?"
            " AND state IN ('pending','offered')",
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
            self.con.execute(
                "UPDATE rooms SET budget_notice_window=? WHERE id=?", (window, room_id)
            )
        return True

    def count_events(self, kind: str, *, room_id: int | None = None,
                     participant_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) FROM events WHERE kind=?"
        args: list[Any] = [kind]
        if room_id is not None:
            sql += " AND room_id=?"
            args.append(room_id)
        if participant_id is not None:
            sql += " AND participant_id=?"
            args.append(participant_id)
        return int(self.con.execute(sql, args).fetchone()[0])
