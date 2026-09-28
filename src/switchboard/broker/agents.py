"""AgentService: the agent side of the broker (DESIGN.md §5.2, §6, §7, §8).

- ``mcp.hello`` verifies the MCP server's process ancestry (§5.3) and records
  which harness session it serves. Nothing is persisted until ``join``.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from switchboard import envelope
from switchboard.adapters.base import HOOK_EVENTS
from switchboard.adapters.testagent import ACK_MODES
from switchboard.broker import proc
from switchboard.broker.hosts import HostViews
from switchboard.broker.peer import HookCandidate, McpIdentity, McpRefused, resolve_hook_participant, verify_mcp_peer
from switchboard.broker.proc import ProcInfo
from switchboard.broker.service import ServiceError
from switchboard.delivery import rules
from switchboard.envelope import TOKEN_RE, clean, sanitize
from switchboard.models import (
    HARNESSES,
    LOCAL_HOST,
    RESERVED_NAMES,
    SCREEN_NAME_RE,
    VERIFYING,
    Action,
    HookEvent,
    Membership,
    Message,
    Notice,
    Participant,
    Room,
    Snapshot,
    canonical_event,
    normalize_room,
    session_key,
    split_session_key,
)
from switchboard.remote import proto

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState
    from switchboard.broker.rpc import Conn

log = logging.getLogger("switchboard.agents")

TEST_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
WAIT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
CURSOR_SID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
# mcp.posted err: a short code ("guard", "bad_text") or an exception type name
POST_ERR_RE = re.compile(r"[A-Za-z0-9_]{1,40}")
# a hook's reported model name (the hook relays only this shape too), for the report:
# an "@" may carry a version (claude-...@20250514), never a domain (nothing email-shaped)
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}(?:@[A-Za-z0-9_:+-]{1,32})?")
NAME_REUSE_S = 24 * 3600.0
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
    test_ack: str = "next_call"
    inbox_attached: bool = False  # Claude: this connection is the session's push channel

    @property
    def harness(self) -> str:
        return self.ident.harness


def _remote(conn: Any) -> bool:
    """A connection carried by a remote host's link (DESIGN.md §27.5.2); a stand-in
    without the flag is a local one."""
    return getattr(conn, "remote", False) is True


def cred_hash(cred: str) -> str:
    return hashlib.sha256(cred.encode()).hexdigest()


def _start_key(start: float | None) -> str:
    return f"{start:.2f}" if start is not None else "?"


def _model_ok(model: Any) -> bool:
    """A hook-reported model name worth recording: short and identifier-shaped."""
    return isinstance(model, str) and len(model) <= 64 and MODEL_RE.fullmatch(model) is not None


class AgentService:
    def __init__(self, state: "BrokerState"):
        self.state = state
        # host -> agent pids of that host's joined sessions (DESIGN.md §27.5.5)
        self._agent_index: dict[str, set[int]] = {}
        self._models: dict[int, str] = {}  # participant id -> last model recorded
        # a SessionStart's model from a session that hasn't joined yet (Claude reports its
        # model only there, before any join): (host, pid, start) of each process above the hook
        self._early_models: dict[tuple[str, int, str], str] = {}
        rec = os.environ.get("SWITCHBOARD_RECORD_PAYLOADS")
        self.record_dir = Path(rec) if rec and state.test_mode else None
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

    def parked_reason(self, membership_id: int) -> str | None:
        return self.engine.parked_reason(membership_id)

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
                    conn.peer, claimed, claude_socket=sock,
                    sessions_dir=self.cfg.claude.sessions_dir, test_mode=self.state.test_mode,
                )
            except McpRefused as e:
                raise ServiceError(e.code, e.message) from None
        test_session = None
        ack = "next_call"
        if ident.harness == "test":
            ts = params.get("test_session")
            if not isinstance(ts, str) or not TEST_SESSION_RE.match(ts):
                raise ServiceError("bad_request", "--harness test needs --test-session KEY ([A-Za-z0-9_.-]{1,64})")
            test_session = ts
            a = params.get("test_ack")
            ack = a if a in ACK_MODES else "next_call"
        ci = params.get("client_info")
        ci = {k: str(v)[:80] for k, v in ci.items() if k in ("name", "version")} if isinstance(ci, dict) else {}
        ev = params.get("evidence")
        ev = {k: str(v)[:120] for k, v in ev.items()} if isinstance(ev, dict) else {}
        sid = params.get("session_id")
        mc = McpConn(
            ident=ident,
            client_info=ci,
            evidence=ev,
            env_leak=bool(params.get("env_leak")) and ident.harness != "claude",
            has_messaging_token=bool(params.get("has_messaging_token")),
            session_id=sid[:128] if isinstance(sid, str) else None,
            test_session=test_session,
            test_ack=ack,
        )
        conn.mcp = mc
        # A reconnecting MCP server (same process) picks its sessions up again.
        mine = self.store.participants_by_mcp(ident.host, ident.mcp_pid, ident.mcp_start)
        for p in mine:
            if p.harness == "test":
                self.engine.ack_modes[p.id] = ack
            if p.status == "offline":
                self.run(self.engine.set_status(p, "starting", "mcp:hello"))
        codex = self.engine.adapters.get("codex")
        if codex is not None:
            # a Codex app-server that runs switchboard's MCP servers (after a daemon restart:
            # the new one), and a restarting session's own MCP process reconnecting
            codex.on_mcp_hello(ident, [p for p in mine if p.harness == "codex"])
        adapter = self.engine.adapter_for(ident.harness, ident.host)
        tier, note = adapter.tier(None)
        log.info("mcp hello: conn %d harness %s%s (%s)", conn.id, ident.harness,
                 f" on {ident.host}" if ident.host else "", ident.evidence)
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
        if harness == "test":
            if not (self.state.test_mode and link.sat_test_mode):
                raise ServiceError("forbidden", "--harness test needs test mode on both machines")
        elif harness != "unknown" and harness not in link.entry.harnesses:
            harness, note = "unknown", f"not allowed for {host}"
        agent = att["agent"]
        return McpIdentity(
            harness=harness,
            mcp_pid=att["mcp"][0],
            mcp_start=att["mcp"][1],
            agent_pid=agent[0] if agent else None,
            agent_start=agent[1] if agent else None,
            evidence=att["evidence"],
            tier_note=note,
            claude_socket=att["claude_socket"] if harness == "claude" else None,
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
        if mc.inbox_attached:
            self._detach(conn, mc)
        acts: list[Action] = self.engine.close_conn_sinks(conn.id)
        for p in self.store.participants_by_mcp(mc.ident.host, mc.ident.mcp_pid, mc.ident.mcp_start):
            if p.harness == "codex" and p.host == LOCAL_HOST:
                continue  # CodexLink tracks a local thread; a remote one has only this connection
            acts += self.engine.expire_pull_batches(p.id, "disconnect")
            acts += self.engine.set_status(p, "offline", "mcp:bye")
        self.run(acts)

    # ======================================================= Claude inbox
    def _claude(self) -> Any:
        return self.engine.adapters.get("claude")

    def attach(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        """``mcp.attach``: make this verified Claude MCP connection its session's
        push channel (DESIGN.md §6.4, §9.2). Only a connection whose process is a
        direct child of a claude whose registry names the same inbox socket,
        that has the session token and whose own guard passed, gets the inbox
        tier; everything else stays ``claude:hook``.

        On a remote host (§27.5.6, §27.7) the same: the parent, registry and socket check
        is the one its satellite ran there (``facts.attest``), and the channel is keyed by
        ``(host, mcp pid)``. Frames to it carry ``chk`` for the satellite's last-mile
        check (``ClaudeAdapter.send``)."""
        mc = self._mcp(conn)
        why = None
        if mc.harness != "claude" or not mc.ident.claude_socket:
            why = "not a verified Claude session"
        elif not mc.has_messaging_token:
            why = "no session messaging token"
        elif params.get("guard_ok") is not True:
            why = "the MCP server's inbox guard refused"
        adapter = self._claude()
        if why is not None or adapter is None:
            return {"attached": False, "reason": why or "no Claude adapter"}
        if mc.inbox_attached:  # a repeated hello on the same connection
            return {"attached": True, "tier": "claude:inbox"}
        adapter.attach(mc.ident.mcp_pid, mc.ident.mcp_start, conn, host=mc.ident.host)
        mc.inbox_attached = True
        self.store.add_event("tier", data={"what": "attach", "harness": "claude"})
        self._refresh_tiers(mc)
        return {"attached": True, "tier": "claude:inbox"}

    def _detach(self, conn: "Conn", mc: McpConn) -> None:
        adapter = self._claude()
        if adapter is not None:
            adapter.detach(conn)
        mc.inbox_attached = False
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
        if not isinstance(bid, int) or isinstance(bid, bool) or not mc.inbox_attached:
            return {}
        adapter = self._claude()
        if adapter is None:
            return {}
        t = params.get("t_post")
        t_ok = isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
        err = params.get("err")
        if err is not None:
            # it reaches the broker log: codes only, never free text
            err = err if isinstance(err, str) and POST_ERR_RE.fullmatch(err) else "post_failed"
        res: dict[str, Any] = {"ok": params.get("ok") is True, "t_post": float(t) if t_ok else None, "err": err}
        if _remote(conn) and conn.facts.get("lastmile") is True:
            res["lastmile"] = True  # the satellite's own report (§27.5.6)
        adapter.posted(bid, conn, res)
        return {}

    # ======================================================== membership
    def _session_key(self, mc: McpConn, thread_id: str | None) -> str:
        h, host = mc.harness, mc.ident.host
        if h == "test":
            return session_key("test", host, str(mc.test_session))
        if h == "codex":
            if not thread_id:
                raise ServiceError("bad_request", "Codex calls must carry _meta.threadId")
            return session_key("codex", host, thread_id)
        if h == "cursor":
            # bound to the conversation id by the join nonce (M5); until then, the agent process
            return session_key("cursor", host, f"agent:{mc.ident.agent_pid}@{_start_key(mc.ident.agent_start)}")
        return session_key(h, host, f"{mc.ident.agent_pid}@{_start_key(mc.ident.agent_start)}")

    def _cursor_session(self, mc: McpConn) -> Participant | None:
        """The active Cursor participant of this MCP server's verified agent process."""
        if mc.ident.agent_pid is None:
            return None
        for p in self.store.active_participants_by_agent("cursor", mc.ident.host, mc.ident.agent_pid):
            if proc.same_start(p.agent_start, mc.ident.agent_start):
                return p
        return None

    @staticmethod
    def _same_mcp(p: Participant, ident: McpIdentity) -> bool:
        """``(host, mcp_pid, mcp_start)`` equal: the very MCP process that holds ``p``."""
        return p.host == ident.host and p.mcp_pid == ident.mcp_pid and proc.same_start(
            p.mcp_start, ident.mcp_start)

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
                raise ServiceError("conflict", f"can't verify this session on {existing.host or 'this machine'}"
                                               " yet; try again in a few seconds")
            if a:
                raise ServiceError("conflict", "this session is already joined from another live switchboard MCP"
                                               " server; ask your user")
        same_agent = existing.host == mc.ident.host and existing.agent_pid == mc.ident.agent_pid and \
            proc.same_start(existing.agent_start, mc.ident.agent_start)
        if not same_agent and existing.agent_pid:
            a = view.alive(existing.agent_pid, existing.agent_start)
            if a is None:
                raise ServiceError("conflict", f"can't verify this session on {existing.host or 'this machine'}"
                                               " yet; try again in a few seconds")
            if a:
                raise ServiceError("conflict", "this session belongs to another live agent process; ask your user")

    def _mcp(self, conn: "Conn") -> McpConn:
        mc = getattr(conn, "mcp", None)
        if mc is None:
            raise ServiceError("unauthorized", "send mcp.hello first")
        return mc

    def _member(self, conn: "Conn", params: dict[str, Any], *, next_call: bool = True
                ) -> tuple[Participant, Membership, Room]:
        mc = self._mcp(conn)
        cred = params.get("cred")
        if not isinstance(cred, str) or not cred or len(cred) > 200:
            raise ServiceError("unauthorized", "missing membership credential: join() first")
        m = self.store.membership_by_cred(cred_hash(cred))
        if m is None:
            raise ServiceError("unauthorized", "not a member (your membership was revoked or rotated): join() again")
        p = self.store.get_participant(m.participant_id)
        if p is None or not p.active:
            raise ServiceError("unauthorized", "this session has ended: join() again")
        if not self._same_mcp(p, mc.ident):
            # (host, mcp_pid, mcp_start): a credential works only from the MCP process that joined,
            # on its own host (§27.5.4)
            raise ServiceError("unauthorized", "this credential belongs to another process")
        if p.harness == "codex":
            tid = params.get("thread_id")
            if not isinstance(tid, str) or p.session_key != session_key("codex", p.host, tid):
                raise ServiceError("unauthorized", "this credential belongs to another Codex thread")
        room = self.store.room_by_id(m.room_id)
        assert room is not None
        if _remote(conn):
            # the allowlists hold for every call, not only at join: a membership its remote no
            # longer allows (the manager ends those at reload) never works over the link (§27.5.2)
            link = getattr(conn, "link", None)
            if link is None or not link.entry.allows_room(room.name):
                raise ServiceError("forbidden", f"members on {p.host} may no longer use {room.name}"
                                                " (the rooms of its remotes.toml entry on the desktop)")
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
                raise ServiceError("forbidden", f"members on {host} may join only {allowed}"
                                                " (the rooms of its remotes.toml entry on the desktop)")
        try:
            room = self.svc.room(raw_room)
        except ServiceError as e:
            if e.code == "not_found":
                n = raw_room.strip().lower()
                n = n if n.startswith("#") else "#" + n
                raise ServiceError("not_found", f"no such room {clean(n)[:40]}: ask your user to create it") from None
            raise
        name = params.get("screen_name")
        name = name.strip().lower().lstrip("@") if isinstance(name, str) else ""
        if not SCREEN_NAME_RE.match(name):
            raise ServiceError("bad_request", "screen names look like claude-1: a-z first, then a-z, 0-9, '_' or '-', at most 24")
        human = self.cfg.human_name.lower()
        if name in RESERVED_NAMES or name.startswith("switchboard") or name.startswith(human):
            raise ServiceError("name_reserved", f"{name} is reserved; pick another screen name")
        tid = params.get("thread_id")
        tid = tid if isinstance(tid, str) and 0 < len(tid) <= 128 else None
        key = self._session_key(mc, tid)
        h = mc.harness
        adapter = self.engine.adapter_for(h, host)
        if h == "codex" and tid and self._held_elsewhere("codex", tid, host) is not None:
            # a thread id is global: no session ever moves between hosts (§27.5.4)
            raise ServiceError("conflict", "this thread is joined from another machine")
        existing = self.store.find_participant(h, key)
        if h == "cursor":
            # one session per agent process; once bound its key is the conversation id
            mine = self._cursor_session(mc)
            if mine is not None:
                existing, key = mine, mine.session_key
        if existing is not None and (self.store.was_kicked(room.id, existing.id) or (
                h == "cursor" and existing.bind_state == "bound"  # a resumed conversation (§9.4)
                and self.store.was_kicked_session(room.id, h, existing.session_key, exclude=existing.id))):
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
        if h == "codex" and not same_mcp:
            # a Codex thread proof belongs to the MCP process that proved it (§9.3):
            # a join from anywhere else proves the thread again
            fields["thread_proof"] = 0
        if h == "cursor" and (existing is None or existing.bind_state != "bound"):
            # bound to its conversation by the postToolUse hook of this join (§6.3)
            fields["bind_state"] = "pending"
        cur = self.store.active_membership(room.id, existing.id) if existing else None
        if cur is None and link is not None:
            joined = {p.id for p in self.store.joined_participants() if p.host == host}
            if (existing is None or existing.id not in joined) and len(joined) >= link.entry.max_members:
                self.run([Notice(room.id, "warn", f"a join from {host} was refused: it already has"
                                                  f" {len(joined)} member(s) (max_members)")])
                raise ServiceError("conflict", f"{host} already has {len(joined)} member(s), its limit"
                                               " (max_members in remotes.toml on the desktop)")
        if cur is None:
            other = self.store.active_membership_by_name(room.id, name)
            if other is not None and (existing is None or other.participant_id != existing.id):
                raise ServiceError("name_taken", f"{name} is taken in {room.name}; pick another screen name")
            if self.store.name_used_by_other(room.id, name, existing.id if existing else -1,
                                             now - NAME_REUSE_S):
                raise ServiceError("name_reserved",
                                   f"{name} was used by another agent in {room.name} today; pick another")
        elif cur.screen_name.lower() != name:
            raise ServiceError("bad_request",
                               f"this session is already in {room.name} as {cur.screen_name};"
                               " leave() first to change your name")
        nonce = secrets.token_hex(8)
        p = self.store.upsert_participant(h, key, bind_nonce=nonce, **fields)
        if h == "test":
            self.engine.ack_modes[p.id] = mc.test_ack
        cred = secrets.token_urlsafe(32)
        rejoined = cur is not None
        if cur is not None:
            # the same verified session again (an MCP reconnect): rotate the credential
            self.store.rotate_cred(cur.id, cred_hash(cred))
            m = cur
            self.run(self.engine.expire_pull_batches(p.id, "rejoin"))
            self.store.add_event("bind", room_id=room.id, membership_id=m.id, participant_id=p.id,
                                 data={"what": "rotate"})
            if h == "codex" and not same_mcp:
                # another MCP process took this thread's membership over (the one that held
                # it is gone, e.g. after a daemon restart): as visible as a new join; its
                # thread is proven again before any push (§9.3)
                self.run([Notice(room.id, "info", f"{m.screen_name} re-joined from a new switchboard MCP server")])
        else:
            m = self.store.create_membership(room.id, p.id, name, cred_hash(cred))
            # a Codex thread proof about to run shows as "verifying...", not "mcp-only"; the
            # event says so, so the proof that passes announces it here (§9.3)
            verifying = note == VERIFYING
            self.store.add_event("join", room_id=room.id, membership_id=m.id, participant_id=p.id,
                                 data={"harness": h, "tier": tier, **({"verifying": True} if verifying else {})})
            where = f"{h} on {host}" if host else h
            shown = VERIFYING if verifying else tier
            self.svc._post(room, sender_name=name, sender_kind="agent", sender_harness=h,
                           sender_membership_id=m.id, via="mcp", kind="join",
                           text=f"joined ({where}, {shown})", sender_host=host or None)
        self.refresh_index()
        if p.agent_pid:
            early = self._early_models.get((p.host, p.agent_pid, _start_key(p.agent_start)))
            if early is not None:
                self._note_model(p, early)
        adapter.on_joined(p, nonce, not same_mcp or not rejoined)
        others = [(x.name, x.harness, x.host) for x in self.store.members(room.id) if x.membership_id != m.id]
        hist = [x for x in self.store.history(room.id, None, 400) if x.kind == "chat"]
        catchup = hist[-self.cfg.delivery.catchup_n:] if self.cfg.delivery.catchup_n else []
        text = envelope.render_join(
            room=room.name, screen_name=m.screen_name, human_name=self.cfg.human_name,
            others=others, catchup=catchup, nonce=nonce,
            guidance=adapter.join_guidance(p, room.name), test_mode=self.state.test_mode,
            rejoined=rejoined,
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
        self.svc._post(room, sender_name=m.screen_name, sender_kind="agent", sender_harness=p.harness,
                       sender_membership_id=m.id, via="mcp", kind="leave", text="left",
                       sender_host=p.host or None)
        self.refresh_index()
        return {"room": room.name, "text": f"[switchboard] you left {room.name}."}

    def who(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        _p, _m, room = self._member(conn, params)
        rows = self.svc.member_rows(room.id)
        lines = [f"[switchboard] {room.name}: {envelope._word(self.cfg.human_name)} (your user, kind=human)"
                 f" and {len(rows)} agent(s)"]
        for x in rows:
            bits = [f"- {envelope._word(x.name)}", f"harness={x.harness}"]
            if x.host:
                bits.append(f"host={envelope._word(x.host)}")
            bits += [f"status={x.status}", f"tier={x.tier or '-'}"]
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

    def say(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        p, m, room = self._member(conn, params)
        text = params.get("text")
        if not isinstance(text, str):
            raise ServiceError("bad_request", "text is required")
        text = text.rstrip()
        if not clean(text).strip():
            raise ServiceError("bad_request", "empty message")
        if len(text) > self.cfg.delivery.max_msg_chars:
            raise ServiceError("bad_request",
                               f"message too long ({len(text)} > {self.cfg.delivery.max_msg_chars} characters)")
        reply_to = params.get("reply_to")
        target: Message | None = None
        if reply_to is not None:
            if isinstance(reply_to, bool) or not isinstance(reply_to, int):
                raise ServiceError("bad_request", "reply_to must be a message id")
            target = self.store.get_message(reply_to)
            if target is None or target.room_id != room.id or target.kind != "chat":
                raise ServiceError("bad_request", f"reply_to {reply_to} is not a message in {room.name}")
        now = self.state.clock.now()
        retry = self.engine.check_say(p, m, target)  # the rate limit (DESIGN.md §8.4)
        if retry is not None:
            utext, bid, count, _more, acts = self.engine.pull(p, m, "say", 50)
            self.run(acts)
            return {"posted_id": None, "reason": "rate_limited", "retry_after_s": retry,
                    "unread_text": utext if count else None, "batch_id": bid, "count": count}
        self.store.mark_handled(m.id)
        self.store.update_participant(p.id, last_say_at=now)
        mentions = rules.parse_mentions(text, self.svc.mention_names(room))
        msg = self.svc._post(room, sender_name=m.screen_name, sender_kind="agent",
                             sender_harness=p.harness, sender_membership_id=m.id, via="mcp",
                             text=text, reply_to=reply_to, mentions=mentions, sender_host=p.host or None)
        utext, bid, count, _more, acts = self.engine.pull(p, m, "say", 50, before_id=msg.id)
        self.run(acts)
        return {"posted_id": msg.id, "unread_text": utext if count else None, "batch_id": bid, "count": count}

    def read(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        p, m, _room = self._member(conn, params)
        limit = params.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int):
            limit = 20
        text, bid, count, more, acts = self.engine.pull(p, m, "read", max(1, min(limit, 50)))
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
            return {"room": room.name, "passed": False, "reason": "read_first", "unread": len(unread),
                    "ids": unread[:20], "text": envelope.render_read_first(room.name, unread)}
        n = self.store.mark_handled(m.id)
        note = params.get("note")
        self.store.add_event("pass", room_id=room.id, membership_id=m.id, participant_id=p.id,
                             data={"handled": n, "note_len": len(note) if isinstance(note, str) else 0})
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

    # =============================================================== hooks
    async def hook_event(self, conn: "Conn", params: dict[str, Any]) -> dict[str, Any]:
        inert: dict[str, Any] = {"out": None}
        harness = params.get("harness")
        if harness == "test" and not self.state.test_mode:
            return inert
        if not isinstance(harness, str) or harness not in HOOK_EVENTS:
            return inert
        event = canonical_event(params.get("event") if isinstance(params.get("event"), str) else "")
        if event not in HOOK_EVENTS[harness]:
            return inert
        if _remote(conn):
            # a remote host's hook: its satellite walked the chain on that host (§27.5.5)
            got = self._remote_chain(conn)
            if got is None:
                return inert
            host, chain, argv_fn = got
            if harness == "test" and not getattr(conn.link, "sat_test_mode", False):
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
            HookCandidate(participant_id=p.id, harness=p.harness, agent_pid=p.agent_pid,
                          agent_start=p.agent_start, session_id=p.session_id,
                          session_key=p.session_key, bind_state=p.bind_state)
            for p in self.store.joined_participants() if p.host == host
        ]
        nonce_bind = harness == "cursor" and bool(ev.join_nonce)
        c = resolve_hook_participant(get_chain(), harness, ev.sid, cands, join_nonce_bind=nonce_bind,
                                     argv_fn=argv_fn, host=host)
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
        chain = [ProcInfo(pid=pid, ppid=facts[i + 1][0] if i + 1 < len(facts) else 0, start=start, uid=-1)
                 for i, (pid, start, _v) in enumerate(facts)]
        verdicts = {pid: v for pid, _s, v in facts}

        def argv_fn(procs: list[ProcInfo]) -> dict[int, str]:
            return {p.pid: proto.verdict_argv(verdicts.get(p.pid, "?")) for p in procs}

        return conn.host, chain, argv_fn

    def _held_elsewhere(self, harness: str, rest: str, host: str) -> Participant | None:
        """An active session of ``harness`` with this global id (a Codex thread, a Cursor
        conversation) on another host than ``host``, this machine included (§27.5.4)."""
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
        the same agent process) is re-keyed the same way."""
        nonce, sid = ev.join_nonce or "", ev.sid or ""
        if not p.bind_nonce or not hmac.compare_digest(p.bind_nonce, nonce):
            # not this participant's current join: only its own bound conversation may go on
            return p if p.bind_state == "bound" and p.session_key == session_key("cursor", p.host, sid) else None
        if not CURSOR_SID_RE.fullmatch(sid):
            return None
        key = session_key("cursor", p.host, sid)
        if p.session_key == key and p.bind_state == "bound":
            self.store.update_participant(p.id, bind_nonce=None)  # single use
            return self.store.get_participant(p.id)
        if self._held_elsewhere("cursor", sid, p.host) is not None:
            # a conversation id is global: no session ever moves between hosts (§27.5.4)
            self.store.add_event("bind", participant_id=p.id, data={"what": "cursor", "ok": False,
                                                                    "why": "conversation on another host"})
            self.run([Notice(m.room_id, "warn",
                             f"{m.screen_name}: can't bind to its Cursor conversation: it is joined from"
                             " another machine (it stays mcp-only)")
                      for m in self.store.participant_memberships(p.id)])
            return None
        other = self.store.find_participant("cursor", key)
        if other is not None and other.id != p.id:
            # the holder's own host decides; one that can't tell (None) counts as alive
            if other.active and other.agent_pid and self.hosts.agent_alive(other) is not False:
                self.store.add_event("bind", participant_id=p.id, data={"what": "cursor", "ok": False,
                                                                        "why": "conversation held"})
                log.warning("cursor participant %d: conversation already bound to live participant %d",
                            p.id, other.id)
                self.run([Notice(m.room_id, "warn",
                                 f"{m.screen_name}: can't bind to its Cursor conversation: another live"
                                 " switchboard session holds it (it stays mcp-only)")
                          for m in self.store.participant_memberships(p.id)])
                return None
            if other.active:  # its agent is gone: end it now, as the liveness check would
                self.check_liveness()
            # the key of an ended session of this conversation (e.g. before a --resume) is freed
            self.store.update_participant(other.id, session_key=f"{key}#ended-{other.id}")
        # the nonce is single use: re-keying again takes a new join() (a visible rejoin)
        p = self.store.update_participant(p.id, session_key=key, session_id=sid[:128], bind_state="bound",
                                          bind_nonce=None)
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
            self.store.add_event("kick", room_id=m.room_id, membership_id=m.id, participant_id=p.id,
                                 data={"why": "resumed a kicked conversation"})
            self.run(self.engine.on_membership_ended(m.id, "kick"))
            room = self.store.room_by_id(m.room_id)
            if room is not None:
                self.svc._post(room, sender_name=m.screen_name, sender_kind="agent", sender_harness="cursor",
                               sender_membership_id=m.id, via="system", kind="leave",
                               text=f"was kicked by {self.cfg.human_name} (earlier, in this conversation)",
                               sender_host=p.host or None)
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
            self._early_models[(host, pi.pid, _start_key(pi.start))] = model
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
                    self.svc._post(room, sender_name=m.screen_name, sender_kind="agent",
                                   sender_harness=p.harness, sender_membership_id=m.id,
                                   via="system", kind="leave", text="left (session ended)",
                                   sender_host=p.host or None)
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
            if (isinstance(t, (list, tuple)) and len(t) == 2 and isinstance(t[0], int)
                    and not isinstance(t[0], bool) and isinstance(t[1], str)):
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
