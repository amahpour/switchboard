"""RoomService: the broker's room operations (DESIGN.md §3, §10).

The human side: rooms, history, the buddy list, human messages and
commands. Agent operations live in ``broker/agents.py`` (AgentService) and
plug into the same persist-then-publish flow through ``self.delivery``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from switchboard import __version__
from switchboard.broker import catchup
from switchboard.broker import commands as cmds
from switchboard.broker.commands import Actor, CommandError
from switchboard.broker.hub import Hub
from switchboard.clock import Clock, SystemClock
from switchboard.config import Config
from switchboard.delivery.rules import parse_mentions
from switchboard.envelope import clean
from switchboard.models import InvalidName, Member, Message, Room, normalize_room, tier_label
from switchboard.store import Conflict, Store

log = logging.getLogger("switchboard.service")


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
    hook_state: str = "not checked"
    codex_link: str = "not started"


class DeliveryHooks(Protocol):
    """What RoomService tells the delivery side (implemented by AgentService)."""

    def on_message(self, msg: Message) -> None: ...

    def on_command(self, room: Room, name: str, membership_id: int | None) -> None: ...

    def on_membership_ended(self, membership_id: int, reason: str) -> None: ...

    def parked_reason(self, membership_id: int) -> str | None: ...


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
        "approval_mode": m.approval_mode,
        "env_leak": m.env_leak,
        "held": m.held,
        "queued": m.queued,
        "inflight": m.inflight,
        "parked": m.parked,
        "parked_reason": m.parked_reason,
        # the member's host: '' for this machine, else the remote's name (§27.11)
        "host": m.host,
    }


def member_label(m: Member) -> str:
    """``bench@fpga-pi`` for a remote member, the plain name on this machine."""
    return f"{m.name}@{m.host}" if m.host else m.name


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
            "name": room.name,
            "slug": room.slug,
            "created_at": room.created_at,
            "members": len(self.store.active_names(room.id)),
            "last_id": self.store.last_message_id(room.id),
            "settings": self.settings(room),
        }

    def rooms(self) -> list[dict[str, Any]]:
        return [self.room_dict(r) for r in self.store.list_rooms()]

    def history(self, name: str, after: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        room = self.room(name)
        return [message_dict(m) for m in self.store.history(room.id, after, limit)]

    def members(self, name: str) -> list[dict[str, Any]]:
        room = self.room(name)
        return [member_dict(m) for m in self.member_rows(room.id)]

    def member_rows(self, room_id: int) -> list[Member]:
        """Buddy-list rows, with the engine's parked state overlaid."""
        rows = self.store.members(room_id)
        if self.delivery is None:
            return rows
        out = []
        for m in rows:
            why = self.delivery.parked_reason(m.membership_id)
            out.append(dataclasses.replace(m, parked=why is not None, parked_reason=why) if why else m)
        return out

    def _members_snapshot(self, room_name: str) -> list[dict[str, Any]] | None:
        room = self.store.get_room(room_name)
        if room is None:
            return None
        return [member_dict(m) for m in self.member_rows(room.id)]

    def _settings_snapshot(self, room_name: str) -> dict[str, Any] | None:
        room = self.store.get_room(room_name)
        return self.settings(room) if room is not None else None

    # ---------------------------------------------------------------- writes
    def create_room(self, name: str) -> Room:
        try:
            n = normalize_room(name)
        except InvalidName as e:
            raise ServiceError("bad_request", str(e)) from None
        try:
            room = self.store.create_room(
                n,
                self.cfg.human_name,
                self.cfg.delivery.budget_per_hour,
                self.cfg.delivery.hop_limit,
            )
        except Conflict:
            raise ServiceError("conflict", f"{n} already exists") from None
        self.store.add_event("room_create", room_id=room.id)
        self._post(room, sender_name="switchboard", sender_kind="system", via="system",
                   kind="notice", text=f"{n} created by {self.cfg.human_name}")
        self.hub.rooms_changed([r.name for r in self.store.list_rooms()])
        return room

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

    def human_say(self, name: str, text: str, via: str, *, skip: tuple[int, ...] = ()) -> Message:
        """A message from the human. Posted literally, never parsed as a command.
        ``skip``: memberships that get no delivery of it (only ``/catchup`` passes any: its
        subjects, §26)."""
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
        mentions = parse_mentions(text, self.mention_names(room))
        msg = self._post(
            room,
            sender_name=self.cfg.human_name,
            sender_kind="human",
            via=via,
            text=text,
            mentions=mentions,
            skip_memberships=skip,
        )
        before = room.hop_count
        if before:
            self.hub.room_settings(room.name, self.settings(room))
        self.hub.members_changed(room.name)
        return msg

    def mention_names(self, room: Room) -> list[str]:
        """Names an @mention can address: active members, plus the human (for UI highlighting;
        the human has no delivery rows)."""
        return self.store.active_names(room.id) + [self.cfg.human_name]

    def post_notice(self, room: Room, text: str, level: str | None = None) -> Message:
        return self._post(room, sender_name="switchboard", sender_kind="system",
                          via="system", kind="notice", text=text, level=level)

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
        posted: Message | None = None
        if result.post is not None:
            # /catchup: one ordinary human chat message (every delivery rule applies); a refusal
            # here (too long, empty) leaves nothing posted or recorded
            posted = self.human_say(room.name, result.post, via=actor.via, skip=result.post_skip)
        room = self.store.room_by_id(room.id) or room
        audit = self._audit_suffix(actor)
        if result.event:
            data = {"via": actor.via, **result.event_data}
            if posted is not None:
                data["message_id"] = posted.id
            self.store.add_event(result.event, room_id=room.id, data=data)
        if result.notice:
            self.post_notice(room, f"{self.cfg.human_name} {result.notice}{audit}")
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
            "pause", "resume", "budget_set", "budget_exhausted", "loop_guard", "hop_limit_set", "rate_limited",
            "pass_refused", "hold", "release", "kick", "watchdog_remind", "watchdog_escalate", "expire",
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
            "pid": self.info.pid,
            "port": self.info.port,
            "url": f"http://switchboard.localhost:{self.info.port}/",
            "uptime_s": round(now - self.info.started_at, 1),
            "home": self.info.home,
            "rooms": rooms,
            "members": self.store.count_active_members(),
            "web_clients": self.hub.ws_count(),
            "codex_link": self.info.codex_link,
            "hooks": self.info.hook_state,
            "test_mode": self.info.test_mode,
            "remotes": self.remotes.summary() if self.remotes is not None else [],
        }
