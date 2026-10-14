"""RoomService: the broker's room operations (DESIGN.md §3, §10).

The human side: rooms, history, the member list (and one member's detail, for
the web UI's Inspector), human messages and commands. Agent operations live in
``broker/agents.py`` (AgentService) and
plug into the same persist-then-publish flow through ``self.delivery``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from switchboard import __version__, build_info, db
from switchboard.broker import catchup
from switchboard.broker import commands as cmds
from switchboard.broker.commands import Actor, CommandError
from switchboard.broker.hub import Hub
from switchboard.clock import Clock, SystemClock
from switchboard.config import Config, DeliveryCfg
from switchboard.delivery.rules import (
    HUMANS_MENTION,
    broadcast_targets,
    mentions_humans,
    parse_broadcast,
    parse_mentions,
)
from switchboard.envelope import clean
from switchboard.models import (
    CONTINUE_PATHS,
    HOOK_PATHS,
    PRIO_LABEL,
    PULL_PATHS,
    SCREEN_NAME_RE,
    InvalidName,
    Member,
    Message,
    Room,
    closed_room_name,
    display_room,
    normalize_room,
    tier_label,
)
from switchboard.report import _safe
from switchboard.store import Ambiguous, Conflict, NotFound, Store, StoreError

log = logging.getLogger("switchboard.service")
MAX_MESSAGE_ID = 2**63 - 1  # SQLite INTEGER; larger Python ints raise OverflowError at the query


class ServiceError(Exception):
    """Maps onto RPC error codes and HTTP statuses."""

    HTTP = {
        "bad_request": 400,
        "unauthorized": 401,
        "forbidden": 403,
        "not_found": 404,
        "name_taken": 409,
        "conflict": 409,
        "paused": 409,
        "internal": 500,
    }

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def http_status(self) -> int:
        return self.HTTP.get(self.code, 400)


@dataclass
class BrokerInfo:
    """Facts about the running broker that /status and sys.status report."""

    pid: int = field(default_factory=os.getpid)
    port: int = 0
    started_at: float = field(default_factory=time.time)
    test_mode: bool = False
    home: str = ""
    url: str = ""  # the web UI's address, as browsers reach it
    hook_state: str = "not checked"
    codex_link: str = "not started"


class DeliveryHooks(Protocol):
    """What RoomService tells the delivery side (implemented by AgentService)."""

    def on_message(self, msg: Message) -> None: ...

    def on_command(self, room: Room, name: str, membership_id: int | None) -> None: ...

    def on_membership_ended(self, membership_id: int, reason: str) -> None: ...

    def on_memberships_ended(self, membership_ids: list[int], reason: str) -> None: ...

    def parked_reason(self, membership_id: int) -> str | None: ...

    def board_reason(self, member: Member) -> str | None: ...

    def member_claims(self, member: Member) -> tuple[str | None, tuple[str, ...] | None]: ...


def _hhmmss(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"


# Engine warnings that open with a fixed phrase (loop guard, wake budget, watchdog), and
# the broker's "⚠ " notices (/catchup's approvals-off warning, §26). A notice's level isn't a
# column, so these are recognised by text to keep them styled as warnings in history and
# replays too (§22). Only the broker writes system notices.
WARN_NOTICE_PREFIXES = ("loop guard:", "the wake budget for this hour is used up", "watchdog:", "⚠ ")


def message_dict(m: Message) -> dict[str, Any]:
    """The §5.5 client shape. Text is display-cleaned (control characters dropped)."""
    d: dict[str, Any] = {
        "id": m.id,
        "ts": m.ts,
        "from": m.sender_name,
        "harness": m.sender_harness,
        "sender_kind": m.sender_kind,
        "via": m.via,
        "kind": m.kind,
        "text": clean(m.text),
        "reply_to": m.reply_to,
        "mentions": m.mentions,
        # the remote host of an agent sender (DESIGN.md §27.11); None on this machine
        "host": m.sender_host,
    }
    if m.kind == "notice" and m.sender_kind == "system" and m.text.startswith(WARN_NOTICE_PREFIXES):
        d["level"] = "warn"
    return d


def member_dict(m: Member) -> dict[str, Any]:
    return {
        "name": m.name,
        "harness": m.harness,
        "status": m.status,
        "tier": m.tier,
        "tier_note": m.tier_note,
        "away": clean(m.away) if m.away else None,
        "status_line": clean(m.status_line) if m.status_line else None,
        "status_line_stale": m.status_line_stale,
        "status_line_remaining_s": m.status_line_remaining_s,
        "approval_mode": m.approval_mode,
        "env_leak": m.env_leak,
        "held": m.held,
        "queued": m.queued,
        "inflight": m.inflight,
        "parked": m.parked,
        "parked_reason": m.parked_reason,
        # the member's host: '' for this machine, else the remote's name (§27.11)
        "host": m.host,
        # why it can't use the review board, or None (#249): switchboard's own words
        "board_why": m.board_why,
        "version_claim": m.version_claim or "unknown",
        "tools_claim": list(m.tools_claim) if m.tools_claim is not None else None,
        "claims": list(m.claims),
    }


def member_label(m: Member) -> str:
    """``bench@fpga-pi`` for a remote member, the plain name on this machine."""
    return f"{m.name}@{m.host}" if m.host else m.name


# ------------------------------------------------------ the Inspector (§29)
# ``RoomService.member_detail`` answers the web UI's GET /api/rooms/{slug}/members/{name}.
# It is human-only (the web session) and read-only. Security reasoning:
#  - Events carry engine-internal ``data`` (reasons, statuses, ids). Nothing of it is passed
#    through: each timeline entry is rebuilt from a per-kind whitelist of plain numbers,
#    known path names and scrubbed, capped strings (``report._safe``: paths and emails out).
#  - No message text ever leaves here: queued items and ``said`` entries are ids only, and
#    the browser resolves them against the history it already shows the human.
#  - The session id is shown to the human only, as in /who (``session_handles``).
TIMELINE_LIMIT = 6
QUEUED_LIMIT = 50
# every delivery path the engine and the adapters use (models.py, report.py); anything else
# is reported as "other" rather than echoed
KNOWN_PATHS = frozenset({"inbox", "turn_start", "queue", "steer"}) | PULL_PATHS | HOOK_PATHS | CONTINUE_PATHS


def _num(v: Any) -> int | float | None:
    """A plain number from event data, else None (bools and strings are not numbers here)."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v: Any, cap: int) -> str | None:
    """A free-form string from event data or the participant row: scrubbed and capped."""
    return clean(_safe(v))[:cap] if isinstance(v, str) else None


def _sender_label(name: str, host: str | None) -> str:
    return f"{name}@{host}" if host else name


# The status sources a real turn end writes: the engine's Stop and Interrupt hooks set the
# member idle with ``f"hook:{E}"``. The ``stop:*`` sources (engine.STOP_BUSY_SRCS: followup,
# block, rearm) are the opposite: switchboard kept the turn going and marked the agent busy.
TURN_END_SRCS = frozenset({"hook:Stop", "hook:Interrupt"})


def _last_seen(t: dict[str, Any]) -> tuple[float | None, str | None]:
    """The member's newest sign of life and what it was: ``said`` (its last say()),
    ``passed`` (a pass() in this room), ``turn ended`` (a Stop or Interrupt hook was the last
    status report, ``TURN_END_SRCS``), else ``seen``. ``(None, None)`` when nothing is known."""
    seen, said, passed = t.get("last_seen"), t.get("last_say_at"), t.get("last_pass_at")
    known = [x for x in (seen, said, passed) if x is not None]
    if not known:
        return None, None
    top = max(known)
    if said is not None and said == top:
        return top, "said"
    if passed is not None and passed == top:
        return top, "passed"
    src = t.get("status_src")
    if src in TURN_END_SRCS and seen == top:
        return top, "turn ended"
    return top, "seen"


class RoomService:
    def __init__(
        self,
        store: Store,
        hub: Hub,
        cfg: Config,
        info: BrokerInfo,
        clock: Clock | None = None,
    ):
        self.store = store
        self.hub = hub
        self.cfg = cfg
        self.info = info
        self.clock = clock or SystemClock()
        self.on_room_changed: list[Callable[[Room, str], None]] = []
        # The delivery side (AgentService); None in unit tests of the human side.
        self.delivery: DeliveryHooks | None = None
        # The remote hosts' links (broker/remote.py RemoteManager), once they run.
        self.remotes: Any = None
        self.machines: Any = None  # the machines that dial in (§31.7), on a hosted broker
        self.boards: Any = None  # the review boards (broker/reviews.py Boards), for /review (§37.8)
        hub.set_members_source(self._members_snapshot)
        hub.set_settings_source(self._settings_snapshot)

    # ---------------------------------------------------------------- lookups
    def room(self, name: str) -> Room:
        try:
            n = normalize_room(name)
        except InvalidName as e:
            raise ServiceError("bad_request", str(e)) from None
        room = self.store.get_room(n)
        if room is None:
            if self.store.closed_rooms(n):
                raise ServiceError(
                    "not_found",
                    f"no such room: {n} (it is closed: reopen it from Closed rooms in the web UI)",
                )
            raise ServiceError("not_found", f"no such room: {n}")
        return room

    def settings(self, room: Room) -> dict[str, Any]:
        room = self.store.refill_budget(room.id)
        return {
            "paused": room.paused,
            "paused_reason": room.paused_reason,
            "budget_remaining": room.budget_remaining,
            "budget_per_hour": room.budget_per_hour,
            "budget_reset_at": room.budget_reset_at,
            "hop_count": room.hop_count,
            "hop_limit": room.hop_limit,
            "test_mode": self.info.test_mode,
        }

    def room_dict(self, room: Room) -> dict[str, Any]:
        return {
            "id": room.id,  # a room closed and re-created keeps its name, not its id (§28.4)
            "name": room.name,
            "slug": room.slug,
            "created_at": room.created_at,
            "members": len(self.store.active_names(room.id)),
            "last_id": self.store.last_message_id(room.id),
            "settings": self.settings(room),
            "rules": room.rules_text,
            # a private room's people, who alone see it (§44); None for a public room
            "private": room.private,
            "people": self.room_people_names(room) if room.private else None,
        }

    def rooms(self, viewer: int | None = None) -> list[dict[str, Any]]:
        """The open rooms ``viewer`` sees (§44): every public one, and the private ones they're in."""
        return [self.room_dict(r) for r in self.store.list_rooms() if self.visible(r, viewer)]

    # ------------------------------------------------------------ private rooms (§44)
    def visible(self, room: Room, viewer: int | None) -> bool:
        """Can ``viewer`` see ``room``: a person's id, 0 the owner, as a web session is keyed?
        None is the local CLI and the broker itself, run by the machine's owner, who holds the
        database anyway (§44.4). A public room, everyone signed in; a private one, its people."""
        return not room.private or viewer is None or viewer in self.store.room_people(room.id)

    def room_for(self, name: str, viewer: int | None) -> Room:
        """``room(name)``, for ``viewer``: a private room they aren't in is "no such room", in the
        words of one that doesn't exist, so its name isn't confirmed either."""
        try:
            room = self.room(name)
        except ServiceError as e:
            # a closed private room's "reopen it from Closed rooms" hint would confirm it too
            closed = self.store.closed_rooms(normalize_room(name)) if e.code == "not_found" else []
            if closed and not any(self.visible(r, viewer) for r in closed):
                raise ServiceError("not_found", f"no such room: {normalize_room(name)}") from None
            raise
        if not self.visible(room, viewer):
            raise ServiceError("not_found", f"no such room: {room.name}")
        return room

    @staticmethod
    def person_key(person_id: int | None) -> int:
        """A web session's person as ``room_people`` keys them: 0 for the owner."""
        return 0 if person_id is None else person_id

    def person_name(self, key: int) -> str | None:
        if key == 0:
            return self.cfg.human_name
        p = self.store.person(key)
        return p.name if p is not None and p.removed_at is None else None

    def person_by_name(self, name: str) -> int | None:
        """The key of the person called ``name`` (any case), or None."""
        n = name.strip().lower().lstrip("@")
        if n == self.cfg.human_name.lower():
            return 0
        p = self.store.person_named(n)
        return p.id if p is not None else None

    def room_people_names(self, room: Room) -> list[str]:
        return [n for k in self.store.room_people(room.id) if (n := self.person_name(k)) is not None]

    def room_humans(self, room: Room) -> list[str]:
        """The people an agent in ``room`` answers to: everyone in a public room (§32), the
        room's own people in a private one (§44)."""
        return self.room_people_names(room) if room.private else self.people_names()

    def host_person(self, host: str) -> int:
        """Whose agents run on ``host`` (§44.3): a machine that dialed in is the person's who made
        its pairing code (#178); this machine and an SSH remote are the owner's."""
        if not host:
            return 0
        m = self.store.machine(host)
        return m.person_id if m is not None and m.person_id is not None else 0

    def add_room_person(self, room: Room, name: str, by: int) -> str:
        """Add a person to a private room (any of its people may, §44.2)."""
        if not room.private:
            raise ServiceError("bad_request", f"{room.name} isn't private: everyone signed in sees it")
        key = self.person_by_name(name)
        if key is None:
            raise ServiceError("not_found", f"no such person: {name.strip().lstrip('@')}")
        who, by_name = self.person_name(key), self.person_name(by) or "someone"
        if not self.store.room_person_add(room.id, key, by):
            raise ServiceError("conflict", f"{who} is in {room.name} already")
        self.store.add_event("room_people", room_id=room.id, data={"what": "add", "person": key, "by": by})
        self.post_notice(room, f"{by_name} added {who} to {room.name}")
        self.rooms_changed()
        return f"added {who}"

    def remove_room_person(self, room: Room, name: str, by: int) -> str:
        """Take a person out of a private room, with their agents (§44.3). A person may leave."""
        if not room.private:
            raise ServiceError("bad_request", f"{room.name} isn't private")
        key = self.person_by_name(name)
        people = self.store.room_people(room.id)
        if key is None or key not in people:
            raise ServiceError("not_found", f"{name.strip().lstrip('@')} isn't in {room.name}")
        if people == [key]:
            raise ServiceError(
                "bad_request", f"the last person can't leave {room.name}: /close it instead, from the room"
            )
        self._drop_person(room, key)
        who, by_name = self.person_name(key), self.person_name(by) or "someone"
        self.store.add_event("room_people", room_id=room.id, data={"what": "remove", "person": key, "by": by})
        self.post_notice(
            room, f"{who} left {room.name}" if key == by else f"{by_name} removed {who} from {room.name}"
        )
        self.rooms_changed()
        return f"removed {who}" if key != by else f"you left {room.name}"

    def _drop_person(self, room: Room, key: int) -> None:
        """``key`` out of ``room``: their row, their agents' memberships, their pages' subscriptions."""
        self.store.room_person_remove(room.id, key)
        for m in self.store.members(room.id):
            p = self.store.get_participant(m.participant_id)
            if p is None or self.host_person(p.host) != key:
                continue
            self.store.end_membership(m.membership_id, "person_left")
            if self.delivery is not None:
                self.delivery.on_membership_ended(m.membership_id, "person_left")
            self._post(
                room,
                sender_name=m.name,
                sender_kind="agent",
                sender_harness=m.harness,
                sender_membership_id=m.membership_id,
                via="system",
                kind="leave",
                text="left: its person is no longer in the room",
                sender_host=m.host or None,
            )
        self.hub.unsubscribe_person(room.name, key)
        self.hub.members_changed(room.name)

    def person_gone(self, person_id: int) -> None:
        """Someone removed from switchboard leaves every private room, with their agents (§44.3)."""
        for rid in self.store.rooms_of_person(person_id):
            room = self.store.room_by_id(rid)
            if room is not None:
                self._drop_person(room, person_id)
                self.post_notice(room, f"a person removed from switchboard left {room.name}")
        self.rooms_changed()

    def private_rooms(self) -> list[dict[str, Any]]:
        """For the admin (§44.4): every private room's name and people, never its contents."""
        out = []
        for r in self.store.list_rooms():
            if r.private:
                out.append({"name": r.name, "people": self.room_people_names(r), "created_by": r.created_by})
        return out

    def closed_room_dicts(self, viewer: int | None = None) -> list[dict[str, Any]]:
        """The closed rooms ``viewer`` sees, newest first (the web's Closed rooms panel,
        ``switchboard rooms --closed``; DESIGN.md §28.4, §44)."""
        out = []
        for r in self.store.list_rooms(closed=True):
            if not self.visible(r, viewer):
                continue
            ev = self.store.latest_close_event(r.id)
            out.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "display": r.display_name,
                    "created_at": r.created_at,
                    "closed_at": ev.ts if ev is not None else None,
                    "closed_by": ev.data.get("by") if ev is not None else None,
                    "messages": self.store.count_messages(r.id),
                    "reopenable": self.store.get_room(r.display_name) is None,
                }
            )
        return out

    def history(self, name: str, after: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        room = self.room(name)
        return [message_dict(m) for m in self.store.history(room.id, after, limit)]

    def members(self, name: str) -> list[dict[str, Any]]:
        room = self.room(name)
        return [member_dict(m) for m in self.member_rows(room.id)]

    def member_rows(self, room_id: int) -> list[Member]:
        """Buddy-list rows, with the engine's parked state, and whether each can use the review
        board, overlaid."""
        by_member: dict[int, list[dict[str, Any]]] = {}
        now = self.clock.now()
        for claim in self.store.claims_for_room(room_id):
            by_member.setdefault(claim.membership_id, []).append(
                {
                    "key": claim.key,
                    "expires_at": claim.expires_at,
                    "remaining_s": max(0.0, claim.expires_at - now),
                }
            )
        rows = [
            dataclasses.replace(m, claims=tuple(by_member.get(m.membership_id, ())))
            for m in self.store.members(room_id)
        ]
        if self.delivery is None:
            return rows
        out = []
        for m in rows:
            why = self.delivery.parked_reason(m.membership_id)
            if why:
                m = dataclasses.replace(m, parked=True, parked_reason=why)
            board = self.delivery.board_reason(m)
            version, tools = self.delivery.member_claims(m)
            out.append(dataclasses.replace(m, board_why=board, version_claim=version, tools_claim=tools))
        return out

    def member_detail(self, room_name: str, name: str) -> dict[str, Any]:
        """One member for the web UI's Inspector (DESIGN.md §29): its ``member_dict`` plus
        times, its session id (human-only), its queued message ids, its delivery counts and
        the last few delivery events. Read-only; nothing here is published or stored, and
        ``member_dict`` (the broadcast ``members`` frame) is unchanged."""
        room = self.room(room_name)
        n = name.strip().lower().lstrip("@")
        if not SCREEN_NAME_RE.fullmatch(n):
            raise ServiceError("bad_request", "not a valid screen name")
        m = next((x for x in self.member_rows(room.id) if x.name.lower() == n), None)
        if m is None:
            raise ServiceError("not_found", f"{n} is not in {room.name}")
        t = self.store.member_times(m.membership_id) or {}
        last_seen, what = _last_seen(t)
        sid, why = catchup.session_id(self.store.get_participant(m.participant_id), self.cfg)
        return {
            "room": room.name,
            "member": {
                **member_dict(m),
                # scrubbed here too: the Inspector shows it in full, in a note box
                "parked_reason": _text(m.parked_reason, 200),
                "joined_at": m.joined_at,
                "held_at": t.get("held_at"),
                "status_at": t.get("status_at"),
                "status_src": _text(t.get("status_src"), 40),
                "last_seen": last_seen,
                "last_seen_what": what,
            },
            "session": {"id": sid, "why": why, "where": m.host or catchup.THIS_MACHINE},
            "queued": [
                {"id": mid, "prio": PRIO_LABEL.get(prio, "chatter")}
                for mid, prio in self.store.pending_deliveries(m.membership_id, QUEUED_LIMIT)
            ],
            "counts": self.store.membership_delivery_counts(m.membership_id),
            "timeline": [
                self._timeline_entry(m, e)
                for e in self.store.member_timeline(m.membership_id, since=m.joined_at, limit=TIMELINE_LIMIT)
            ],
        }

    def _timeline_entry(self, m: Member, e: dict[str, Any]) -> dict[str, Any]:
        """One Inspector timeline row, rebuilt field by field from a per-kind whitelist
        (DESIGN.md §29): the event's own ``data`` is never passed through."""
        kind, d = e["kind"], e["data"]
        out: dict[str, Any] = {"ts": e["ts"], "kind": kind}
        path = d.get("path")
        path = path if isinstance(path, str) and path in KNOWN_PATHS else "other"
        if kind == "offer":
            ids = [i for i in d.get("ids") or [] if isinstance(i, int) and not isinstance(i, bool)][:20]
            senders = self.store.offer_senders(m.membership_id, ids)
            labels: list[str] = []
            for _prio, sname, shost in senders:
                lab = _sender_label(sname, shost)
                if lab not in labels:
                    labels.append(lab)
            top = max((p for p, _n, _h in senders), default=None)
            out.update(
                path=path,
                n=_num(d.get("n")),
                counted=bool(d.get("counted")),
                prio=PRIO_LABEL.get(top, "chatter") if top is not None else None,
                **{"from": labels[:3]},
            )
        elif kind in ("expire", "cancel"):
            out.update(path=path, reason=_text(d.get("reason"), 200))
        elif kind == "parked":
            out.update(reason=_text(d.get("reason"), 200))
        elif kind == "unparked":
            out.update(seconds=_num(d.get("seconds")))
        elif kind == "rearm":
            out.update(n=_num(d.get("n")))
        elif kind == "requeue":
            reason = d.get("reason")
            out.update(
                reason="redeliver" if reason == "redeliver" else _text(reason, 200), n=_num(d.get("n"))
            )
        elif kind in ("watchdog_remind", "watchdog_escalate"):
            out.update(n=_num(d.get("n")), why=_text(d.get("why"), 40))
        elif kind == "said":
            out.update(id=e["id"])
        # "pass": the kind and the time say it all
        return out

    def _members_snapshot(self, room_name: str) -> list[dict[str, Any]] | None:
        room = self.store.get_room(room_name)
        if room is None:
            return None
        return [member_dict(m) for m in self.member_rows(room.id)]

    def _settings_snapshot(self, room_name: str) -> dict[str, Any] | None:
        room = self.store.get_room(room_name)
        return self.settings(room) if room is not None else None

    def wake_defaults(self, person_id: int | None) -> dict[str, dict[str, Any]]:
        """What a new room this person creates starts with, and where it comes from (#131):
        their own default from Settings if they set one, else ``[delivery]`` in config.toml,
        else the built-in default. Shown in Settings so it's clear what applies; reused by
        ``create_room`` so the precedence is computed in exactly one place."""
        prefs = self.store.preferences(person_id)
        out: dict[str, dict[str, Any]] = {}
        builtin = DeliveryCfg()
        for key in ("budget_per_hour", "hop_limit"):
            own = prefs[key]
            if own is not None:
                out[key] = {"value": own, "source": "your default"}
            else:
                val = getattr(self.cfg.delivery, key)
                source = "built-in default" if val == getattr(builtin, key) else "config.toml"
                out[key] = {"value": val, "source": source}
        return out

    # ---------------------------------------------------------------- writes
    def create_room(
        self, name: str, *, creator: str | None = None, person_id: int | None = None, private: bool = False
    ) -> Room:
        """``private`` (§44): seen only by its people, the creator first; it stays private."""
        try:
            n = normalize_room(name)
        except InvalidName as e:
            raise ServiceError("bad_request", str(e)) from None
        defaults = self.wake_defaults(person_id)
        try:
            room = self.store.create_room(
                n,
                creator or self.cfg.human_name,
                defaults["budget_per_hour"]["value"],
                defaults["hop_limit"]["value"],
                self.store.preferences(person_id)["room_rules"],
                private_for=self.person_key(person_id) if private else None,
            )
        except Conflict:
            raise ServiceError("conflict", f"{n} already exists") from None
        self.store.add_event("room_create", room_id=room.id)
        self._post(
            room,
            sender_name="switchboard",
            sender_kind="system",
            via="system",
            kind="notice",
            text=f"{n} created by {creator or self.cfg.human_name}",
        )
        self.rooms_changed()
        return room

    def rename_room(self, room: Room, name: str, actor: str) -> Room:
        """Rename an open room, keeping its id, members, history, settings and review board."""
        try:
            new_name = normalize_room(name)
        except InvalidName as e:
            raise ServiceError("bad_request", str(e)) from None
        if new_name == room.name:
            raise ServiceError("conflict", f"{new_name} already exists")
        with db.tx(self.store.con):
            try:
                renamed = self.store.rename_open_room(room.id, new_name, expect=room.name)
            except Conflict:
                raise ServiceError("conflict", f"{new_name} already exists") from None
            msg = self.store.insert_message(
                room.id,
                sender_name="switchboard",
                sender_kind="system",
                via="system",
                kind="notice",
                text=f"{actor} renamed {room.name} to {new_name}",
                deliver_notice=True,
            )
            self.store.add_event(
                "room_rename",
                room_id=room.id,
                data={"from": room.name, "to": new_name, "by": actor},
            )
        # Subscribers follow the old name until the rooms frame tells pages to subscribe again.
        self.hub.message(room.name, message_dict(msg))
        self.hub.drop_room(room.name)
        self.rooms_changed()
        if self.delivery is not None:
            self.delivery.on_command(renamed, "rename", None)
        return renamed

    def set_room_rules(self, room_name: str, text: str, actor: str) -> Room:
        if not isinstance(text, str) or len(text) > 2000:
            raise ServiceError("bad_request", "room rules must be at most 2000 characters")
        room = self.room(room_name)
        room = self.store.set_room_rules(room.id, text)
        self.store.add_event("room_rules", room_id=room.id, data={"by": actor})
        self._post(
            room,
            sender_name="switchboard",
            sender_kind="system",
            via="system",
            kind="notice",
            text=f"{actor} updated the room rules",
        )
        self.rooms_changed()
        return room

    def set_room_wake(self, room_name: str, *, budget_per_hour: int, hop_limit: int, actor: str) -> Room:
        """Budget per hour and hop limit from the room's own settings dialog (#131), live and
        without a restart: the rate ``/budget <n>`` can't set (it only ever sets what's left
        this hour) and the hop limit ``/hops <n>`` does, saved together in one dialog. Bounds
        and unknown fields are checked by the caller (the web route). Only a signed-in web
        session reaches this route, the same role ``/budget`` and ``/hops`` need to raise
        activity (``commands.required_role``), so no further role check runs here; lowering
        needs nothing more there either."""
        room = self.room(room_name)
        room = self.store.refill_budget(room.id)  # judge "was" against what's really left now
        old_rate, old_hops = room.budget_per_hour, room.hop_limit
        changed = False
        if budget_per_hour != old_rate:
            room = self.store.set_budget_rate(room.id, budget_per_hour)
            self.store.add_event(
                "budget_set",
                room_id=room.id,
                data={"old": old_rate, "new": budget_per_hour, "per_hour": True},
            )
            self.post_notice(
                room, f"{actor} set the wake budget to {budget_per_hour}/hour (was {old_rate}/hour)"
            )
            changed = True
        if hop_limit != old_hops:
            room = self.store.set_hop_limit(room.id, hop_limit)
            self.store.add_event("hop_limit_set", room_id=room.id, data={"old": old_hops, "new": hop_limit})
            self.post_notice(room, f"{actor} {cmds.hop_limit_notice(old_hops, hop_limit)}")
            changed = True
        if changed:
            self.hub.room_settings(room.name, self.settings(room))
            self.hub.members_changed(room.name)
            if self.delivery is not None:
                # "budget" re-evaluates at once (more may now be deliverable); a hop limit
                # change never un-pauses on its own (cmds._hops_set), so "hops" only needs the
                # snapshot ``on_command``'s default branch already gives it.
                if budget_per_hour != old_rate:
                    self.delivery.on_command(room, "budget", None)
                if hop_limit != old_hops:
                    self.delivery.on_command(room, "hops", None)
        return room

    def notice_everywhere(self, text: str) -> None:
        """A stored notice in every open room (a person renamed, #114): history keeps it, and
        anyone reading a room later sees why the names change."""
        for room in self.store.list_rooms():
            self._post(
                room, sender_name="switchboard", sender_kind="system", via="system", kind="notice", text=text
            )

    def rooms_changed(self) -> None:
        """Tell web clients the open rooms changed (create, close, reopen, delete). A private
        room's name isn't in the frame, which goes to every page (§44): each page reads its own
        list back from GET /api/rooms."""
        self.hub.rooms_changed([r.name for r in self.store.list_rooms() if not r.private])

    def close_room(self, room: Room, actor: Actor) -> str:
        """``/close`` (DESIGN.md §28.3): every member leaves (reason ``closed``, not kicked, the
        credential kept only to name the room in its error), the leave lines and a notice are
        stored, and the room is renamed ``#name~closed-<id>``, all in one transaction. The
        lines are published after COMMIT under the old name, which subscribers follow;
        then nobody follows it any more. Synchronous: nothing else runs in between."""
        if room.closed:  # defensive: room() never returns a closed room
            raise ServiceError("conflict", f"{room.display_name} is already closed")
        members = self.store.members(room.id)
        new = closed_room_name(room.name, room.id)
        human = self.cfg.human_name
        audit = self._audit_suffix(actor)
        posted: list[Message] = []
        with db.tx(self.store.con):
            for m in members:
                self.store.end_membership(m.membership_id, "closed", keep_cred=True)
                posted.append(
                    self.store.insert_message(
                        room.id,
                        sender_name=m.name,
                        sender_kind="agent",
                        sender_harness=m.harness,
                        sender_membership_id=m.membership_id,
                        via="system",
                        kind="leave",
                        text=f"left ({room.name} closed)",
                        sender_host=m.host or None,
                    )
                )
            posted.append(
                self.store.insert_message(
                    room.id,
                    sender_name="switchboard",
                    sender_kind="system",
                    via="system",
                    kind="notice",
                    text=f"{room.name} closed by {human}{audit}: {len(members)} agent(s) removed;"
                    " the history is kept",
                )
            )
            self.store.add_event(
                "room_close",
                room_id=room.id,
                data={
                    "name": room.name,
                    "closed_name": new,
                    "by": human,
                    "via": actor.via,
                    "chain": actor.chain,
                    "members": [m.membership_id for m in members],
                },
            )
            self.store.rename_room(room.id, new, expect=room.name)
        for msg in posted:
            self.hub.message(room.name, message_dict(msg))
        self.hub.drop_room(room.name)
        if self.delivery is not None:
            self.delivery.on_memberships_ended([m.membership_id for m in members], "closed")
        self.rooms_changed()
        hosts: dict[str, int] = {}
        for m in members:
            if m.host:
                hosts[m.host] = hosts.get(m.host, 0) + 1
        extra = " (" + ", ".join(f"{n} on {h}" for h, n in sorted(hosts.items())) + ")" if hosts else ""
        log.info("%s closed via %s: %d member(s) ended", room.name, actor.via, len(members))
        return (
            f"closed {room.name}: {len(members)} agent(s) removed{extra}; history kept."
            " The name is free again; reopen this room from Closed rooms in the web UI"
        )

    def reopen_room(self, room_id: int, via: str = "web") -> Room:
        """A closed room gets its name back (DESIGN.md §28.4). Nobody is re-added: former
        members join() again, and kicked ones are still refused."""
        human = self.cfg.human_name
        with db.tx(self.store.con):
            before = self.store.room_by_id(room_id)
            try:
                room = self.store.reopen_room(room_id)
            except NotFound:
                raise ServiceError("not_found", f"no closed room with id {room_id}") from None
            except Conflict:
                assert before is not None  # reopen_room raised NotFound for a missing room
                raise ServiceError(
                    "conflict",
                    f"{before.display_name} is taken by an open room: close or delete that room first,"
                    " then reopen this one",
                ) from None
            msg = self.store.insert_message(
                room.id,
                sender_name="switchboard",
                sender_kind="system",
                via="system",
                kind="notice",
                text=f"{room.name} reopened by {human} (via {via}); agents join() it again",
            )
            assert before is not None
            self.store.add_event(
                "room_reopen",
                room_id=room.id,
                data={
                    "name": room.name,
                    "from": before.name,
                    "by": human,
                    "via": via,
                },
            )
        self.hub.message(room.name, message_dict(msg))
        self.rooms_changed()
        return room

    def delete_room(
        self,
        ref: str,
        *,
        dry_run: bool,
        room_id: int | None,
        db_path: Any,
        chain: str | None,
        name: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        """``switchboard rooms delete`` (DESIGN.md §28.6): the plan (``dry_run``), or, pinned by
        the plan's ``room_id``, ``name`` and ``created_at``, a checked backup of the whole
        database and then the delete in one transaction. The id alone is not enough: a
        reopen keeps it (only the name changes), and a room re-created after a delete can
        reuse it (only ``created_at`` differs). Refused while the room has members.
        Synchronous: nothing runs between the backup and the delete."""
        try:
            room = self.store.resolve_room(ref)
        except InvalidName as e:
            raise ServiceError(
                "bad_request", f"{e} (or a closed room's full name, e.g. #build~closed-7)"
            ) from None
        except Ambiguous as e:
            n = display_room(e.names[0])
            raise ServiceError(
                "bad_request",
                f"{n} names {len(e.names)} closed rooms: {', '.join(e.names)};"
                " give the full name of the one to delete",
            ) from None
        except NotFound as e:
            raise ServiceError("not_found", str(e)) from None
        if room_id is not None and (
            room.id != room_id
            or (name is not None and room.name != name)
            or (created_at is not None and room.created_at != created_at)
        ):
            raise ServiceError(
                "conflict",
                f"{name or room.name} changed since the plan (reopened, deleted or re-created);"
                " run the command again",
            )
        members = self.store.members(room.id)
        if members:
            labels = ", ".join(member_label(m) for m in members)
            raise ServiceError(
                "conflict",
                f"{room.display_name} has {len(members)} agent(s) ({labels}): close it first"
                f" (/close in the web UI, or switchboard cmd '{room.display_name}' /close)",
            )
        ev = self.store.latest_close_event(room.id) if room.closed else None
        backup = db.delete_backup_path(db_path, room)
        plan = {
            "room_id": room.id,
            "name": room.name,
            "display": room.display_name,
            "state": "closed" if room.closed else "open",
            "created_at": room.created_at,
            "closed_at": ev.ts if ev is not None else None,
            "closed_by": ev.data.get("by") if ev is not None else None,
            "counts": self.store.room_delete_counts(room.id),
            "backup": str(backup),
        }
        if dry_run:
            return plan
        try:
            dest, counts = db.backup_verified(
                self.store.con, backup, tables=db.TABLES, what="pre-delete backup"
            )
        except (db.SchemaError, OSError, sqlite3.Error) as e:
            raise ServiceError("internal", f"the backup failed ({e}); nothing was deleted") from None
        try:
            removed = self.store.delete_room(
                room.id,
                name=room.name,
                created_at=room.created_at,
                expect_counts=counts,
                event={
                    "room_id": room.id,
                    "name": room.name,
                    "display": room.display_name,
                    "backup": dest.name,
                    "chain": chain,
                },
            )
        except Conflict as e:
            raise ServiceError("conflict", f"{e}; nothing was deleted (backup: {dest.name})") from None
        except (StoreError, sqlite3.Error) as e:
            raise ServiceError("internal", f"{e}; nothing was deleted (backup: {dest.name})") from None
        if not room.closed:
            self.hub.drop_room(room.name)
        self.rooms_changed()
        self.hub.notice(None, "warn", f"{room.name} deleted via cli ({chain}); backup {dest.name}")
        log.warning(
            "%s (room %d) deleted via cli (%s): %s; backup %s", room.name, room.id, chain, removed, dest
        )
        return {
            "room_id": room.id,
            "name": room.name,
            "display": room.display_name,
            "removed": removed,
            "backup": str(dest),
        }

    def _post(self, room: Room, *, level: str | None = None, **kw: Any) -> Message:
        """Persist, then publish. Every broker-side message goes through here.

        ``level`` (a notice's 'info'/'warn') rides on the published frame only; it
        isn't stored (history re-derives 'warn' for the engine's fixed-phrase warnings,
        ``WARN_NOTICE_PREFIXES``)."""
        msg = self.store.insert_message(room.id, **kw)
        if msg.kind == "chat":
            self.store.add_event("msg", room_id=room.id, data={"id": msg.id, "via": msg.via})
        frame = message_dict(msg)
        if level is not None and msg.kind == "notice":
            frame["level"] = level
        self.hub.message(room.name, frame)
        if msg.kind == "chat" and self.delivery is not None:
            self.delivery.on_message(msg)
        return msg

    def reply_target(self, room: Room, reply_to: Any) -> Message | None:
        """Validate a reply id for both human and agent says before querying SQLite."""
        if reply_to is None:
            return None
        if type(reply_to) is not int or not 0 < reply_to <= MAX_MESSAGE_ID:
            raise ServiceError("bad_request", "reply_to must be a message id")
        target = self.store.get_message(reply_to)
        if target is None or target.room_id != room.id or target.kind != "chat":
            raise ServiceError("bad_request", f"reply_to {reply_to} is not a message in {room.name}")
        return target

    def human_say(
        self,
        name: str,
        text: str,
        via: str,
        *,
        skip: tuple[int, ...] = (),
        person: tuple[str, int | None] | None = None,
        reply_to: int | None = None,
    ) -> Message:
        """A message from a human. Posted literally, never parsed as a command.
        ``skip``: memberships that get no delivery of it (only ``/catchup`` passes any: its
        subjects, §26). ``person``: who, on a hosted broker with people (§32): their name
        and id (None: the owner); by default the owner, ``human_name``. An ``@here``/
        ``@everyone`` broadcast (issue #111) is expanded here, since every caller is a
        person's message (a /catchup post, a review board handoff, or the web/CLI say).
        ``@humans`` (issue #138) is expanded here too, though it works the same from an
        agent's own ``say()`` (``broker/agents.py``), which expands it independently."""
        room = self.room(name)
        if not isinstance(text, str):
            raise ServiceError("bad_request", "text must be a string")
        text = text.rstrip()
        if not clean(text).strip():
            # e.g. only control or zero-width characters: it would show as an empty line
            raise ServiceError("bad_request", "empty message")
        if len(text) > self.cfg.delivery.max_msg_chars:
            raise ServiceError(
                "bad_request",
                f"message too long ({len(text)} > {self.cfg.delivery.max_msg_chars} characters)",
            )
        self.reply_target(room, reply_to)
        mentions = parse_mentions(text, self.mention_names(room))
        broadcast = parse_broadcast(text)
        if broadcast:
            # A safe start (issue #111): only a person's @here/@everyone reaches every agent;
            # from an agent it's plain text (human_say is never called for an agent's say()).
            # Expanding mentions with the targets' own names is the whole mechanism -- see
            # rules.broadcast_targets for why no other delivery rule needs to change.
            members = [(m.name, m.status) for m in self.store.members(room.id)]
            mentions = sorted(set(mentions) | set(broadcast_targets(broadcast, members)) | {broadcast})
        if mentions_humans(text):
            # Addresses every person in the room, never an agent (issue #138): the literal
            # word is the whole mechanism -- it can never match a real agent's screen name
            # ("humans" is reserved), so it changes no delivery. app.js's addressesHumans is
            # the only thing that reads it back off the message's own mentions.
            mentions = sorted(set(mentions) | {HUMANS_MENTION})
        msg = self._post(
            room,
            sender_name=person[0] if person is not None else self.cfg.human_name,
            sender_kind="human",
            via=via,
            text=text,
            reply_to=reply_to,
            mentions=mentions,
            skip_memberships=skip,
            sender_person_id=person[1] if person is not None else None,
        )
        before = room.hop_count
        if before:
            self.hub.room_settings(room.name, self.settings(room))
        self.hub.members_changed(room.name)
        return msg

    def mention_names(self, room: Room) -> list[str]:
        """Names an @mention can address: active members, plus the human (for UI highlighting;
        the human has no delivery rows)."""
        return self.store.active_names(room.id) + self.room_humans(room)

    def people_names(self) -> list[str]:
        """The humans (§32): the owner, then everyone the owner added on a hosted broker."""
        return [self.cfg.human_name] + [p.name for p in self.store.people()]

    def post_notice(self, room: Room, text: str, level: str | None = None) -> Message:
        return self._post(
            room,
            sender_name="switchboard",
            sender_kind="system",
            via="system",
            kind="notice",
            text=text,
            level=level,
        )

    def kick(self, room: Room, member: Member) -> None:
        self.store.end_membership(member.membership_id, "kick", kicked=True)
        if self.delivery is not None:
            self.delivery.on_membership_ended(member.membership_id, "kick")
        self._post(
            room,
            sender_name=member.name,
            sender_kind="agent",
            sender_harness=member.harness,
            sender_membership_id=member.membership_id,
            via="system",
            kind="leave",
            text=f"was kicked by {self.cfg.human_name}",
            sender_host=member.host or None,
        )

    def command(self, name: str, text: str, actor: Actor) -> dict[str, Any]:
        room = self.room(name)
        try:
            cmd = cmds.parse_command(text)
            result = cmds.apply(cmd, room, actor, self)
        except CommandError as e:
            raise ServiceError(e.code, e.message) from None
        if result.done:
            # /close published everything itself; the steps below would publish under the
            # room's new name (§28.3)
            return {"ok": result.ok, "text": result.text}
        posted: Message | None = None
        if result.post is not None:
            # /catchup: one ordinary human chat message (every delivery rule applies); a refusal
            # here (too long, empty) leaves nothing posted or recorded
            posted = self.human_say(
                room.name,
                result.post,
                via=actor.via,
                skip=result.post_skip,
                person=(actor.who(self.cfg.human_name), actor.person_id),
            )
        room = self.store.room_by_id(room.id) or room
        audit = self._audit_suffix(actor)
        if result.event:
            data = {"via": actor.via, **result.event_data}
            if posted is not None:
                data["message_id"] = posted.id
            self.store.add_event(result.event, room_id=room.id, data=data)
        if result.notice:
            self.post_notice(room, f"{actor.who(self.cfg.human_name)} {result.notice}{audit}")
        elif actor.via == "cli":
            # Every command issued over the CLI leaves an audit line (§10).
            self.post_notice(room, f"/{cmd.name} by {self.cfg.human_name}{audit}")
        for w in result.warnings:
            self.post_notice(room, w, level="warn")
        if result.room_changed:
            self.hub.room_settings(room.name, self.settings(room))
        if result.members_changed or result.room_changed:
            self.hub.members_changed(room.name)
        if self.delivery is not None and result.event:
            self.delivery.on_command(room, cmd.name, result.event_data.get("membership_id"))
        for fn in self.on_room_changed:
            fn(room, cmd.name)
        return {"ok": result.ok, "text": result.text}

    @staticmethod
    def _audit_suffix(actor: Actor) -> str:
        if actor.via == "cli":
            return f" (via cli: {actor.chain or 'unknown'})"
        return f" (via {actor.via})"

    # ------------------------------------------------------------- reporting
    def session_handles(self, room: Room) -> dict[str, str]:
        """Member name -> ``<session id> @ <host>`` (``<id> @ this machine``), for members
        whose session id /catchup would pass on (§26). Shown to the human only (/who,
        `switchboard who`)."""
        out = {}
        for m in self.store.members(room.id):
            sid, _why = catchup.session_id(self.store.get_participant(m.participant_id), self.cfg)
            if sid:
                out[m.name] = f"{sid} @ {m.host or catchup.THIS_MACHINE}"
        return out

    def who_text(self, room: Room) -> str:
        members = self.member_rows(room.id)
        sessions = self.session_handles(room)
        lines = [f"{room.name}: {self.cfg.human_name} (you, human) + {len(members)} agent(s)"]
        if not members:
            lines.append("  no agents have joined yet")
        for m in members:
            flags = []
            if m.approval_mode == "bypass":
                flags.append("⚠ approvals off")
            elif m.approval_mode == "unknown":
                flags.append("? approval mode unknown (treat like ⚠)")
            if m.env_leak:
                flags.append("env shared")
            if m.held:
                flags.append("⏸ held")
            if m.queued:
                flags.append(f"{m.queued} queued")
            if m.inflight:
                flags.append(f"{m.inflight} in flight")
            if m.parked:
                flags.append(f"parked — needs a poke ({m.parked_reason or 'no wake path'})")
            if m.name in sessions:
                flags.append(f"session: {sessions[m.name]}")
            tier = tier_label(m.tier, m.tier_note)
            away = f' away: "{clean(m.away)}"' if m.away else ""
            lines.append(
                f"  {member_label(m)}  {m.harness}  {m.status}  {tier}"
                + (f"  [{', '.join(flags)}]" if flags else "")
                + away
            )
        return "\n".join(lines)

    def status_text(self, room: Room) -> str:
        room = self.store.refill_budget(room.id)
        state = f"paused ({room.paused_reason})" if room.paused else "active"
        lines = [
            f"{room.name}: {state}",
            f"  budget {room.budget_remaining}/{room.budget_per_hour} wakes this hour"
            f" (refills {_hhmmss(room.budget_reset_at)}); {cmds.hops_state(room)}",
        ]
        members = self.member_rows(room.id)
        if not members:
            lines.append("  agents: none")
        for m in members:
            counts = self.store.membership_delivery_counts(m.membership_id)
            exp = self.store.batch_expiry_counts(m.membership_id)
            exp_s = ", ".join(f"{k} {v}" for k, v in sorted(exp.items())) or "none"
            tier = tier_label(m.tier, m.tier_note)
            lines.append(
                f"  {member_label(m)}: {m.status}, tier {tier}, queued {counts.get('pending', 0)},"
                f" in flight {counts.get('offered', 0)}, expired: {exp_s}"
            )
        rule_kinds = (
            "pause",
            "resume",
            "budget_set",
            "budget_exhausted",
            "loop_guard",
            "hop_limit_set",
            "rate_limited",
            "pass_refused",
            "hold",
            "release",
            "kick",
            "watchdog_remind",
            "watchdog_escalate",
            "expire",
            "requeue",
        )
        evs = self.store.recent_events(room_id=room.id, kinds=rule_kinds, limit=10)
        if evs:
            lines.append("  recent rule events:")
            for e in evs:
                lines.append(f"    [{_hhmmss(e.ts)}] {e.kind}")
        else:
            lines.append("  recent rule events: none")
        lines.append(f"  codex link: {self.info.codex_link}")
        lines.append(f"  hooks: {self.info.hook_state}")
        if self.info.test_mode:
            lines.append("  TEST MODE")
        return "\n".join(lines)

    def status(self) -> dict[str, Any]:
        now = self.clock.now()
        rooms = []
        for r in self.store.list_rooms():
            r = self.store.refill_budget(r.id)
            rooms.append(
                {
                    "name": r.name,
                    "paused": r.paused,
                    "paused_reason": r.paused_reason,
                    "members": len(self.store.active_names(r.id)),
                    "budget_remaining": r.budget_remaining,
                    "budget_per_hour": r.budget_per_hour,
                    "hop_count": r.hop_count,
                    "hop_limit": r.hop_limit,
                }
            )
        return {
            "version": __version__,
            "commit": build_info.commit(),
            "pid": self.info.pid,
            "port": self.info.port,
            "url": self.info.url or f"http://switchboard.localhost:{self.info.port}/",
            "uptime_s": round(now - self.info.started_at, 1),
            "home": self.info.home,
            "rooms": rooms,
            "closed_rooms": self.store.count_closed_rooms(),
            "members": self.store.count_active_members(),
            "web_clients": self.hub.ws_count(),
            "codex_link": self.info.codex_link,
            "hooks": self.info.hook_state,
            "test_mode": self.info.test_mode,
            "remotes": self.remotes.summary() if self.remotes is not None else [],
            "machines": self.machines.summary() if self.machines is not None else [],
        }
