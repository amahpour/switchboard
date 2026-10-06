"""Codex CLI (DESIGN.md §9.3): tiers ``codex:daemon`` and ``codex:queue``.

- **CodexLink** keeps one long-lived, *unsubscribed* connection to the
  app-server control socket (``codex.control_socket``). An unsubscribed
  connection still receives ``thread/status/changed`` and ``thread/closed`` for
  every loaded thread, and never receives approval requests (FINDINGS §4a).
  Status: ``idle`` -> idle; ``active`` with ``waitingOnApproval`` or
  ``waitingOnUserInput`` -> waiting-approval (every delivery is held);
  other ``active`` -> busy; ``thread/closed`` -> offline. Every 10 s it
  refreshes ``thread/loaded/list``. It retries a missing socket with a 1-30 s
  backoff and **never** starts, stops or restarts the daemon.
- **codex:daemon** (the thread is loaded in the control socket's app-server):
  - idle wake: a fresh, short-lived connection sends ``turn/start``
    ``{threadId, input, clientUserMessageId}`` and closes. Confirmed by RPC
    success; turn start is the next ``thread/status/changed -> active``. Sent
    only when a fresh ``thread/read`` on that connection says ``idle``;
  - mid-task priority: ``thread/read`` gives the in-progress turn id, then
    ``turn/steer`` with ``expectedTurnId`` (``-32600``, or no turn in
    progress: re-routed as an idle wake; if the thread still says active, the
    rest of that turn gets PostToolUse context instead of another steer). A
    steer is confirmed by the UserPromptSubmit it fires, else when the thread
    goes idle and its batch token is in the thread's history (else it expired:
    e.g. lost at a declined approval, FINDINGS §4a 3.4).
- **codex:queue** (the thread isn't loaded there: an embedded TUI, or a TUI
  on another app-server): idle wake through ``codex queue --thread T
  --message M`` (plus ``--remote unix://<control socket>`` whenever that socket
  is live, so the queue call can never reach, or auto-start, another daemon).
  Confirmed by the UserPromptSubmit that carries the token. Without a live
  socket it runs only if ``features.daemon_auto_start`` is explicitly false.
- Mid-task priority outside ``turn/steer`` goes through PostToolUse
  ``additionalContext`` (best effort; developer role, model-dependent).
- **Thread proof.** After ``join``, ``thread/read {includeTurns}`` must show
  ``yk:j<nonce>`` in the result of switchboard's own ``join`` tool call (tries at
  2, 5 and 15 s, then again at each turn end and link start, up to 20 more:
  an in-progress turn's items aren't in ``thread/read``). Until then (with
  ``require_thread_proof``) the member is ``mcp-only``: "verifying..." while the
  first tries run (its join line and /who say so), "unverified thread" after
  they failed. A proof that passes posts "<name> is verified: <tier>" in each
  room whose join line said "verifying..." since the session's last passed
  proof; a proof of a membership kept across an MCP or daemon restart (a
  "re-joined" notice, no join line) posts none.
- **Liveness guard** before every ``turn/start``, ``turn/steer`` and queue call:
  no SessionEnd seen and not offline; for the daemon tier, the thread is in a
  ``thread/loaded/list`` at most 30 s old and ``lsof`` shows a Codex TUI (not
  an app-server, ``exec`` or ``queue`` run, not switchboard) connected to the
  control socket; for the queue tier, the thread's own process is alive and,
  if it is an app-server, has a Codex TUI of its own. A thread outlives its
  TUI by 60-65 s and a turn started then runs headless, and ``lsof`` can't
  tell whose TUI left: so when any TUI disconnects, every thread on that
  server is held until one of them unloads (and the TUIs cover the loaded
  threads again), its own human types or interrupts, or it has been idle for
  70 s.
- **Session end.** A SessionEnd hook marks the thread ended (no push). It
  stops being ended when there is evidence the thread runs again: a later
  hook of that thread, a join that kept its thread proof or a thread proof,
  or the app-server reporting it loaded again after it was seen gone
  (unloaded, listed without it, or its app-server died; a link blip alone
  isn't). The liveness guard above still decides whether anyone is watching.
- **Daemon restarts** (the managed daemon auto-updates and restarts itself;
  the TUI reconnects and keeps its thread id). When a joined thread's
  app-server dies while the thread was loaded on the control socket's server,
  the session is kept for ``codex.restart_grace_s`` (30 s): shown as
  "Codex daemon restarting", nothing pushed. If the thread shows up loaded on
  the new app-server in that window, the participant is re-bound to it (its
  memberships, credentials and queued messages kept, one room notice);
  otherwise it ends as before.
- **The codex binary** (``codex queue``) is re-resolved, with the same
  checks, whenever the cached path is gone or no longer executable (e.g. a
  Homebrew upgrade removed the old version's directory): before a queue
  send, in the clients loop and at each link up (``tier()`` only checks the
  cached path; route() stays pure).

Never: an override field, an answer to a server request, a subscription (no
resume of any kind), a daemon start or stop. Thread contents are searched in
memory only, never logged or stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, Adapter, SendError
from switchboard.adapters.codex_rpc import (
    INVALID_REQUEST,
    CodexRpc,
    RpcError,
    SocketRefused,
    active_turn_id,
    check_socket,
    contains,
    join_proven,
    one_shot,
    server_version,
    thread_status,
    turn_start_params,
    turn_steer_params,
)
from switchboard.broker import proc
from switchboard.broker.peer import match_agent
from switchboard.clock import Clock, SystemClock
from switchboard.config import Config
from switchboard.models import (
    LOCAL_HOST,
    VERIFYING,
    Batch,
    HookEvent,
    Notice,
    Participant,
    Release,
    Route,
    Snapshot,
    session_key,
    split_session_key,
    tier_label,
)

log = logging.getLogger("switchboard.codex")

TIER_DAEMON = "codex:daemon"
TIER_QUEUE = "codex:queue"
PREFIX = "codex:"

LOADED_POLL_S = 10.0  # thread/loaded/list refresh on the status connection
LOADED_FRESH_S = 30.0  # the daemon tier needs a list at most this old (§9.3)
CLIENTS_POLL_S = 5.0  # lsof cache (§9.3, F§12 Codex 6)
CLIENTS_FRESH_S = 15.0
LINK_BACKOFF_S = (1.0, 30.0)
PROOF_AT_S = (2.0, 5.0, 15.0)
PROOF_RETRY_MAX = 20  # more tries, one at each turn end / link start
PROOF_RETRY_DELAY_S = 0.5
DROP_HOLD_S = 70.0  # a thread unloads 60-65 s after its last subscriber leaves (F§4a 3.1)
REROUTE_FREE = 2  # uncounted re-routes in a row before they back off too
REROUTE_BACKOFF_S = (1.0, 30.0)
HOLD_WHY = "a Codex TUI disconnected; holding until it's clear whose thread it was"
ACTIVE_WAIT_S = 0.5  # after turn/start, wait this long for status -> active
QUEUE_TIMEOUT_S = 30.0
SEND_BACKOFF_S = (1.0, 30.0)
LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof", "/sbin/lsof", "/bin/lsof")
RESTART_NOTE = "Codex daemon restarting"
# a Codex row with a host (a remote member, M8c) is never this machine's daemon's
REMOTE_WHY = "not a Codex session on this machine"
# hook events that show a thread whose SessionEnd was seen is running again
RESUME_EVENTS = frozenset({"UserPromptSubmit", "PostToolUse", "Stop", "Interrupt"})
BIN_RETRY_S = 5.0  # a codex binary that can't be found is looked up again at most this often
FRESH_AGENTS_MAX = 64
# Homebrew keeps its own git repository at its prefix on Apple Silicon (/opt/homebrew/.git):
# the installed files below these are not a work tree anyone edits. Only these fixed
# prefixes qualify (a workspace can fake Homebrew's marker files, not its location).
HOMEBREW_PREFIXES = ("/opt/homebrew", "/home/linuxbrew/.linuxbrew")
HOMEBREW_INSTALL_DIRS = frozenset({"bin", "sbin", "Caskroom", "Cellar", "opt"})
# 0.157.0: threads the app-server runs itself (ephemeral, no TUI ever shows them), by their
# ``threadSource``; only these are left out of the holds' TUI-coverage counts
INTERNAL_THREAD_SOURCES = frozenset({"thread_title"})
_BIN_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)(?:$|[-+])")
THREAD_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.:-]{0,127}")
QUEUE_RUN_RE = re.compile(r"(^|/)codex\s+queue(\s|$)")
# codex subcommands that are never a human's TUI
NON_TUI_RE = re.compile(r"(^|/)codex\s+(app-server|exec|e|queue|mcp-server|mcp)(\s|$)")
APP_SERVER_RE = re.compile(r"(^|\s)app-server(\s|$)")


def thread_of(p: Participant) -> str:
    """The Codex thread id of a session on this machine (key ``codex:<thread>``), else ''.
    A remote Codex row (``codex@<host>:<thread>``) has none here: this adapter serves
    this machine's Codex daemon, and a remote thread is never one of its threads
    (DESIGN.md §27.7)."""
    parts = split_session_key(p.session_key)
    return parts[2] if parts is not None and parts[:2] == ("codex", LOCAL_HOST) else ""


def client_id(batch_id: int) -> str:
    return f"yk-b{batch_id}"


# ------------------------------------------------------------------ helpers
def _temp_roots() -> set[str]:
    roots = {"/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp"}
    with contextlib.suppress(OSError):
        roots.add(os.path.realpath(tempfile.gettempdir()))
    return roots


def unsafe_bin_dir(d: str) -> bool:
    """A PATH entry switchboard won't take ``codex`` from: relative, under a temp
    dir, or inside a git work tree (an agent's workspace: e.g. an activated
    project ``.venv/bin`` or a direnv ``bin``), where a sandboxed agent could
    plant a ``codex`` that switchboard would then run outside its sandbox."""
    if not d or not os.path.isabs(d):
        return True
    real = os.path.realpath(d)
    for t in _temp_roots():
        if real == t or real.startswith(t.rstrip("/") + "/"):
            return True
    cur = real
    while True:
        if os.path.lexists(os.path.join(cur, ".git")):
            return not _homebrew_install_dir(cur, real)
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def _homebrew_install_dir(root: str, path: str) -> bool:
    """True if the git work tree at ``root`` is Homebrew's own prefix (Apple
    Silicon keeps Homebrew's repository at /opt/homebrew) and ``path`` is in
    one of the directories Homebrew installs into (``bin``, ``Cellar``,
    ``Caskroom``, ...), which that repository ignores: not an agent's
    workspace. ``root`` must be exactly one of the fixed HOMEBREW_PREFIXES
    (marker files alone can be planted in any workspace), look like
    Homebrew, and be writable only by its owner (this user or root)."""
    if root not in HOMEBREW_PREFIXES:
        return False
    rel = os.path.relpath(path, root)
    top = rel.split(os.sep, 1)[0]
    if rel == "." or top not in HOMEBREW_INSTALL_DIRS:
        return False
    try:
        st = os.stat(root)
    except OSError:
        return False
    if st.st_uid not in (os.getuid(), 0) or st.st_mode & 0o022:
        return False
    return os.path.isfile(os.path.join(root, "bin", "brew")) and os.path.isdir(
        os.path.join(root, "Library", "Homebrew")
    )


def internal_thread(th: dict[str, Any]) -> bool:
    """A thread the app-server runs itself (0.157.0's title generation):
    ``ephemeral`` and a known internal ``threadSource``. Any other thread,
    ephemeral or not, may be a human's (``codex --ephemeral``)."""
    return th.get("ephemeral") is True and th.get("threadSource") in INTERNAL_THREAD_SOURCES


def bin_usable(path: str | None) -> bool:
    """A resolved codex binary is still there: a regular file owned by this
    user or root, executable (the checks ``resolve_bin`` made)."""
    if not path:
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid in (os.getuid(), 0) and os.access(path, os.X_OK)


def resolve_bin(name: str) -> str | None:
    """``codex.bin`` resolved to an absolute file owned by this user or root.
    A bare name is looked up on PATH, skipping unsafe entries (above); an
    absolute path is the user's own choice and is taken as given."""
    if os.path.isabs(name):
        path: str | None = name
    elif os.sep in name:
        return None
    else:
        path = None
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if unsafe_bin_dir(d):
                continue
            found = shutil.which(name, path=d)
            if found:
                path = found
                break
    if not path:
        return None
    real = os.path.realpath(path)
    if not os.path.isabs(name) and unsafe_bin_dir(os.path.dirname(real)):
        return None  # a safe-looking PATH entry that links into a workspace
    return real if bin_usable(real) else None


def resolve_codex(configured: str) -> str | None:
    """The codex binary to run: ``codex.bin`` as configured, or, when an
    absolute configured path no longer resolves (a package upgrade removed
    it), ``codex`` looked up on PATH with the same checks. Nothing else."""
    found = resolve_bin(configured)
    if found is None and os.path.isabs(configured):
        found = resolve_bin("codex")
    return found


def bin_version(path: str) -> str | None:
    """The version of a resolved codex binary, for the record, read from where
    it is installed **without running it** (any codex run writes into
    ``~/.codex/tmp``): a versioned directory on its path (Homebrew's
    ``Caskroom/codex/<v>/``, the daemon's ``releases/<v>-<target>/``), else
    the ``package.json`` of an npm ``@openai/codex`` install above it."""
    real = os.path.realpath(path)
    parts = real.split(os.sep)[:-1]
    for seg in reversed(parts[-4:]):
        m = _BIN_VERSION_RE.match(seg)
        if m:
            return m.group(1)
    d = os.path.dirname(real)
    for _ in range(4):
        pj = os.path.join(d, "package.json")
        try:
            with open(pj, "rb") as f:
                data = json.loads(f.read(65536))
        except (OSError, ValueError):
            data = None
        if (
            isinstance(data, dict)
            and data.get("name") == "@openai/codex"
            and isinstance(data.get("version"), str)
        ):
            m = _BIN_VERSION_RE.match(
                data["version"]
            )  # it reaches `switchboard status`: a version or nothing
            return m.group(1) if m else None
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def codex_home(cfg: Config) -> str:
    return os.path.expanduser(cfg.codex.home or "~/.codex")


def daemon_auto_start(cfg: Config) -> bool | None:
    """Could a bare ``codex queue`` auto-start the daemon? Read-only parse of
    ``$CODEX_HOME/config.toml``: False **only** when ``features.daemon_auto_start``
    is explicitly ``false`` there (and the selected default profile, if any,
    doesn't set it otherwise). A missing file or key is True (Codex's default
    can change; fail closed); None when the file can't be read (also refused).
    System or managed config layers are not read."""
    path = os.path.join(codex_home(cfg), "config.toml")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return True
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    feats = data.get("features")
    val = feats.get("daemon_auto_start") if isinstance(feats, dict) else None
    prof = data.get("profile")
    profiles = data.get("profiles")
    if isinstance(prof, str) and isinstance(profiles, dict) and isinstance(profiles.get(prof), dict):
        pfeats = profiles[prof].get("features")
        if isinstance(pfeats, dict) and "daemon_auto_start" in pfeats:
            val = pfeats["daemon_auto_start"]
    return val is not False


def queue_argv(bin_path: str, thread_id: str, text: str, remote_sock: str | None) -> list[str]:
    """The only argv switchboard runs ``codex queue`` with: no model, sandbox,
    approval or trust flags, ever (DESIGN §11 item 4)."""
    if text.startswith("-"):
        raise ValueError("queue text must not look like a flag")  # it starts "[switchboard]"
    argv = [bin_path, "queue"]
    if remote_sock:
        argv += ["--remote", f"unix://{remote_sock}"]
    return argv + ["--thread", thread_id, "--message", text]


def queue_env(bin_path: str, cfg: Config) -> dict[str, str]:
    env = {"PATH": f"/usr/bin:/bin:{os.path.dirname(bin_path)}", "HOME": os.path.expanduser("~")}
    if cfg.codex.home:
        env["CODEX_HOME"] = os.path.expanduser(cfg.codex.home)
    return env


def lsof_bin() -> str | None:
    for p in LSOF_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def parse_lsof(out: str) -> list[tuple[int, str, str]]:
    """``lsof -F pdn`` output -> [(pid, device, name)] for every file set.

    Linux output (``+E -F pfdin``, see :func:`_linux_unix_rec`) is rewritten to
    the macOS shape, so :func:`socket_peers` reads both."""
    recs: list[tuple[int, str, str]] = []
    pid = -1
    dev: str | None = None
    name: str | None = None
    ino: str | None = None  # Linux only: the socket's inode (the ``i`` field)

    def flush() -> None:
        if pid > 0 and dev is not None and name is not None:
            recs.append((pid, dev, name) if ino is None else (pid, *_linux_unix_rec(dev, name, ino)))

    for line in out.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            flush()
            dev = name = ino = None
            try:
                pid = int(val)
            except ValueError:
                pid = -1
        elif tag == "f":
            flush()
            dev = name = ino = None
        elif tag == "d":
            dev = val
        elif tag == "i":
            ino = val
        elif tag == "n":
            name = val
    flush()
    return recs


# ------------------------------------------------------------ Linux lsof
# macOS lsof names a Unix socket by its bound path, and a client's end by
# ``->0x<kernel address of the server's end>`` (the server end's ``d`` field).
# Linux lsof names it ``[<path> ]type=STREAM`` and, with ``+E``, appends the
# peer as ``->INO=<peer inode> <pid>,<cmd>,<fd>``; ``d`` pairs with nothing, the
# inode (``i``) does. Linux lsof also cuts a path at its first space, so a
# control socket path with whitespace is never matched (no TUI seen: fail closed).
LSOF_ARGS_LINUX = ("-n", "-P", "-U", "+E", "-F", "pfdin")
_LINUX_UNIX_NAME = re.compile(r"^(?:(?P<path>.*?) )?type=\w+(?: ->INO=(?P<peer>\d+)(?: .*)?)?$")


def _linux_unix_rec(dev: str, name: str, ino: str) -> tuple[str, str]:
    """(device, name) of a Linux lsof Unix-socket record in the macOS shape:
    the device becomes ``ino:<inode>``; the name the bound path, or
    ``->ino:<peer inode>`` for an unbound (client) end."""
    m = _LINUX_UNIX_NAME.match(name)
    if m is None:
        return dev, name
    if m["path"]:
        return f"ino:{ino}", m["path"]
    return f"ino:{ino}", (f"->ino:{m['peer']}" if m["peer"] else "")


def socket_peers(recs: list[tuple[int, str, str]], names: set[str]) -> tuple[set[int], set[int]]:
    """(server pids, client pids) of the Unix socket bound at one of ``names``.

    The server's listening and accepted sockets carry the bound path as their
    name; a client's end shows ``->0x<address of the server's end>``."""
    server_addrs = {d for _p, d, n in recs if n in names}
    servers = {p for p, _d, n in recs if n in names}
    clients = {p for p, _d, n in recs if n.startswith("->") and n[2:] in server_addrs} - servers
    return servers, clients


def bound_names(recs: list[tuple[int, str, str]], pid: int) -> set[str]:
    """The paths of the Unix sockets ``pid`` has bound (its listening and accepted ends)."""
    return {n for p, _d, n in recs if p == pid and n.startswith("/")}


def is_tui_argv(argv: str) -> bool:
    """A Codex TUI (a human's session), not an app-server, ``exec`` or ``queue``
    run, or another codex helper. Unknown argv is not a TUI (fail closed)."""
    if not argv or match_agent(argv) != "codex":
        return False
    return not (NON_TUI_RE.search(argv) or APP_SERVER_RE.search(argv))


def is_app_server_argv(argv: str) -> bool:
    return bool(APP_SERVER_RE.search(argv))


@dataclass
class SteerState:
    batch_id: int
    participant_id: int
    thread_id: str
    token: str
    turn_id: str
    sent_at: float
    approval_seen: bool = False


@dataclass
class Clients:
    ok: bool
    at: float
    n: int = 0
    why: str | None = None
    pids: frozenset[int] = frozenset()


@dataclass
class AgentClients:
    """Liveness of a queue-tier thread's own process (§9.3)."""

    start: float | None
    at: float
    ok: bool
    server: bool = False  # an app-server (else: the TUI itself, with its embedded server)
    n: int = 0
    why: str | None = None


@dataclass
class Hold:
    """Threads held because a TUI disconnected from their server (§9.3)."""

    tids: set[str] = field(default_factory=set)


@dataclass
class Orphan:
    """A joined thread whose app-server died: kept for the restart grace window."""

    since: float
    agent_pid: int | None
    agent_start: float | None


# ------------------------------------------------------------------ adapter
class CodexAdapter(Adapter):
    harness = "codex"
    serial_push = True  # one turn/start, steer or queue item in flight per thread

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self.clock: Clock = SystemClock()
        self.runner: Any = None
        self.sock = os.path.expanduser(cfg.codex.control_socket)
        self.link_state = "off"  # off (not started) | down | up
        self.link_note: str | None = None
        self.link_since: float | None = None
        self.rpc: CodexRpc | None = None
        self.loaded: set[str] = set()
        self.loaded_at: float | None = None
        # loaded thread id -> the app-server's own (0.157.0: an ephemeral "thread_title" thread
        # it runs after a session's first prompt; no TUI ever shows it). Unknown = not.
        self.internal: dict[str, bool] = {}
        self.view: dict[str, tuple[str, float]] = {}  # thread id -> (status, since)
        self.ended: dict[str, float] = {}  # thread id -> SessionEnd seen
        # ended thread id -> when it was seen gone (unloaded, or its server lost) since
        # its SessionEnd: loaded again after that, it was resumed
        self.ended_gone: dict[str, float] = {}
        self.orphans: dict[int, Orphan] = {}  # participant id -> its app-server died (restart grace)
        # participant id -> re-joined from a new MCP process inside the grace window: the
        # reconnect notice waits for its thread proof
        self._rejoins: dict[int, Orphan] = {}
        self.lost: tuple[float, frozenset[str]] | None = None  # the last link drop: (when, threads loaded)
        # (pid, start) of Codex app-servers whose MCP servers said hello -> when (last seen)
        self.fresh_agents: dict[tuple[int, float | None], float] = {}
        self.clients: Clients | None = None
        self.server_pids: set[int] = set()
        self.agent_clients: dict[int, AgentClients] = {}  # queue-tier agent pid -> its liveness
        self.suspect: dict[str, float] = {}  # thread id -> held since (a TUI left its server)
        self.holds: dict[Any, Hold] = {}  # server key -> threads held after a TUI left it
        self._seen: dict[Any, frozenset[int]] = {}  # server key -> TUI pids at the last lsof
        self.no_steer: set[str] = set()  # threads whose current turn refused a steer
        self.steers: dict[int, SteerState] = {}
        self.backoff: dict[int, tuple[float, int]] = {}  # participant id -> (until, failures)
        self.reroutes: dict[int, tuple[float, int]] = {}  # participant id -> (until, re-routes in a row)
        self.notes: Counter[str] = Counter()
        self.server_requests: Counter[str] = Counter()
        self.server_version: str | None = None
        self.bin_path: str | None = None
        self.bin_version: str | None = None
        self.bin_fallback = False  # the configured absolute path is gone: ``codex`` from PATH
        self._bin_tried_at: float | None = None
        self.autostart: bool | None = None
        self._active: dict[str, asyncio.Event] = {}
        self._active_at: dict[str, float] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._proofs: dict[int, asyncio.Task[Any]] = {}
        # participants whose first proof tries (PROOF_AT_S, after a join) still run: "verifying..."
        self._verifying: set[int] = set()
        # proofs that passed before the first look for a TUI hold their "is verified" notice
        # (marked in their bind event, so a broker restart keeps it): posted after that look
        self._held_notices = True
        self._proof_tries: dict[tuple[int, str], int] = {}
        self._main: list[asyncio.Task[Any]] = []

    # ============================================================ plumbing
    @property
    def st(self) -> Any:
        return self.runner.state if self.runner is not None else None

    def now(self) -> float:
        return self.clock.now()

    def _local_view(self) -> Any:
        """This machine's host view (DESIGN.md §27.5.6): the broker's
        (``state.hosts``), else one made from the config (unit tests without a broker)."""
        views = getattr(self.st, "hosts", None)
        if views is not None:
            return views.local
        from switchboard.broker.hosts import LocalView

        return LocalView(self.cfg.claude.sessions_dir)

    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no loop (a synchronous unit test)
            coro.close()
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _run(self, acts: list[Any]) -> None:
        if acts and self.runner is not None:
            self.runner.execute(acts)

    def _participant(self, tid: str) -> Participant | None:
        if self.st is None:
            return None
        p = self.st.store.find_participant("codex", PREFIX + tid)
        if p is None or not p.active or not self.st.store.participant_memberships(p.id):
            return None
        return p

    def _joined(self) -> list[Participant]:
        """Joined Codex sessions on this machine. CodexLink, ``live`` and the clients
        check are about this machine's daemon and processes: a remote Codex row
        (``host`` set) is never theirs (DESIGN.md §27.5.6, §27.7)."""
        if self.st is None:
            return []
        return [
            p for p in self.st.store.joined_participants() if p.harness == "codex" and p.host == LOCAL_HOST
        ]

    # ======================================================== capabilities
    def loaded_fresh(self, now: float | None = None) -> bool:
        now = self.now() if now is None else now
        return (
            self.link_state == "up" and self.loaded_at is not None and now - self.loaded_at <= LOADED_FRESH_S
        )

    def attached(self, tid: str, now: float | None = None) -> bool:
        return self.loaded_fresh(now) and tid in self.loaded

    def user_threads(self) -> set[str]:
        """The loaded threads a TUI may be showing (the TUI-coverage counts and
        the held set of the holds, §9.3, use these): all but those known to be
        the app-server's own. A joined member's thread always counts, whatever
        its flags (a human can start an ephemeral thread and join it)."""
        if not any(self.internal.get(t, False) for t in self.loaded):
            return set(self.loaded)
        mine = {thread_of(p) for p in self._joined()}
        return {t for t in self.loaded if t in mine or not self.internal.get(t, False)}

    async def _learn_threads(self, rpc: CodexRpc) -> None:
        """Whether each loaded thread is the app-server's own (``thread/read``
        without turns, on the status link, once per thread). A read that fails
        leaves it unknown, which counts as a user thread (fail closed: more
        holds)."""
        for tid in [t for t in self.loaded if t not in self.internal][:32]:
            if rpc.closed:
                return
            try:
                th = await rpc.read_thread(tid)
            except Exception:
                continue
            if th:
                self.internal[tid] = internal_thread(th)

    def queue_guard(self) -> tuple[bool, str | None, str | None]:
        """(ok, --remote socket or None, why not). ``codex queue`` must never be
        the thing that starts a daemon (with the broker's minimal env). Pure
        (route() calls it through tier()): a binary that vanished is looked up
        again by ``codex_bin()`` in the transport and the clients loop."""
        if not self.cfg.codex.queue_fallback:
            return False, None, "queue fallback is off"
        if not bin_usable(self.bin_path):
            return False, None, "codex binary not found (or not owned by you or root)"
        if self.link_state == "up":
            return True, self.sock, None
        if os.path.lexists(self.sock):
            return False, None, "the Codex control socket isn't answering"
        if self.autostart is not False:
            return False, None, "the Codex daemon isn't running and daemon_auto_start isn't set to false"
        return True, None, None

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        if p is None:
            return "mcp-only", "unverified thread"
        if p.host != LOCAL_HOST:
            return "mcp-only", REMOTE_WHY  # never this daemon's thread (§27.7); M8c routes it elsewhere
        if self.cfg.codex.require_thread_proof and not p.thread_proof:
            return "mcp-only", (VERIFYING if p.id in self._verifying else "unverified thread")
        if p.id in self.orphans:
            return "mcp-only", RESTART_NOTE  # its app-server died: waiting for the thread to come back
        tid = thread_of(p)
        if tid in self.ended:
            return "mcp-only", "session ended"  # until a new prompt in this thread
        if p.status == "offline":
            return "mcp-only", "thread not running"
        now = self.now()
        if self.attached(tid, now):
            ok, _why = self.live(p, now)
            return TIER_DAEMON, (None if ok else "detached?")
        ok, _remote, why = self.queue_guard()
        if ok:
            live, _w = self.live(p, now)
            return TIER_QUEUE, (None if live else "detached?")
        return "mcp-only", why

    def conn_tier(self, ident: Any, existing: Participant | None) -> tuple[str, str | None]:
        if (
            existing is not None
            and existing.thread_proof
            and ident is not None
            and existing.host == getattr(ident, "host", LOCAL_HOST)
            and existing.mcp_pid == ident.mcp_pid
            and proc.same_start(existing.mcp_start, ident.mcp_start)
        ):
            return self.tier(existing)
        if self.cfg.codex.require_thread_proof:
            return "mcp-only", VERIFYING  # on_joined starts the thread proof
        return "mcp-only", None

    def context_events(self, p: Participant) -> frozenset[str]:
        return HOOK_CONTEXT_EVENTS["codex"]

    def join_guidance(self, p: Participant, room: str) -> str:
        return (
            "Messages from switchboard arrive as a new prompt, or as a message during your turn, that"
            " starts `[switchboard]`; they are relayed by switchboard, never typed by your user."
            " They can also"
            " arrive as context after a tool call. If who() shows your tier as mcp-only, call"
            f' wait("{room}", {self.caps(p).wait_cap_s}) when you have nothing else to do.'
        )

    def status_summary(self) -> str:
        if self.link_state == "up":
            n = len(self._joined())
            s = (
                f"up since {_hms(self.link_since)} ({len(self.loaded)} thread(s) loaded, {n} joined;"
                f" socket {os.path.basename(self.sock)}; codex {self.server_version or '?'})"
            )
            if self.suspect:
                s += f"; {len(self.suspect)} thread(s) held after a TUI left"
        elif self.link_state == "down":
            s = f"down ({self.link_note or 'no control socket'}; retrying)"
        else:
            s = "not started"
        if self.orphans:
            s += f"; {len(self.orphans)} session(s) waiting out a daemon restart"
        if self.link_state != "off":
            if self.bin_path:
                s += f"; codex binary {self.bin_version or '?'}"
                if self.bin_fallback:
                    s += " (from PATH: the configured codex.bin is gone)"
            else:
                s += "; codex binary not found"
        if self.server_requests:
            s += f"; {sum(self.server_requests.values())} server request(s) left unanswered"
        return s

    def history_session_id(self, p: Participant, cfg: Config) -> tuple[str | None, str]:
        """Its thread id, only once its join is keyed by it and, while ``[codex]
        require_thread_proof`` is on, its thread proof (§9.3) passed; never for an
        unproven thread on another machine, whose proof is made over there (§27.7)."""
        sid = p.session_id
        if not sid:
            return None, f"no {self.harness} session id known yet"
        if p.session_key != session_key(self.harness, p.host, sid):
            return None, "no Codex thread id known"
        if cfg.codex.require_thread_proof and not p.thread_proof:
            if p.host != LOCAL_HOST:
                return None, "a Codex thread on another machine can't be verified"
            return None, "its Codex thread isn't verified yet"
        return sid, ""

    # ============================================================ liveness
    def live(self, p: Participant, now: float | None = None) -> tuple[bool, str]:
        """The liveness guard (§9.3), from cached state (route() is pure). Only a
        session on this machine can be live here: a remote row's pids are pids on its
        own host, never probed on this one (§27.5.6)."""
        if p.host != LOCAL_HOST:
            return False, REMOTE_WHY
        now = self.now() if now is None else now
        if not p.active or p.status == "offline":
            return False, "offline"
        if p.id in self.orphans:
            return False, "its Codex app-server restarted"
        tid = thread_of(p)
        if tid in self.ended:
            return False, "session ended"
        if self.attached(tid, now):
            c = self.clients
            if c is None or now - c.at > CLIENTS_FRESH_S:
                return False, "can't tell whether a Codex TUI is attached"
            if not c.ok:
                return False, c.why or "no Codex TUI attached"
            if self._held(tid, p, now, attached=True):
                return False, HOLD_WHY
            return True, ""
        # queue tier: the thread's own process (an embedded TUI or another app-server)
        if not p.agent_pid or not self._local_view().alive(p.agent_pid, p.agent_start):
            return False, "the Codex process is gone"
        if p.agent_pid in self.server_pids:
            return False, "thread not loaded in the Codex daemon"
        ac = self.agent_clients.get(p.agent_pid)
        if ac is None or not proc.same_start(ac.start, p.agent_start) or now - ac.at > CLIENTS_FRESH_S:
            return False, "can't tell whether a Codex TUI is attached"
        if not ac.ok:
            return False, ac.why or "no Codex TUI attached"
        if self._held(tid, p, now, attached=False):
            return False, HOLD_WHY
        return True, ""

    def _held(self, tid: str, p: Participant, now: float, *, attached: bool) -> bool:
        """Held after a TUI left this thread's server (§9.3), until released, or
        until the thread has been idle for DROP_HOLD_S since the hold (or since
        its last turn ended): by then a thread whose TUI left has unloaded."""
        since = self.suspect.get(tid)
        if since is None:
            return False
        idle = self.thread_view(tid) == "idle" if attached else p.status in ("idle", "starting")
        return not (idle and now - since >= DROP_HOLD_S)

    def _backing_off(self, p: Participant, now: float) -> bool:
        b = self.backoff.get(p.id)
        r = self.reroutes.get(p.id)
        return (b is not None and now < b[0]) or (r is not None and now < r[0])

    def _rerouted(self, p: Participant) -> None:
        """An uncounted re-route. A few in a row are normal (the world changed
        under a send); more back off too (1 s doubling to 30 s), so a state
        that keeps refusing can never spin sends against the app-server."""
        _u, n = self.reroutes.get(p.id, (0.0, 0))
        n += 1
        until = 0.0
        if n > REROUTE_FREE:
            until = self.now() + min(REROUTE_BACKOFF_S[0] * 2 ** (n - REROUTE_FREE - 1), REROUTE_BACKOFF_S[1])
        self.reroutes[p.id] = (until, n)

    def _failed(self, p: Participant) -> None:
        _u, n = self.backoff.get(p.id, (0.0, 0))
        n += 1
        self.backoff[p.id] = (self.now() + min(SEND_BACKOFF_S[0] * 2 ** (n - 1), SEND_BACKOFF_S[1]), n)

    def thread_view(self, tid: str) -> str | None:
        v = self.view.get(tid)
        return v[0] if v else None

    # ============================================================= routing
    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        tier, note = self.tier(p)
        tid = thread_of(p)
        if rel.kind == "priority":
            if (
                tier == TIER_DAEMON
                and self.live(p, now)[0]
                and self.thread_view(tid) == "busy"
                and tid not in self.no_steer
                and not self._backing_off(p, now)
            ):
                return Route("push", path="steer")
            return Route("pull", reason="next tool call")
        if tier == TIER_DAEMON:
            ok, why = self.live(p, now)
            if not ok:
                return Route("none", reason=f"detached? {why}")
            view = self.thread_view(tid)
            if view is None:
                return Route("defer", reason="thread status not known yet")
            if view in ("busy", "waiting-approval") or p.status not in ("idle", "starting"):
                return Route("defer", reason="turn still running")
            if self._backing_off(p, now):
                return Route("defer", reason="turn/start failed; retrying")
            return Route("push", path="turn_start")
        if tier == TIER_QUEUE:
            ok, why = self.live(p, now)
            if not ok:
                return Route("none", reason=f"detached? {why}")
            if p.hooks_seen_at is None:
                return Route(
                    "none",
                    reason="no switchboard hooks seen from this thread: run `switchboard install"
                    " codex` and review the hooks in /hooks",
                )
            if p.status not in ("idle", "starting"):
                return Route("defer", reason="turn still running")
            if self._backing_off(p, now):
                return Route("defer", reason="codex queue failed; retrying")
            return Route("push", path="queue")
        return Route("none", reason=note or "idle and not listening: call wait() or poke it")

    def on_hook(self, p: Participant, ev: HookEvent) -> dict[str, Any] | None:
        tid = thread_of(p)
        E = ev.ev
        if E == "SessionEnd":
            now = self.now()
            self.ended[tid] = now  # the thread is detached from its TUI
            if self.link_state != "up" or tid not in self.loaded or self.thread_view(tid) == "offline":
                self.ended_gone[tid] = now  # already unloaded (a TUI quit: SessionEnd comes last)
            else:
                self.ended_gone.pop(tid, None)  # still loaded (e.g. its server shutting down)
            self._soon(self.refresh_tiers)
        elif (
            tid in self.ended
            and E in RESUME_EVENTS
            and (E == "UserPromptSubmit" or ev.t is None or ev.t > self.ended[tid])
        ):
            # a hook of this thread that started after its SessionEnd: it runs again
            # (a resume, or its TUI reconnected to a restarted daemon)
            self._clear_ended(tid, f"hook:{E}")
        if E == "Interrupt" or (E == "UserPromptSubmit" and not ev.tokens):
            # a prompt switchboard didn't send, or an Esc: this thread's own human is there
            if self.suspect.pop(tid, None) is not None:
                self._soon(self.refresh_tiers)
        elif E == "Stop" and tid in self.suspect:
            self.suspect[tid] = max(self.suspect[tid], self.now())  # idle again: count from now
        if E in ("UserPromptSubmit", "Stop", "Interrupt"):
            self.no_steer.discard(tid)  # a new turn (or none): steering may work again
        if E == "Stop":
            self._reprove(p)  # the join turn is over: its items are readable now
        return None

    def _soon(self, fn: Any) -> None:
        try:
            asyncio.get_running_loop().call_soon(fn)
        except RuntimeError:
            pass

    def expire_due(self, p: Participant, b: Batch, now: float) -> str | None:
        if b.state != "offered" or b.path not in ("turn_start", "steer", "queue"):
            return None
        if not p.active or p.status == "offline":
            return "offline"
        return None

    def push_expired(self, p: Participant, b: Batch, reason: str, now: float) -> None:
        self.steers.pop(b.id, None)

    # =========================================================== transport
    async def send(self, p: Participant, batch: Batch, text: str, **meta: Any) -> float | None:
        tid = thread_of(p)
        if not THREAD_ID_RE.fullmatch(tid):
            raise SendError("bad thread id")
        try:
            if batch.path == "turn_start":
                r = await self._turn_start(p, batch, text, tid)
            elif batch.path == "steer":
                r = await self._steer(p, batch, text, tid)
            elif batch.path == "queue":
                r = await self._queue(p, batch, text, tid)
            else:
                raise SendError("no such codex path")
        except SendError as e:
            if not e.counted:
                self._rerouted(p)
            raise
        self.reroutes.pop(p.id, None)
        return r

    async def _guard(self, p: Participant, *, fresh_clients: bool) -> None:
        if fresh_clients:
            await self.refresh_clients()
        p = self.st.store.get_participant(p.id) or p
        ok, why = self.live(p)
        if not ok:
            self._soon(self.refresh_tiers)
            raise SendError(f"liveness: {why}", counted=False)

    async def _turn_start(self, p: Participant, batch: Batch, text: str, tid: str) -> float | None:
        await self._guard(p, fresh_clients=True)
        if self.thread_view(tid) != "idle":
            raise SendError("thread not idle", counted=False)  # re-evaluated: a steer, or a hold
        ev = asyncio.Event()  # set by the status -> active notification
        t0 = self.now()

        async def go(r: CodexRpc) -> Any:
            # the status now, on this connection, not the cached view: a turn/start
            # into an active thread acts as a steer, and into an approval wait
            # it would be delivery during the prompt (F§4a 3.3, §11 item 7)
            th = await r.read_thread(tid)
            st = thread_status(th.get("status"))
            if st != "idle":
                if st is not None:
                    self._set_view(tid, st, self.now())
                raise SendError("thread not idle", counted=False)
            self._active[tid] = ev
            return await r.request("turn/start", turn_start_params(tid, text, client_id(batch.id)))

        try:
            try:
                await one_shot(self.sock, go)
            except SendError:
                raise
            except RpcError as e:
                self._failed(p)
                raise SendError(f"turn/start error {e.code}") from None
            except (OSError, ConnectionError, SocketRefused, TimeoutError, asyncio.TimeoutError):
                self._failed(p)
                raise SendError("codex control socket unavailable") from None
            t_rpc = self.now()
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(ev.wait(), ACTIVE_WAIT_S)
        finally:
            if self._active.get(tid) is ev:
                self._active.pop(tid, None)
        t_act = self._active_at.get(tid)
        started = t_act if t_act is not None and t_act >= t0 else t_rpc
        self.backoff.pop(p.id, None)
        st = self.st
        st.store.set_batch_times(batch.id, turn_start_at=started)
        self._run(st.engine.on_confirm(batch.id, "rpc:turn/start"))
        return None

    async def _steer(self, p: Participant, batch: Batch, text: str, tid: str) -> float | None:
        await self._guard(p, fresh_clients=True)
        token = self.st.engine.token(batch)

        def reroute(th: dict[str, Any], why: str) -> SendError:
            # tell the engine what the thread is doing now, so the re-route goes
            # the right way (an idle wake, or a hold) instead of steering again
            status = thread_status(th.get("status"))
            if status is not None:
                self._set_view(tid, status, self.now())
            if self.thread_view(tid) == "busy":
                # still active, yet no turn to steer (or it refused): this turn
                # gets PostToolUse context instead, until its status or turn changes
                self.no_steer.add(tid)
            return SendError(why, counted=False)

        async def go(r: CodexRpc) -> str:
            th = await r.read_thread(tid, include_turns=True)
            if thread_status(th.get("status")) == "waiting-approval":
                raise reroute(th, "waiting on approval")
            turn = active_turn_id(th)
            if turn is None:
                raise reroute(th, "no turn in progress")
            try:
                await r.request("turn/steer", turn_steer_params(tid, turn, text, client_id(batch.id)))
            except RpcError as e:
                if e.code != INVALID_REQUEST:
                    raise
                # "no active turn to steer" / a wrong turn id: the turn changed under us
                raise reroute(await r.read_thread(tid), "steer rejected") from None
            return turn

        try:
            turn = await one_shot(self.sock, go)
        except SendError:
            raise
        except RpcError as e:
            self._failed(p)
            raise SendError(f"turn/steer error {e.code}") from None
        except (OSError, ConnectionError, SocketRefused, TimeoutError, asyncio.TimeoutError):
            self._failed(p)
            raise SendError("codex control socket unavailable") from None
        self.backoff.pop(p.id, None)
        self.steers[batch.id] = SteerState(batch.id, p.id, tid, token, turn, self.now())
        if self.thread_view(tid) in ("idle", "offline"):
            self._spawn(self._settle(tid))  # the turn already ended while we steered
        return None

    async def _queue(self, p: Participant, batch: Batch, text: str, tid: str) -> float | None:
        await self._guard(p, fresh_clients=True)
        bin_path = self.codex_bin()  # the cached one, or looked up again if it vanished
        ok, remote, why = self.queue_guard()
        if not ok or not bin_path:
            raise SendError(f"queue guard: {why}", counted=False)
        try:
            argv = queue_argv(bin_path, tid, text, remote)
        except ValueError:
            raise SendError("queue text refused") from None
        try:
            child = await asyncio.create_subprocess_exec(
                *argv,
                env=queue_env(bin_path, self.cfg),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            self._failed(p)
            raise SendError("codex queue could not start") from None
        try:
            rc = await asyncio.wait_for(child.wait(), QUEUE_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            with contextlib.suppress(ProcessLookupError):
                child.kill()
            self._failed(p)
            raise SendError("codex queue timed out") from None
        if rc != 0:
            self._failed(p)
            raise SendError(f"codex queue exit {rc}")
        self.backoff.pop(p.id, None)
        return None  # confirmed by the UserPromptSubmit that carries the token

    # ------------------------------------------------------------ steers
    async def _settle(self, tid: str) -> None:
        """The thread went idle (or away): did each outstanding steer make it
        into the thread's history? Yes: confirmed. No: expired, re-delivered."""
        pending = [s for s in self.steers.values() if s.thread_id == tid]
        if not pending:
            return
        th: dict[str, Any] | None = None
        with contextlib.suppress(Exception):
            th = await self._read(tid, include_turns=True)
        acts: list[Any] = []
        engine = self.st.engine
        for s in pending:
            if self.steers.pop(s.batch_id, None) is None:
                continue
            if th:
                if contains(th, s.token):
                    acts += engine.on_confirm(s.batch_id, "rpc:steer+history")
                else:
                    acts += engine.on_expire(s.batch_id, "steer_lost")
            elif s.approval_seen:
                acts += engine.on_expire(s.batch_id, "steer_approval")  # lost if declined (F§4a 3.4)
            else:
                acts += engine.on_confirm(s.batch_id, "rpc:steer+idle")
        self._run(acts)

    # ============================================================ the link
    def _set_link(self, state: str, note: str | None = None) -> None:
        if state != self.link_state:
            self.link_since = self.now()
            if self.st is not None:
                self.st.store.add_event("codex_link", data={"state": state, "note": note or ""})
            log.info("codex link %s%s", state, f" ({note})" if note else "")
        self.link_state = state
        self.link_note = note
        if self.st is not None:
            self.st.info.codex_link = self.status_summary()

    async def _read(self, tid: str, include_turns: bool = False) -> dict[str, Any]:
        """A read on a fresh connection: a full history can be many MB, and the
        status link must never wait behind it (or be closed by it)."""
        return await one_shot(self.sock, lambda r: r.read_thread(tid, include_turns))

    def _on_note(self, method: str, params: dict[str, Any], t: float) -> None:
        self.notes[method] += 1
        tid = params.get("threadId")
        if not isinstance(tid, str) or not tid:
            return
        if method == "thread/status/changed":
            st = thread_status(params.get("status"))
            if st is not None:
                self._set_view(tid, st, t)
        elif method == "thread/closed":
            self.loaded.discard(tid)
            self._set_view(tid, "offline", t)
            self.internal.pop(tid, None)

    def _set_view(self, tid: str, st: str, t: float) -> None:
        prev = self.thread_view(tid)
        self.view[tid] = (st, t)
        if st != prev:
            self.no_steer.discard(tid)
        if st != "offline":
            new_thread = tid not in self.loaded
            self.loaded.add(tid)
            if new_thread:
                self._soon(self.refresh_tiers)
                if self.orphans:
                    self._spawn(self.try_rebind())  # maybe a restarted daemon's thread
            gone = self.ended_gone.get(tid)
            if tid in self.ended and gone is not None and t > gone:
                self._clear_ended(tid, "loaded again")  # it was gone after its SessionEnd: resumed
        else:
            if tid in self.ended:
                self.ended_gone.setdefault(tid, t)
            self._unloaded(tid)
        if st == "idle" and prev != "idle" and tid in self.suspect:
            self.suspect[tid] = max(self.suspect[tid], t)  # a held thread's idle time counts from now
        if st in ("busy", "waiting-approval") and prev not in ("busy", "waiting-approval"):
            self._active_at[tid] = t
            ev = self._active.get(tid)
            if ev is not None:
                ev.set()
        if st == "waiting-approval":
            for s in self.steers.values():
                if s.thread_id == tid:
                    s.approval_seen = True
        if st in ("idle", "offline") and any(s.thread_id == tid for s in self.steers.values()):
            self._spawn(self._settle(tid))
        self._apply_status(tid, st)
        if st == "idle" and prev in ("busy", "waiting-approval"):
            p = self._participant(tid)
            if p is not None:
                self._reprove(p)  # a turn ended: its items are in thread/read now

    # ------------------------------------------------------------- holds
    def _hold(self, key: Any, tids: set[str], now: float, why: str) -> None:
        """A TUI left server ``key`` (or, at a first look, it has more loaded
        threads than TUIs): hold every thread on it (§9.3)."""
        if not tids:
            return
        h = self.holds.setdefault(key, Hold())
        h.tids |= tids
        for tid in tids:
            self.suspect[tid] = now
        if self.st is not None:
            self.st.store.add_event("codex_hold", data={"why": why, "threads": len(tids)})
        log.info("codex: holding %d thread(s) (%s)", len(tids), why)
        self._soon(self.refresh_tiers)

    def _unloaded(self, tid: str) -> None:
        """A thread closed on the control socket's server. If it was held, it
        may be the one whose TUI left: when the TUIs we see cover every thread
        still loaded, the rest of that hold is released."""
        self.suspect.pop(tid, None)
        h = self.holds.get("control")
        if h is None or tid not in h.tids:
            return
        h.tids.discard(tid)
        c = self.clients
        loaded = self.user_threads() - {tid}
        if c is not None and c.n >= len(loaded):
            del self.holds["control"]
            for t in h.tids:
                if not any(t in o.tids for o in self.holds.values()):
                    self.suspect.pop(t, None)
            log.info("codex: hold released (a held thread unloaded)")
            self._soon(self.refresh_tiers)

    def _prune_holds(self, now: float) -> None:
        for tid, since in list(self.suspect.items()):
            p = self._participant(tid)
            if p is not None:
                lapsed = not self._held(tid, p, now, attached=self.attached(tid, now))
            else:  # not a member: it only matters for the release count
                lapsed = tid not in self.loaded or (
                    self.thread_view(tid) == "idle" and now - since >= DROP_HOLD_S
                )
            if lapsed:
                self.suspect.pop(tid, None)
        live = set(self.suspect)
        for key in list(self.holds):
            self.holds[key].tids &= live
            if not self.holds[key].tids:
                del self.holds[key]

    def _track(
        self, key: Any, tuis: frozenset[int], now: float, tids: set[str] | None, n_threads: int
    ) -> None:
        """Compare the TUIs connected to one server with the last look."""
        if tids is None:
            return  # which threads it has isn't known yet: look again next time
        prev = self._seen.get(key)
        self._seen[key] = tuis
        if prev is None:
            if len(tuis) < n_threads:
                self._hold(key, tids, now, "first look: more threads than TUIs")
        elif prev - tuis:
            self._hold(key, tids, now, "a TUI disconnected")

    def _apply_status(self, tid: str, st: str) -> None:
        p = self._participant(tid)
        if p is None or self.st is None:
            return
        engine = self.st.engine
        if tid in self.ended and st != "offline":
            return  # after SessionEnd the member stays offline until a new prompt
        if p.status == st:
            self._run(engine.evaluate_participant(p.id))
            return
        bump = st == "idle" and p.status in ("busy", "waiting-approval")
        self._run(engine.set_status(p, st, "codex:status", bump=bump))

    async def _link_loop(self) -> None:
        delay = LINK_BACKOFF_S[0]
        while True:
            self.autostart = daemon_auto_start(self.cfg)
            try:
                check_socket(self.sock)
            except SocketRefused as e:
                self._set_link("down", str(e))
                self.refresh_tiers()
                await asyncio.sleep(delay)
                delay = min(delay * 2, LINK_BACKOFF_S[1])
                continue
            rpc = CodexRpc(self.sock, on_notification=self._on_note, clock=self.now)
            try:
                await rpc.connect()
            except Exception as e:
                await rpc.close()
                self._set_link("down", "connect failed" if not isinstance(e, SocketRefused) else str(e))
                self.refresh_tiers()
                await asyncio.sleep(delay)
                delay = min(delay * 2, LINK_BACKOFF_S[1])
                continue
            self.rpc = rpc
            prev_version, self.server_version = self.server_version, server_version(rpc.init_result)
            self._set_link("up")
            if self.st is not None and self.server_version != prev_version:
                self.st.store.add_event(
                    "codex_link",
                    data={
                        "state": "version",
                        "version": self.server_version or "",
                        "was": prev_version or "",
                    },
                )
                log.info("codex app-server version %s (was %s)", self.server_version, prev_version)
            self._resolve_bin(force=True)  # a daemon restart may be a Codex upgrade: find the binary again
            delay = LINK_BACKOFF_S[0]
            try:
                await self.poll()
                for p in self._joined():
                    self._reprove(p)
                while not rpc.closed:
                    try:
                        await asyncio.wait_for(rpc.closed_event.wait(), LOADED_POLL_S)
                    except (asyncio.TimeoutError, TimeoutError):
                        await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("codex link failed")
            finally:
                self.server_requests.update(rpc.server_requests)
                self.rpc = None
                with contextlib.suppress(Exception):
                    await rpc.close()
                self._link_lost()
            await asyncio.sleep(delay)

    def _link_lost(self) -> None:
        """The status link dropped. The server that had these threads may be
        restarting: a joined thread among them whose app-server died gets the
        restart grace window (``lost``). A drop alone is no evidence that an
        ended thread was gone (the same server may still have it loaded): only
        its agent dying (``defer_end``), an unload, or a list without it is."""
        self.lost = (self.now(), frozenset(self.loaded))
        self.loaded.clear()
        self.loaded_at = None
        self.internal.clear()
        self.view.clear()
        self.no_steer.clear()
        self._seen.pop("control", None)  # the next link starts with a first look
        self._set_link("down", "connection lost")
        self.refresh_tiers()

    async def poll(self) -> None:
        """Refresh the loaded list; seed the status of joined threads we have no view of."""
        rpc = self.rpc
        if rpc is None or rpc.closed:
            return
        t_req = self.now()
        try:
            loaded = await rpc.loaded_threads()
        except Exception:
            log.debug("thread/loaded/list failed", exc_info=True)
            return
        self.loaded = loaded
        self.loaded_at = self.now()
        for tid in [t for t in self.internal if t not in loaded]:
            del self.internal[tid]
        await self._learn_threads(rpc)
        for tid, at in list(self.ended.items()):
            # a list asked for after the SessionEnd: gone, or (once gone) loaded again
            if t_req <= at:
                continue
            gone = self.ended_gone.get(tid)
            if tid not in loaded:
                self.ended_gone.setdefault(tid, t_req)
            elif gone is not None and t_req > gone and self._clear_ended(tid, "loaded again"):
                self._sync_status(tid)  # its status was ignored while it was ended
        for p in self._joined():
            tid = thread_of(p)
            if tid in loaded and self.thread_view(tid) in (None, "offline"):
                with contextlib.suppress(Exception):
                    th = await rpc.read_thread(tid)
                    st = thread_status(th.get("status"))
                    if st is not None and self.thread_view(tid) in (None, "offline"):
                        self._set_view(tid, st, self.now())
        if self.st is not None:
            self.st.info.codex_link = self.status_summary()
        self.refresh_tiers()
        if self.orphans:
            self._spawn(self.try_rebind())

    # ------------------------------------------------------------- lsof
    def _no_clients(self, now: float, why: str) -> Clients:
        # fail closed; the last TUI sets stay, so a TUI that left meanwhile is
        # still noticed at the next good look
        self.clients = Clients(False, now, 0, why)
        self.agent_clients = {}
        return self.clients

    async def refresh_clients(self) -> Clients:
        """Which Codex TUIs are connected to the control socket, and to each
        queue-tier thread's own app-server (one ``lsof -U``)."""
        now = self.now()
        lsof = lsof_bin()
        if lsof is None:
            return self._no_clients(now, "can't run lsof to see who is attached")
        try:
            real = os.path.realpath(self.sock)
            out = await asyncio.to_thread(_run_lsof, lsof)
        except Exception:
            return self._no_clients(now, "lsof failed")
        recs = parse_lsof(out)
        if self._lost_a_tui(recs):
            # Linux lsof can skip a socket in one look (it reads /proc/net/unix while sockets come
            # and go, and a client's end then shows no peer), and a hold isn't undone when the TUI
            # shows again: look once more at once, and count a socket either look saw (#189)
            try:
                recs += parse_lsof(await asyncio.to_thread(_run_lsof, lsof))
            except Exception:
                return self._no_clients(now, "lsof failed")
        me = os.getpid()
        servers, clients = socket_peers(recs, {self.sock, real})
        clients.discard(me)
        joined = self._joined()
        agents: dict[int, float | None] = {
            p.agent_pid: p.agent_start
            for p in joined
            if p.agent_pid and p.agent_pid not in servers and p.agent_pid != me
        }
        peers: dict[int, set[int]] = {}
        for a in agents:
            names = bound_names(recs, a)
            if names:
                _s, c = socket_peers(recs, names)
                peers[a] = c - {me, a}
        want = set(clients) | set(agents)
        for c in peers.values():
            want |= c
        infos = {i.pid: i for i in (proc.info(x) for x in sorted(want)) if i is not None}
        for a in agents:  # an app-server with no socket of its own: its parent TUI
            i = infos.get(a)
            if i is not None and a not in peers and i.ppid not in infos:
                pi = proc.info(i.ppid)
                if pi is not None:
                    infos[pi.pid] = pi
        argvs = proc.argv_many(list(infos.values())) if infos else {}
        tuis = frozenset(x for x in clients if is_tui_argv(argvs.get(x, "")))
        self.server_pids = servers
        up = self.link_state == "up" and self.loaded_at is not None
        if up and self.rpc is not None:
            await self._learn_threads(self.rpc)
        users = self.user_threads()
        self._track("control", tuis, now, users if up else None, len(users))
        self.clients = Clients(
            bool(tuis), self.now(), len(tuis), None if tuis else "no Codex TUI attached", tuis
        )
        acs: dict[int, AgentClients] = {}
        for a, start in agents.items():
            i = infos.get(a)
            if i is None or not proc.same_start(i.start, start):
                continue  # gone (live() says so)
            if not is_app_server_argv(argvs.get(a, "")):
                acs[a] = AgentClients(start, now, True)  # the TUI itself, with its embedded app-server
            elif a in peers:
                t = frozenset(x for x in peers[a] if is_tui_argv(argvs.get(x, "")))
                tids = {thread_of(p) for p in joined if p.agent_pid == a}
                self._track(("agent", a, start), t, now, tids, len(tids))
                acs[a] = AgentClients(
                    start,
                    now,
                    bool(t),
                    True,
                    len(t),
                    None if t else "no Codex TUI attached to its app-server",
                )
            else:
                par = infos.get(i.ppid)
                ok = par is not None and is_tui_argv(argvs.get(par.pid, ""))
                acs[a] = AgentClients(start, now, ok, True, int(ok), None if ok else "no Codex TUI attached")
        self.agent_clients = acs
        for key in [k for k in self._seen if k != "control" and k[1] not in acs]:
            del self._seen[key]
        return self.clients

    def _lost_a_tui(self, recs: list[tuple[int, str, str]]) -> bool:
        """A TUI of the last look that ``recs`` shows connected to no bound socket."""
        seen: set[int] = set().union(*self._seen.values())
        if not seen:
            return False
        bound = {d for _p, d, n in recs if n.startswith("/")}
        return bool(seen - {p for p, _d, n in recs if n.startswith("->") and n[2:] in bound})

    async def _clients_loop(self) -> None:
        while True:
            try:
                if self._joined():
                    self.codex_bin()  # a binary that vanished (an upgrade): found again for the tiers
                    await self.refresh_clients()
                    self._prune_holds(self.now())
                    self._rebind_ready()
                    self.refresh_tiers()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("codex client check failed")
            await asyncio.sleep(CLIENTS_POLL_S)

    # ------------------------------------------------------ thread proof
    def on_joined(self, p: Participant, nonce: str, fresh: bool) -> None:
        if p.harness != "codex":
            return
        tid = thread_of(p)
        # a join that kept its thread proof (the same MCP process) or with proofs off is
        # evidence at once; a join from another MCP process only once its thread proof passes
        proven = bool(p.thread_proof) or not self.cfg.codex.require_thread_proof
        o = self.orphans.pop(p.id, None)
        if o is not None:
            # joined again inside the restart grace window (the join re-bound it to the
            # joining MCP server's app-server; memberships kept)
            if proven:
                self._reconnected(p, o, "join")
            else:
                self._rejoins[p.id] = o  # "reconnected" only once the thread is proven (_prove)
        if proven:
            self._clear_ended(tid, "join")  # a join from this thread: it runs
        if self.cfg.codex.require_thread_proof and not p.thread_proof:
            old = self._proofs.pop(p.id, None)
            if old is not None:
                old.cancel()
            try:
                t = asyncio.get_running_loop().create_task(self._prove(p.id, thread_of(p), nonce))
            except RuntimeError:
                return
            self._proofs[p.id] = t
            self._verifying.add(p.id)
        # a brand-new thread may not be in the loaded list yet, and nobody has
        # looked for its TUI: do both now rather than at the next poll
        self._spawn(self._after_join())

    async def _after_join(self) -> None:
        await self.poll()
        with contextlib.suppress(Exception):
            await self.refresh_clients()
        self.refresh_tiers()

    def _reprove(self, p: Participant) -> None:
        """One more proof try (at a turn end, or when the link comes up), up to
        PROOF_RETRY_MAX: a join inside a long turn is only readable once that
        turn is over (0.156.1 lists an in-progress turn without its items)."""
        if (
            p.harness != "codex"
            or not self.cfg.codex.require_thread_proof
            or p.thread_proof
            or not p.bind_nonce
            or not p.active
            or p.id in self._proofs
        ):
            return
        key = (p.id, p.bind_nonce)
        n = self._proof_tries.get(key, 0)
        if n >= PROOF_RETRY_MAX:
            return
        self._proof_tries[key] = n + 1
        try:
            t = asyncio.get_running_loop().create_task(
                self._prove(p.id, thread_of(p), p.bind_nonce, (PROOF_RETRY_DELAY_S,), report=False)
            )
        except RuntimeError:
            return
        self._proofs[p.id] = t

    async def _prove(
        self,
        participant_id: int,
        tid: str,
        nonce: str,
        schedule: tuple[float, ...] | None = None,
        *,
        report: bool = True,
    ) -> None:
        start = self.now()
        needle = f"yk:j{nonce}"
        try:
            for n, at in enumerate(PROOF_AT_S if schedule is None else schedule, 1):
                await asyncio.sleep(max(0.0, start + at - self.now()))
                p = self.st.store.get_participant(participant_id)
                if p is None or not p.active or p.bind_nonce != nonce:
                    return
                if p.thread_proof:
                    return
                try:
                    th = await self._read(tid, include_turns=True)
                except Exception:
                    continue
                if join_proven(th, needle):
                    joined = self.st.store.joins_since_thread_proof(participant_id)
                    held = bool(joined) and self.clients is None
                    self.st.store.update_participant(participant_id, thread_proof=1)
                    self.st.store.add_event(
                        "bind",
                        participant_id=participant_id,
                        data={
                            "what": "thread_proof",
                            "ok": True,
                            "attempt": n,
                            **({"notice": "held"} if held else {}),
                        },
                    )
                    log.info("codex participant %d: thread proof ok (attempt %d)", participant_id, n)
                    o = self._rejoins.pop(participant_id, None)
                    if o is not None:  # a re-join after a daemon restart, now proven: the notice
                        self._reconnected(self.st.store.get_participant(participant_id) or p, o, "join")
                    elif self._clear_ended(tid, "thread proof"):
                        self._sync_status(tid)
                    self._verifying.discard(participant_id)
                    self.refresh_tiers()
                    if not held:
                        self._announce_verified(participant_id, joined)
                    return
            if report:
                self.st.store.add_event(
                    "bind", participant_id=participant_id, data={"what": "thread_proof", "ok": False}
                )
                log.warning(
                    "codex participant %d: thread proof not found yet (retried at turn ends)", participant_id
                )
            self._verifying.discard(participant_id)
            self.refresh_tiers()
        finally:
            if self._proofs.get(participant_id) is asyncio.current_task():
                self._proofs.pop(participant_id, None)
                self._verifying.discard(participant_id)

    def _announce_verified(self, participant_id: int, joined: set[int]) -> None:
        """A passed thread proof: one info notice with the tier it has now in each room whose
        join line said "verifying..." since the session's last passed proof (``joined``: nothing
        else says it moved on). None for a membership kept across an MCP or daemon restart:
        its join was announced before."""
        p = self.st.store.get_participant(participant_id)
        label = tier_label(p.tier, p.tier_note) if p is not None else "-"  # rows are never deleted
        self._run(
            [
                Notice(m.room_id, "info", f"{m.screen_name} is verified: {label}")
                for m in self.st.store.participant_memberships(participant_id)
                if m.id in joined
            ]
        )

    # ------------------------------------------------------- session end
    def _clear_ended(self, tid: str, why: str) -> bool:
        """The thread runs again after its SessionEnd (evidence: ``why``). Its
        link statuses count again; the liveness guard still decides whether
        a TUI is watching. True if it was ended."""
        if self.ended.pop(tid, None) is None:
            return False
        self.ended_gone.pop(tid, None)
        p = self._participant(tid)
        if self.st is not None:
            self.st.store.add_event(
                "codex_session",
                participant_id=p.id if p else None,
                data={"what": "running_again", "why": why},
            )
        log.info(
            "codex participant %s: thread running again after its SessionEnd (%s)", p.id if p else "-", why
        )
        self._soon(self.refresh_tiers)
        return True

    def _sync_status(self, tid: str) -> None:
        """Apply the link's view of the thread to its member (e.g. after the
        view was ignored while the thread was ended or its server restarting)."""
        v = self.thread_view(tid)
        if v is not None:
            self._apply_status(tid, v)

    # ---------------------------------------------------- daemon restarts
    def _on_watched_server(self, tid: str, now: float) -> bool:
        """Was this thread loaded on the control socket's app-server (now, or
        on the one the link just lost)? Only then can its agent's death be a
        daemon restart (an embedded TUI that quits is simply gone)."""
        if self.link_state == "up" and tid in self.loaded:
            return True
        lost = self.lost
        return lost is not None and now - lost[0] <= self.cfg.codex.restart_grace_s and tid in lost[1]

    def defer_end(self, p: Participant) -> bool:
        """The liveness check found ``p``'s app-server gone. A daemon restart
        (an auto-update) looks exactly like that, and the TUI reconnects to the
        new daemon with the same thread: keep the session for
        ``restart_grace_s`` and re-bind it if its thread shows up on the new
        app-server; after that, end it as before (False)."""
        if p.harness != "codex" or self.st is None:
            return False
        grace = self.cfg.codex.restart_grace_s
        now = self.now()
        o = self.orphans.get(p.id)
        if o is None:
            if grace <= 0 or not self._on_watched_server(thread_of(p), now):
                return False
            o = self.orphans[p.id] = Orphan(now, p.agent_pid, p.agent_start)
            if thread_of(p) in self.ended:
                self.ended_gone.setdefault(thread_of(p), now)  # its server died: loaded again = back
            self.st.store.add_event(
                "codex_restart", participant_id=p.id, data={"what": "app_server_gone", "grace_s": grace}
            )
            log.info(
                "codex participant %d: its app-server is gone; waiting up to %.0f s for a restarted"
                " daemon to load its thread",
                p.id,
                grace,
            )
            self.refresh_tiers()
            self._spawn(self.try_rebind())
            return True
        if now - o.since < grace:
            return True
        self._rebind_ready()  # a last look with what is known
        if p.id not in self.orphans:
            return True  # re-bound just now
        del self.orphans[p.id]
        self.st.store.add_event(
            "codex_restart", participant_id=p.id, data={"what": "gave_up", "after_s": round(now - o.since, 1)}
        )
        log.info("codex participant %d: its thread didn't come back within %.0f s; ending it", p.id, grace)
        return False

    def on_mcp_hello(self, ident: Any, mine: list[Participant]) -> None:
        """A verified ``mcp.hello``. A Codex one names a live app-server that runs
        switchboard's MCP servers (remembered: after a restart, the new daemon's).
        If it is a restarting session's own MCP process (a reconnect), its
        credentials are still valid: re-bind it to that process's app-server.
        An identity from another host (a remote Codex, §27.7) is none of this
        adapter's business: its pids are pids on that host."""
        if getattr(ident, "host", ""):
            return
        if getattr(ident, "harness", None) == "codex" and ident.agent_pid:
            self.fresh_agents[(ident.agent_pid, ident.agent_start)] = self.now()
            while len(self.fresh_agents) > FRESH_AGENTS_MAX:
                self.fresh_agents.pop(next(iter(self.fresh_agents)))
        for p in mine:
            o = self.orphans.get(p.id)
            if o is None or p.harness != "codex":
                continue
            if (
                getattr(ident, "harness", None) == "codex"
                and ident.agent_pid
                and self._local_view().alive(ident.agent_pid, ident.agent_start)
            ):
                self._rebind(p, o, (ident.agent_pid, ident.agent_start), "mcp_hello")
        if self.orphans:
            self._spawn(self.try_rebind())

    async def try_rebind(self) -> None:
        """Re-bind restarting sessions whose thread is loaded on the control
        socket's (new) app-server: a fresh lsof first, to name that server."""
        if not self.orphans or self.st is None or not self.loaded_fresh():
            return
        with contextlib.suppress(Exception):
            await self.refresh_clients()
        self._rebind_ready()

    def _rebind_ready(self) -> None:
        if not self.orphans or self.st is None:
            return
        now = self.now()
        agent: tuple[int, float | None] | None = None
        looked = False
        for pid_, o in list(self.orphans.items()):
            p = self.st.store.get_participant(pid_)
            if p is None or not p.active or not self.st.store.participant_memberships(p.id):
                self.orphans.pop(pid_, None)
                continue
            if not self.attached(thread_of(p), now):
                continue  # not (yet) loaded on the server the link talks to now
            if not looked:
                agent, looked = self._new_agent(), True
            if agent is None:
                return  # which process that is isn't clear (yet): look again later
            self._rebind(p, o, agent, "loaded")

    def _new_agent(self) -> tuple[int, float | None] | None:
        """The app-server now serving the control socket: the one Codex
        app-server lsof shows listening on it; failing that (no such look,
        e.g. no lsof), the one Codex app-server whose MCP servers said hello
        since the link was lost. Either way a Codex ``app-server`` of this user
        (never a TUI, ``exec`` or other codex process). None unless exactly one."""
        found: set[tuple[int, float | None]] = set()
        for pid in self.server_pids:
            a = _codex_app_server(pid)
            if a is not None:
                found.add(a)
        if not found and self.lost is not None:
            for (pid, start), seen in self.fresh_agents.items():
                a = _codex_app_server(pid, start) if seen >= self.lost[0] else None
                if a is not None:
                    found.add(a)
        return next(iter(found)) if len(found) == 1 else None

    def _rebind(self, p: Participant, o: Orphan, agent: tuple[int, float | None], via: str) -> None:
        """Re-bind a restarting session to the app-server now running its
        thread. Memberships, credentials (they belong to the MCP process),
        the thread proof and queued messages are kept."""
        if self.st is None:
            return
        self.orphans.pop(p.id, None)
        p = self.st.store.update_participant(p.id, agent_pid=agent[0], agent_start=agent[1])
        agents = getattr(self.st, "agents", None)
        if agents is not None:
            agents.refresh_index()  # hooks from the new app-server's children resolve now
        self._reconnected(p, o, via)

    def _reconnected(self, p: Participant, o: Orphan, via: str) -> None:
        tid = thread_of(p)
        now = self.now()
        self.st.store.add_event(
            "codex_restart",
            participant_id=p.id,
            data={"what": "rebound", "via": via, "after_s": round(now - o.since, 1)},
        )
        log.info(
            "codex participant %d re-bound after a daemon restart (%s, after %.1f s)",
            p.id,
            via,
            now - o.since,
        )
        self.backoff.pop(p.id, None)
        self.reroutes.pop(p.id, None)
        self._clear_ended(tid, "daemon restart")  # the old daemon's SessionEnd
        self._sync_status(tid)
        acts: list[Any] = [
            Notice(m.room_id, "info", f"{m.screen_name} reconnected after a Codex daemon restart")
            for m in self.st.store.participant_memberships(p.id)
        ]
        self._run(acts)
        self.refresh_tiers()
        self._run(self.st.engine.evaluate_participant(p.id))

    # ------------------------------------------------------ codex binary
    def codex_bin(self) -> str | None:
        """The codex binary for ``codex queue``: the cached one while it is
        still there, else looked up again (with the same checks)."""
        if bin_usable(self.bin_path):
            return self.bin_path
        # a cached path that vanished: look again now; nothing found last time: at most every BIN_RETRY_S
        self._resolve_bin(force=self.bin_path is not None)
        return self.bin_path

    def _resolve_bin(self, *, force: bool = False) -> None:
        """Look ``codex`` up again (``force``: now; else at most every
        BIN_RETRY_S). Its version is re-read too: an in-place upgrade (npm)
        keeps the path. A configured absolute path that is gone and replaced by
        ``codex`` on PATH is recorded and shown as a fallback."""
        now = self.now()
        if not force and self._bin_tried_at is not None and now - self._bin_tried_at < BIN_RETRY_S:
            return
        self._bin_tried_at = now
        conf = self.cfg.codex.bin
        new = resolve_codex(conf)
        ver = bin_version(new) if new else None
        fell_back = new is not None and os.path.isabs(conf) and new != os.path.realpath(conf)
        if (new, ver, fell_back) == (self.bin_path, self.bin_version, self.bin_fallback):
            return
        old, self.bin_path = self.bin_path, new
        prev, self.bin_version = self.bin_version, ver
        self.bin_fallback = fell_back
        log.info(
            "codex binary %s%s%s",
            f"found (version {ver or '?'})" if new else "not found",
            " on PATH (the configured path is gone)" if fell_back else "",
            f"; it was version {prev or '?'}" if old else "",
        )
        if self.st is not None:
            self.st.store.add_event(
                "codex_bin",
                data={
                    "found": new is not None,
                    "version": ver or "",
                    "was": prev or "",
                    "changed": old is not None,
                    "fell_back": fell_back,
                },
            )
            self.st.info.codex_link = self.status_summary()

    # ------------------------------------------------------------- tiers
    def refresh_tiers(self) -> None:
        """Re-derive every joined Codex member's tier (link, loaded list, proof, liveness)."""
        if self.st is None:
            return
        store, engine = self.st.store, self.st.engine
        acts: list[Any] = []
        joined = self._joined()
        ids = {p.id for p in joined}
        for pid_ in [x for x in self.orphans if x not in ids]:
            self.orphans.pop(pid_, None)  # left, kicked or ended meanwhile
        for pid_ in [x for x in self._rejoins if x not in ids]:
            self._rejoins.pop(pid_, None)
        self._verifying &= ids
        for p in joined:
            tier, note = self.tier(p)
            if (tier, note) != (p.tier, p.tier_note):
                store.update_participant(p.id, tier=tier, tier_note=note)
                store.add_event("tier", participant_id=p.id, data={"tier": tier, "note": note or ""})
                acts += engine.evaluate_participant(p.id)
                acts += [Snapshot(m.room_id) for m in store.participant_memberships(p.id)]
        self._run(acts)
        if self._held_notices and self.clients is not None:
            self._post_held_notices(joined)
        self.st.info.codex_link = self.status_summary()

    def _post_held_notices(self, joined: list[Participant]) -> None:
        """After the first look for a TUI: the "is verified" notices of proofs that passed
        before it (the lsof after the join, which a fast proof can beat), held so the tier
        comes from a look rather than "detached?" on no evidence. Read from the events, so a
        held notice survives a broker restart. A session whose proof was reset since (a join
        from a new MCP server) waits for its next proof, which still finds those joins."""
        self._held_notices = False
        store = self.st.store
        for p in joined:
            if not p.thread_proof:
                continue
            ms = store.joins_since_thread_proof(p.id)
            if ms:
                store.add_event("bind", participant_id=p.id, data={"what": "verified_notice"})
                self._announce_verified(p.id, ms)

    # ------------------------------------------------------------ start/stop
    async def start(self, runner: Any) -> None:
        self.runner = runner
        self.clock = runner.state.clock
        self._resolve_bin(force=True)
        self.autostart = daemon_auto_start(self.cfg)
        self._set_link("down", "starting")
        loop = asyncio.get_running_loop()
        self._main = [loop.create_task(self._link_loop()), loop.create_task(self._clients_loop())]

    async def stop(self) -> None:
        tasks = self._main + list(self._tasks) + list(self._proofs.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        rpc, self.rpc = self.rpc, None
        if rpc is not None:
            with contextlib.suppress(Exception):
                await rpc.close()


def _codex_app_server(pid: int, start: float | None = None) -> tuple[int, float | None] | None:
    """(pid, start) if ``pid`` is a live Codex ``app-server`` of this user
    (started at ``start``, when given), else None."""
    i = proc.info(pid)
    if i is None or i.uid != os.getuid() or (start is not None and not proc.same_start(i.start, start)):
        return None
    argv = proc.argv(pid, i.start)
    return (pid, i.start) if match_agent(argv) == "codex" and is_app_server_argv(argv) else None


def _run_lsof(lsof: str) -> str:
    args = LSOF_ARGS_LINUX if sys.platform.startswith("linux") else ("-n", "-P", "-U", "-F", "pdn")
    r = subprocess.run([lsof, *args], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    return r.stdout


def _hms(t: float | None) -> str:
    import time as _t

    return _t.strftime("%H:%M:%S", _t.localtime(t)) if t else "?"
