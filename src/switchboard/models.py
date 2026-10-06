"""Plain dataclasses for rows the store returns, and the delivery engine's
value types (DESIGN.md §4, §8).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

ROOM_RE = re.compile(r"^#[a-z0-9][a-z0-9_-]{0,31}$")
# A closed room keeps its row under ``#<name>~closed-<its id>`` (DESIGN.md §28.2). '~' is
# outside ROOM_RE, so the name is free again and nothing an agent, an MCP call, a web route
# or remotes.toml names can reach it. Use it with fullmatch only (a trailing '\n' must fail).
CLOSED_ROOM_RE = re.compile(r"(#[a-z0-9][a-z0-9_-]{0,31})~closed-([1-9][0-9]{0,18})")
SCREEN_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
# A remote host's name (DESIGN.md §27.5.1): the config name of a remote in remotes.toml.
# It never contains '@' or ':', so it can't be confused with a session key's parts.
# The broker's own machine is the empty host ''.
HOST_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
LOCAL_HOST = ""

SENDER_KINDS = ("human", "agent", "system")
VIAS = ("web", "cli", "mcp", "system")
MSG_KINDS = ("chat", "join", "leave", "notice")
STATUSES = ("starting", "idle", "busy", "waiting-approval", "offline")
HARNESSES = ("claude", "codex", "cursor", "devin", "test", "unknown")
# The tier note of a Codex member whose thread proof is still running (DESIGN.md §9.3): its
# join line, /who and the buddy list show this instead of "mcp-only". ASCII dots, not "…":
# text shown through envelope.clean (NFKC) would turn that into "..." in some places only.
VERIFYING = "verifying..."


def tier_label(tier: str | None, note: str | None) -> str:
    """A member's delivery tier as people read it: ``codex:daemon``, ``mcp-only (unverified
    thread)``, or just ``verifying...`` while a Codex thread proof runs."""
    if note == VERIFYING:
        return VERIFYING
    return (tier or "-") + (f" ({note})" if note else "")


class InvalidName(ValueError):
    pass


def normalize_room(name: str) -> str:
    """'#Build' / 'build' -> '#build'; raises InvalidName if it can't be valid."""
    if not isinstance(name, str):
        raise InvalidName("room name must be a string")
    n = name.strip().lower()
    if not n.startswith("#"):
        n = "#" + n
    if not ROOM_RE.match(n):
        raise InvalidName("room names look like #build: a-z, 0-9, '_' or '-', at most 32 characters")
    return n


def room_slug(name: str) -> str:
    return name[1:] if name.startswith("#") else name


def closed_room_name(name: str, room_id: int) -> str:
    """The name a closed room keeps (DESIGN.md §28.2): ``'#build', 7 -> '#build~closed-7'``.
    Its own id makes it unique, so ``UNIQUE(name)`` never blocks a close."""
    out = f"{name}~closed-{room_id}"
    if split_closed(out) != (name, room_id):
        raise ValueError(f"not an open room name and id: {name!r}, {room_id!r}")
    return out


def split_closed(name: str) -> tuple[str, int] | None:
    """``(base, id)`` for a closed room's name, None for any other string."""
    m = CLOSED_ROOM_RE.fullmatch(name) if isinstance(name, str) else None
    return (m.group(1), int(m.group(2))) if m else None


def display_room(name: str) -> str:
    """The name people see: a closed room's base name, an open name unchanged."""
    s = split_closed(name)
    return s[0] if s else name


def room_ref(ref: str) -> str:
    """A room the human typed (``report --room``, ``rooms delete``; DESIGN.md §28.5): a closed
    room's full name as is (lower-cased), else ``normalize_room`` (which raises InvalidName)."""
    n = ref.strip().lower() if isinstance(ref, str) else ref
    if split_closed(n) is not None:
        return n
    return normalize_room(ref)


def valid_host(host: str) -> bool:
    """A remote host name (``HOST_RE``); the local host '' is not one."""
    return isinstance(host, str) and HOST_RE.fullmatch(host) is not None


def session_key(harness: str, host: str, rest: str) -> str:
    """A participant's session key (DESIGN.md §27.5.4): ``<harness>:<rest>`` on this
    machine (the keys every earlier version wrote), ``<harness>@<host>:<rest>`` for a
    remote host. ``UNIQUE(harness, session_key)`` then keeps hosts apart."""
    if host == LOCAL_HOST:
        return f"{harness}:{rest}"
    if not valid_host(host):
        raise ValueError(f"not a host name: {host!r}")
    return f"{harness}@{host}:{rest}"


def split_session_key(key: str) -> tuple[str, str, str] | None:
    """The inverse of ``session_key``: ``(harness, host, rest)``, or None for a key in
    neither form. Neither a harness nor a host name contains ':', so the first ':'
    ends the head (a Codex thread id may contain ':' itself)."""
    head, sep, rest = key.partition(":")
    if not sep or not head:
        return None
    harness, at, host = head.partition("@")
    if not harness or (at and not valid_host(host)):
        return None
    return harness, host, rest


@dataclass(frozen=True)
class Room:
    id: int
    name: str
    created_at: float
    created_by: str
    paused: bool
    paused_reason: str | None
    budget_per_hour: int
    budget_remaining: int
    budget_window_start: float
    budget_notice_window: float | None
    hop_count: int
    hop_limit: int
    last_msg_at: float | None
    rules_text: str = ""
    rules_version: int = 1

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Room":
        return cls(
            id=r["id"],
            name=r["name"],
            created_at=r["created_at"],
            created_by=r["created_by"],
            paused=bool(r["paused"]),
            paused_reason=r["paused_reason"],
            budget_per_hour=r["budget_per_hour"],
            budget_remaining=r["budget_remaining"],
            budget_window_start=r["budget_window_start"],
            budget_notice_window=r["budget_notice_window"],
            hop_count=r["hop_count"],
            hop_limit=r["hop_limit"],
            last_msg_at=r["last_msg_at"],
            rules_text=r["rules_text"] if "rules_text" in r.keys() else "",  # read-only old-schema reports
            rules_version=r["rules_version"] if "rules_version" in r.keys() else 1,
        )

    @property
    def slug(self) -> str:
        return room_slug(self.name)

    @property
    def closed(self) -> bool:
        """Closed by ``/close`` (DESIGN.md §28): its name is ``#<name>~closed-<id>``."""
        return split_closed(self.name) is not None

    @property
    def display_name(self) -> str:
        return display_room(self.name)

    @property
    def budget_reset_at(self) -> float:
        return self.budget_window_start + 3600.0


@dataclass(frozen=True)
class Message:
    id: int
    room_id: int
    ts: float
    sender_membership_id: int | None
    sender_name: str
    sender_harness: str | None
    sender_kind: str
    via: str
    kind: str
    text: str
    reply_to: int | None
    mentions: list[str] = field(default_factory=list)
    sender_host: str | None = None  # the remote host of an agent sender (schema v2); None on this machine
    sender_person_id: int | None = None  # a human sender other than the owner (schema v4, §32.2)

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Message":
        try:
            mentions = json.loads(r["mentions"] or "[]")
        except ValueError:
            mentions = []
        return cls(
            id=r["id"],
            room_id=r["room_id"],
            ts=r["ts"],
            sender_membership_id=r["sender_membership_id"],
            sender_name=r["sender_name"],
            sender_harness=r["sender_harness"],
            sender_kind=r["sender_kind"],
            via=r["via"],
            kind=r["kind"],
            text=r["text"],
            reply_to=r["reply_to"],
            mentions=list(mentions),
            sender_host=r["sender_host"] if "sender_host" in r.keys() else None,
            sender_person_id=r["sender_person_id"] if "sender_person_id" in r.keys() else None,
        )


@dataclass(frozen=True)
class Member:
    """An active membership joined with its participant: the buddy-list row."""

    membership_id: int
    participant_id: int
    room_id: int
    name: str
    harness: str
    status: str
    tier: str | None
    tier_note: str | None
    away: str | None
    approval_mode: str
    env_leak: bool
    held: bool
    joined_at: float
    queued: int = 0
    inflight: int = 0
    parked: bool = False
    parked_reason: str | None = None
    host: str = ""  # the participant's host: '' for this machine (DESIGN.md §27.6)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Event:
    id: int
    ts: float
    room_id: int | None
    membership_id: int | None
    participant_id: int | None
    kind: str
    data: dict[str, Any]

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Event":
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            data = {}
        return cls(
            id=r["id"],
            ts=r["ts"],
            room_id=r["room_id"],
            membership_id=r["membership_id"],
            participant_id=r["participant_id"],
            kind=r["kind"],
            data=data,
        )


@dataclass(frozen=True)
class RemoteRow:
    """A ``remotes`` row (DESIGN.md §27.6): the owner's consent for one remote's
    exact config, and its last block and link-up times."""

    name: str
    config_hash: str
    enabled_at: float | None
    enabled_via: str | None
    blocked_at: float | None
    blocked_reason: str | None
    last_up_at: float | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "RemoteRow":
        return cls(**{k: r[k] for k in r.keys()})

    def enabled_for(self, config_hash: str) -> bool:
        """The owner's consent is for exactly this config (any edit means "needs
        enable"). Consent only: a blocked remote is still enabled. Whether to dial
        is ``may_dial``."""
        return self.enabled_at is not None and self.config_hash == config_hash

    @property
    def blocked(self) -> bool:
        return self.blocked_at is not None

    def may_dial(self, config_hash: str) -> bool:
        """The one dial gate (DESIGN.md §27.4.7, §27.5.8): enabled for exactly this
        config and not blocked. A block (a host-key mismatch, an auth failure, a
        replaced key) holds until the next enable clears it: no automatic retry."""
        return self.enabled_for(config_hash) and not self.blocked


@dataclass(frozen=True)
class PasskeyRow:
    """A ``passkeys`` row (DESIGN.md §31.2): one of the owner's passkeys, as WebAuthn
    registered it: the credential id, its COSE public key, the signature counter last seen,
    the name the owner gave it and the authenticator's AAGUID."""

    credential_id: bytes
    public_key: bytes
    sign_count: int
    name: str
    aaguid: str | None
    created_at: float
    last_used_at: float | None
    person_id: int | None = None  # whose (schema 4, §32.2): None is the owner's

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "PasskeyRow":
        return cls(**{k: r[k] for k in r.keys()})


@dataclass(frozen=True)
class PersonRow:
    """A ``people`` row (DESIGN.md §32.2): someone other than the owner, added by the owner in
    the admin section. They sign in with their name and a password (the first one is a
    one-time password the owner hands them, ``must_reset`` until they choose their own) or a
    passkey of their own (``handle`` is their WebAuthn user id). A removed person keeps the
    row, so their messages keep their name, and loses their password, passkeys and sessions."""

    id: int
    name: str
    handle: bytes
    password_hash: str | None
    must_reset: bool
    password_expires_at: float | None  # a one-time password's end; None once they chose their own
    created_at: float
    removed_at: float | None
    email: str | None = None  # who they are: they sign in with it, Google too (#192, §39)
    first_name: str | None = None  # shown in People, Members and the invite (#192, §39.6)
    last_name: str | None = None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "PersonRow":
        d = {k: r[k] for k in r.keys()}
        d["handle"] = bytes(d["handle"] or b"")
        d["must_reset"] = bool(d["must_reset"])
        return cls(**d)

    @property
    def active(self) -> bool:
        return self.removed_at is None


@dataclass(frozen=True)
class MachineRow:
    """A ``link_machines`` row (DESIGN.md §31.7): a machine that dials in. Pending from its
    pairing until the owner approves it; approved until Remove, which forgets its key."""

    name: str
    key: bytes
    key_fp: str
    facts: dict[str, Any]
    rooms: list[str]
    harnesses: list[str]
    created_at: float
    approved_at: float | None
    approved_via: str | None
    removed_at: float | None
    last_seen_at: float | None
    person_id: int | None = None  # who paired it (schema 4, §32.2): None is the owner
    approved_by: int | None = None  # who approved it (schema 11, #178): None is the owner

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "MachineRow":
        d = {k: r[k] for k in r.keys()}
        for k, default in (("facts", {}), ("rooms", ["*"]), ("harnesses", [])):
            try:
                v = json.loads(d[k] or "null")
            except ValueError:
                v = None
            d[k] = v if isinstance(v, type(default)) else default
        d["key"] = bytes(d["key"] or b"")
        return cls(**d)

    @property
    def removed(self) -> bool:
        return self.removed_at is not None

    @property
    def approved(self) -> bool:
        return self.approved_at is not None and self.removed_at is None

    @property
    def pending(self) -> bool:
        return self.approved_at is None and self.removed_at is None


# --------------------------------------------------------------------- agents
RESERVED_NAMES = frozenset({"system", "user", "human", "admin", "root"})
PULL_PATHS = frozenset({"wait", "read", "say"})
# A turn continuation a hook returns at Stop (DESIGN.md §9.4, §9.5): a Cursor
# follow-up message, or a Devin Stop block. Two-phase: the hook's ack, then
# the next hook of that session.
CONTINUE_PATHS = frozenset({"stop_followup", "stop_block"})
HOOK_PATHS = frozenset({"hook_ctx", "hook_ups"}) | CONTINUE_PATHS
PRIO_LABEL = {2: "human", 1: "mention", 0: "chatter"}
# deliveries.reminders counts watchdog reminders; this is added once the watchdog is
# done with the item (escalated to the human, or answered with say()/pass())
WATCHDOG_DONE = 100


def _row_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


@dataclass(frozen=True)
class Participant:
    """One agent session (DESIGN.md §4 ``participants``). The human is not one."""

    id: int
    harness: str
    session_key: str
    session_id: str | None
    agent_pid: int | None
    agent_start: float | None
    mcp_pid: int | None
    mcp_start: float | None
    claude_socket: str | None
    bind_state: str
    bind_nonce: str | None
    thread_proof: bool
    status: str
    status_at: float | None
    status_src: str | None
    tier: str | None
    tier_note: str | None
    approval_mode: str
    env_leak: bool
    away: str | None
    boundary_seq: int
    gen: str | None
    gen_tainted: bool
    rearms_in_gen: int
    last_loop_count: int | None
    unconfirmed_followups: int
    push_expiries: int
    hooks_seen_at: float | None
    last_say_at: float | None
    created_at: float
    last_seen: float | None
    ended_at: float | None
    # '' for this machine; a remote's config name for a member on that host, whose
    # pids are pids on that host (DESIGN.md §27.6)
    host: str = LOCAL_HOST

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Participant":
        d = _row_dict(r)
        for k in ("thread_proof", "env_leak", "gen_tainted"):
            d[k] = bool(d[k])
        return cls(**d)

    @property
    def active(self) -> bool:
        return self.ended_at is None


@dataclass(frozen=True)
class Membership:
    id: int
    room_id: int
    participant_id: int
    screen_name: str
    cred_hash: str | None
    joined_at: float
    join_msg_id: int
    left_at: float | None
    left_reason: str | None
    kicked: bool
    held: bool
    held_at: float | None
    cursor_id: int
    peer_batch_boundary: int
    rules_seen: int = 0

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Membership":
        d = _row_dict(r)
        d["kicked"] = bool(d["kicked"])
        d["held"] = bool(d["held"])
        return cls(**d)

    @property
    def active(self) -> bool:
        return self.left_at is None


@dataclass(frozen=True)
class Item:
    """A delivery row joined with its message: what an envelope renders."""

    membership_id: int
    message_id: int
    prio: int
    mentioned: bool
    state: str
    batch_id: int | None
    attempts: int
    notified_at: float | None
    ts: float
    sender_name: str
    sender_kind: str
    sender_harness: str | None
    text: str
    reply_to: int | None
    mentions: tuple[str, ...] = ()
    redelivered: bool = False  # re-delivered once after a turn ended unanswered (§8.5)
    # the watchdog's state for an unanswered @mention (§8.5): the number of
    # reminders sent, plus WATCHDOG_DONE once it is finished (escalated or answered)
    reminders: int = 0
    in_context_at: float | None = None
    sender_host: str | None = None  # a remote agent sender's host (DESIGN.md §27.11)

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Item":
        try:
            mentions = tuple(json.loads(r["mentions"] or "[]"))
        except (ValueError, IndexError, KeyError):
            mentions = ()
        opt: dict[str, Any] = {}
        for k in ("redelivered", "reminders", "in_context_at", "sender_host"):
            try:
                opt[k] = r[k]
            except (IndexError, KeyError):
                pass
        return cls(
            membership_id=r["membership_id"],
            message_id=r["message_id"],
            prio=r["prio"],
            mentioned=bool(r["mentioned"]),
            state=r["state"],
            batch_id=r["batch_id"],
            attempts=r["attempts"],
            notified_at=r["notified_at"],
            ts=r["ts"],
            sender_name=r["sender_name"],
            sender_kind=r["sender_kind"],
            sender_harness=r["sender_harness"],
            text=r["text"],
            reply_to=r["reply_to"],
            mentions=mentions,
            redelivered=bool(opt.get("redelivered") or 0),
            reminders=int(opt.get("reminders") or 0),
            in_context_at=opt.get("in_context_at"),
            sender_host=opt.get("sender_host"),
        )

    @property
    def prio_label(self) -> str:
        return PRIO_LABEL.get(self.prio, "chatter")

    @property
    def reminded(self) -> bool:
        """The watchdog brought this @mention back at least once and it is still
        unanswered (§8.5): shown as ``reminder=yes``."""
        return self.reminders % WATCHDOG_DONE > 0

    @property
    def watch_done(self) -> bool:
        return self.reminders >= WATCHDOG_DONE


@dataclass(frozen=True)
class Batch:
    id: int
    membership_id: int
    path: str
    kind: str
    wake_kind: str | None
    wake_reason: str | None
    budget_counted: bool
    state: str
    created_at: float
    posted_at: float | None
    confirmed_at: float | None
    expired_at: float | None
    expire_reason: str | None
    turn_start_at: float | None
    first_action_at: float | None
    evidence: str | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Batch":
        d = _row_dict(r)
        d["budget_counted"] = bool(d["budget_counted"])
        return cls(**d)


@dataclass(frozen=True)
class Release:
    """What ``rules.releasable`` decided may go out now (DESIGN.md §8.2)."""

    items: tuple[Item, ...]
    kind: str  # 'priority' (mid-task) | 'wake' (idle) | 'pull'
    counted: bool  # decrements the room's wake budget
    reason: str  # 'human' | 'reminder' | 'mention' | 'chatter' (report split)

    @property
    def has_chatter(self) -> bool:
        return any(i.prio == 0 for i in self.items)

    @property
    def has_human(self) -> bool:
        return any(i.prio == 2 for i in self.items)


@dataclass(frozen=True)
class Route:
    """An adapter's pure routing verdict: push(path) | sink(id, path) | pull |
    defer (nothing now, not parked: re-evaluated on the next status change or
    tick) | none(reason) (parked)."""

    kind: str
    path: str | None = None
    sink_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class HookEvent:
    """The allowlisted fields a hook relays (DESIGN.md §7.2 step 4)."""

    harness: str
    event: str
    sid: str | None = None
    gen: str | None = None
    tool: str | None = None
    tool_use_id: str | None = None
    ok: bool | None = None
    status: str | None = None
    loop_count: int | None = None
    stop_hook_active: bool | None = None
    source: str | None = None
    reason: str | None = None
    permission_mode: str | None = None
    tokens: tuple[tuple[int, str], ...] = ()
    join_nonce: str | None = None
    subagent_bg: bool = False
    t: float | None = None
    max_wait_s: float | None = None

    @property
    def ev(self) -> str:
        """Event name normalised to the Claude spelling (postToolUse -> PostToolUse)."""
        return canonical_event(self.event)


_EVENT_ALIASES = {
    "sessionstart": "SessionStart",
    "userpromptsubmit": "UserPromptSubmit",
    "beforesubmitprompt": "UserPromptSubmit",
    "pretooluse": "PreToolUse",
    "posttooluse": "PostToolUse",
    "posttoolusefailure": "PostToolUseFailure",
    "stop": "Stop",
    "interrupt": "Interrupt",
    "sessionend": "SessionEnd",
}


def canonical_event(name: str) -> str:
    return _EVENT_ALIASES.get((name or "").lower(), name or "")


@dataclass(frozen=True)
class HookOut:
    kind: str  # 'context' | 'continue' | 'park' (Cursor: the hook waits on sink_id)
    text: str
    batch_id: int | None = None
    ack: str | None = None
    sink_id: int | None = None


# ----------------------------------------------------------------- actions
@dataclass(frozen=True)
class Push:
    batch_id: int
    participant_id: int
    path: str
    text: str
    room: str = ""  # room name, for the transport's sender label
    sender: str = ""  # first item's sender (the batch is human-first ordered)
    rules_version: int = 0  # set only after the transport accepts the whole frame


@dataclass(frozen=True)
class ResolveSink:
    sink_id: int
    result: dict[str, Any]


@dataclass(frozen=True)
class Notice:
    room_id: int | None
    level: str  # 'info' | 'warn'
    text: str
    persist: bool = True


@dataclass(frozen=True)
class Snapshot:
    room_id: int


Action = Push | ResolveSink | Notice | Snapshot
