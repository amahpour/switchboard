"""AgentService: the agent side of the broker (DESIGN.md §5.2, §6, §7, §8, §49).

Every agent reaches the broker at ``/mcp`` (``broker/agent_http.py``, §49.2) with an agent token. Its calls
are served here, with a ``HttpConn`` standing in for a connection: the token's device is its host (§49.3),
and a call names a membership (``m:<id>``) that is checked against the caller's own session, worked out
from the token and the harness's session id. There are no credentials and no processes: a session is
told apart by what its harness says, and what it may do by its token (§11).

- ``join``, ``leave``, ``who``, ``say``, ``read``, ``wait``, ``pass``, ``review``, ``claim``, ``away`` and
  ``status`` are the tools (``mcp/server.py``).
- ``http_hook`` takes a harness's hook report (§49.4): the engine updates status, confirms offers and,
  where the hook reply table allows it (``broker/hook_replies.py``), returns priority context, or holds
  a Stop hook until a message comes.

All policy lives in the engine; this module validates input, resolves identities and executes the
engine's actions through the runner.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from switchboard import claims, envelope
from switchboard.broker.service import MAX_MESSAGE_ID, ServiceError
from switchboard.delivery import rules
from switchboard.envelope import TOKEN_RE, clean, sanitize
from switchboard.models import (
    NAME_REUSE_S,
    RESERVED_NAMES,
    SCREEN_NAME_RE,
    Action,
    HookEvent,
    Membership,
    Message,
    Participant,
    Room,
    Snapshot,
    join_code_hash,
)
from switchboard.reviews import ProofRequired, ReviewError

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.agent_http import HttpConn
    from switchboard.broker.app import BrokerState

log = logging.getLogger("switchboard.agents")

# how a session over HTTPS learns why it is in no room (§49.4): the ends it didn't choose
ENDED_BY_ITSELF = frozenset({"leave", "session_end"})
ENDED_WHY = {
    "kick": "your user removed it",
    "closed": "the room was closed",
    "person_left": "your user's account was removed",
    "not_allowed": "it is no longer allowed in that room",
    "unreachable": "the server took its machine for unreachable",
    "removed": "its machine was removed",
    "token revoked": "its agent token was revoked",
}

WAIT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
# the code at the end of a join's answer ("join yk:j<16 hex>"), given back to reclaim a name (#304)
JOIN_CODE_RE = re.compile(r"^(?:yk:j)?([0-9a-f]{16})$")
# the refusal for a screen name SCREEN_NAME_RE doesn't match: an example name, not a harness rule
BAD_SCREEN_NAME = "screen names look like claude-1: a-z first, then a-z, 0-9, '_' or '-', at most 24"
# a hook's reported model name (the hook relays only this shape too), for the report:
# an "@" may carry a version (claude-...@20250514), never a domain (nothing email-shaped)
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}(?:@[A-Za-z0-9_:+-]{1,32})?")
AWAY_MAX = 80


# the harnesses a hook over HTTPS may name (§49.4): the four, and the scripted test agent
HOOK_EVENTS: dict[str, frozenset[str]] = {
    "claude": frozenset(
        {"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"}
    ),
    "codex": frozenset({"UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "SessionEnd"}),
    "cursor": frozenset(
        {"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"}
    ),
    "devin": frozenset(
        {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"}
    ),
    "test": frozenset(
        {"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"}
    ),
}


@dataclass
class AgentConn:
    """Who is calling over HTTPS (§49.3), as far as the call says: the harness a client claims to be
    (its ``clientInfo``), the device of its token, and, for a scripted test agent of a test-mode broker,
    the session it names and how its answers are confirmed (§9.6). Claims, used only to pick wording and
    adapter; what a caller may do comes from its token."""

    harness: str
    host: str
    client_info: dict[str, Any] = field(default_factory=dict)
    test_session: str | None = None
    ack_mode: str | None = None


# what an agent over HTTPS passes instead of a credential: the membership it means (§49.3)
HTTP_MEMBERSHIP_RE = re.compile(r"m:([1-9][0-9]{0,17})")


# Why a join code didn't bring a name back (#370), each its own words: one sentence for all of them
# hid what to do. The ones after "mismatch" are said only to a caller whose code matched, so a
# guess learns nothing about the name's last holder.
RECLAIM_WHY = {
    "no_code": "that join has no code on record: it was made before join codes could bring a name back",
    "mismatch": (
        "that join code isn't this name's current one: after a /clear or a compaction, switchboard's"
        " reminder gives each room a fresh code and the earlier one stops working, so use the newest"
        " code you were given"
    ),
    "kicked": "that agent was kicked from this room, so its name can't be taken back",
    "harness": (
        "that code is from another harness's session: a code brings a name back only to the same harness"
    ),
    "host": "that code is from a session on another machine: a code brings a name back only on the same one",
    "gone": "that session is no longer on record",
}


def name_reserved_text(name: str, room: str, code: str | None, why: str | None = None) -> str:
    """Why a join was refused a reserved name, and what to do. A caller that gave ``code`` has the
    ``resume`` parameter already, and is told why it didn't work (``why``, a ``RECLAIM_WHY`` key,
    #370); any other is told the way back: ``resume=``, the code at the end of its earlier join."""
    text = f"{name} was used by another agent in {room} today; pick another"
    if code is not None:
        return f"{text} ({RECLAIM_WHY.get(why or 'mismatch', RECLAIM_WHY['mismatch'])})"
    return (
        text + ", or, if it was you before a restart, join again with resume= set to"
        " the code at the end of that join's answer (yk:j...)"
    )


# a rate-limited say() waits its turn (§8.4); a burst past this many per member is refused
MAX_QUEUED_SAYS = 3


def _model_ok(model: Any) -> bool:
    """A hook-reported model name worth recording: short and identifier-shaped."""
    return isinstance(model, str) and len(model) <= 64 and MODEL_RE.fullmatch(model) is not None


def clamp_before(value: Any) -> int:
    """``read(before=...)``'s cursor must be a message id (issue #300 review): it picks a
    different call (a history page, not the normal pending-items answer), so a wrong type is
    refused rather than silently falling back the way ``limit`` does. ``type(value) is not
    int`` rather than ``isinstance``, so a bool (``True``/``False``) is refused too, not read
    as 1/0. A valid int is clamped to a message id's range, as ``reply_to`` is (service.py)."""
    if type(value) is not int:
        raise ServiceError("bad_request", "before must be a message id")
    return max(0, min(value, MAX_MESSAGE_ID))


class AgentService:
    def __init__(self, state: "BrokerState"):
        self.state = state
        self._models: dict[int, str] = {}  # participant id -> last model recorded
        rec = os.environ.get("SWITCHBOARD_RECORD_PAYLOADS")
        self.record_dir = Path(rec) if rec and state.test_mode else None
        # membership id -> its rate-limited says waiting their turn: (text, reply_to), oldest first
        self._queued_says: dict[int, deque[tuple[str, int | None]]] = {}

    # ------------------------------------------------------------ plumbing
    @property
    def store(self) -> Any:
        return self.state.store

    @property
    def engine(self) -> Any:
        return self.state.engine

    @property
    def cfg(self) -> Any:
        return self.state.cfg

    @property
    def svc(self) -> Any:
        return self.state.service

    def run(self, actions: list[Action]) -> None:
        if actions:
            self.state.runner.execute(actions)

    # -------------------------------------------------- RoomService hooks
    def on_message(self, msg: Message) -> None:
        self.run(self.engine.on_message(msg.id))

    def on_command(self, room: Room, name: str, membership_id: int | None) -> None:
        self.run(self.engine.on_command(room.id, name, membership_id))

    def on_membership_ended(self, membership_id: int, reason: str) -> None:
        self.run(self.engine.on_membership_ended(membership_id, reason))

    def on_memberships_ended(self, membership_ids: list[int], reason: str) -> None:
        """Several at once (``/close``, §28.3): the engine per membership."""
        for mid in membership_ids:
            self.run(self.engine.on_membership_ended(mid, reason))

    def parked_reason(self, membership_id: int) -> str | None:
        return self.engine.parked_reason(membership_id)

    def wakes_at(self, membership_id: int) -> float | None:
        return self.engine.wakes_at(membership_id)

    # ======================================================== membership
    def _session_key(self, mc: AgentConn, thread_id: str | None) -> str:
        """This harness's session key for a call on the token's device (§6.3): Codex's thread, Claude
        Code's, Cursor's and Devin's session id (each harness's own, named by its hooks), the scripted
        test agent's own key."""
        try:
            return self.engine.adapter_for(mc.harness).session_key(
                mc.host, thread_id, test_session=mc.test_session
            )
        except ValueError as e:
            raise ServiceError("bad_request", str(e)) from e

    def _reclaim(
        self, holder: Membership, code: str | None, harness: str, host: str
    ) -> tuple[Membership | None, str | None]:
        """The membership whose reserved name a join may take back (#304, DESIGN.md §6.1), or None
        with why not (a ``RECLAIM_WHY`` key, #370; None when no code was given).

        Only with the code that membership's own join answer ended with (or the newest a reminder
        gave it), which only the session that read it has, and only for the same harness on the
        same device (so the same person): never a name another session or device used. Not
        from a kicked agent, and not while the old membership is still in the room. The code is
        checked before anything about the holder is said, so a wrong one learns only that."""
        if code is None:
            return None, None
        if holder.active:
            return None, "mismatch"  # the caller checks this first (name_taken); never a reclaim
        if holder.join_code_hash is None:
            return None, "no_code"
        if not hmac.compare_digest(holder.join_code_hash, join_code_hash(code)):
            return None, "mismatch"
        if holder.kicked:
            return None, "kicked"
        old = self.store.get_participant(holder.participant_id)
        if old is None:
            return None, "gone"
        if old.harness != harness:
            return None, "harness"
        if (old.host or "") != (host or ""):
            return None, "host"
        if self.store.was_kicked(holder.room_id, old.id):
            return None, "kicked"
        return holder, None

    def _member(
        self, conn: "HttpConn", params: dict[str, Any], *, next_call: bool = True
    ) -> tuple[Participant, Membership, Room]:
        """The call's membership (§49.3): the one it names (``m:<id>``) must be the caller's own
        session's, and active. A session is told apart by what its harness names on every call, so a
        process that merely knows a Codex thread id on another device can't be taken for it: the
        token's device is part of the key."""
        tid = params.get("thread_id")
        tid = tid if isinstance(tid, str) and 0 < len(tid) <= 128 else None
        got = params.get("cred")
        got = HTTP_MEMBERSHIP_RE.fullmatch(got) if isinstance(got, str) else None
        p = self.http_session(conn, tid)
        m = self.store.get_membership(int(got.group(1))) if got is not None else None
        if p is None or m is None or m.participant_id != p.id:
            raise ServiceError("unauthorized", "not a member: join() first")
        room = self.store.room_by_id(m.room_id)
        if not m.active or room is None:
            raise ServiceError("unauthorized", "not a member (your membership ended): join() again")
        credential_error = self.engine.adapter(p).credential_error(p, params)
        if credential_error is not None:
            raise ServiceError("unauthorized", credential_error)
        if next_call:
            self.run(self.engine.before_call(p))
            p = self.store.get_participant(p.id) or p
        return p, m, room

    # ======================================================= the caller's session (§49.3)
    def http_ended(self, conn: "HttpConn", thread_id: str | None) -> str | None:
        """Why the caller's session is in no room now, when something other than the session
        itself ended it (a kick, a closed room, a removed person): the line its listener wakes it
        with, once (§49.4), since nothing else reaches an idle session over HTTPS. None while it is
        in a room, before it ever joined, or after it left or quit on its own."""
        mc = conn.agent
        p = self.store.find_participant(mc.harness, self._session_key(mc, thread_id))
        if p is None or p.host != conn.host or self.store.participant_memberships(p.id):
            return None
        m = self.store.last_ended_membership(p.id)
        if m is None or m.left_at is None or m.left_reason in ENDED_BY_ITSELF:
            return None
        room = self.store.room_by_id(m.room_id)
        why = ENDED_WHY.get(m.left_reason or "", f"it ended ({m.left_reason})")
        when = time.strftime("%H:%M UTC", time.gmtime(m.left_at))
        return (
            f"[switchboard] {m.screen_name} is no longer in {room.name if room else 'its room'}: {why},"
            f" at {when}. switchboard can't reach this session until it joins a room again."
        )

    def http_session(self, conn: "HttpConn", thread_id: str | None) -> Participant | None:
        """The caller's own session, if it joined: by the harness's session key on the token's
        device (a Codex thread). None before its first join."""
        mc = conn.agent
        p = self.store.find_participant(mc.harness, self._session_key(mc, thread_id))
        return p if p is not None and p.active and p.host == conn.host else None

    def http_rooms(self, conn: "HttpConn", thread_id: str | None) -> list[tuple[Room, Membership]]:
        """The rooms the caller's session is in now, with its membership in each."""
        p = self.http_session(conn, thread_id)
        if p is None:
            return []
        out = []
        for m in self.store.participant_memberships(p.id):
            room = self.store.room_by_id(m.room_id)
            if room is not None:
                out.append((room, m))
        return out

    def http_room(self, conn: "HttpConn", name: str, thread_id: str | None) -> tuple[Room, Membership] | None:
        """The caller's membership in ``name`` (a canonical room name), or in the room renamed
        from it (#316): both names work, as they do over the socket."""
        rooms = self.http_rooms(conn, thread_id)
        for room, m in rooms:
            if room.name == name:
                return room, m
        renamed = self.store.room_by_alias(name)
        if renamed is not None and self.store.get_room(name) is None:
            for room, m in rooms:
                if room.id == renamed.id:
                    return room, m
        return None

    def join(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        mc = conn.agent
        raw_room = params.get("room")
        if not isinstance(raw_room, str) or not raw_room.strip():
            raise ServiceError("bad_request", "room is required, e.g. #build")
        host = mc.host
        # whose agent this is (§44.3): a private room takes only its own people's agents, and
        # says "no such room" to any other, as it does to the people who aren't in it
        person = self.svc.host_person(host)
        try:
            room = self.svc.room(raw_room)
        except ServiceError as e:
            if e.code == "not_found":
                n = raw_room.strip().lower()
                n = n if n.startswith("#") else "#" + n
                alias = self.store.room_by_alias(n)
                if alias is not None and self.svc.visible(alias, person):
                    raise ServiceError(
                        "not_found", f"{n} was renamed to {alias.name}: join {alias.name} instead"
                    ) from None
                if any(self.svc.visible(r, person) for r in self.store.closed_rooms(n)):
                    raise ServiceError(
                        "not_found", f"{n} was closed by {self.cfg.human_name}: ask your user to reopen it"
                    ) from None
                raise ServiceError(
                    "not_found", f"no such room {clean(n)[:40]}: ask your user to create it"
                ) from None
            raise
        if not self.svc.visible(room, person):
            raise ServiceError(
                "not_found", f"no such room {clean(room.name)[:40]}: ask your user to create it"
            )
        name = params.get("screen_name")
        name = name.strip().lower().lstrip("@") if isinstance(name, str) else ""
        if not SCREEN_NAME_RE.match(name):
            raise ServiceError("bad_request", BAD_SCREEN_NAME)
        human = self.cfg.human_name.lower()
        if (
            name in RESERVED_NAMES
            or name.startswith("switchboard")
            or name.startswith(human)
            or any(name.startswith(person.lower()) for person in self.svc.people_names())
        ):
            raise ServiceError("name_reserved", f"{name} is reserved; pick another screen name")
        resume = params.get("resume")
        code: str | None = None
        if resume is not None:
            got = (
                JOIN_CODE_RE.match(resume.strip()) if isinstance(resume, str) and len(resume) <= 40 else None
            )
            if got is None:
                raise ServiceError(
                    "bad_request", "resume is the join code from your earlier join: yk:j and 16 hex"
                )
            code = got.group(1)
        tid = params.get("thread_id")
        tid = tid if isinstance(tid, str) and 0 < len(tid) <= 128 else None
        key = self._session_key(mc, tid)
        h = mc.harness
        adapter = self.engine.adapter_for(h)
        conflict = adapter.join_conflict(self.store, tid, host)
        if conflict is not None:
            raise ServiceError("conflict", conflict)
        existing = self.store.find_participant(h, key)
        if existing is not None and self.store.was_kicked(room.id, existing.id):
            raise ServiceError("kicked", f"you were kicked from {room.name}; ask your user")
        now = self.state.clock.now()
        tier, note = adapter.tier(existing)
        fields: dict[str, Any] = dict(tier=tier, tier_note=note, session_id=tid, host=host)
        # a harness whose status comes from its sinks (the test agent) starts busy, as does a
        # session whose hooks were already seen: its next hook or wait() says otherwise
        if adapter.sink_status or (existing is not None and existing.hooks_seen_at is not None):
            fields.update(status="busy", status_at=now, status_src="join")
        elif existing is None or existing.status == "offline":
            fields.update(status="starting", status_at=now, status_src="join")
        cur = self.store.active_membership(room.id, existing.id) if existing else None
        resumed: Membership | None = None
        if cur is None:
            other = self.store.active_membership_by_name(room.id, name)
            if other is not None and (existing is None or other.participant_id != existing.id):
                raise ServiceError("name_taken", f"{name} is taken in {room.name}; pick another screen name")
            holder = self.store.last_holder_of(
                room.id, name, existing.id if existing else -1, now - NAME_REUSE_S
            )
            if holder is not None:
                resumed, why = self._reclaim(holder, code, h, host)
                if resumed is None:
                    raise ServiceError("name_reserved", name_reserved_text(name, room.name, code, why))
        elif cur.screen_name.lower() != name:
            raise ServiceError(
                "bad_request",
                f"this session is already in {room.name} as {cur.screen_name};"
                " leave() first to change your name",
            )
        nonce = secrets.token_hex(8)
        p = self.store.upsert_participant(h, key, **fields)
        if mc.ack_mode is not None:
            self.engine.ack_modes[p.id] = mc.ack_mode
        rejoined = cur is not None
        if cur is not None:
            # the same session again (a reconnect, a retry): a fresh join code
            self.store.set_join_code(cur.id, join_code_hash(nonce))
            m = cur
            self.run(self.engine.expire_pull_batches(p.id, "rejoin"))
            self.store.add_event(
                "bind", room_id=room.id, membership_id=m.id, participant_id=p.id, data={"what": "rotate"}
            )
        else:
            m = self.store.create_membership(room.id, p.id, name, join_code_hash(nonce))
            self.store.add_event(
                "join",
                room_id=room.id,
                membership_id=m.id,
                participant_id=p.id,
                data={
                    "harness": h,
                    "tier": tier,
                    **({"resumed": resumed.id} if resumed is not None else {}),
                },
            )
            where = f"{h} on {host}" if host else h
            shown = tier
            self.svc._post(
                room,
                sender_name=name,
                sender_kind="agent",
                sender_harness=h,
                sender_membership_id=m.id,
                via="mcp",
                kind="join",
                # peers see it's the same agent, not a new one to brief from the start (#304)
                text=f"joined ({where}, {shown})" + (", back after a restart" if resumed is not None else ""),
                sender_host=host or None,
            )
        others = [(x.name, x.harness, x.host) for x in self.store.members(room.id) if x.membership_id != m.id]
        hist = [x for x in self.store.history(room.id, None, 400) if x.kind == "chat"]
        catchup = hist[-self.cfg.delivery.catchup_n :] if self.cfg.delivery.catchup_n else []
        # the catch-up window is short (issue #272); how many more there are, and the exact
        # read(before=...) that pages back to them, stay accurate (issue #300 review)
        earlier = max(0, self.store.count_messages(room.id, kind="chat") - len(catchup))
        earlier_before = None
        if earlier:
            earlier_before = catchup[0].id if catchup else self.store.last_message_id(room.id) + 1
        text = envelope.render_join(
            pins=[x.message for x in self.store.pins(room.id)],
            room=room.name,
            screen_name=m.screen_name,
            human_name=self.cfg.human_name,
            others=others,
            catchup=catchup,
            earlier=earlier,
            earlier_before=earlier_before,
            nonce=nonce,
            guidance=adapter.join_guidance(p, room.name),
            test_mode=self.state.test_mode,
            rejoined=rejoined,
            people=self.svc.room_humans(room),
            room_rules=room.rules_text,
        )
        self.store.mark_rules_seen(m.id, room.rules_version)
        # the join text's length (issue #275, DESIGN.md §12.6): every join() answer, first one
        # or a rejoin/reconnect alike, so a report can add these up as its own path
        self.store.add_event(
            "join_chars", room_id=room.id, membership_id=m.id, participant_id=p.id, data={"chars": len(text)}
        )
        self.run(self.engine.evaluate(m.id) + [Snapshot(room.id)])
        return {
            "membership_id": m.id,
            "room": room.name,
            "screen_name": m.screen_name,
            "text": text,
            "nonce": nonce,
            "tier": tier,
            "rejoined": rejoined,
        }

    def leave(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        p, m, room = self._member(conn, params)
        self.store.end_membership(m.id, "leave")
        self.store.add_event("leave", room_id=room.id, membership_id=m.id, participant_id=p.id)
        self.run(self.engine.on_membership_ended(m.id, "leave"))
        self.svc._post(
            room,
            sender_name=m.screen_name,
            sender_kind="agent",
            sender_harness=p.harness,
            sender_membership_id=m.id,
            via="mcp",
            kind="leave",
            text="left",
            sender_host=p.host or None,
        )
        return {"room": room.name, "text": f"[switchboard] you left {room.name}."}

    def who(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        _p, _m, room = self._member(conn, params)
        rows = self.svc.member_rows(room.id)
        humans = self.svc.room_humans(room)
        who = (
            f"{envelope._word(humans[0])} (your user, kind=human)"
            if len(humans) == 1
            else f"{envelope.users_phrase(humans)} (your users, kind=human)"
        )
        lines = [f"[switchboard] {room.name}: {who} and {len(rows)} agent(s)"]
        for x in rows:
            bits = [f"- {envelope._word(x.name)}", f"harness={x.harness}"]
            if x.host:
                bits.append(f"host={envelope._word(x.host)}")
            bits += [f"status={x.status}", f"tier={x.tier or '-'}"]
            if x.tier_note:
                bits.append(f"tier_note={envelope._word(x.tier_note)}")
            mode = {"bypass": "approvals_off", "prompting": "prompting"}.get(x.approval_mode, "unknown")
            bits.append(f"approvals={mode}")
            if x.held:
                bits.append("held=yes")
            if x.parked:
                bits.append("parked=yes")
            if x.away:
                bits.append(f"away={sanitize(x.away, 80)}")
            lines.append(" ".join(bits))
        return {"room": room.name, "text": "\n".join(lines), "count": len(rows)}

    def review(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        """One move on the room's review board (DESIGN.md §37). A move that's another agent's to
        answer (a raise, a concession to another owner, a fix) is posted as this agent's message
        naming them, so it wakes them as a say() would, under the room's budget and loop guard;
        any other move is a notice that wakes no one. Not rate-limited like ``say``: the board
        caps its own size."""
        p, m, room = self._member(conn, params)
        try:
            res = self.state.boards.agent(room, m.screen_name, params)
        except ProofRequired as e:
            raise ServiceError("bad_request", str(e)) from None
        except ReviewError as e:
            raise ServiceError("bad_request", str(e)) from None
        handoff = res.pop("handoff", None)
        if handoff is not None:
            self.svc._post(
                room,
                sender_name=m.screen_name,
                sender_kind="agent",
                sender_harness=p.harness,
                sender_membership_id=m.id,
                via="mcp",
                text=handoff["text"][: self.cfg.delivery.max_msg_chars],
                mentions=handoff["mentions"],
                sender_host=p.host or None,
            )
        return res

    def say(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        p, m, room = self._member(conn, params)
        text = params.get("text")
        if not isinstance(text, str):
            raise ServiceError("bad_request", "text is required")
        text = text.rstrip()
        if not clean(text).strip():
            raise ServiceError("bad_request", "empty message")
        if len(text) > self.cfg.delivery.max_msg_chars:
            raise ServiceError(
                "bad_request",
                f"message too long ({len(text)} > {self.cfg.delivery.max_msg_chars} characters)",
            )
        if "since" in params:
            try:
                since = rules.since_id(params["since"], self.store.last_message_id(room.id))
            except ValueError as e:
                raise ServiceError("bad_request", str(e)) from None
            # Look at delivery rows, including already-read ones: only chat this member could
            # see may make a composed answer stale. Refuse before rate limiting or queueing.
            newer = self.store.visible_items_since(m.id, since)
            if newer:
                overflow = len(newer) > 50
                fit = envelope.fit_batch(
                    newer[:50],
                    room=room.name,
                    recipient=m.screen_name,
                    human_name=self.cfg.human_name,
                    peer_inline=True,
                    max_chars=self.cfg.delivery.pull_max_chars,
                    item_limit=None,
                    shrink=False,
                )
                more = overflow or fit.more
                handback = envelope.render_batch(
                    fit.items,
                    room=room.name,
                    recipient=m.screen_name,
                    human_name=self.cfg.human_name,
                    token=None,
                    peer_inline=True,
                    item_limit=None,
                    limits=fit.limits,
                )
                return {
                    "ok": False,
                    "code": "stale",
                    "error": "newer messages arrived; revise your post and send it again",
                    "posted_id": None,
                    "text": handback,
                    "batch_id": None,
                    "count": len(fit.items),
                    "more": more,
                    **(
                        {
                            "next_since": fit.items[-1].message_id,
                            "note": "more newer messages: retry say with next_since to see them",
                        }
                        if more
                        else {}
                    ),
                }
        reply_to = params.get("reply_to")
        target = self.svc.reply_target(room, reply_to)
        # the rate limit (DESIGN.md §8.4): a say it refuses waits in a short queue. One it lets
        # through (a reply to a person, or to a mention of this agent) goes now, even ahead of it
        retry = self.engine.check_say(p, m, target)
        if retry is not None:
            utext, bid, count, _more, acts = self.engine.pull(p, m, "say", 50)
            self.run(acts)
            unread = {"unread_text": utext if count else None, "batch_id": bid, "count": count}
            queue = self._queued_says.setdefault(m.id, deque())
            if len(queue) >= MAX_QUEUED_SAYS:
                return {
                    "posted_id": None,
                    "reason": "rate_limited",
                    "retry_after_s": retry,
                    "note": f"{len(queue)} of your messages already wait to be posted: this one wasn't",
                    **unread,
                }
            queue.append((text, reply_to))
            wait = retry + (len(queue) - 1) * self.cfg.delivery.rate_limit_s
            if len(queue) == 1:
                self._schedule_say(m.id, retry)
            return {
                "posted_id": None,
                "queued": True,
                "post_in_s": round(wait, 1),
                "note": f"rate limited: it's posted for you in about {wait:.0f} s; don't send it again",
                **unread,
            }
        msg = self._post_say(p, m, room, text, reply_to)
        utext, bid, count, _more, acts = self.engine.pull(p, m, "say", 50, before_id=msg.id)
        self.run(acts)
        return {"posted_id": msg.id, "unread_text": utext if count else None, "batch_id": bid, "count": count}

    def _say_wait(self, p: Participant) -> float | None:
        """Seconds until ``p`` may say again by the rate limit alone (no event: a queued say's
        own refusal was its event)."""
        return rules.check_rate_limit(
            p.last_say_at, self.state.clock.now(), self.cfg.delivery.rate_limit_s, False
        )

    def _schedule_say(self, membership_id: int, delay: float) -> None:
        asyncio.get_running_loop().call_later(max(delay, 0.0), self._post_queued_say, membership_id)

    def _post_queued_say(self, membership_id: int) -> None:
        """Post the oldest queued say of this member once the rate limit allows, then schedule the
        next. Dropped, not posted, if the member left (or its room closed) meanwhile."""
        queue = self._queued_says.get(membership_id)
        if not queue:
            self._queued_says.pop(membership_id, None)
            return
        m = self.store.get_membership(membership_id)
        p = self.store.get_participant(m.participant_id) if m is not None else None
        room = self.store.room_by_id(m.room_id) if m is not None else None
        if m is None or p is None or room is None or not m.active:
            log.info("dropped %d queued say(s) of membership %d: it ended", len(queue), membership_id)
            self._queued_says.pop(membership_id, None)
            return
        wait = self._say_wait(p)
        if wait is not None:
            self._schedule_say(membership_id, wait)
            return
        text, reply_to = queue.popleft()
        target = self.store.get_message(reply_to) if reply_to is not None else None
        self._post_say(p, m, room, text, reply_to if target is not None else None)
        if queue:
            self._schedule_say(membership_id, self.cfg.delivery.rate_limit_s)
        else:
            self._queued_says.pop(membership_id, None)

    def _post_say(
        self, p: Participant, m: Membership, room: Room, text: str, reply_to: int | None
    ) -> Message:
        """Post an agent's say: what it had pending counts as answered, and the message goes out
        like any other (the delivery, the wake budget and the loop guard)."""
        now = self.state.clock.now()
        self.store.mark_handled(m.id)
        self.store.update_participant(p.id, last_say_at=now)
        mentions = rules.parse_mentions(text, self.svc.mention_names(room))
        if rules.mentions_humans(text):
            # @humans (issue #138) addresses every person, never an agent, from an agent's own
            # say() the same way it does from a person's message (service.human_say): see
            # rules.mentions_humans for why adding the literal word here is always safe.
            mentions = sorted(set(mentions) | {rules.HUMANS_MENTION})
        msg = self.svc._post(
            room,
            sender_name=m.screen_name,
            sender_kind="agent",
            sender_harness=p.harness,
            sender_membership_id=m.id,
            via="mcp",
            text=text,
            reply_to=reply_to,
            mentions=mentions,
            sender_host=p.host or None,
        )
        return msg

    def read(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        p, m, room = self._member(conn, params)
        limit = params.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int):
            limit = 20
        limit = max(1, min(limit, 50))
        before = params.get("before")
        if before is not None:
            # a history view (issue #300 review), not a delivery: no batch, nothing marked or
            # confirmed, and it never satisfies pass()'s read-first rule (§24)
            before = clamp_before(before)
            msgs = self.store.chat_history_before(room.id, before, limit)
            earlier = self.store.count_messages(room.id, before=msgs[0].id, kind="chat") if msgs else 0
            text = envelope.render_history(
                room=room.name, recipient=m.screen_name, messages=msgs, earlier=earlier
            )
            return {"text": text, "batch_id": None, "count": len(msgs), "more": earlier > 0}
        text, bid, count, more, acts = self.engine.pull(p, m, "read", limit)
        self.run(acts)
        return {"text": text, "batch_id": bid, "count": count, "more": more}

    async def wait(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        p, m, room = self._member(conn, params)
        caps = self.engine.adapter(p).caps(p)
        timeout = rules.clamp_wait(params.get("timeout_s"), caps.wait_cap_s)
        wid = params.get("wait_id")
        wid = wid if isinstance(wid, str) and WAIT_ID_RE.match(wid) else secrets.token_hex(8)
        sink, acts = self.engine.open_wait(p, m, wid, timeout, conn.id)
        runner = self.state.runner
        fut = runner.future_for(sink.id)
        self.run(acts)
        loop = asyncio.get_running_loop()
        handle = loop.call_later(timeout, lambda: self.run(self.engine.sink_timeout(sink.id)))
        try:
            res = await fut
        finally:
            handle.cancel()
            runner.drop_future(sink.id)
        return {"timeout_s": timeout, "wait_id": wid, **res}

    def unwait(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        # not a "next call": the harness cancelled the wait, it may never have seen the answer
        p, _m, _room = self._member(conn, params, next_call=False)
        wid = params.get("wait_id")
        if not isinstance(wid, str) or not WAIT_ID_RE.match(wid):
            raise ServiceError("bad_request", "wait_id is required")
        self.run(self.engine.unwait(p, wid))
        self.run(self.engine.wait_dropped(p))  # a Devin over HTTPS that never calls again is gone (#448)
        return {}

    def pass_(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        # _member runs before_call first: a hook-less agent's read() answer is confirmed by this call
        p, m, room = self._member(conn, params)
        unread = self.engine.check_pass(p, m)  # the read-first rule (DESIGN.md §24)
        if unread:
            # a normal result, like a rate-limited say: nothing handled, nothing logged as a pass
            return {
                "room": room.name,
                "passed": False,
                "reason": "read_first",
                "unread": len(unread),
                "ids": unread[:20],
                "text": envelope.render_read_first(room.name, unread),
            }
        n = self.store.mark_handled(m.id)
        note = params.get("note")
        self.store.add_event(
            "pass",
            room_id=room.id,
            membership_id=m.id,
            participant_id=p.id,
            data={"handled": n, "note_len": len(note) if isinstance(note, str) else 0},
        )
        self.run(self.engine.evaluate(m.id) + [Snapshot(room.id)])
        return {"room": room.name, "passed": True, "handled": n, "text": "[switchboard] logged, not posted."}

    def away(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        p, _m, _room = self._member(conn, params)
        msg = params.get("message")
        away = None
        if isinstance(msg, str):
            away = clean(msg).replace("\n", " ").strip()[:AWAY_MAX] or None
        self.store.update_participant(p.id, away=away)
        self.run([Snapshot(r) for r in {x.room_id for x in self.store.participant_memberships(p.id)}])
        return {"away": away}

    def status(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        p, _m, _room = self._member(conn, params)
        raw = params.get("text")
        if not isinstance(raw, str):
            raise ServiceError("bad_request", "status text must be a string")
        line = " ".join(clean(raw).split())[:160] or None
        self.store.update_participant(
            p.id, status_line=line, status_line_at=self.store.clock.now() if line else None
        )
        self.run([Snapshot(r) for r in {x.room_id for x in self.store.participant_memberships(p.id)}])
        return {"status_line": line}

    def claim(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        """Advisory outside work, per room: only its member may renew or release it (§47)."""
        _p, m, room = self._member(conn, params)
        try:
            key = claims.normalize_key(params.get("key"))
            ttl = claims.normalize_ttl(params.get("ttl"))
        except claims.ClaimError as e:
            raise ServiceError("bad_request", str(e)) from None
        decision = self.store.move_claim(room.id, m.id, key, ttl)
        if decision.action == "held":
            raise ServiceError("conflict", f"{key} is claimed by {decision.holder}")
        if decision.action == "limit":
            raise ServiceError("bad_request", f"a member can hold at most {claims.MEMBER_MAX} claims")
        self.state.hub.members_changed(room.name)
        return {
            "ok": True,
            "room": room.name,
            "key": key,
            "holder": m.screen_name,
            "released": decision.action == "released",
            "expires_at": decision.expires_at if ttl else None,
        }

    # =============================================================== hooks
    async def http_hook(
        self, conn: "HttpConn", harness: str, event: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """A hook over HTTPS (§49.4): its session is the one it names on the token's device, then the
        engine's: status, confirmations, and (only where the harness's table allows it, §7.3) context,
        or a Stop hook held until a message comes."""
        inert: dict[str, Any] = {"out": None}
        if harness not in HOOK_EVENTS or event not in HOOK_EVENTS[harness]:
            return inert
        if self.engine.adapter_for(harness).test_only and not self.state.test_mode:
            return inert  # a scripted stand-in's hook counts only in test mode (§9.6)
        sid = params.get("sid")
        try:
            p = self.http_session(conn, sid if isinstance(sid, str) and 0 < len(sid) <= 128 else None)
        except ServiceError:
            return inert  # no session to name (a Codex hook without its thread)
        if p is None:
            return inert  # a session that hasn't joined: nothing of it to report
        ev = parse_hook_event(harness, event, params)
        self._record(harness, event, params)
        self._note_model(p, params.get("model"))
        out, acts = self.engine.claim_for_hook(p, ev)
        if out is not None and out.kind == "park" and out.sink_id is not None:
            return await self._park(conn, out.sink_id, acts, ended=True)
        self.run(acts)
        if out is None:
            return inert
        return {"out": {"kind": out.kind, "text": out.text}, "batch_id": out.batch_id, "ack": out.ack}

    async def _park(
        self, conn: "HttpConn", sink_id: int, acts: list[Action], *, ended: bool = False
    ) -> dict[str, Any]:
        """A Stop hook waits here (DESIGN.md §9.4, §49.4) until the engine fills its park with a
        follow-up, the park is released, or it times out. ``ended`` adds why a park that brought
        nothing ended (``timeout``, ``paused``, ``cancelled``), for a listener that waits again
        after a round that ran out."""
        runner = self.state.runner
        fut = runner.future_for(sink_id)
        s = self.engine.sinks.get(sink_id)
        if s is not None and s.open:
            s.conn_id = conn.id  # the park ends if this hook's connection does
        self.run(acts)
        delay = max(0.0, s.deadline - self.state.clock.now()) if s is not None else 0.0
        loop = asyncio.get_running_loop()
        handle = loop.call_later(delay, lambda: self.run(self.engine.sink_timeout(sink_id)))
        try:
            res = await fut
        finally:
            handle.cancel()
            runner.drop_future(sink_id)
        text, bid, ack = res.get("text"), res.get("batch_id"), res.get("ack")
        if res.get("status") != "messages" or not isinstance(text, str) or not isinstance(bid, int):
            return {"out": None, "ended": res.get("status")} if ended else {"out": None}
        return {"out": {"kind": "continue", "text": text}, "batch_id": bid, "ack": ack}

    def hook_ack(self, conn: "HttpConn", params: dict[str, Any]) -> dict[str, Any]:
        bid = params.get("batch_id")
        ack = params.get("ack")
        if isinstance(bid, int) and not isinstance(bid, bool) and isinstance(ack, str) and len(ack) <= 64:
            self.run(self.engine.on_hook_ack(bid, ack))
        return {}

    def _note_model(self, p: Participant, model: Any) -> None:
        """A verified session's hook reported its model: one ``model`` event per
        change, so ``switchboard report`` can name the model per participant (§12.6)."""
        if not _model_ok(model) or self._models.get(p.id) == model:
            return
        self._models[p.id] = model
        self.store.add_event("model", participant_id=p.id, data={"model": model})

    def _record(self, harness: str, event: str, params: dict[str, Any]) -> None:
        """SWITCHBOARD_RECORD_PAYLOADS (test mode): keep the allowlisted fields a hook sent."""
        if self.record_dir is None:
            return
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            keep = {k: v for k, v in params.items() if k != "t"}
            with open(self.record_dir / f"{harness}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"event": event, "params": keep}) + "\n")
        except OSError:
            pass

    # ============================================================ liveness
    def end_device(self, device: str, why: str) -> int:
        """End every session of an agent device: its token was revoked, or its person removed.
        Each leaves its rooms with "left (<device> <why>)". Returns how many ended."""
        n = 0
        for p in self.store.joined_participants():
            if p.host != device:
                continue
            n += 1
            for m in self.store.end_participant(p.id, why):
                self.run(self.engine.on_membership_ended(m.id, why))
                room = self.store.room_by_id(m.room_id)
                if room is not None:
                    self.svc._post(
                        room,
                        sender_name=m.screen_name,
                        sender_kind="agent",
                        sender_harness=p.harness,
                        sender_membership_id=m.id,
                        via="system",
                        kind="leave",
                        text=f"left ({device} {why})",
                        sender_host=device,
                    )
        if n:
            log.info("device %s: %d session(s) ended (%s)", device, n, why)
        return n


# ----------------------------------------------------------------------------
def _s(v: Any, n: int = 128) -> str | None:
    return v[:n] if isinstance(v, str) and v else None


def parse_hook_event(harness: str, event: str, p: dict[str, Any]) -> HookEvent:
    """Validate a hook report's params into a HookEvent (unknown keys are ignored)."""
    toks: list[tuple[int, str]] = []
    raw = p.get("tokens")
    if isinstance(raw, list):
        for t in raw[:20]:
            if (
                isinstance(t, (list, tuple))
                and len(t) == 2
                and isinstance(t[0], int)
                and not isinstance(t[0], bool)
                and isinstance(t[1], str)
            ):
                if TOKEN_RE.fullmatch(f"yk:b{t[0]}.{t[1]}"):
                    toks.append((t[0], t[1]))
    ok = p.get("ok")
    t = p.get("t")
    mw = p.get("max_wait_s")
    return HookEvent(
        harness=harness,
        event=event,
        sid=_s(p.get("sid")),
        gen=_s(p.get("gen")),
        tool=_s(p.get("tool")),
        tool_use_id=_s(p.get("tool_use_id")),
        ok=ok if isinstance(ok, bool) else None,
        stop_hook_active=p.get("stop_hook_active") if isinstance(p.get("stop_hook_active"), bool) else None,
        source=_s(p.get("source"), 32),
        reason=_s(p.get("reason"), 64),
        permission_mode=_s(p.get("permission_mode"), 32),
        tokens=tuple(toks),
        subagent_bg=p.get("subagent_bg") is True,
        t=float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None,
        max_wait_s=float(mw) if isinstance(mw, (int, float)) and not isinstance(mw, bool) else None,
    )
