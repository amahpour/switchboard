"""The Codex adapter's pure parts (DESIGN.md §9.3): tiers, routing, the liveness
guard, the queue argv and guard, lsof parsing, and engine integration with a
FakeClock (no daemon, no I/O)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from conftest import FakeClock
from engine_world import World
from switchboard.adapters import codex as cx
from switchboard.adapters.codex import AgentClients, Clients, CodexAdapter
from switchboard.broker import proc
from switchboard.config import Config, CodexCfg
from switchboard.models import Push

TID = "019a0000-0000-7000-8000-000000000001"


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def ad(w: World) -> CodexAdapter:
    a = w.engine.adapters["codex"]
    assert isinstance(a, CodexAdapter)
    a.clock = w.clock
    return a


def codex(w: World, *, status: str = "idle", proof: bool = True, hooks: bool = True, tid: str = TID,
          self_agent: bool = False):
    p, m = w.agent("codex-1", harness="codex", status=status, hooks=hooks)
    upd = {"session_key": f"codex:{tid}", "thread_proof": int(proof), "approval_mode": "prompting"}
    if self_agent:  # a live agent process (the queue tier checks it)
        me = proc.info(os.getpid())
        upd.update(agent_pid=os.getpid(), agent_start=me.start if me else None)
        # its lsof look: the TUI itself (an embedded app-server)
        ad(w).agent_clients[os.getpid()] = AgentClients(me.start if me else None, w.clock.now(), True)
    p = w.store.update_participant(p.id, **upd)
    return p, m


def attach(w: World, tid: str = TID, view: str | None = "idle", clients: bool = True) -> CodexAdapter:
    a = ad(w)
    a.link_state = "up"
    a.loaded = {tid}
    a.loaded_at = w.clock.now()
    a.clients = Clients(clients, w.clock.now(), 1 if clients else 0, None if clients else "no Codex TUI attached")
    if view is not None:
        a.view[tid] = (view, w.clock.now())
    return a


def pushes(w: World) -> list[Push]:
    return [x for x in w.take() if isinstance(x, Push)]


# ------------------------------------------------------------------ tiers
def test_tier_needs_the_thread_proof(w: World) -> None:
    p, _m = codex(w, proof=False)
    attach(w)
    assert ad(w).tier(p) == ("mcp-only", "unverified thread")
    p = w.store.update_participant(p.id, thread_proof=1)
    assert ad(w).tier(p) == ("codex:daemon", None)
    assert ad(w).tier(None) == ("mcp-only", "unverified thread")


def test_tier_without_proof_requirement(tmp_path: Path, clock: FakeClock) -> None:
    cfg = Config().replace(codex=CodexCfg(require_thread_proof=False)).with_delivery(quiet_s=0.0, max_hold_s=0.0)
    w = World(tmp_path, clock, cfg)
    p, _m = codex(w, proof=False)
    attach(w)
    assert ad(w).tier(p) == ("codex:daemon", None)


def test_daemon_tier_shows_detached_when_no_tui_is_attached(w: World) -> None:
    p, _m = codex(w)
    attach(w, clients=False)
    assert ad(w).tier(p) == ("codex:daemon", "detached?")
    a = attach(w)
    a.clients = Clients(True, w.clock.now() - cx.CLIENTS_FRESH_S - 1, 1)  # a stale lsof read
    assert a.tier(p) == ("codex:daemon", "detached?")


def test_a_stale_loaded_list_is_not_attached(w: World) -> None:
    p, _m = codex(w)
    a = attach(w)
    w.clock.advance(cx.LOADED_FRESH_S + 1)
    a.clients = Clients(True, w.clock.now(), 1)
    assert not a.attached(TID)
    assert a.tier(p)[0] != "codex:daemon"


def test_queue_tier_when_the_thread_is_not_loaded(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, _m = codex(w, self_agent=True)
    a = attach(w)
    a.loaded = set()
    a.bin_path = "/usr/bin/true"
    assert a.tier(p) == ("codex:queue", None)
    monkeypatch.setattr(cx, "resolve_codex", lambda _name: pytest.fail("tier() looked the binary up"))
    a.bin_path = None
    assert a.tier(p)[0] == "mcp-only"


def test_queue_guard_never_lets_codex_queue_start_a_daemon(w: World, tmp_path: Path) -> None:
    a = ad(w)
    a.bin_path = "/usr/bin/true"
    a.sock = str(tmp_path / "no-such.sock")
    a.link_state = "down"
    for autostart in (True, None):  # on, or the config can't be read: refuse
        a.autostart = autostart
        ok, remote, why = a.queue_guard()
        assert not ok and remote is None and "daemon_auto_start" in (why or "")
    a.autostart = False
    assert a.queue_guard() == (True, None, None)  # no daemon, no auto-start: the bare form
    a.link_state = "up"
    assert a.queue_guard() == (True, a.sock, None)  # a live socket: always --remote it
    a.link_state = "down"
    (tmp_path / "no-such.sock").write_text("")  # a socket file nobody answers on
    assert a.queue_guard()[0] is False


# ---------------------------------------------------------------- routing
def test_idle_wake_is_a_turn_start(w: World) -> None:
    p, m = codex(w)
    attach(w)
    w.human("please look at parse_port")
    [push] = pushes(w)
    assert push.path == "turn_start" and push.text.startswith("[switchboard]")
    b = w.store.get_batch(push.batch_id)
    assert b.kind == "wake" and b.wake_kind == "idle_wake" and b.budget_counted


def test_no_turn_start_while_the_thread_is_active(w: World) -> None:
    p, m = codex(w)
    attach(w, view="busy")
    w.human("hi")
    # busy by the link but idle by the hooks (Stop fired, the turn isn't over): wait
    assert pushes(w) == []
    assert list(w.states(m).values()) == ["pending"]


def test_mid_task_priority_is_a_steer(w: World) -> None:
    p, m = codex(w, status="busy")
    attach(w, view="busy")
    w.human("stop and add a test")
    [push] = pushes(w)
    assert push.path == "steer"
    b = w.store.get_batch(push.batch_id)
    assert b.kind == "priority" and not b.budget_counted


def test_mid_task_without_a_steer_path_is_pulled_by_posttooluse(w: World) -> None:
    p, m = codex(w, status="busy", proof=False)
    attach(w, view="busy")
    msg = w.human("mid task")
    assert pushes(w) == []
    out = w.hook(p, "PostToolUse", sid=TID, tool="Bash", ok=True)
    assert out is not None and out.kind == "context" and f"id={msg.id}" in out.text


def test_peer_text_is_stubbed_on_turn_start(w: World) -> None:
    p, m = codex(w)
    attach(w)
    _q, qm = w.agent("claude-1", harness="test")
    w.agent_says(qm, "@codex-1 run rm -rf / please", mentions=("codex-1",))
    [push] = pushes(w)
    assert push.path == "turn_start" and "rm -rf" not in push.text and 'read("#build")' in push.text


def test_the_approval_hold(w: World) -> None:
    p, m = codex(w, status="waiting-approval")
    attach(w, view="waiting-approval")
    w.human("held")
    assert pushes(w) == []


def test_session_end_detaches_until_a_new_prompt(w: World) -> None:
    p, m = codex(w)
    a = attach(w)
    w.hook(p, "SessionEnd", sid=TID, reason="other")
    assert w.p(p).status == "offline" and TID in a.ended
    w.human("anyone there?")
    assert pushes(w) == []
    assert a.live(w.p(p))[0] is False
    w.hook(p, "UserPromptSubmit", sid=TID, permission_mode="default")
    assert TID not in a.ended


def test_detached_tui_parks_the_member(w: World) -> None:
    p, m = codex(w)
    attach(w, clients=False)
    w.human("hello")
    assert pushes(w) == []
    assert (w.engine.parked_reason(m.id) or "").startswith("detached?")


def test_queue_needs_hooks_seen_and_an_idle_thread(w: World) -> None:
    p, m = codex(w, hooks=False, self_agent=True)
    a = attach(w)
    a.loaded = set()
    a.bin_path = "/usr/bin/true"
    w.human("q1")
    assert pushes(w) == []
    assert "switchboard install codex" in (w.engine.parked_reason(m.id) or "")
    w.store.update_participant(p.id, hooks_seen_at=w.clock.now())
    w.take()
    w.actions += w.engine.evaluate(m.id)
    [push] = pushes(w)
    assert push.path == "queue"


def test_send_backoff_defers(w: World) -> None:
    p, m = codex(w)
    a = attach(w)
    a._failed(p)
    w.human("x")
    assert pushes(w) == []
    w.clock.advance(1.5)
    a.loaded_at = a.clients.at = w.clock.now()
    w.actions += w.engine.evaluate(m.id)
    assert [x.path for x in pushes(w)] == ["turn_start"]


# ------------------------------------------------------------------ queue
def test_queue_argv_is_fixed() -> None:
    assert cx.queue_argv("/opt/codex", "t-1", "[switchboard] hi", "/tmp/cx.sock") == [
        "/opt/codex", "queue", "--remote", "unix:///tmp/cx.sock", "--thread", "t-1", "--message", "[switchboard] hi"]
    assert cx.queue_argv("/opt/codex", "t-1", "[switchboard] hi", None) == [
        "/opt/codex", "queue", "--thread", "t-1", "--message", "[switchboard] hi"]
    from switchboard import guardrails

    assert guardrails.find_forbidden(cx.queue_argv("/opt/codex", "t", "x", "/s")) == []


def test_queue_env_is_minimal(tmp_path: Path) -> None:
    env = cx.queue_env("/opt/bin/codex", Config())
    assert set(env) == {"PATH", "HOME"} and env["PATH"] == "/usr/bin:/bin:/opt/bin"
    env = cx.queue_env("/opt/bin/codex", Config().replace(codex=CodexCfg(home=str(tmp_path))))
    assert env["CODEX_HOME"] == str(tmp_path)


def test_daemon_auto_start_is_read_only_parsed(tmp_path: Path) -> None:
    """Off only when explicitly false: an absent key or file counts as on (fail closed)."""
    cfg = Config().replace(codex=CodexCfg(home=str(tmp_path)))
    assert cx.daemon_auto_start(cfg) is True  # no file: Codex's default, unknown
    (tmp_path / "config.toml").write_text('[features]\ndaemon_auto_start = true\n')
    assert cx.daemon_auto_start(cfg) is True
    (tmp_path / "config.toml").write_text('model = "x"\n[features]\napps = true\n')
    assert cx.daemon_auto_start(cfg) is True  # key absent
    (tmp_path / "config.toml").write_text('[features]\ndaemon_auto_start = false\n')
    assert cx.daemon_auto_start(cfg) is False
    # the selected default profile wins
    (tmp_path / "config.toml").write_text('profile = "p"\n[features]\ndaemon_auto_start = false\n'
                                          '[profiles.p.features]\ndaemon_auto_start = true\n')
    assert cx.daemon_auto_start(cfg) is True
    (tmp_path / "config.toml").write_text("not = [toml")
    assert cx.daemon_auto_start(cfg) is None


def test_resolve_bin(tmp_path: Path) -> None:
    f = tmp_path / "codex"
    f.write_text("#!/bin/sh\n")
    assert cx.resolve_bin(str(f)) is None  # not executable
    f.chmod(0o755)
    assert cx.resolve_bin(str(f)) == os.path.realpath(f)
    assert cx.resolve_bin(str(tmp_path / "missing")) is None
    assert cx.resolve_bin("/usr/bin/true") == os.path.realpath("/usr/bin/true")  # root-owned is fine
    assert stat.S_IMODE(f.stat().st_mode) == 0o755
    assert cx.resolve_bin("bin/codex") is None  # relative with a slash: never


def test_resolve_bin_skips_workspace_and_temp_path_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare name is looked up on PATH, but never in a git work tree (an agent's
    workspace: e.g. an activated .venv/bin), a temp dir or a relative entry."""
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    planted = ws / ".venv" / "bin"
    planted.mkdir(parents=True)
    safe = tmp_path / "safe" / "bin"
    safe.mkdir(parents=True)
    for d in (planted, safe):
        (d / "codex").write_text("#!/bin/sh\n")
        (d / "codex").chmod(0o755)
    monkeypatch.setattr(cx, "_temp_roots", lambda: set())  # tmp_path itself is under the temp dir
    monkeypatch.setenv("PATH", os.pathsep.join(["rel/bin", str(planted), str(safe)]))
    assert cx.unsafe_bin_dir(str(planted)) and cx.unsafe_bin_dir("rel/bin") and not cx.unsafe_bin_dir(str(safe))
    assert cx.resolve_bin("codex") == os.path.realpath(safe / "codex")
    # a safe-looking entry that links into the workspace is refused too
    (safe / "codex").unlink()
    (safe / "codex").symlink_to(planted / "codex")
    assert cx.resolve_bin("codex") is None
    monkeypatch.undo()
    assert cx.unsafe_bin_dir(str(tmp_path))  # under the temp dir


# ------------------------------------------------------------------ lsof
LSOF = """p100
f7
d0xserverlisten
n/private/tmp/codex-daemon-501/abc
f9
d0xserveraccepted1
n/private/tmp/codex-daemon-501/abc
f10
d0xserveraccepted2
n/private/tmp/codex-daemon-501/abc
p200
f5
d0xtuiend
n->0xserveraccepted1
f6
d0xother
n->0xsomethingelse
p300
f4
d0xswitchboardend
n->0xserveraccepted2
p400
f3
d0xunrelated
n->0xnotours
"""


def test_socket_peers_from_lsof() -> None:
    recs = cx.parse_lsof(LSOF)
    assert (200, "0xtuiend", "->0xserveraccepted1") in recs
    servers, clients = cx.socket_peers(recs, {"/private/tmp/codex-daemon-501/abc", "/link/cx.sock"})
    assert servers == {100} and clients == {200, 300}
    assert cx.socket_peers(recs, {"/elsewhere"}) == (set(), set())
    assert cx.bound_names(recs, 100) == {"/private/tmp/codex-daemon-501/abc"}
    assert cx.bound_names(recs, 200) == set()


# Linux lsof 4.95 (`+E -F pfdin`), recorded in the switchboard-test container: pid 100
# listens on (f3) and accepted two ends (f4, f5) of /tmp/cx.sock; 200 (a TUI) and
# 300 (switchboard) hold the client ends; 200 also has an unrelated socketpair; 400 an
# unbound socket; 500 is connected to some other server.
LSOF_LINUX = """p100
f3
d0x00000000e8dbcd26
i1000
n/tmp/cx.sock type=STREAM
f4
d0x00000000b216a37a
i1001
n/tmp/cx.sock type=STREAM ->INO=2001 200,codex,4u
f5
d0x00000000b216a37b
i1002
n/tmp/cx.sock type=STREAM ->INO=3001 300,python3,9u
p200
f4
d0x000000007ccb49e8
i2001
ntype=STREAM ->INO=1001 100,codex,4u
f6
d0x00000000ae7385c9
i2002
ntype=STREAM ->INO=2003 200,codex,7u
f7
d0x000000003f453f37
i2003
ntype=STREAM ->INO=2002 200,codex,6u
p300
f9
d0x00000000c1ca34eb
i3001
ntype=STREAM ->INO=1002 100,codex,5u
p400
f3
d0x00000000c1ca34ec
i4001
ntype=STREAM
p500
f3
d0x00000000c1ca34ed
i5001
ntype=SEQPACKET ->INO=9999 1,systemd,12u
"""


def test_socket_peers_from_linux_lsof() -> None:
    recs = cx.parse_lsof(LSOF_LINUX)
    assert (100, "ino:1000", "/tmp/cx.sock") in recs
    assert (200, "ino:2001", "->ino:1001") in recs
    assert (400, "ino:4001", "") in recs
    servers, clients = cx.socket_peers(recs, {"/tmp/cx.sock", "/link/cx.sock"})
    assert servers == {100} and clients == {200, 300}
    assert cx.socket_peers(recs, {"/elsewhere"}) == (set(), set())
    assert cx.bound_names(recs, 100) == {"/tmp/cx.sock"}
    assert cx.bound_names(recs, 200) == set()


def test_linux_lsof_names() -> None:
    rec = cx._linux_unix_rec
    assert rec("0xa", "/p/x.sock type=STREAM", "1") == ("ino:1", "/p/x.sock")
    assert rec("0xa", "/p/x.sock type=STREAM ->INO=7 12,codex,4u", "1") == ("ino:1", "/p/x.sock")
    assert rec("0xa", "type=DGRAM ->INO=7 12,codex,4u", "1") == ("ino:1", "->ino:7")
    assert rec("0xa", "@abstract type=STREAM", "1") == ("ino:1", "@abstract")
    assert rec("0xa", "type=STREAM", "1") == ("ino:1", "")
    assert rec("0xa", "something else", "1") == ("0xa", "something else")  # unknown shape: as is
    # macOS output has no inode field, so its records are never rewritten
    assert cx.parse_lsof("p1\nf3\nd0xa\nn/p/x.sock type=STREAM\n") == [(1, "0xa", "/p/x.sock type=STREAM")]


@pytest.mark.skipif(cx.lsof_bin() is None, reason="lsof not installed")
def test_real_lsof_sees_a_client_of_our_socket() -> None:
    """The platform's own lsof, through the adapter's arguments and parser:
    this process listens, a child connects, and only the child is a client."""
    import shutil
    import socket
    import subprocess
    import sys
    import tempfile

    # a private 0700 directory under /tmp (short enough for AF_UNIX): no stale or
    # planted file at a guessable shared name can make bind() fail
    d = tempfile.mkdtemp(prefix="yk-lsof-", dir="/tmp")
    path = os.path.join(d, "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen()
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); "
             "print('ok', flush=True); sys.stdin.read()", path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout is not None and child.stdout.readline().strip() == "ok"
            conn, _ = srv.accept()
            recs = cx.parse_lsof(cx._run_lsof(cx.lsof_bin()))
            servers, clients = cx.socket_peers(recs, {path, os.path.realpath(path)})
            assert servers == {os.getpid()}
            assert clients == {child.pid}
            assert cx.bound_names(recs, os.getpid()) & {path, os.path.realpath(path)}
            conn.close()
        finally:
            child.kill()
            child.wait()
    finally:
        srv.close()
        shutil.rmtree(d, ignore_errors=True)


def test_only_a_codex_tui_counts_as_attached() -> None:
    tui = cx.is_tui_argv
    assert tui("/opt/homebrew/Caskroom/codex/0.156.1/bin/codex")
    assert tui("codex --remote unix:///tmp/cx.sock -m gpt-6-luna -c model_reasoning_effort=low")
    assert tui("codex resume 019a0000")
    assert tui("codex --remote unix:///Users/me/.codex/app-server-control/app-server-control.sock")
    for argv in ("codex app-server daemon pid-update-loop", "/x/codex app-server --listen unix:///tmp/a.sock",
                 "codex exec 'fix it'", "codex e hi", "codex queue --thread t --message m",
                 "codex mcp-server", "/usr/bin/python3 -m pytest", "", "claude"):
        assert not tui(argv), argv
    assert cx.is_app_server_argv("codex app-server --listen unix:///tmp/a.sock")
    assert not cx.is_app_server_argv("codex --remote unix:///tmp/a.sock")


def test_client_id_and_thread_ids() -> None:
    assert cx.client_id(42) == "yk-b42"
    assert cx.THREAD_ID_RE.fullmatch(TID)
    assert not cx.THREAD_ID_RE.fullmatch("--dangerously")


def test_a_steer_lands_as_a_same_turn_userpromptsubmit(w: World) -> None:
    """Live 0.156.1: steered input fires UserPromptSubmit with the running turn's id.
    It confirms the steer but is not a new turn (no turn_start, no pull expiry)."""
    p, m = codex(w, status="idle")
    attach(w)
    w.hook(p, "UserPromptSubmit", sid=TID, gen="turn-7", permission_mode="default")
    ad(w).view[TID] = ("busy", w.clock.now())
    msg = w.human("steer me")
    [push] = pushes(w)
    assert push.path == "steer"
    b = w.store.get_batch(push.batch_id)
    token = w.engine.token(b)
    bid_s, mac = token.removeprefix("yk:b").split(".")
    starts = w.store.count_events("turn_start")
    w.hook(p, "UserPromptSubmit", sid=TID, gen="turn-7", tokens=((int(bid_s), mac),), permission_mode="default")
    b = w.store.get_batch(push.batch_id)
    assert b.state == "confirmed" and b.turn_start_at is None
    assert w.store.count_events("turn_start") == starts  # not a new turn
    assert w.p(p).gen == "turn-7" and w.delivery(m, msg)["state"] == "in_context"
    # a new turn id is a new turn
    w.hook(p, "Stop", sid=TID, gen="turn-7")
    w.hook(p, "UserPromptSubmit", sid=TID, gen="turn-8", permission_mode="default")
    assert w.store.count_events("turn_start") == starts + 1


# ------------------------------------------------------------ holds (§9.3)
def test_a_tui_leaving_holds_every_thread_on_that_server(w: World) -> None:
    """lsof can't tell whose TUI left: another TUI on the same daemon must not
    make a thread whose own TUI quit look attached (it lingers 60-65 s)."""
    p, m = codex(w)
    a = attach(w)
    other = "019a0000-0000-7000-8000-0000000000ff"
    a.loaded = {TID, other}
    a.view[other] = ("idle", w.clock.now())
    now = w.clock.now()
    a._track("control", frozenset({11, 12}), now, set(a.loaded), 2)  # first look: 2 TUIs, 2 threads
    assert a.suspect == {} and a.live(p)[0]
    a._track("control", frozenset({12}), now, set(a.loaded), 2)  # one TUI left
    assert set(a.suspect) == {TID, other}
    assert a.live(p) == (False, cx.HOLD_WHY)
    assert a.tier(w.p(p)) == ("codex:daemon", "detached?")
    w.human("anyone?")
    assert pushes(w) == [] and (w.engine.parked_reason(m.id) or "").startswith("detached?")
    # the thread that unloads was the one whose TUI left; the TUIs cover the rest: released
    a.clients = Clients(True, w.clock.now(), 1, None, frozenset({12}))
    a._on_note("thread/closed", {"threadId": other}, w.clock.now())
    assert a.suspect == {} and a.live(w.p(p))[0]


def test_a_hold_is_not_released_while_threads_outnumber_tuis(w: World) -> None:
    p, _m = codex(w)
    a = attach(w)
    x, y = "019a0000-0000-7000-8000-0000000000aa", "019a0000-0000-7000-8000-0000000000bb"
    a.loaded = {TID, x, y}
    a._track("control", frozenset({1, 2, 3}), w.clock.now(), set(a.loaded), 3)
    a._track("control", frozenset({2, 3}), w.clock.now(), set(a.loaded), 3)
    a.clients = Clients(True, w.clock.now(), 1, None, frozenset({3}))  # and another TUI left since
    a._on_note("thread/closed", {"threadId": x}, w.clock.now())
    assert TID in a.suspect  # 1 TUI, 2 threads still loaded: y (or TID) may be an orphan too


def test_first_look_with_more_threads_than_tuis_holds(w: World) -> None:
    p, _m = codex(w)
    a = attach(w)
    a.loaded = {TID, "019a0000-0000-7000-8000-0000000000cc"}
    a._track("control", frozenset({7}), w.clock.now(), set(a.loaded), 2)
    assert TID in a.suspect and not a.live(p)[0]
    # nothing to compare with yet (the loaded list isn't known): no verdict
    a2 = ad(w)
    a2._seen.clear()
    a2.suspect.clear()
    a2._track("control", frozenset(), w.clock.now(), None, 5)
    assert a2._seen == {} and a2.suspect == {}


def test_a_hold_ends_with_the_threads_own_human_or_after_70_s_idle(w: World) -> None:
    p, _m = codex(w)
    a = attach(w)
    a._track("control", frozenset({1, 2}), w.clock.now(), {TID}, 1)
    a._track("control", frozenset({2}), w.clock.now(), {TID}, 1)
    assert not a.live(p)[0]
    # a switchboard prompt (it carries a token) is no evidence; the human's own prompt is
    w.hook(p, "UserPromptSubmit", sid=TID, gen="t1", tokens=((1, "x"),), permission_mode="default")
    assert TID in a.suspect
    w.hook(p, "UserPromptSubmit", sid=TID, gen="t2", permission_mode="default")
    assert TID not in a.suspect
    # held again; this time it just stays idle: 70 s after its last turn ended, it's free
    a._track("control", frozenset({2, 3}), w.clock.now(), {TID}, 1)
    a._track("control", frozenset({3}), w.clock.now(), {TID}, 1)
    a._set_view(TID, "busy", w.clock.now())
    w.clock.advance(cx.DROP_HOLD_S + 5)
    a.loaded_at = a.clients.at = w.clock.now()
    assert not a.live(w.p(p))[0]  # busy (maybe headless): still held
    a._set_view(TID, "idle", w.clock.now())
    w.clock.advance(cx.DROP_HOLD_S - 5)
    a.loaded_at = a.clients.at = w.clock.now()
    assert not a.live(w.p(p))[0]
    w.clock.advance(6)
    a.loaded_at = a.clients.at = w.clock.now()
    assert a.live(w.p(p))[0]
    a._prune_holds(w.clock.now())
    assert TID not in a.suspect
    # an Esc (Interrupt) is the human too
    a._track("control", frozenset({3, 4}), w.clock.now(), {TID}, 1)
    a._track("control", frozenset({4}), w.clock.now(), {TID}, 1)
    w.hook(p, "Interrupt", sid=TID)
    assert TID not in a.suspect


def test_queue_tier_app_server_needs_its_own_tui(w: World) -> None:
    """A queue-tier thread on another app-server: that server outlives its TUI."""
    p, m = codex(w, self_agent=True)
    a = attach(w)
    a.loaded = set()
    a.bin_path = "/usr/bin/true"
    me = proc.info(os.getpid())
    a.agent_clients[os.getpid()] = AgentClients(me.start, w.clock.now(), False, True, 0, "no Codex TUI attached")
    assert a.tier(w.p(p)) == ("codex:queue", "detached?")
    a.agent_clients[os.getpid()] = AgentClients(me.start, w.clock.now(), True, True, 1)
    assert a.tier(w.p(p)) == ("codex:queue", None)
    w.clock.advance(cx.CLIENTS_FRESH_S + 1)  # a stale look: can't tell
    assert a.live(w.p(p)) == (False, "can't tell whether a Codex TUI is attached")
    a.agent_clients.clear()
    assert a.live(w.p(p))[0] is False


# ------------------------------------------------- steers and re-routes
def test_a_turn_that_refused_a_steer_gets_hook_context(w: World) -> None:
    p, m = codex(w, status="busy")
    a = attach(w, view="busy")
    a.no_steer.add(TID)  # a steer was re-routed and the thread still says active
    msg = w.human("mid task, again")
    assert pushes(w) == []
    out = w.hook(p, "PostToolUse", sid=TID, tool="Bash", ok=True)
    assert out is not None and f"id={msg.id}" in out.text
    # a status change (the turn ended) clears it
    a._set_view(TID, "idle", w.clock.now())
    assert TID not in a.no_steer


def test_uncounted_reroutes_back_off_after_a_few(w: World) -> None:
    p, m = codex(w)
    a = attach(w)
    for _ in range(cx.REROUTE_FREE):
        a._rerouted(p)
    assert not a._backing_off(p, w.clock.now())
    a._rerouted(p)
    assert a._backing_off(p, w.clock.now())
    w.clock.advance(cx.REROUTE_BACKOFF_S[0] + 0.1)
    assert not a._backing_off(p, w.clock.now())
    for _ in range(20):
        a._rerouted(p)
    until, n = a.reroutes[p.id]
    assert until - w.clock.now() <= cx.REROUTE_BACKOFF_S[1]


def test_no_idle_wake_before_the_thread_status_is_known(w: World) -> None:
    p, m = codex(w)
    attach(w, view=None)
    w.human("hello")
    assert pushes(w) == []
    assert list(w.states(m).values()) == ["pending"]


# ------------------------------------------- session end, daemon restarts
def wire(w: World) -> CodexAdapter:
    """Give the adapter the broker state it acts through (store, engine, a runner
    that collects actions), as ``start()`` would, without any I/O."""
    from types import SimpleNamespace

    a = ad(w)
    a.runner = SimpleNamespace(
        state=SimpleNamespace(store=w.store, engine=w.engine, clock=w.clock, agents=None,
                              info=SimpleNamespace(codex_link="")),
        execute=lambda acts: w.actions.extend(acts))
    return a


def dead_agent(w: World, p) -> None:
    """The participant's app-server: a (pid, start) that is not a live process."""
    w.store.update_participant(p.id, agent_pid=999_999, agent_start=1.0)


def notices(acts: list) -> list[str]:
    from switchboard.models import Notice

    return [x.text for x in acts if isinstance(x, Notice)]


def test_a_join_or_a_thread_proof_clears_session_end(w: World) -> None:
    """The bug of 2026-09-25: after a daemon restart the thread kept its id, the
    user re-ran join and the proof passed, yet the member stayed "session
    ended" because only a UserPromptSubmit (inert before the join) cleared it."""
    p, m = codex(w)
    a = wire(w)
    attach(w)
    w.hook(p, "SessionEnd", sid=TID, reason="other")
    assert TID in a.ended and a.tier(w.p(p)) == ("mcp-only", "session ended")
    a.on_joined(w.p(p), "0" * 16, True)
    assert TID not in a.ended
    a._sync_status(TID)
    assert w.p(p).status == "idle" and a.tier(w.p(p)) == ("codex:daemon", None)
    msg = w.human("after the re-join")
    assert [x.path for x in pushes(w)] == ["turn_start"] and w.delivery(m, msg)["state"] == "offered"


def test_a_later_hook_of_the_thread_clears_session_end(w: World) -> None:
    p, _m = codex(w)
    a = attach(w)
    w.hook(p, "SessionEnd", sid=TID, reason="other")
    t_end = a.ended[TID]
    # a hook that started before the SessionEnd was recorded (it raced it) is no evidence
    w.hook(p, "Stop", sid=TID, t=t_end - 0.5)
    assert TID in a.ended
    for ev in ("PostToolUse", "Stop", "Interrupt"):
        w.hook(p, "SessionEnd", sid=TID, reason="other")
        w.clock.advance(1)
        w.hook(p, ev, sid=TID, tool="Bash", ok=True)
        assert TID not in a.ended, ev
    w.hook(p, "SessionEnd", sid=TID, reason="other")
    w.hook(p, "UserPromptSubmit", sid=TID, permission_mode="default")  # as before: a prompt always
    assert TID not in a.ended


def test_the_thread_loaded_again_after_session_end_clears_it(w: World) -> None:
    p, _m = codex(w)
    wire(w)
    a = attach(w)
    # a TUI quit: the thread unloads (notLoaded, closed), then SessionEnd comes last
    a._on_note("thread/status/changed", {"threadId": TID, "status": {"type": "notLoaded"}}, w.clock.now())
    w.clock.advance(0.5)
    w.hook(p, "SessionEnd", sid=TID, reason="other")
    assert TID in a.ended and TID in a.ended_gone
    w.clock.advance(30)  # later its TUI (or a new one) resumes it: loaded again
    a._on_note("thread/status/changed", {"threadId": TID, "status": {"type": "idle"}}, w.clock.now())
    assert TID not in a.ended and w.p(p).status == "idle"


def test_session_end_while_still_loaded_needs_the_thread_gone_first(w: World) -> None:
    """E.g. the old daemon's SessionEnd while it shuts down, or a thread that stays
    loaded a while: a status of that same load is no evidence it runs again."""
    p, _m = codex(w)
    wire(w)
    a = attach(w)
    w.hook(p, "SessionEnd", sid=TID, reason="other")
    assert TID in a.ended and TID not in a.ended_gone
    w.clock.advance(1)
    a._on_note("thread/status/changed", {"threadId": TID, "status": {"type": "idle"}}, w.clock.now())
    assert TID in a.ended and w.p(p).status == "offline"
    # the status link blips (the same server, the thread still loaded): no evidence either
    a._link_lost()
    assert TID not in a.ended_gone
    attach(w, view=None)
    w.clock.advance(1)
    a._on_note("thread/status/changed", {"threadId": TID, "status": {"type": "idle"}}, w.clock.now())
    assert TID in a.ended and w.p(p).status == "offline"
    # its server really died (the thread's app-server is gone) and a restarted one loads it
    dead_agent(w, p)
    assert a.defer_end(w.p(p)) is True and TID in a.ended_gone
    w.clock.advance(1)
    a._on_note("thread/status/changed", {"threadId": TID, "status": {"type": "idle"}}, w.clock.now())
    assert TID not in a.ended


def app_server(monkeypatch: pytest.MonkeyPatch, *pids: int) -> None:
    """These pids look like the managed daemon (``codex app-server --listen unix://``)."""
    real = proc.argv
    monkeypatch.setattr(cx.proc, "argv", lambda pid, start: "/opt/homebrew/bin/codex app-server --listen unix://"
                        if pid in pids else real(pid, start))


def test_a_daemon_restart_keeps_the_member_and_rebinds_it(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    app_server(monkeypatch, os.getpid())
    p, m = codex(w)
    a = wire(w)
    attach(w)
    dead_agent(w, p)
    now = w.clock.now()
    a.lost = (now, frozenset({TID}))  # the link dropped with the thread loaded
    a.link_state, a.loaded, a.loaded_at = "down", set(), None
    a.view.clear()
    assert a.defer_end(w.p(p)) is True
    assert p.id in a.orphans and a.tier(w.p(p)) == ("mcp-only", cx.RESTART_NOTE)
    msg = w.human("queued while the daemon restarts")
    assert pushes(w) == [] and cx.RESTART_NOTE in (w.engine.parked_reason(m.id) or "")
    w.clock.advance(5)
    assert a.defer_end(w.p(p)) is True  # still inside the window
    # the restarted daemon loaded the thread; its MCP servers said hello
    me = proc.info(os.getpid())
    from types import SimpleNamespace

    a.on_mcp_hello(SimpleNamespace(harness="codex", agent_pid=os.getpid(), agent_start=me.start), [])
    attach(w)
    w.take()
    a._rebind_ready()
    acts = w.take()
    q = w.p(p)
    assert (q.agent_pid, q.active) == (os.getpid(), True) and p.id not in a.orphans
    assert notices(acts) == ["codex-1 reconnected after a Codex daemon restart"]
    assert [x.path for x in acts if isinstance(x, Push)] == ["turn_start"]
    assert w.delivery(m, msg)["state"] == "offered"
    assert a.tier(q) == ("codex:daemon", None)
    ev = w.store.recent_events(kinds=["codex_restart"], limit=20)
    assert {x.data["what"] for x in ev} == {"app_server_gone", "rebound"}


def test_a_restart_past_the_grace_window_ends_the_session(w: World) -> None:
    p, _m = codex(w)
    a = wire(w)
    attach(w)
    dead_agent(w, p)
    a.lost = (w.clock.now(), frozenset({TID}))
    a.link_state, a.loaded = "down", set()
    assert a.defer_end(w.p(p)) is True
    w.clock.advance(a.cfg.codex.restart_grace_s + 1)
    assert a.defer_end(w.p(p)) is False and p.id not in a.orphans


def test_no_grace_for_a_thread_the_daemon_did_not_have(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))
    p, _m = codex(w)
    a = wire(w)
    dead_agent(w, p)
    assert a.defer_end(w.p(p)) is False  # an embedded TUI (queue tier) that quit: gone
    a.lost = (w.clock.now(), frozenset({"019a0000-0000-7000-8000-0000000000aa"}))
    assert a.defer_end(w.p(p)) is False
    cfg = Config().replace(codex=CodexCfg(restart_grace_s=0)).with_delivery(quiet_s=0.0, max_hold_s=0.0)
    w2 = World(tmp_path / "w2", clock, cfg) if (tmp_path / "w2").mkdir() is None else None
    p2, _m2 = codex(w2)
    a2 = wire(w2)
    attach(w2)
    dead_agent(w2, p2)
    assert a2.defer_end(w2.p(p2)) is False  # restart_grace_s = 0: end at once, as before


def test_the_same_mcp_process_saying_hello_again_rebinds_with_its_credentials(w: World) -> None:
    from types import SimpleNamespace

    p, m = codex(w)
    a = wire(w)
    attach(w)
    dead_agent(w, p)
    before = (w.p(p).mcp_pid, w.p(p).mcp_start, w.m(m).cred_hash)
    assert a.defer_end(w.p(p)) is True
    me = proc.info(os.getpid())
    a.on_mcp_hello(SimpleNamespace(harness="codex", agent_pid=os.getpid(), agent_start=me.start), [w.p(p)])
    q = w.p(p)
    assert p.id not in a.orphans and q.agent_pid == os.getpid()
    # its MCP process and membership credential are untouched: the MCP server's in-memory
    # credential still works (agent.* calls check exactly these)
    assert (q.mcp_pid, q.mcp_start, w.m(m).cred_hash) == before and w.m(m).left_at is None
    # a hello whose parent isn't a live Codex process re-binds nothing
    dead_agent(w, p)
    assert a.defer_end(w.p(p)) is True
    a.on_mcp_hello(SimpleNamespace(harness="unknown", agent_pid=1, agent_start=None), [w.p(p)])
    assert p.id in a.orphans


def test_the_new_app_server_must_be_unambiguous(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    a = wire(w)
    me = proc.info(os.getpid())
    parent = proc.info(os.getppid())
    a.lost = (w.clock.now(), frozenset())
    a.fresh_agents[(os.getpid(), me.start)] = w.clock.now()
    assert a._new_agent() is None  # its MCP servers said hello, but it isn't a Codex app-server
    monkeypatch.setattr(cx.proc, "argv", lambda pid, start: "/opt/homebrew/bin/codex --remote unix://x"
                        if pid == os.getpid() else "")
    assert a._new_agent() is None  # a TUI (or exec, or anything but an app-server) never
    app_server(monkeypatch, os.getpid(), os.getppid())
    a.fresh_agents[(os.getpid(), me.start)] = w.clock.now() - 60  # said hello before the link was lost
    assert a._new_agent() is None
    a.fresh_agents[(os.getpid(), me.start)] = w.clock.now()
    assert a._new_agent() == (os.getpid(), me.start)
    a.fresh_agents[(os.getpid(), me.start + 5)] = w.clock.now()  # a recycled pid: not that process
    assert a._new_agent() == (os.getpid(), me.start)
    a.fresh_agents[(os.getppid(), parent.start)] = w.clock.now()
    assert a._new_agent() is None  # two candidates: don't guess
    # the Codex app-server lsof shows on the control socket wins over hellos
    a.server_pids = {os.getppid()}
    assert a._new_agent() == (os.getppid(), parent.start)
    a.server_pids = {os.getppid(), os.getpid()}  # two listeners: which one runs the thread isn't clear
    assert a._new_agent() is None
    p, _m = codex(w)
    attach(w)
    dead_agent(w, p)
    a.orphans[p.id] = cx.Orphan(w.clock.now(), 999_999, 1.0)
    a._rebind_ready()  # its thread is loaded, but on which server isn't clear: it waits (or ends)
    assert p.id in a.orphans and w.p(p).agent_pid == 999_999


# ----------------------------------------------------------- codex binary
def _exe(path: Path, version: str = "9.8.7") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho 'codex-cli {version}'\n")
    path.chmod(0o755)
    return path


def test_the_codex_binary_is_found_again_when_its_path_vanishes(
        tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Homebrew upgrade removes the old version's directory: the configured (or
    cached) path is gone, and ``codex`` is looked up again on PATH (same checks)."""
    monkeypatch.setattr(cx, "_temp_roots", lambda: set())
    old = _exe(tmp_path / "Caskroom" / "codex" / "0.156.1" / "bin" / "codex", "0.156.1")
    new = _exe(tmp_path / "safe" / "0.157.0" / "bin" / "codex", "0.157.0")
    monkeypatch.setenv("PATH", str(new.parent))
    cfg = Config().replace(codex=CodexCfg(bin=str(old))).with_delivery(quiet_s=0.0, max_hold_s=0.0)
    w = World(tmp_path / "w", clock, cfg) if (tmp_path / "w").mkdir() is None else None
    a = wire(w)
    a._resolve_bin(force=True)
    assert a.bin_path == os.path.realpath(old) and a.codex_bin() == os.path.realpath(old)
    assert a.bin_version == "0.156.1" and not a.bin_fallback
    attach(w)  # link up
    assert a.queue_guard()[0] is True
    old.unlink()
    # tier() and queue_guard() are pure (route() calls them): they only see the binary is gone
    assert a.queue_guard() == (False, None, "codex binary not found (or not owned by you or root)")
    assert a.bin_path == os.path.realpath(old)
    assert a.codex_bin() == os.path.realpath(new)  # the transport / clients loop look again
    assert a.queue_guard()[0] is True and a.bin_version == "0.157.0" and a.bin_fallback
    assert "from PATH: the configured codex.bin is gone" in a.status_summary()
    ev = w.store.recent_events(kinds=["codex_bin"], limit=5)[0].data
    assert ev["fell_back"] is True and ev["version"] == "0.157.0" and ev["was"] == "0.156.1"
    # nothing at all: not found, and not looked up again on every call
    new.unlink()
    assert a.codex_bin() is None
    calls = []
    monkeypatch.setattr(cx, "resolve_codex", lambda name: calls.append(name))
    assert a.codex_bin() is None and calls == []  # within BIN_RETRY_S
    w.clock.advance(cx.BIN_RETRY_S + 0.1)
    assert a.codex_bin() is None and calls == [str(old)]


def test_the_binary_version_is_read_from_where_it_is_installed(tmp_path: Path) -> None:
    """Never by running it: any codex run writes into ~/.codex/tmp."""
    assert cx.bin_version(str(_exe(tmp_path / "Caskroom" / "codex" / "0.157.0" / "bin" / "codex"))) == "0.157.0"
    daemon = tmp_path / "releases" / "0.157.0-aarch64-apple-darwin" / "bin" / "codex"
    assert cx.bin_version(str(_exe(daemon, "x"))) == "0.157.0"
    npm = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    _exe(npm / "bin" / "codex.js")
    (npm / "package.json").write_text('{"name": "@openai/codex", "version": "0.158.0"}')
    link = tmp_path / "npmbin" / "codex"
    link.parent.mkdir()
    link.symlink_to(npm / "bin" / "codex.js")
    assert cx.bin_version(str(link)) == "0.158.0"
    probe = tmp_path / "plain" / "bin" / "codex"
    probe.parent.mkdir(parents=True)
    probe.write_text(f"#!/bin/sh\ntouch {tmp_path / 'ran'}\necho 'codex-cli 1.2.3'\n")
    probe.chmod(0o755)
    assert cx.bin_version(str(probe)) is None  # no version on its path: unknown, and never run
    assert not (tmp_path / "ran").exists()
    # a package.json version reaches `switchboard status`: a version, or nothing
    (npm / "package.json").write_text('{"name": "@openai/codex", "version": "0.159.0-alpha.2"}')
    assert cx.bin_version(str(link)) == "0.159.0"
    (npm / "package.json").write_text('{"name": "@openai/codex", "version": "\\u001b[31mred"}')
    assert cx.bin_version(str(link)) is None


def test_an_in_place_upgrade_updates_the_recorded_version(
        tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    """npm upgrades @openai/codex in place (the same path): the version is read again
    at each forced look (link up), not only when the path changes."""
    npm = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    exe = _exe(npm / "bin" / "codex.js")
    (npm / "package.json").write_text('{"name": "@openai/codex", "version": "0.157.0"}')
    cfg = Config().replace(codex=CodexCfg(bin=str(exe))).with_delivery(quiet_s=0.0, max_hold_s=0.0)
    w = World(tmp_path / "w", clock, cfg) if (tmp_path / "w").mkdir() is None else None
    a = wire(w)
    a._resolve_bin(force=True)
    assert (a.bin_path, a.bin_version) == (os.path.realpath(exe), "0.157.0")
    (npm / "package.json").write_text('{"name": "@openai/codex", "version": "0.158.0"}')
    a._resolve_bin(force=True)
    assert (a.bin_path, a.bin_version, a.bin_fallback) == (os.path.realpath(exe), "0.158.0", False)
    ev = w.store.recent_events(kinds=["codex_bin"], limit=5)[0].data
    assert (ev["version"], ev["was"]) == ("0.158.0", "0.157.0")
    n = len(w.store.recent_events(kinds=["codex_bin"], limit=20))
    a._resolve_bin(force=True)  # nothing changed: nothing recorded
    assert len(w.store.recent_events(kinds=["codex_bin"], limit=20)) == n


def test_resolve_codex_falls_back_to_path_only_for_a_vanished_absolute_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cx, "_temp_roots", lambda: set())
    new = _exe(tmp_path / "safe" / "bin" / "codex")
    monkeypatch.setenv("PATH", str(new.parent))
    assert cx.resolve_codex(str(tmp_path / "gone" / "codex")) == os.path.realpath(new)
    assert cx.resolve_codex("codex") == os.path.realpath(new)
    assert cx.resolve_codex("codex-nope") is None  # a bare name is looked up as itself only
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    _exe(ws / ".venv" / "bin" / "codex")
    monkeypatch.setenv("PATH", str(ws / ".venv" / "bin"))
    assert cx.resolve_codex(str(tmp_path / "gone" / "codex")) is None  # never from a workspace


def _fake_brew(root: Path) -> Path:
    """A git work tree with Homebrew's marker files and a codex in bin/ (what any
    agent can create inside its own workspace)."""
    (root / ".git").mkdir(parents=True)
    (root / "Library" / "Homebrew").mkdir(parents=True)
    _exe(root / "bin" / "brew")
    cask = _exe(root / "Caskroom" / "codex" / "0.157.0" / "bin" / "codex")
    (root / "bin" / "codex").symlink_to(cask)
    os.chmod(root, 0o755)
    return cask


def test_a_workspace_faking_a_homebrew_prefix_is_still_a_workspace(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The review's forge: bin/brew and Library/Homebrew planted in a workspace whose
    bin/ is on PATH (a direnv PATH_add bin) must not make its codex trusted."""
    monkeypatch.setattr(cx, "_temp_roots", lambda: set())
    ws = tmp_path / "proj"
    cask = _fake_brew(ws)
    for d in (ws / "bin", cask.parent, ws / "Cellar", ws / "opt", ws / "sbin"):
        assert cx.unsafe_bin_dir(str(d)), d
    monkeypatch.setenv("PATH", str(ws / "bin"))
    assert cx.resolve_bin("codex") is None
    assert cx.resolve_codex(str(tmp_path / "gone" / "Caskroom" / "codex" / "0.156.1" / "codex")) is None


@pytest.mark.skipif(not os.path.isdir("/opt/homebrew/.git"), reason="no Homebrew repository at /opt/homebrew")
def test_the_real_homebrew_prefix_is_an_install_location() -> None:
    assert not cx.unsafe_bin_dir("/opt/homebrew/bin")
    assert cx.unsafe_bin_dir("/opt/homebrew/Library/Homebrew")  # the repository itself


def test_a_homebrew_prefix_is_not_a_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apple Silicon Homebrew keeps its git repository at its prefix (/opt/homebrew/.git);
    the files it installs (bin, Caskroom, Cellar, ...) are not an agent's work tree.
    (Here the fixed prefix list is pointed at a temp copy.)"""
    monkeypatch.setattr(cx, "_temp_roots", lambda: set())
    brew = tmp_path / "homebrew"
    cask = _fake_brew(brew)
    monkeypatch.setattr(cx, "HOMEBREW_PREFIXES", (os.path.realpath(brew),))
    assert not cx.unsafe_bin_dir(str(brew / "bin"))
    assert not cx.unsafe_bin_dir(str(cask.parent))
    assert cx.unsafe_bin_dir(str(brew / "Library" / "Homebrew"))  # the repository itself
    assert cx.unsafe_bin_dir(str(brew))
    monkeypatch.setenv("PATH", str(brew / "bin"))
    assert cx.resolve_bin("codex") == os.path.realpath(cask)
    os.chmod(brew, 0o775)  # writable by the group: not trusted
    assert cx.unsafe_bin_dir(str(brew / "bin"))
    os.chmod(brew, 0o755)
    (brew / "Library" / "Homebrew").rmdir()  # a git work tree that only looks a bit like it
    assert cx.unsafe_bin_dir(str(brew / "bin"))


# --------------------------------------------- 0.157.0: ephemeral threads
def test_an_ephemeral_thread_needs_no_tui_of_its_own(w: World) -> None:
    """0.157.0 runs an ephemeral "thread_title" thread after a session's first prompt
    (loaded about 60 s, no TUI ever shows it): it must not make the first look hold
    every thread. A thread not known to be ephemeral still counts (fail closed)."""
    p, _m = codex(w)
    a = attach(w)
    title = "019a0000-0000-7000-8000-00000000e0e0"
    a.loaded = {TID, title}
    a._track("control", frozenset({7}), w.clock.now(), a.user_threads(), len(a.user_threads()))
    assert TID in a.suspect  # unknown: counted
    a.suspect.clear()
    a.holds.clear()
    a._seen.clear()
    a.internal[title] = True
    assert a.user_threads() == {TID}
    a._track("control", frozenset({7}), w.clock.now(), a.user_threads(), len(a.user_threads()))
    assert a.suspect == {} and a.live(w.p(p))[0]


def test_an_ephemeral_thread_unloading_releases_no_hold(w: World) -> None:
    p, _m = codex(w)
    a = attach(w)
    other, title = "019a0000-0000-7000-8000-0000000000aa", "019a0000-0000-7000-8000-00000000e0e0"
    a.loaded = {TID, other, title}
    a.internal[title] = True
    a._track("control", frozenset({1, 2}), w.clock.now(), a.user_threads(), 2)
    a._track("control", frozenset({2}), w.clock.now(), a.user_threads(), 2)  # one TUI left
    assert set(a.suspect) == {TID, other}  # the ephemeral thread isn't held (nothing to hold)
    a.clients = Clients(True, w.clock.now(), 1, None, frozenset({2}))
    a._on_note("thread/closed", {"threadId": title}, w.clock.now())
    assert set(a.suspect) == {TID, other}  # not evidence of anything: still held
    a._on_note("thread/closed", {"threadId": other}, w.clock.now())
    assert a.suspect == {}  # the orphan unloaded and 1 TUI covers the 1 user thread left


def test_only_the_app_servers_own_ephemeral_threads_are_internal() -> None:
    assert cx.internal_thread({"ephemeral": True, "threadSource": "thread_title"})
    # a human's `codex --ephemeral` session, or a flag missing: may be shown by a TUI
    assert not cx.internal_thread({"ephemeral": True, "threadSource": None})
    assert not cx.internal_thread({"ephemeral": True})
    assert not cx.internal_thread({"ephemeral": False, "threadSource": "thread_title"})
    assert not cx.internal_thread({"threadSource": "thread_title"})


def test_a_joined_thread_is_held_whatever_its_flags(w: World) -> None:
    """The review's case: a member's own thread that reads as internal (e.g. a human's
    ephemeral session) is still counted and held when a TUI leaves: no headless turn."""
    p, _m = codex(w)
    wire(w)
    a = attach(w)
    other = "019a0000-0000-7000-8000-0000000000aa"
    a.loaded = {TID, other}
    a.internal[TID] = True
    assert a.user_threads() == {TID, other}
    a._track("control", frozenset({1, 2}), w.clock.now(), a.user_threads(), len(a.user_threads()))
    a._track("control", frozenset({2}), w.clock.now(), a.user_threads(), len(a.user_threads()))
    a.clients = Clients(True, w.clock.now(), 1, None, frozenset({2}))
    assert TID in a.suspect and a.live(w.p(p)) == (False, cx.HOLD_WHY)
    w.take()
    w.human("hello")
    assert pushes(w) == []


class _Rpc:
    """A status link stand-in for ``poll()``: a loaded list, idle threads."""

    closed = False

    def __init__(self, loaded: set[str]):
        self._loaded = loaded

    async def loaded_threads(self) -> set[str]:
        return set(self._loaded)

    async def read_thread(self, tid: str, include_turns: bool = False) -> dict:
        return {"id": tid, "status": {"type": "idle"}, "ephemeral": False}


def test_a_poll_that_sees_the_thread_back_restores_its_status(w: World) -> None:
    """The review's case: SessionEnd while loaded (the view stays idle), a poll finds the
    thread gone, a later one finds it loaded again: "session ended" clears and the
    member gets the link's status back (not left offline until some hook)."""
    import asyncio

    p, _m = codex(w)
    a = wire(w)
    attach(w)
    w.hook(p, "SessionEnd", sid=TID, reason="other")
    assert w.p(p).status == "offline" and a.thread_view(TID) == "idle"
    w.clock.advance(1)
    a.rpc = _Rpc(set())
    asyncio.run(a.poll())
    assert TID in a.ended and TID in a.ended_gone
    w.clock.advance(1)
    a.rpc = _Rpc({TID})
    asyncio.run(a.poll())
    assert TID not in a.ended and w.p(p).status == "idle"
    assert a.tier(w.p(p)) == ("codex:daemon", None)


def test_a_rejoin_from_a_new_mcp_server_is_a_reconnect_only_once_proven(w: World) -> None:
    """Inside the grace window, a join from another MCP process (its proof reset by the
    join) claims the thread: no "reconnected" notice and "session ended" stays until
    its thread proof passes. A join that kept its proof (the same MCP process) is one
    at once."""
    p, _m = codex(w)
    a = wire(w)
    attach(w)
    w.hook(p, "SessionEnd", sid=TID, reason="other")
    dead_agent(w, p)
    assert a.defer_end(w.p(p)) is True
    q = w.store.update_participant(p.id, thread_proof=0)  # what a join from another MCP process does
    w.take()
    a.on_joined(q, "0" * 16, True)
    assert notices(w.take()) == [] and p.id in a._rejoins and p.id not in a.orphans
    assert TID in a.ended and a.tier(w.p(p)) == ("mcp-only", "unverified thread")
    # the same MCP process (proof kept): a reconnect at once
    a._rejoins.clear()
    a.orphans[p.id] = cx.Orphan(w.clock.now(), 999_999, 1.0)
    q = w.store.update_participant(p.id, thread_proof=1)
    a.on_joined(q, "0" * 16, False)
    assert notices(w.take()) == ["codex-1 reconnected after a Codex daemon restart"]
    assert TID not in a.ended and p.id not in a.orphans and p.id not in a._rejoins
