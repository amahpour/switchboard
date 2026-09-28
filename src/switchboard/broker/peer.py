"""Who is on the other end of the broker's Unix socket (DESIGN.md §5.3).

Pids come from the kernel (``LOCAL_PEERPID`` / ``SO_PEERCRED``), never from
request params. Human verbs over the UDS need ``human_cli``: same uid and no
agent harness anywhere in the caller's ancestry, checked all the way up to
pid 1. The check fails closed: a chain that can't be walked to the root, or
any ancestor whose argv can't be read, is treated as an agent's. This stops
an agent's Bash running ``switchboard cmd /budget 999``, however deeply nested;
it is not a boundary against a detached same-user process (reparented to
pid 1). The sandbox is the boundary.
"""

from __future__ import annotations

import os
import re
import socket
import struct
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from switchboard import claude_registry
from switchboard.broker import proc
from switchboard.broker.proc import ProcInfo
from switchboard.models import LOCAL_HOST, session_key

_SOL_LOCAL = 0
_LOCAL_PEERCRED = 1
_LOCAL_PEERPID = 2

AGENT_MATCHERS: dict[str, tuple[re.Pattern[str], ...]] = {
    "claude": (re.compile(r"(^|/)claude(\s|$)"), re.compile(r"/claude/versions/")),
    "codex": (re.compile(r"(^|/)codex(\s|$)"), re.compile(r"codex app-server")),
    # Unconfirmed against a live ps capture (Cursor not yet run live); see DESIGN §5.3.
    "cursor": (re.compile(r"cursor-agent"),),
    "devin": (re.compile(r"(^|/)devin(\s.*)?\sacp(\s|$)"),),
}


def peer_pid(sock: socket.socket) -> int | None:
    try:
        if sys.platform == "darwin":
            return struct.unpack("i", sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4))[0]
        pid, _uid, _gid = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        return pid
    except (OSError, AttributeError, struct.error):
        return None


def peer_uid(sock: socket.socket) -> int | None:
    try:
        if sys.platform == "darwin":
            # struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]; }
            xucred = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, 76)
            return struct.unpack_from("I", xucred, 4)[0]
        _pid, uid, _gid = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        return uid
    except (OSError, AttributeError, struct.error):
        return None


# DESIGN.md §27.5.7: the two SSH rules of the human side of broker.sock.
# A relay peer: the process on the broker socket is itself an SSH or socket relay.
# Through any forward of the socket (``ssh -R``/``-L``, socat) the kernel peer is
# the relay, never the program behind it, so it never gets a human role.
RELAY_NAMES = frozenset(
    {"ssh", "sshd", "sshd-session", "socat", "nc", "ncat", "netcat", "autossh", "dropbear", "dbclient"}
)
# A process title "<name>: ..." (sshd's "sshd-session: alice@notty", an ssh ControlMaster's
# "ssh: <control path> [mux]", which serves every forward of the connections it multiplexes).
_TITLE = re.compile(r"^([A-Za-z0-9_.-]+):(\s|$)")
# A script run by an interpreter (``python3 /tmp/x/ssh``) is named by its script,
# and a busybox applet by its applet.
_SCRIPT_HOSTS = re.compile(
    r"^(python[0-9.]*|pypy[0-9.]*|perl[0-9.]*|ruby[0-9.]*|node|sh|bash|dash|zsh|ksh|busybox)$"
)
# A remote-login server above the caller, by its program's own name (never its arguments):
# sshd (its titles "sshd: ..." / "sshd-session: ...", its binaries), dropbear, mosh-server and,
# beyond the design's list, other servers that start a login shell for a remote user.
REMOTE_LOGIN_NAMES: dict[str, str] = {
    "sshd": "sshd",
    "sshd-session": "sshd",
    "dropbear": "dropbear",
    "mosh-server": "mosh-server",
    "tinysshd": "tinysshd",
    "tailscaled": "tailscaled",  # Tailscale SSH: the login shell runs under tailscaled
    "etserver": "etserver",  # Eternal Terminal
    "etterminal": "etterminal",
    "telnetd": "telnetd",
    "in.telnetd": "telnetd",
}


def program_name(argv: str) -> str | None:
    """The program an argv runs, by its own name: a process title's ``<name>:``
    (``sshd-session: alice@notty``), else the basename of the first word."""
    a = argv.strip()
    if not a:
        return None
    m = _TITLE.match(a)
    if m:
        return m.group(1)
    return os.path.basename(a.split()[0]) or None


def relay_name(argv: str) -> str | None:
    """The relay this argv runs (``ssh``, ``socat``, ...), or None.

    Looks at the program's own name: the basename of the first word, or for an
    interpreter (``python3``, ``sh``, ...) of its script, and at process titles
    (``sshd-session: alice@notty``, an ssh ControlMaster's ``ssh: <path> [mux]``).
    Arguments are never searched, so ``switchboard say '#r' 'see /usr/bin/ssh'``
    is not a relay.
    """
    a = argv.strip()
    if not a:
        return None
    m = _TITLE.match(a)
    if m:
        return m.group(1) if m.group(1) in RELAY_NAMES else None
    words = a.split()
    name = os.path.basename(words[0])
    if name in RELAY_NAMES:
        return name
    if _SCRIPT_HOSTS.match(name.lower()):
        for w in words[1:]:
            if not w.startswith("-"):
                script = os.path.basename(w)
                return script if script in RELAY_NAMES else None
    return None


def remote_login_name(argv: str) -> str | None:
    """``sshd``, ``dropbear``, ``mosh-server``, ... if this argv is a remote-login server, else None.

    Only the program's own name counts (``program_name``), never its arguments, so
    ``uv run switchboard say '#r' 'I restarted /usr/sbin/sshd'`` is no remote login."""
    name = program_name(argv)
    return REMOTE_LOGIN_NAMES.get(name) if name else None


def relay_refusal(chain: Sequence[ProcInfo], argvs: dict[int, str]) -> str | None:
    """The relay rule: ``chain[0]`` (the kernel peer) is an SSH or socket relay.
    No setting relaxes it."""
    if not chain:
        return None
    relay = relay_name(argvs.get(chain[0].pid, ""))
    if relay is None:
        return None
    return (f"arrived through ssh or a socket relay (the process on the broker socket is {relay});"
            " human commands must come from a terminal on this machine")


def remote_login_refusal(chain: Sequence[ProcInfo], argvs: dict[int, str], allow_ssh_cli: bool) -> str | None:
    """The remote-login rule: a remote-login server above the caller (``chain[1:]``),
    unless ``allow_ssh_cli``. The caller's own argv (the message text) is never looked at."""
    if allow_ssh_cli:
        return None
    for p in chain[1:]:
        login = remote_login_name(argvs.get(p.pid, ""))
        if login is not None:
            return (f"arrived through ssh or another remote login ({login} above the caller);"
                    " human commands must come from a terminal on this machine,"
                    " or set [security] allow_ssh_cli = true")
    return None


def ssh_verdict(
    chain: Sequence[ProcInfo], complete: bool, argvs: dict[int, str], allow_ssh_cli: bool
) -> tuple[bool, str | None]:
    """``(human_cli, reason)`` for a chain under the SSH rules (§27.5.7) and the older ones.

    The relay reason comes first (it has no setting to suggest). The remote-login
    reason is given only to a chain that would otherwise be human: under an agent,
    or on an incomplete chain, the old refusal stands, so an agent is never told
    to turn ``allow_ssh_cli`` on (it would not help it anyway). Neither rule is a
    boundary against a detached same-user process (§5.3); they stop the routes
    remote members make routine.
    """
    why = relay_refusal(chain, argvs)
    if why is not None:
        return False, why
    if not chain_is_human(chain, complete, argvs):
        return False, None
    why = remote_login_refusal(chain, argvs, allow_ssh_cli)
    return why is None, why


def match_agent(argv: str) -> str | None:
    for harness, pats in AGENT_MATCHERS.items():
        if any(p.search(argv) for p in pats):
            return harness
    return None


def is_agent_chain(argvs: Iterable[str]) -> bool:
    """True if any process in the chain looks like an agent harness."""
    return any(match_agent(a) for a in argvs if a)


@dataclass
class Peer:
    """A connected UDS client, identified by the kernel."""

    pid: int | None
    uid: int | None
    start: float | None = None
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_socket(cls, sock: socket.socket | None) -> "Peer":
        if sock is None:
            return cls(pid=None, uid=None)
        pid, uid = peer_pid(sock), peer_uid(sock)
        start = None
        if pid:
            info = proc.info(pid)
            start = info.start if info else None
        return cls(pid=pid, uid=uid, start=start)


# pid -> (chain [pid, parent, ...], complete: the walk reached the root)
ChainFn = Callable[[int], tuple[Sequence[ProcInfo], bool]]


def short_chain(chain: Sequence[ProcInfo], n: int = 2) -> str:
    """'zsh ← Terminal' from [cli, zsh, Terminal, ...] (skips the caller itself)."""
    names = [os.path.basename(p.comm) or str(p.pid) for p in chain[1 : 1 + n]]
    return " ← ".join(names) if names else "unknown"


class PeerPolicy:
    """Decides the human roles of a UDS peer. Default: nobody."""

    def human_cli_allowed(self, peer: Peer) -> bool:
        return False

    def human_allowed(self, peer: Peer) -> bool:
        """``human`` over the UDS exists only under test trust."""
        return False

    def login_allowed(self, peer: Peer) -> bool:
        return False

    def refusal(self, peer: Peer) -> str | None:
        """A reason worth naming when a human role is refused (the SSH rules), or None."""
        return None

    def describe(self, peer: Peer) -> str:
        return "unknown"


def chain_is_human(chain: Sequence[ProcInfo], complete: bool, argvs: dict[int, str]) -> bool:
    """Fail-closed verdict on a process chain: it reached the root, every
    argv is known, and none of them is an agent harness."""
    if not complete or not chain:
        return False
    vals = [argvs.get(p.pid, "") for p in chain]
    return all(vals) and not is_agent_chain(vals)


class ProcessPeerPolicy(PeerPolicy):
    """The production policy: same uid and no agent harness anywhere in the
    ancestry, walked to pid 1 (fail closed). The SSH rules (§27.5.7) also
    refuse a relay peer always and a remote-login ancestor unless
    ``allow_ssh_cli`` (``[security] allow_ssh_cli``).

    ``chain_fn``, ``argv_fn`` and ``tty_fn`` are injectable for tests."""

    def __init__(
        self,
        chain_fn: ChainFn | None = None,
        cap: int = proc.MAX_CHAIN,
        *,
        allow_ssh_cli: bool = False,
        argv_fn: Callable[[list[ProcInfo]], dict[int, str]] | None = None,
        tty_fn: Callable[[int], str | None] | None = None,
    ):
        self._chain_fn = chain_fn or (lambda pid: proc.ancestry_to_root(pid, cap))
        self._argv_fn = argv_fn or proc.argv_many
        self._tty_fn = tty_fn or proc.tty
        self.allow_ssh_cli = allow_ssh_cli

    def _walk(self, peer: Peer) -> tuple[Sequence[ProcInfo], bool]:
        if "walk" not in peer._cache:
            if peer.pid:
                chain, complete = self._chain_fn(peer.pid)
                peer._cache["walk"] = (list(chain), bool(complete))
            else:
                peer._cache["walk"] = ([], False)
        return peer._cache["walk"]

    def chain(self, peer: Peer) -> Sequence[ProcInfo]:
        return self._walk(peer)[0]

    def _verdict(self, peer: Peer) -> tuple[bool, str | None]:
        if "human_cli" not in peer._cache:
            ok, why = False, None
            if peer.uid == os.getuid() and peer.pid:
                chain, complete = self._walk(peer)
                if chain and proc.same_start(chain[0].start, peer.start):
                    argvs = self._argv_fn(list(chain))
                    ok, why = ssh_verdict(chain, complete, argvs, self.allow_ssh_cli)
            peer._cache["human_cli"] = ok
            peer._cache["refusal"] = why
        return peer._cache["human_cli"], peer._cache["refusal"]

    def human_cli_allowed(self, peer: Peer) -> bool:
        return self._verdict(peer)[0]

    def login_allowed(self, peer: Peer) -> bool:
        return self.human_cli_allowed(peer) and peer.pid is not None and (
            self._tty_fn(peer.pid) is not None
        )

    def refusal(self, peer: Peer) -> str | None:
        return self._verdict(peer)[1]

    def describe(self, peer: Peer) -> str:
        return short_chain(self.chain(peer))


class AllowAllHumans(PeerPolicy):
    """Test policy: every same-uid peer is human (in-process tests, --test-trust-uds)."""

    def __init__(self, chain_fn: Callable[[int], Sequence[ProcInfo]] | None = None):
        # Only for the audit notice's "zsh ← Terminal"; no trust decision uses it.
        self._chain_fn = chain_fn or (lambda pid: proc.ancestry(pid, 4))

    def human_cli_allowed(self, peer: Peer) -> bool:
        return peer.uid == os.getuid()

    def human_allowed(self, peer: Peer) -> bool:
        return peer.uid == os.getuid()

    def login_allowed(self, peer: Peer) -> bool:
        return peer.uid == os.getuid()

    def describe(self, peer: Peer) -> str:
        if "chain" not in peer._cache:
            peer._cache["chain"] = list(self._chain_fn(peer.pid)) if peer.pid else []
        return short_chain(peer._cache["chain"])


@dataclass(frozen=True)
class HookCandidate:
    """Just what resolve_hook_participant needs to know about a participant."""

    participant_id: int
    harness: str
    agent_pid: int | None
    agent_start: float | None
    session_id: str | None = None
    session_key: str | None = None
    bind_state: str = "bound"


# Harnesses whose hook session id *is* the participant's key (one agent process
# may host many sessions): a hook must name its own session exactly.
SID_KEYED = frozenset({"codex", "cursor"})


def _chain_index(chain: Sequence[ProcInfo], pid: int | None, start: float | None) -> int | None:
    if pid is None:
        return None
    for i, p in enumerate(chain):
        if p.pid == pid and proc.same_start(p.start, start):
            return i
    return None


def nearest_agent_is(
    chain: Sequence[ProcInfo],
    idx: int,
    argv_fn: Callable[[list[ProcInfo]], dict[int, str]],
) -> bool:
    """True if no other agent harness sits between the hook process (``chain[0]``)
    and ``chain[idx]``. Fails closed: an unreadable argv in between is a no.

    A ``claude -p`` (or ``devin acp``) started from a joined agent's Bash fires
    the same user-level hooks; its events must not be credited to the outer
    session just because the outer agent is further up the chain.
    """
    between = list(chain[1:idx])
    if not between:
        return True
    argvs = argv_fn(between)
    for p in between:
        a = argvs.get(p.pid, "")
        if not a or match_agent(a) is not None:
            return False
    return True


def resolve_hook_participant(
    chain: Sequence[ProcInfo],
    harness: str,
    sid: str | None,
    candidates: Iterable[HookCandidate],
    *,
    argv_fn: Callable[[list[ProcInfo]], dict[int, str]],
    join_nonce_bind: bool = False,
    host: str = LOCAL_HOST,
) -> HookCandidate | None:
    """Pick the one participant a hook event may affect, or None (inert).

    1. Candidates are active participants of ``harness`` whose verified agent
       ``(pid, start)`` is in the hook peer's ancestry, with no other agent
       harness process between the hook and that agent (a nested session's
       hooks are inert, not credited to the outer one). Pending (unbound)
       participants count only for a join-nonce bind.
    2. Codex, and bound Cursor participants, are keyed by the hook's session
       id: ``session_key`` must equal ``session_key(harness, host, sid)``
       (``<harness>:<sid>`` on this machine) exactly, even with a single
       candidate (an unjoined sibling thread under the same daemon is inert).
       A Cursor join-nonce bind skips this: the caller checks that the nonce
       is the one this participant's own join issued, and may re-key it.
    3. If several candidates remain, keep those whose session id or key
       equals ``sid``. Exactly one wins.

    ``chain``, ``argv_fn`` and the candidates all describe one host, ``host``
    ('' for this machine): the caller passes only that host's participants, and
    that host's view's ``argv_many`` (DESIGN.md §27.5.6). ``argv_fn`` has no
    default, so no caller can fall back to this machine's process table for
    another host's chain.
    """
    sid_key = session_key(harness, host, sid) if sid else None
    cands = []
    for c in candidates:
        if c.harness != harness or not (c.bind_state == "bound" or join_nonce_bind):
            continue
        idx = _chain_index(chain, c.agent_pid, c.agent_start)
        if idx is None:
            continue
        if harness in SID_KEYED and c.bind_state == "bound" and not (join_nonce_bind and harness == "cursor"):
            if not sid or c.session_key != sid_key:
                continue
        cands.append((idx, c))
    if len(cands) > 1 and sid:
        cands = [
            (i, c) for i, c in cands
            if c.session_id == sid or c.session_key == sid_key
        ]
    if len(cands) != 1:
        return None
    idx, c = cands[0]
    if harness in AGENT_MATCHERS and not nearest_agent_is(chain, idx, argv_fn):
        return None
    return c


# --------------------------------------------------------------------------
# MCP server verification (DESIGN.md §5.3, §6.2)
# --------------------------------------------------------------------------
class McpRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class McpIdentity:
    """What the broker verified about an ``mcp.hello`` peer, from the kernel."""

    harness: str
    mcp_pid: int
    mcp_start: float | None
    agent_pid: int | None
    agent_start: float | None
    evidence: str
    tier_note: str | None = None
    claude_socket: str | None = None
    # the host whose kernel vouched for these pids: '' for this machine (the broker's own
    # socket peer); a remote's name for an attest from that host's satellite (§27.5.3)
    host: str = LOCAL_HOST


def claude_registry_socket(sessions_dir: str | os.PathLike, claude_pid: int) -> str | None:
    """``messagingSocketPath`` from ``<sessions_dir>/<pid>.json``, if the file is ours and readable
    (``claude_registry.read_registry``: a regular file, never blocking, never raising)."""
    data = claude_registry.read_registry(sessions_dir, claude_pid)
    v = data.get("messagingSocketPath") if data is not None else None
    return v if isinstance(v, str) and v else None


def verify_mcp_peer(
    peer: Peer,
    claimed: str,
    *,
    claude_socket: str | None,
    sessions_dir: str,
    test_mode: bool,
    chain_fn: Callable[[int, int], Sequence[ProcInfo]] | None = None,
    argv_fn: Callable[[list[ProcInfo]], dict[int, str]] | None = None,
) -> McpIdentity:
    """Check a claimed harness against the MCP server's real ancestry.

    The MCP server's own verdict is advisory; this is the broker's. A claim that
    fails its check falls back to ``unknown`` (tier ``mcp-only``), except the
    test harness, which a non-test broker refuses outright.
    """
    chain_fn = chain_fn or (lambda pid, depth: proc.ancestry(pid, depth))
    argv_fn = argv_fn or proc.argv_many
    if not peer.pid:
        raise McpRefused("forbidden", "can't identify the MCP server process")
    chain = list(chain_fn(peer.pid, 5))
    if not chain or chain[0].pid != peer.pid:
        raise McpRefused("forbidden", "can't read the MCP server process")
    me = chain[0]
    parent = chain[1] if len(chain) > 1 else None
    argvs = argv_fn(chain[:5])
    relay = relay_name(argvs.get(me.pid, ""))
    if relay is not None:
        # a forward of the socket (ssh -R/-L, socat): the kernel peer is the relay, so every
        # process behind it would be one "MCP server" (§27.4.2). A remote machine's agents
        # join over a remote link (§27.4), never through a forward (M8c; deferred from M8a).
        raise McpRefused("forbidden", f"the process on the socket is {relay}, a relay: agents on another"
                                      " machine join over a remote link (switchboard remote), not a socket forward")

    def ident(harness: str, agent: ProcInfo | None, evidence: str, note: str | None = None,
              sock: str | None = None) -> McpIdentity:
        return McpIdentity(
            harness=harness,
            mcp_pid=me.pid,
            mcp_start=me.start,
            agent_pid=agent.pid if agent else None,
            agent_start=agent.start if agent else None,
            evidence=evidence,
            tier_note=note,
            claude_socket=sock,
        )

    if claimed == "test":
        if not test_mode:
            raise McpRefused("forbidden", "--harness test needs a test-mode broker")
        return ident("test", parent, "flag:test")
    if claimed == "claude":
        if parent is not None and match_agent(argvs.get(parent.pid, "")) == "claude":
            reg = claude_registry_socket(sessions_dir, parent.pid)
            if reg and claude_socket and reg == claude_socket:
                return ident("claude", parent, "parent:claude+registry", sock=reg)
            return ident("unknown", parent, "parent:claude,registry-mismatch", "unverified claude")
        return ident("unknown", parent, "claude-claim,parent-mismatch", "unverified claude")
    if claimed == "codex":
        if parent is not None and match_agent(argvs.get(parent.pid, "")) == "codex":
            return ident("codex", parent, "parent:codex")
        return ident("unknown", parent, "codex-claim,parent-mismatch", "unverified codex")
    if claimed == "devin":
        for anc in chain[1:3]:
            if match_agent(argvs.get(anc.pid, "")) == "devin":
                return ident("devin", anc, "ancestor:devin-acp")
        return ident("unknown", parent, "devin-claim,no-acp", "unverified devin")
    if claimed == "cursor":
        for anc in chain[1:4]:
            if match_agent(argvs.get(anc.pid, "")) == "cursor":
                return ident("cursor", anc, "ancestor:cursor")
        # clientInfo said Cursor but no cursor-agent ancestor is visible (matcher unconfirmed)
        return ident("unknown", parent, "cursor-claim,no-ancestor", "unverified cursor")
    return ident("unknown", parent, "unknown")
