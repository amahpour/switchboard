"""AgentService: the agent side of the broker (DESIGN.md §5.2, §6, §7, §8).

- ``mcp.hello`` verifies the MCP server's process ancestry (§5.3) and records
  which harness session it serves. Nothing is persisted until ``join``. What a
  harness keeps of the hello, and which connection may be its push channel
  (``mcp.attach``), are its adapter's to say (§6.3, §6.4).
- ``agent.*`` calls carry a membership credential that works only on the
  verified MCP connection it was issued to (the participant's mcp pid/start).
- ``hook.event`` resolves which joined session (if any) a hook belongs to from
  the kernel peer pid's ancestry, then lets the engine update status, confirm
  offers and, where the harness table allows it, return priority context.

All policy lives in the engine; this module validates input, resolves
identities and executes the engine's actions through the runner.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from switchboard import claims, envelope, reviews
from switchboard.adapters.base import HOOK_EVENTS, start_key
from switchboard.broker import proc
from switchboard.broker.hosts import HostViews
from switchboard.broker.peer import (
    HookCandidate,
    McpIdentity,
    McpRefused,
    resolve_hook_participant,
    verify_mcp_peer,
)
from switchboard.broker.proc import ProcInfo
from switchboard.broker.service import MAX_MESSAGE_ID, ServiceError
from switchboard.delivery import rules
from switchboard.envelope import TOKEN_RE, clean, sanitize
from switchboard.models import (
    HARNESSES,
    LOCAL_HOST,
    NAME_REUSE_S,
    RESERVED_NAMES,
    SCREEN_NAME_RE,
    VERIFYING,
    Action,
    HookEvent,
    Member,
    Membership,
    Message,
    Notice,
    Participant,
    Room,
    Snapshot,
    behind_by_minor,
    canonical_event,
    has_feature,
    join_code_hash,
    normalize_room,
    split_session_key,
)
from switchboard.remote import proto
from switchboard.reviews import ProofRequired, ReviewError

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState
    from switchboard.broker.rpc import Conn

log = logging.getLogger("switchboard.agents")

WAIT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
# the code at the end of a join's answer ("join yk:j<16 hex>"), given back to reclaim a name (#304)
JOIN_CODE_RE = re.compile(r"^(?:yk:j)?([0-9a-f]{16})$")
# mcp.posted err: a short code ("guard", "bad_text") or an exception type name
POST_ERR_RE = re.compile(r"[A-Za-z0-9_]{1,40}")
# a hook's reported model name (the hook relays only this shape too), for the report:
# an "@" may carry a version (claude-...@20250514), never a domain (nothing email-shaped)
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}(?:@[A-Za-z0-9_:+-]{1,32})?")
AWAY_MAX = 80
LIVENESS_S = 2.0


@dataclass
class McpConn:
    """What a connection proved in ``mcp.hello`` (kept in memory only)."""

    ident: McpIdentity
    client_info: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    env_leak: bool = False
    has_messaging_token: bool = False
    session_id: str | None = None
    test_session: str | None = None
    ack_mode: str | None = None  # the test agent's --ack, from its adapter's ``hello_claims``
    # the channels (an adapter's ``channel``) this connection is: Claude's push inbox, or a
    # remote Codex's wake channel (issue #63)
    attached: set[str] = field(default_factory=set)
    # what the MCP server says it is and offers (#249): claims, shown to a person, granting nothing
    version: str | None = None
    tools: tuple[str, ...] | None = None

    @property
    def harness(self) -> str:
        return self.ident.harness

    @property
    def inbox_attached(self) -> bool:
        """Claude: this connection is the session's push channel."""
        return "inbox" in self.attached

    @inbox_attached.setter
    def inbox_attached(self, on: bool) -> None:
        (self.attached.add if on else self.attached.discard)("inbox")

    @property
    def codex_attached(self) -> bool:
        """Remote Codex: this connection wakes its threads."""
        return "codex" in self.attached

    @codex_attached.setter
    def codex_attached(self, on: bool) -> None:
        (self.attached.add if on else self.attached.discard)("codex")


def _mcp_key(host: str, pid: int | None, start: float | None) -> tuple[str, int, str]:
    """One MCP process: its host, pid and start time (to the hundredth, as stored)."""
    return (host, pid or 0, f"{start or 0.0:.2f}")


def _remote(conn: Any) -> bool:
    """A connection carried by a remote host's link (DESIGN.md §27.5.2); a stand-in
    without the flag is a local one."""
    return getattr(conn, "remote", False) is True


TOOL_NAME_RE = re.compile(r"^[a-z][a-z_]{0,31}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}$")
MAX_TOOLS = 64


def tools_claim(value: Any) -> tuple[str, ...] | None:
    """The tools an MCP server says it offers (``mcp.hello``'s ``tools``, #249). A claim, used
    only to tell a person which members can't use the review board; the method table still
    decides what a member may call. Not a list: None, an unknown, which counts as unable. A name
    that isn't one is dropped; at most ``MAX_TOOLS`` are read."""
    if not isinstance(value, list):
        return None
    return tuple(sorted({v for v in value[:MAX_TOOLS] if isinstance(v, str) and TOOL_NAME_RE.fullmatch(v)}))


def version_claim(value: Any) -> str | None:
    """The switchboard version an MCP server says it runs, or None: only to name it to a person."""
    return value if isinstance(value, str) and VERSION_RE.fullmatch(value) else None


def name_reserved_text(name: str, room: str, code: str | None, claim: str | None) -> str:
    """Why a join was refused a reserved name, and what to do. The way back (``resume=``) is named
    only to a session whose MCP server has it (``claim``, its claimed version, is 0.25.0 or later):
    an older one keeps the ``join`` schema it started with and refuses the call, so it is told to
    update, and to pick another name meanwhile (#340). A caller that gave ``code`` has the
    parameter already."""
    text = f"{name} was used by another agent in {room} today; pick another"
    if code is not None:
        return text + " (that join code isn't this name's)"
    if has_feature(claim, "join.resume"):
        return (
            text + ", or, if it was you before a restart, join again with resume= set to"
            " the code at the end of that join's answer (yk:j...)"
        )
    return (
        f"{text}. If it was you before a restart, {envelope.OLDER_SWITCHBOARD} and its join can't"
        f" take a name back. {envelope.RESTART_UPDATES}"
    )


# a rate-limited say() waits its turn (§8.4); a burst past this many per member is refused
MAX_QUEUED_SAYS = 3


def cred_hash(cred: str) -> str:
    return hashlib.sha256(cred.encode()).hexdigest()


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
        # host -> agent pids of that host's joined sessions (DESIGN.md §27.5.5)
        self._agent_index: dict[str, set[int]] = {}
        self._models: dict[int, str] = {}  # participant id -> last model recorded
        # a SessionStart's model from a session that hasn't joined yet (Claude reports its
        # model only there, before any join): (host, pid, start) of each process above the hook
        self._early_models: dict[tuple[str, int, str], str] = {}
        # an MCP process's claims in its latest mcp.hello: (conn id, version, tools) (#249)
        self._mcp_claims: dict[tuple[str, int, str], tuple[int, str | None, tuple[str, ...] | None]] = {}
        rec = os.environ.get("SWITCHBOARD_RECORD_PAYLOADS")
        self.record_dir = Path(rec) if rec and state.test_mode else None
        # membership id -> its rate-limited says waiting their turn: (text, reply_to), oldest first
        self._queued_says: dict[int, deque[tuple[str, int | None]]] = {}
        self.refresh_index()

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

    @property
    def hosts(self) -> HostViews:
        """Every probe about a participant goes through its host's view (§27.5.6)."""
        return self.state.hosts

    def run(self, actions: list[Action]) -> None:
        if actions:
            self.state.runner.execute(actions)

    def refresh_index(self) -> None:
        """Agent pids of joined sessions, per host: hooks from anywhere else are inert at once.
        A local hook only ever meets the local set (a remote row's pid is a pid on its host)."""
        idx: dict[str, set[int]] = {}
        for p in self.store.joined_participants():
            if p.agent_pid:
                idx.setdefault(p.host, set()).add(p.agent_pid)
        self._agent_index = idx
        remotes = getattr(self.state, "remotes", None)
        if remotes is not None:
            remotes.refresh_watch()  # each link's watch follows its host's joined set (§27.4.4)

    # -------------------------------------------------- RoomService hooks
    def on_message(self, msg: Message) -> None:
        self.run(self.engine.on_message(msg.id))

    def on_command(self, room: Room, name: str, membership_id: int | None) -> None:
        self.run(self.engine.on_command(room.id, name, membership_id))

    def on_membership_ended(self, membership_id: int, reason: str) -> None:
        self.run(self.engine.on_membership_ended(membership_id, reason))
        self.refresh_index()

    def on_memberships_ended(self, membership_ids: list[int], reason: str) -> None:
        """Several at once (``/close``, §28.3): the engine per membership, the index once."""
        for mid in membership_ids:
            self.run(self.engine.on_membership_ended(mid, reason))
        self.refresh_index()

    def parked_reason(self, membership_id: int) -> str | None:
        return self.engine.parked_reason(membership_id)

    def board_reason(self, member: Member) -> str | None:
        """Why ``member`` can't use the review board, from its MCP server's claims (#249). None
        when it can, or when its MCP process isn't connected now: an offline member shows as
        offline already, and its claims went with its connection."""
        p = self.store.get_participant(member.participant_id)
        if p is None or p.mcp_pid is None or p.mcp_start is None:
            return None
        claim = self._mcp_claims.get(_mcp_key(p.host, p.mcp_pid, p.mcp_start))
        if claim is None:
            return None
        return reviews.board_reason(member.name, claim[1], claim[2])

    def member_claims(self, member: Member) -> tuple[str | None, tuple[str, ...] | None]:
        """The current MCP process's bounded claims, or unknown after it disconnects."""
        p = self.store.get_participant(member.participant_id)
        if p is None or p.mcp_pid is None or p.mcp_start is None:
            return None, None
        claim = self._mcp_claims.get(_mcp_key(p.host, p.mcp_pid, p.mcp_start))
        return (claim[1], claim[2]) if claim is not None else (None, None)

    # ================================================================= mcp
    def hello(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        claimed = params.get("harness")
        claimed = claimed if isinstance(claimed, str) and claimed in HARNESSES else "unknown"
        sock = params.get("claude_socket")
        sock = sock if isinstance(sock, str) and len(sock) < 1024 else None
        if _remote(conn):
            ident = self._remote_ident(conn, claimed)
        else:
            try:
                ident = verify_mcp_peer(
                    conn.peer,
                    claimed,
                    claude_socket=sock,
                    sessions_dir=self.cfg.claude.sessions_dir,
                    test_mode=self.state.test_mode,
                )
            except McpRefused as e:
                raise ServiceError(e.code, e.message) from None
        adapter = self.engine.adapter_for(ident.harness, ident.host)
        try:
            claims = adapter.hello_claims(params)
        except ValueError as e:
            raise ServiceError("bad_request", str(e)) from None
        ci = params.get("client_info")
        ci = (
            {k: str(v)[:80] for k, v in ci.items() if k in ("name", "version")}
            if isinstance(ci, dict)
            else {}
        )
        ev = params.get("evidence")
        ev = {k: str(v)[:120] for k, v in ev.items()} if isinstance(ev, dict) else {}
        sid = params.get("session_id")
        mc = McpConn(
            ident=ident,
            client_info=ci,
            evidence=ev,
            env_leak=claims.env_leak,
            has_messaging_token=bool(params.get("has_messaging_token")),
            session_id=sid[:128] if isinstance(sid, str) else None,
            test_session=claims.test_session,
            ack_mode=claims.ack_mode,
            version=version_claim(params.get("version")),
            tools=tools_claim(params.get("tools")),
        )
        conn.mcp = mc
        # the claims are about the MCP process's code: its members are told apart by it (#249)
        self._mcp_claims[_mcp_key(ident.host, ident.mcp_pid, ident.mcp_start)] = (
            conn.id,
            mc.version,
            mc.tools,
        )
        # A reconnecting MCP server (same process) picks its sessions up again.
        mine = self.store.participants_by_mcp(ident.host, ident.mcp_pid, ident.mcp_start)
        for p in mine:
            if claims.ack_mode is not None and p.harness == ident.harness:
                self.engine.ack_modes[p.id] = claims.ack_mode
            if p.status == "offline":
                self.run(self.engine.set_status(p, "starting", "mcp:hello"))
        # every adapter hears every hello, with the sessions of that MCP process that are its own
        # harness's (Codex: a live app-server that runs switchboard's MCP servers, after a daemon
        # restart the new one, and a restarting session's own MCP process reconnecting)
        for heard in self.engine.adapters.values():
            heard.on_mcp_hello(ident, [p for p in mine if p.harness == heard.harness])
        tier, note = adapter.tier(None)
        log.info(
            "mcp hello: conn %d harness %s%s (%s)",
            conn.id,
            ident.harness,
            f" on {ident.host}" if ident.host else "",
            ident.evidence,
        )
        return {
            "conn_id": conn.id,
            "harness": ident.harness,
            "tier": tier,
            "tier_note": ident.tier_note or note,
            "test_mode": self.state.test_mode,
        }

    def _remote_ident(self, conn: "Conn", claimed: str) -> McpIdentity:
        """The identity of an MCP server on a remote host (DESIGN.md §27.5.3): what that
        host's satellite attested (its own ``verify_mcp_peer`` on its own kernel peer,
        checked strictly by the link), under this remote's rules: a harness outside its
        ``harnesses`` list is ``unknown``, and ``test`` needs test mode at both ends.
        The host is the link's config name; nothing the remote says changes it."""
        link = getattr(conn, "link", None)
        att = conn.facts.get("attest")
        if link is None or not isinstance(att, dict):
            raise ServiceError("forbidden", "can't identify the MCP server process")
        host = conn.host
        harness, note = att["harness"], att["tier_note"]
        if harness not in (claimed, "unknown"):
            raise ServiceError("forbidden", "can't identify the MCP server process")
        adapter = self.engine.adapter_for(harness, host)
        if adapter.test_only:
            if not (self.state.test_mode and link.sat_test_mode):
                raise ServiceError("forbidden", "--harness test needs test mode on both machines")
        elif harness != "unknown" and harness not in link.entry.harnesses:
            harness, note = "unknown", f"not allowed for {host}"
            adapter = self.engine.adapter_for(harness, host)
        agent = att["agent"]
        return McpIdentity(
            harness=harness,
            mcp_pid=att["mcp"][0],
            mcp_start=att["mcp"][1],
            agent_pid=agent[0] if agent else None,
            agent_start=agent[1] if agent else None,
            evidence=att["evidence"],
            tier_note=note,
            claude_socket=adapter.attested_socket(att),
            host=host,
        )

    def conn_closed(self, conn: "Conn") -> None:
        """mcp.bye or connection loss: open waits end; the session goes offline (not Codex).
        A hook's connection: a Cursor stop park on it ends (the hook is gone)."""
        mc = getattr(conn, "mcp", None)
        if mc is None:
            if self.engine is not None and self.engine.sinks.for_conn(conn.id):
                self.run(self.engine.close_conn_sinks(conn.id))
            return
        conn.mcp = None
        key = _mcp_key(mc.ident.host, mc.ident.mcp_pid, mc.ident.mcp_start)
        if self._mcp_claims.get(key, (None,))[0] == conn.id:
            del self._mcp_claims[key]  # a newer connection of the same process keeps its own
        for channel in sorted(mc.attached):
            self._detach(conn, mc, channel)
        acts: list[Action] = self.engine.close_conn_sinks(conn.id)
        for p in self.store.participants_by_mcp(mc.ident.host, mc.ident.mcp_pid, mc.ident.mcp_start):
            if self.engine.adapter(p).keep_on_mcp_disconnect(p):
                continue
            acts += self.engine.expire_pull_batches(p.id, "disconnect")
            acts += self.engine.set_status(p, "offline", "mcp:bye")
        self.run(acts)

    # ===================================================== push channels
    def _channel_named(self, channel: str) -> Any:
        """The adapter whose ``channel`` this is, or None."""
        for adapter in self.engine.adapters.values():
            if adapter.channel == channel:
                return adapter
        return None

    def _channel_asked(self, params: dict[str, Any]) -> Any:
        """The adapter an ``mcp.attach`` asks to be the channel of: the one whose ``channel_flag``
        the request sets (``codex: true``, issue #63), else the one with no flag, which a bare
        request gets (Claude's inbox, from before there was another)."""
        bare = None
        for adapter in self.engine.adapters.values():
            if not adapter.channel:
                continue
            if not adapter.channel_flag:
                bare = adapter
            elif params.get(adapter.channel_flag) is True:
                return adapter
        return bare

    def attach(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        """``mcp.attach``: make this verified MCP connection its session's push or wake channel
        (DESIGN.md §6.4, §9.2). Each channel's adapter says whether this connection may be it
        (``channel_refusal``: for Claude's inbox a direct child of a claude whose registry names the
        same inbox socket, with the session token and a passing guard; for a remote Codex's wake
        channel, a verified Codex over a link whose guard passed); everything else stays on its
        hooks (``claude:hook``).

        On a remote host (§27.5.6, §27.7) it is the same: the parent, registry and socket check
        is the one its satellite ran there (``facts.attest``), and the channel is keyed by
        ``(host, mcp pid)``. Frames to it carry ``chk`` for the satellite's last-mile check
        (``ClaudeAdapter.send``)."""
        mc = self._mcp(conn)
        adapter = self._channel_asked(params)
        if adapter is None:
            return {"attached": False, "reason": "no adapter for that channel"}
        why = adapter.channel_refusal(
            mc.ident,
            has_token=mc.has_messaging_token,
            guard_ok=params.get("guard_ok") is True,
            remote=_remote(conn),
        )
        if why is not None:
            return {"attached": False, "reason": why}
        if adapter.channel in mc.attached:  # a repeated hello on the same connection
            return {"attached": True, "tier": adapter.channel_tier}
        adapter.attach(mc.ident.mcp_pid, mc.ident.mcp_start, conn, host=mc.ident.host)
        mc.attached.add(adapter.channel)
        self.store.add_event("tier", data={"what": "attach", "harness": adapter.harness})
        self._refresh_tiers(mc)
        return {"attached": True, "tier": adapter.channel_tier}

    def _detach(self, conn: "Conn", mc: McpConn, channel: str) -> None:
        adapter = self._channel_named(channel)
        if adapter is not None:
            adapter.detach(conn)
        mc.attached.discard(channel)
        self._refresh_tiers(mc)

    def _refresh_tiers(self, mc: McpConn) -> None:
        """Re-derive the tier of every session this MCP process serves."""
        acts: list[Action] = []
        for p in self.store.participants_by_mcp(mc.ident.host, mc.ident.mcp_pid, mc.ident.mcp_start):
            adapter = self.engine.adapter(p)
            tier, note = adapter.tier(p)
            note = mc.ident.tier_note or note
            if (tier, note) != (p.tier, p.tier_note):
                self.store.update_participant(p.id, tier=tier, tier_note=note)
                self.store.add_event("tier", participant_id=p.id, data={"tier": tier})
                acts += self.engine.evaluate_participant(p.id)
                acts += [Snapshot(m.room_id) for m in self.store.participant_memberships(p.id)]
        self.run(acts)

    def posted(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        """``mcp.posted {batch_id, ok, t_post, err?}`` from the channel the frame went to.
        Over a link it may come from the satellite itself, which marks its own report
        ``facts.lastmile`` (a client can't set facts): its last-mile check dropped the frame,
        ``err: "stale_status"`` (the adapter re-routes it, uncounted) or a counted
        ``no_chk``/``bad_chk`` (§27.5.6)."""
        mc = self._mcp(conn)
        bid = params.get("batch_id")
        if not isinstance(bid, int) or isinstance(bid, bool) or not mc.attached:
            return {}
        # the report of a frame handed to this connection: a Claude inbox post, or a remote
        # Codex server's wake (issue #63), by the channel it is
        adapter = self._channel_named(min(mc.attached))
        if adapter is None:
            return {}
        t = params.get("t_post")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t):
            t_post = float(t)
        else:
            t_post = None
        err = params.get("err")
        if err is not None:
            # it reaches the broker log: codes only, never free text
            err = err if isinstance(err, str) and POST_ERR_RE.fullmatch(err) else "post_failed"
        res: dict[str, Any] = {
            "ok": params.get("ok") is True,
            "t_post": t_post,
            "err": err,
        }
        if _remote(conn) and conn.facts.get("lastmile") is True:
            res["lastmile"] = True  # the satellite's own report (§27.5.6)
        adapter.posted(bid, conn, res)
        return {}

    # ======================================================== membership
    def _session_key(self, mc: McpConn, thread_id: str | None) -> str:
        h, host = mc.harness, mc.ident.host
        # Each harness's rule: Codex keys by thread, Cursor starts pending, the test
        # agent by its --test-session, and claude, devin and unknown by process (§6.3).
        try:
            return self.engine.adapter_for(h, host).session_key(
                mc.ident, thread_id, test_session=mc.test_session
            )
        except ValueError as e:
            raise ServiceError("bad_request", str(e)) from e

    @staticmethod
    def _same_mcp(p: Participant, ident: McpIdentity) -> bool:
        """``(host, mcp_pid, mcp_start)`` equal: the very MCP process that holds ``p``."""
        return (
            p.host == ident.host
            and p.mcp_pid == ident.mcp_pid
            and proc.same_start(p.mcp_start, ident.mcp_start)
        )

    def _reclaim(self, holder: Membership, code: str | None, harness: str, host: str) -> Membership | None:
        """The membership whose reserved name a join may take back (#304, DESIGN.md §6.1), or None.

        Only with the code that membership's own join answer ended with, which only the session
        that read that answer has, and only for the same harness on the same machine (so the
        same person): never a name another session or machine used. Not from a kicked agent,
        and not while the old membership is still in the room."""
        if code is None or holder.join_code_hash is None or holder.active or holder.kicked:
            return None
        if not hmac.compare_digest(holder.join_code_hash, join_code_hash(code)):
            return None
        old = self.store.get_participant(holder.participant_id)
        if old is None or old.harness != harness or (old.host or "") != (host or ""):
            return None
        if self.store.was_kicked(holder.room_id, old.id):
            return None
        return holder

    def _check_same_session(self, existing: Participant, mc: McpConn) -> None:
        """A re-join may take over a session's memberships (rotating the
        credential) only from the MCP process that holds it (a broker
        reconnect) or once that process is gone (an MCP restart), and never
        from under another live agent process. Otherwise a process that merely
        knows a Codex thread id could post as that thread's agent.

        Both probes go to the session's own host (§27.5.6); a host that can't
        tell (``None``) counts as alive: no takeover until it can."""
        if self._same_mcp(existing, mc.ident):
            return
        view = self.hosts.view(existing.host)
        if existing.mcp_pid:
            a = view.alive(existing.mcp_pid, existing.mcp_start)
            if a is None:
                raise ServiceError(
                    "conflict",
                    f"can't verify this session on {existing.host or 'this machine'}"
                    " yet; try again in a few seconds",
                )
            if a:
                raise ServiceError(
                    "conflict",
                    "this session is already joined from another live switchboard MCP server; ask your user",
                )
        same_agent = (
            existing.host == mc.ident.host
            and existing.agent_pid == mc.ident.agent_pid
            and proc.same_start(existing.agent_start, mc.ident.agent_start)
        )
        if not same_agent and existing.agent_pid:
            a = view.alive(existing.agent_pid, existing.agent_start)
            if a is None:
                raise ServiceError(
                    "conflict",
                    f"can't verify this session on {existing.host or 'this machine'}"
                    " yet; try again in a few seconds",
                )
            if a:
                raise ServiceError(
                    "conflict", "this session belongs to another live agent process; ask your user"
                )

    def _mcp(self, conn: "Conn") -> McpConn:
        mc = getattr(conn, "mcp", None)
        if mc is None:
            raise ServiceError("unauthorized", "send mcp.hello first")
        return mc

    def _member(
        self, conn: "Conn", params: dict[str, Any], *, next_call: bool = True
    ) -> tuple[Participant, Membership, Room]:
        mc = self._mcp(conn)
        cred = params.get("cred")
        if not isinstance(cred, str) or not cred or len(cred) > 200:
            raise ServiceError("unauthorized", "missing membership credential: join() first")
        h = cred_hash(cred)
        m = self.store.membership_by_cred(h)
        if m is None:
            closed = self.store.closed_membership_by_cred(h)
            if closed is not None:
                # still `unauthorized`: the MCP server then forgets the credential (§28.3)
                raise ServiceError(
                    "unauthorized",
                    f"{closed[1].display_name} was closed by {self.cfg.human_name}; you are no longer in it",
                )
            raise ServiceError(
                "unauthorized", "not a member (your membership was revoked or rotated): join() again"
            )
        p = self.store.get_participant(m.participant_id)
        if p is None or not p.active:
            raise ServiceError("unauthorized", "this session has ended: join() again")
        if not self._same_mcp(p, mc.ident):
            # (host, mcp_pid, mcp_start): a credential works only from the MCP process that joined,
            # on its own host (§27.5.4)
            raise ServiceError("unauthorized", "this credential belongs to another process")
        credential_error = self.engine.adapter(p).credential_error(p, params)
        if credential_error is not None:
            raise ServiceError("unauthorized", credential_error)
        room = self.store.room_by_id(m.room_id)
        assert room is not None
        if _remote(conn):
            # the allowlists hold for every call, not only at join: a membership its remote no
            # longer allows (the manager ends those at reload) never works over the link (§27.5.2)
            link = getattr(conn, "link", None)
            names = (room.name, *self.store.room_aliases(room.id))
            if link is None or not any(link.entry.allows_room(name) for name in names):
                raise ServiceError(
                    "forbidden",
                    f"members on {p.host} may no longer use {room.name}"
                    " (the rooms of its remotes.toml entry on the desktop)",
                )
            if p.harness != mc.ident.harness:
                raise ServiceError("unauthorized", "this credential belongs to another process")
        if next_call:
            self.run(self.engine.before_call(p))
            p = self.store.get_participant(p.id) or p
        return p, m, room

    def join(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        mc = self._mcp(conn)
        raw_room = params.get("room")
        if not isinstance(raw_room, str) or not raw_room.strip():
            raise ServiceError("bad_request", "room is required, e.g. #build")
        host = mc.ident.host
        link = getattr(conn, "link", None) if _remote(conn) else None
        if _remote(conn):
            # a remote host's members may join only its allowlisted rooms (remotes.toml, §27.5.2)
            try:
                wanted = normalize_room(raw_room)
            except ValueError:
                wanted = ""
            if link is None or not link.entry.allows_room(wanted):
                allowed = ", ".join(link.entry.rooms) if link is not None else "none"
                raise ServiceError(
                    "forbidden",
                    f"members on {host} may join only {allowed}"
                    " (the rooms of its remotes.toml entry on the desktop)",
                )
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
            raise ServiceError(
                "bad_request",
                "screen names look like claude-1: a-z first, then a-z, 0-9, '_' or '-', at most 24",
            )
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
        adapter = self.engine.adapter_for(h, host)
        conflict = adapter.join_conflict(self.store, tid, host)
        if conflict is not None:
            raise ServiceError("conflict", conflict)
        existing = self.store.find_participant(h, key)
        mine = adapter.existing_session(self.store, mc.ident)
        if mine is not None:
            # Cursor: one session per agent process until its key is the conversation id (§6.3)
            existing, key = mine, mine.session_key
        if existing is not None and (
            self.store.was_kicked(room.id, existing.id)
            or adapter.conversation_kicked(self.store, room.id, existing)
        ):
            raise ServiceError("kicked", f"you were kicked from {room.name}; ask your user")
        if existing is not None and existing.active:
            self._check_same_session(existing, mc)
        now = self.state.clock.now()
        tier, note = adapter.conn_tier(mc.ident, existing)
        fields: dict[str, Any] = dict(
            agent_pid=mc.ident.agent_pid,
            agent_start=mc.ident.agent_start,
            mcp_pid=mc.ident.mcp_pid,
            mcp_start=mc.ident.mcp_start,
            claude_socket=mc.ident.claude_socket,
            tier=tier,
            tier_note=mc.ident.tier_note or note,
            env_leak=int(mc.env_leak),
            session_id=(tid or mc.session_id),
            host=mc.ident.host,
        )
        if h == "test" or (existing is not None and existing.hooks_seen_at is not None):
            fields.update(status="busy", status_at=now, status_src="join")
        elif existing is None or existing.status == "offline":
            fields.update(status="starting", status_at=now, status_src="join")
        same_mcp = existing is not None and self._same_mcp(existing, mc.ident)
        fields.update(adapter.rebind_fields(existing, same_mcp=same_mcp))
        fields.update(adapter.join_fields(existing))
        cur = self.store.active_membership(room.id, existing.id) if existing else None
        if cur is None and link is not None:
            joined = {p.id for p in self.store.joined_participants() if p.host == host}
            if (existing is None or existing.id not in joined) and len(joined) >= link.entry.max_members:
                self.run(
                    [
                        Notice(
                            room.id,
                            "warn",
                            f"a join from {host} was refused: it already has"
                            f" {len(joined)} member(s) (max_members)",
                        )
                    ]
                )
                raise ServiceError(
                    "conflict",
                    f"{host} already has {len(joined)} member(s), its limit"
                    " (max_members in remotes.toml on the desktop)",
                )
        resumed: Membership | None = None
        if cur is None:
            other = self.store.active_membership_by_name(room.id, name)
            if other is not None and (existing is None or other.participant_id != existing.id):
                raise ServiceError("name_taken", f"{name} is taken in {room.name}; pick another screen name")
            holder = self.store.last_holder_of(
                room.id, name, existing.id if existing else -1, now - NAME_REUSE_S
            )
            if holder is not None:
                resumed = self._reclaim(holder, code, h, host)
                if resumed is None:
                    raise ServiceError("name_reserved", name_reserved_text(name, room.name, code, mc.version))
        elif cur.screen_name.lower() != name:
            raise ServiceError(
                "bad_request",
                f"this session is already in {room.name} as {cur.screen_name};"
                " leave() first to change your name",
            )
        nonce = secrets.token_hex(8)
        p = self.store.upsert_participant(h, key, bind_nonce=nonce, **fields)
        if mc.ack_mode is not None:
            self.engine.ack_modes[p.id] = mc.ack_mode
        cred = secrets.token_urlsafe(32)
        rejoined = cur is not None
        if cur is not None:
            # the same verified session again (an MCP reconnect): rotate the credential
            self.store.rotate_cred(cur.id, cred_hash(cred), join_code_hash(nonce))
            m = cur
            self.run(self.engine.expire_pull_batches(p.id, "rejoin"))
            self.store.add_event(
                "bind", room_id=room.id, membership_id=m.id, participant_id=p.id, data={"what": "rotate"}
            )
            notice = adapter.rejoin_notice(p, m.screen_name, same_mcp=same_mcp)
            if notice is not None:
                self.run([Notice(room.id, "info", notice)])
        else:
            m = self.store.create_membership(room.id, p.id, name, cred_hash(cred), join_code_hash(nonce))
            # a Codex thread proof about to run shows as "verifying...", not "mcp-only"; the
            # event says so, so the proof that passes announces it here (§9.3)
            verifying = note == VERIFYING
            self.store.add_event(
                "join",
                room_id=room.id,
                membership_id=m.id,
                participant_id=p.id,
                data={
                    "harness": h,
                    "tier": tier,
                    **({"verifying": True} if verifying else {}),
                    **({"resumed": resumed.id} if resumed is not None else {}),
                },
            )
            where = f"{h} on {host}" if host else h
            shown = VERIFYING if verifying else tier
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
            if behind_by_minor(mc.version):
                self.run(
                    [
                        Notice(
                            room.id,
                            "info",
                            f"{name} reports switchboard {mc.version}; run switchboard update"
                            " where it runs, then restart its session.",
                        )
                    ]
                )
        self.refresh_index()
        if p.agent_pid:
            early = self._early_models.get((p.host, p.agent_pid, start_key(p.agent_start)))
            if early is not None:
                self._note_model(p, early)
        adapter.on_joined(p, nonce, not same_mcp or not rejoined)
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
            can_page=has_feature(mc.version, "read.before"),
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
            "cred": cred,
            "membership_id": m.id,
            "room": room.name,
            "screen_name": m.screen_name,
            "text": text,
            "nonce": nonce,
            "tier": tier,
            "rejoined": rejoined,
        }

    def leave(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
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
        self.refresh_index()
        return {"room": room.name, "text": f"[switchboard] you left {room.name}."}

    def who(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
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
            bits.append(f"version_claim={x.version_claim or 'unknown'}")
            bits.append(f"tools_claim={','.join(x.tools_claim) if x.tools_claim is not None else 'unknown'}")
            if x.tier_note:
                bits.append(f"tier_note={envelope._word(x.tier_note)}")
            mode = {"bypass": "approvals_off", "prompting": "prompting"}.get(x.approval_mode, "unknown")
            bits.append(f"approvals={mode}")
            if x.env_leak:
                bits.append("env_shared=yes")
            if x.held:
                bits.append("held=yes")
            if x.parked:
                bits.append("parked=yes")
            if x.away:
                bits.append(f"away={sanitize(x.away, 80)}")
            lines.append(" ".join(bits))
        return {"room": room.name, "text": "\n".join(lines), "count": len(rows)}

    def review(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        """One move on the room's review board (DESIGN.md §37). A move that's another agent's to
        answer (a raise, a concession to another owner, a fix) is posted as this agent's message
        naming them, so it wakes them as a say() would, under the room's budget and loop guard;
        any other move is a notice that wakes no one. Not rate-limited like ``say``: the board
        caps its own size."""
        p, m, room = self._member(conn, params)
        try:
            res = self.state.boards.agent(room, m.screen_name, params)
        except ProofRequired as e:
            if has_feature(self._mcp(conn).version, "review.proof"):
                raise ServiceError("bad_request", str(e)) from None
            # its review tool has no proof field, so "proof is required" can't be followed (#340)
            raise ServiceError(
                "bad_request",
                "fix needs proof, a test's name or an http(s) link, but"
                f" {envelope.OLDER_SWITCHBOARD} and its review tool has no proof field."
                f" {envelope.RESTART_UPDATES}",
            ) from None
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

    def say(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
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

    def read(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
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

    async def wait(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
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

    def unwait(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        # not a "next call": the harness cancelled the wait, it may never have seen the answer
        p, _m, _room = self._member(conn, params, next_call=False)
        wid = params.get("wait_id")
        if not isinstance(wid, str) or not WAIT_ID_RE.match(wid):
            raise ServiceError("bad_request", "wait_id is required")
        self.run(self.engine.unwait(p, wid))
        return {}

    def pass_(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
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

    def away(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        p, _m, _room = self._member(conn, params)
        msg = params.get("message")
        away = None
        if isinstance(msg, str):
            away = clean(msg).replace("\n", " ").strip()[:AWAY_MAX] or None
        self.store.update_participant(p.id, away=away)
        self.run([Snapshot(r) for r in {x.room_id for x in self.store.participant_memberships(p.id)}])
        return {"away": away}

    def status(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
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

    def claim(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
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
    async def hook_event(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        inert: dict[str, Any] = {"out": None}
        harness = params.get("harness")
        if harness == "test" and not self.state.test_mode:
            return inert
        if not isinstance(harness, str) or harness not in HOOK_EVENTS:
            return inert
        raw_event = params.get("event")
        event = canonical_event(raw_event if isinstance(raw_event, str) else "")
        if event not in HOOK_EVENTS[harness]:
            return inert
        if _remote(conn):
            # a remote host's hook: its satellite walked the chain on that host (§27.5.5)
            got = self._remote_chain(conn)
            if got is None:
                return inert
            host, chain, argv_fn = got
            if harness == "test" and not getattr(getattr(conn, "link", None), "sat_test_mode", False):
                return inert
            pid: int | None = chain[0].pid
        else:
            # a local connection: its kernel peer is a process on this machine, and only this
            # machine's participants can be its candidates
            host = LOCAL_HOST
            view = self.hosts.view(host)
            pid = conn.peer.pid
            chain = None
            argv_fn = lambda procs: view.argv_many(procs) or {}  # noqa: E731
        walked: list[ProcInfo] | None = chain

        def get_chain() -> list[ProcInfo]:
            nonlocal walked
            if walked is None:
                walked = (self.hosts.view(host).ancestry(pid, 8) or []) if pid else []
            return walked

        if pid and event == "SessionStart":
            self._remember_model(host, get_chain(), params.get("model"))
        index = self._agent_index.get(host)
        if not pid or not index:
            return inert
        if not any(pi.pid in index for pi in get_chain()):
            return inert
        ev = parse_hook_event(harness, event, params)
        self._record(harness, event, params)
        cands = [
            HookCandidate(
                participant_id=p.id,
                harness=p.harness,
                agent_pid=p.agent_pid,
                agent_start=p.agent_start,
                session_id=p.session_id,
                session_key=p.session_key,
                bind_state=p.bind_state,
            )
            for p in self.store.joined_participants()
            if p.host == host
        ]
        nonce_bind = self.engine.adapter_for(harness, host).join_nonce_bind(ev)
        c = resolve_hook_participant(
            get_chain(), harness, ev.sid, cands, join_nonce_bind=nonce_bind, argv_fn=argv_fn, host=host
        )
        if c is None:
            return inert
        p = self.store.get_participant(c.participant_id)
        if p is None or not p.active:
            return inert
        if nonce_bind:
            p = self._bind_cursor(p, ev)
            if p is None:
                return inert
        self._note_model(p, params.get("model"))
        out, acts = self.engine.claim_for_hook(p, ev)
        if out is not None and out.kind == "park" and out.sink_id is not None:
            return await self._park(conn, out.sink_id, acts)
        self.run(acts)
        if out is None:
            return inert
        return {"out": {"kind": out.kind, "text": out.text}, "batch_id": out.batch_id, "ack": out.ack}

    @staticmethod
    def _remote_chain(conn: "Conn") -> tuple[str, list[ProcInfo], Any] | None:
        """``(host, chain, argv_fn)`` from a remote hook's ``facts.chain``: synthetic
        ``ProcInfo`` entries (pids on that host) and an argv reader that maps each
        verdict back to a canonical argv, so ``resolve_hook_participant`` and
        ``nearest_agent_is`` run unchanged and fail closed exactly as locally. None
        without a chain (the hook is inert)."""
        facts = conn.facts.get("chain")
        if not facts:
            return None
        chain = [
            ProcInfo(pid=pid, ppid=facts[i + 1][0] if i + 1 < len(facts) else 0, start=start, uid=-1)
            for i, (pid, start, _v) in enumerate(facts)
        ]
        verdicts = {pid: v for pid, _s, v in facts}

        def argv_fn(procs: list[ProcInfo]) -> dict[int, str]:
            return {p.pid: proto.verdict_argv(verdicts.get(p.pid, "?")) for p in procs}

        return conn.host, chain, argv_fn

    def _held_elsewhere(self, harness: str, rest: str, host: str) -> Participant | None:
        """An active session of ``harness`` with this global id (a Cursor conversation) on
        another host than ``host``, this machine included (§27.5.4)."""
        for p in self.store.active_participants():
            if p.harness != harness or p.host == host:
                continue
            parts = split_session_key(p.session_key)
            if parts is not None and parts[2] == rest:
                return p
        return None

    async def _park(self, conn: "Conn", sink_id: int, acts: list[Action]) -> dict[str, Any]:
        """A Cursor stop hook waits here (DESIGN.md §9.4) until the engine fills its
        park with a follow-up, the park is released, or it times out."""
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
            return {"out": None}
        return {"out": {"kind": "continue", "text": text}, "batch_id": bid, "ack": ack}

    def _bind_cursor(self, p: Participant, ev: HookEvent) -> Participant | None:
        """The ``postToolUse`` for ``MCP:join`` carries the join's nonce and the
        ``conversation_id`` (DESIGN.md §6.3): key the session by it. Only the
        participant whose own join issued that nonce, and whose agent is in this
        hook's ancestry (checked by the resolver), can be bound; anything else
        is inert. A bound session that joins again (e.g. after a chat switch in
        the same agent process) is re-keyed the same way. The nonce rule, the "already
        bound" check and the conversation id's key are the Cursor adapter's own
        (``nonce_ok``, ``bound_to``, ``conversation_key``); every store write and engine
        call here stays this method's."""
        adapter = self.engine.adapter_for(p.harness, p.host)
        sid = ev.sid or ""
        if not adapter.nonce_ok(p, ev):
            # not this participant's current join: only its own bound conversation may go on
            return p if adapter.bound_to(p, sid) else None
        key = adapter.conversation_key(p, sid)
        if key is None:
            return None
        if p.session_key == key and p.bind_state == "bound":
            self.store.update_participant(p.id, bind_nonce=None)  # single use
            return self.store.get_participant(p.id)
        if self._held_elsewhere(p.harness, sid, p.host) is not None:
            # a conversation id is global: no session ever moves between hosts (§27.5.4)
            self.store.add_event(
                "bind",
                participant_id=p.id,
                data={"what": "cursor", "ok": False, "why": "conversation on another host"},
            )
            self.run(
                [
                    Notice(
                        m.room_id,
                        "warn",
                        f"{m.screen_name}: can't bind to its Cursor conversation: it is joined from"
                        " another machine (it stays mcp-only)",
                    )
                    for m in self.store.participant_memberships(p.id)
                ]
            )
            return None
        other = self.store.find_participant("cursor", key)
        if other is not None and other.id != p.id:
            # the holder's own host decides; one that can't tell (None) counts as alive
            if other.active and other.agent_pid and self.hosts.agent_alive(other) is not False:
                self.store.add_event(
                    "bind",
                    participant_id=p.id,
                    data={"what": "cursor", "ok": False, "why": "conversation held"},
                )
                log.warning(
                    "cursor participant %d: conversation already bound to live participant %d", p.id, other.id
                )
                self.run(
                    [
                        Notice(
                            m.room_id,
                            "warn",
                            f"{m.screen_name}: can't bind to its Cursor conversation: another live"
                            " switchboard session holds it (it stays mcp-only)",
                        )
                        for m in self.store.participant_memberships(p.id)
                    ]
                )
                return None
            if other.active:  # its agent is gone: end it now, as the liveness check would
                self.check_liveness()
            # the key of an ended session of this conversation (e.g. before a --resume) is freed
            self.store.update_participant(other.id, session_key=f"{key}#ended-{other.id}")
        # the nonce is single use: re-keying again takes a new join() (a visible rejoin)
        p = self.store.update_participant(
            p.id, session_key=key, session_id=sid[:128], bind_state="bound", bind_nonce=None
        )
        self.store.add_event("bind", participant_id=p.id, data={"what": "cursor", "ok": True})
        self._carry_kicks(p, key)
        acts = self.engine.refresh_tier(p.id) + self.engine.evaluate_participant(p.id)
        self.run(acts)
        log.info("cursor participant %d bound to its conversation", p.id)
        return self.store.get_participant(p.id)

    def _carry_kicks(self, p: Participant, key: str) -> None:
        """A kick sticks to the conversation (as it does to a Claude session or a
        Codex thread): a new agent process that resumes a conversation that was
        kicked from a room is removed from that room again once it binds."""
        for m in self.store.participant_memberships(p.id):
            if not self.store.was_kicked_session(m.room_id, "cursor", key, exclude=p.id):
                continue
            self.store.end_membership(m.id, "kick", kicked=True)
            self.store.add_event(
                "kick",
                room_id=m.room_id,
                membership_id=m.id,
                participant_id=p.id,
                data={"why": "resumed a kicked conversation"},
            )
            self.run(self.engine.on_membership_ended(m.id, "kick"))
            room = self.store.room_by_id(m.room_id)
            if room is not None:
                self.svc._post(
                    room,
                    sender_name=m.screen_name,
                    sender_kind="agent",
                    sender_harness="cursor",
                    sender_membership_id=m.id,
                    via="system",
                    kind="leave",
                    text=f"was kicked by {self.cfg.human_name} (earlier, in this conversation)",
                    sender_host=p.host or None,
                )
        self.refresh_index()

    def hook_ack(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
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

    def _remember_model(self, host: str, chain: list[ProcInfo], model: Any) -> None:
        """Keep a SessionStart's model for the processes above the hook (``chain``, on
        ``host``), until a join names one."""
        if not _model_ok(model):
            return
        for pi in chain:
            self._early_models[(host, pi.pid, start_key(pi.start))] = model
        while len(self._early_models) > 256:  # bounded: oldest first
            self._early_models.pop(next(iter(self._early_models)))

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
    def check_liveness(self) -> None:
        """A dead agent process ends its session: leave every room (DESIGN.md §6.3).

        For Codex the agent is the app-server (the daemon, or an embedded TUI)
        that runs the thread's MCP server: when it is gone, so is the thread,
        unless it was the daemon restarting (an auto-update): the adapter keeps
        such a session for a grace window and re-binds it to the new daemon if
        the thread comes back there (DESIGN §9.3). Everything finer (a TUI
        quitting while the daemon lives) is CodexLink's."""
        for p in self.store.joined_participants():
            if not p.agent_pid:
                continue
            # asked of the participant's own host (§27.5.6); None: that host can't tell now
            # (a remote whose link is down), so the session is left alone, never ended
            if self.hosts.agent_alive(p) is not False:
                continue
            if self.engine.adapter(p).defer_end(p):
                continue
            ended = self.store.end_participant(p.id, "session_end")
            for m in ended:
                self.run(self.engine.on_membership_ended(m.id, "session_end"))
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
                        text="left (session ended)",
                        sender_host=p.host or None,
                    )
            log.info("participant %d ended (agent gone)", p.id)
        self.refresh_index()

    async def liveness_loop(self) -> None:
        while True:
            await asyncio.sleep(LIVENESS_S)
            try:
                self.check_liveness()
            except Exception:
                log.exception("liveness check failed")


# ----------------------------------------------------------------------------
def _s(v: Any, n: int = 128) -> str | None:
    return v[:n] if isinstance(v, str) and v else None


def parse_hook_event(harness: str, event: str, p: dict[str, Any]) -> HookEvent:
    """Validate a hook.event's params into a HookEvent (unknown keys are ignored)."""
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
    lc = p.get("loop_count")
    ok = p.get("ok")
    t = p.get("t")
    mw = p.get("max_wait_s")
    nonce = p.get("join_nonce")
    return HookEvent(
        harness=harness,
        event=event,
        sid=_s(p.get("sid")),
        gen=_s(p.get("gen")),
        tool=_s(p.get("tool")),
        tool_use_id=_s(p.get("tool_use_id")),
        ok=ok if isinstance(ok, bool) else None,
        status=_s(p.get("status"), 32),
        loop_count=lc if isinstance(lc, int) and not isinstance(lc, bool) else None,
        stop_hook_active=p.get("stop_hook_active") if isinstance(p.get("stop_hook_active"), bool) else None,
        source=_s(p.get("source"), 32),
        reason=_s(p.get("reason"), 64),
        permission_mode=_s(p.get("permission_mode"), 32),
        tokens=tuple(toks),
        join_nonce=nonce if isinstance(nonce, str) and re.fullmatch(r"[0-9a-f]{16}", nonce) else None,
        subagent_bg=p.get("subagent_bg") is True,
        t=float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None,
        max_wait_s=float(mw) if isinstance(mw, (int, float)) and not isinstance(mw, bool) else None,
    )
