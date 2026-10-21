"""peer: kernel peer identity, agent-ancestry denial, hook resolution (DESIGN.md §5.3)."""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from itertools import takewhile
from pathlib import Path

import pytest

from switchboard.broker import proc
from switchboard.broker.peer import (
    AllowAllHumans,
    Peer,
    PeerPolicy,
    ProcessPeerPolicy,
    chain_is_human,
    is_agent_chain,
    match_agent,
    peer_pid,
    peer_uid,
    short_chain,
)

CLIENT = (
    "import socket, sys, time\ns = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(3)\n"
)


def below_pytest(pid: int):
    """Ancestry up to (not including) this pytest process, treated as a complete
    chain, so the test is independent of whatever harness runs pytest itself."""
    chain, _ = proc.ancestry_to_root(pid)
    return list(takewhile(lambda p: p.pid != os.getpid(), chain)), True


@pytest.fixture
def listener():
    d = tempfile.mkdtemp(prefix="yk-peer-", dir="/tmp")
    path = os.path.join(d, "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    srv.settimeout(10)
    yield srv, path
    srv.close()
    os.unlink(path)
    os.rmdir(d)


def connect_via(listener, argv: list[str]) -> tuple[Peer, subprocess.Popen]:
    srv, path = listener
    # A new session: the child has no controlling terminal wherever pytest runs.
    child = subprocess.Popen(argv + [path], stdin=subprocess.DEVNULL, start_new_session=True)
    conn, _ = srv.accept()
    peer = Peer.from_socket(conn)
    conn.close()
    return peer, child


def test_peer_pid_uid_from_kernel(listener) -> None:
    peer, child = connect_via(listener, [sys.executable, "-c", CLIENT])
    try:
        assert peer.pid == child.pid
        assert peer.uid == os.getuid()
        assert peer.start is not None
    finally:
        child.kill()
        child.wait()


def fake_agent_bin(tmp: Path, name: str) -> Path:
    """A shell script named like an agent harness that runs the socket client as its child."""
    p = tmp / name
    p.write_text(f'#!/bin/sh\n"{sys.executable}" -c "$1" "$2"\n')  # no exec: the script stays the parent
    p.chmod(0o755)
    return p


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_child_of_fake_agent_binary_is_denied(listener, tmp_path: Path, name: str) -> None:
    fake = fake_agent_bin(tmp_path, name)
    peer, child = connect_via(listener, [str(fake), CLIENT])
    try:
        pol = ProcessPeerPolicy(chain_fn=below_pytest)
        chain = pol.chain(peer)
        argvs = proc.argv_many(chain)
        assert any(match_agent(argvs[p.pid]) == name for p in chain), argvs
        assert not pol.human_cli_allowed(peer)
        assert not pol.login_allowed(peer)
        assert not pol.human_allowed(peer)
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_symlinked_fake_binary_is_denied(listener, tmp_path: Path, name: str) -> None:
    """The peer process itself runs as '<tmp>/claude' (a symlink to python)."""
    link = tmp_path / name
    link.symlink_to(sys.executable)
    peer, child = connect_via(listener, [str(link), "-I", "-S", "-c", CLIENT])
    try:
        pol = ProcessPeerPolicy(chain_fn=below_pytest)
        argv0 = proc.argv_many(pol.chain(peer)[:1])[peer.pid]
        if not argv0.startswith(str(link)):
            # a python.org framework build re-execs itself as Python.app, so the
            # stand-in never runs under the symlink's name; nothing to test here
            pytest.skip(f"this Python re-execs itself (argv0 {argv0.split()[0]!r})")
        assert not pol.human_cli_allowed(peer)
    finally:
        child.kill()
        child.wait()


def test_plain_child_is_allowed(listener) -> None:
    peer, child = connect_via(listener, [sys.executable, "-c", CLIENT])
    try:
        pol = ProcessPeerPolicy(chain_fn=below_pytest)
        assert pol.human_cli_allowed(peer)
        assert not pol.login_allowed(peer)  # no controlling TTY here
        assert not pol.human_allowed(peer)  # UDS never gets 'human' without test trust
        assert pol.describe(peer) == "unknown" or isinstance(pol.describe(peer), str)
    finally:
        child.kill()
        child.wait()


def nested_sh(fake_agent: Path, depth: int) -> list[str]:
    """argv for: fake agent -> ``depth`` nested non-exec ``sh -c`` -> the socket client."""
    # Inside ``sh -c CMD a b`` the extra args are $0 and $1; in the script file, $1 and $2.
    inner = f'"{sys.executable}" -c "$0" "$1"; true'
    for _ in range(depth - 1):
        inner = "sh -c " + shlex.quote(inner) + ' "$0" "$1"; true'
    runner = fake_agent.parent / "runner.sh"
    runner.write_text(f'#!/bin/sh\nsh -c {shlex.quote(inner)} "$1" "$2"; true\n')
    runner.chmod(0o755)
    return [str(fake_agent), str(runner), CLIENT]


def test_agent_far_up_a_nested_chain_is_denied(listener, tmp_path: Path) -> None:
    """An agent's Bash can't push the harness out of view by nesting shells:
    the walk goes all the way up, however deep (review finding, M1)."""
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\n"$1" "$2" "$3"\n')  # no exec: 'claude' stays an ancestor
    fake.chmod(0o755)
    peer, child = connect_via(listener, nested_sh(fake, 10))
    try:
        pol = ProcessPeerPolicy(chain_fn=below_pytest)
        chain = pol.chain(peer)
        argvs = proc.argv_many(list(chain))
        depth = next(i for i, p in enumerate(chain) if match_agent(argvs[p.pid]) == "claude")
        assert depth >= 10, [argvs[p.pid] for p in chain]
        assert not pol.human_cli_allowed(peer)
        assert not pol.login_allowed(peer)
        # the production walk (to pid 1) denies it too, wherever pytest runs
        assert not ProcessPeerPolicy().human_cli_allowed(Peer(pid=peer.pid, uid=peer.uid, start=peer.start))
    finally:
        child.kill()
        child.wait()


def test_incomplete_chain_or_unknown_argv_is_denied(listener, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: a walk that didn't reach the root, or an ancestor whose argv
    can't be read, never counts as human."""
    peer, child = connect_via(listener, [sys.executable, "-c", CLIENT])
    try:
        assert ProcessPeerPolicy(chain_fn=below_pytest).human_cli_allowed(Peer(**_ids(peer)))
        truncated = ProcessPeerPolicy(chain_fn=lambda pid: (below_pytest(pid)[0], False))
        assert not truncated.human_cli_allowed(Peer(**_ids(peer)))
        assert not ProcessPeerPolicy(chain_fn=lambda pid: ([], True)).human_cli_allowed(Peer(**_ids(peer)))
        real = proc.argv_many
        monkeypatch.setattr(proc, "argv_many", lambda ps: {**real(ps), ps[-1].pid: ""})
        assert not ProcessPeerPolicy(chain_fn=below_pytest).human_cli_allowed(Peer(**_ids(peer)))
    finally:
        child.kill()
        child.wait()


def _ids(peer: Peer) -> dict:
    return {"pid": peer.pid, "uid": peer.uid, "start": peer.start}


def test_chain_is_human() -> None:
    a, b = _pi(10, 1.0), _pi(11, 2.0)
    ok = {10: "python -m switchboard say", 11: "-zsh"}
    assert chain_is_human([a, b], True, ok)
    assert not chain_is_human([a, b], False, ok)
    assert not chain_is_human([], True, ok)
    assert not chain_is_human([a, b], True, {10: "python -m switchboard say"})  # 11 unknown
    assert not chain_is_human([a, b], True, {**ok, 11: "/opt/homebrew/bin/claude"})


def test_recycled_pid_is_not_trusted() -> None:
    me = proc.info(os.getpid())
    assert me is not None
    pol = ProcessPeerPolicy(chain_fn=below_pytest)
    # a peer whose recorded start time doesn't match the live process
    assert not pol.human_cli_allowed(Peer(pid=os.getpid(), uid=os.getuid(), start=me.start + 5))
    assert not pol.human_cli_allowed(Peer(pid=os.getpid(), uid=os.getuid() + 1, start=me.start))


def test_default_policy_denies_everything() -> None:
    p = PeerPolicy()
    peer = Peer(pid=os.getpid(), uid=os.getuid())
    assert not p.human_cli_allowed(peer) and not p.human_allowed(peer) and not p.login_allowed(peer)


def test_allow_all_humans_is_uid_bound() -> None:
    p = AllowAllHumans()
    assert p.human_cli_allowed(Peer(pid=1, uid=os.getuid()))
    assert p.human_allowed(Peer(pid=1, uid=os.getuid()))
    assert not p.human_allowed(Peer(pid=1, uid=os.getuid() + 1))


@pytest.mark.parametrize(
    "argv,harness",
    [
        ("/Users/x/.local/share/claude/versions/2.1.282 --resume", "claude"),
        ("claude", "claude"),
        ("/opt/homebrew/bin/claude --model haiku", "claude"),
        ("/Applications/Claude.app/Contents/MacOS/claude --output-format stream-json", "claude"),
        ("/home/x/.claude/remote/ccd-cli/2.1.284 --output-format stream-json --verbose", "claude"),
        ("/home/x/.claude/remote/ccd-cli/2.1.284", "claude"),
        ("/home/x/.claude/remote/srv/f601/server --serve --socket /home/x/.claude/remote/s.sock", None),
        ("cat /home/x/.claude/remote/ccd-cli/2.1.284", None),
        ("/home/x/.claude/remote/ccd-cli/notes.txt", None),
        ("node /opt/homebrew/bin/codex --remote unix:///tmp/x", "codex"),
        ("codex app-server --listen unix:///tmp/cx.sock", "codex"),
        ("/Users/x/.local/share/cursor-agent/versions/2026.09.23/cursor-agent", "cursor"),
        ("/opt/homebrew/bin/devin acp", "devin"),
        ("devin --foo acp", "devin"),
        ("/bin/zsh -l", None),
        ("python -m switchboard say #build hi", None),
        ("vim claude.md", None),
        ("/usr/bin/codexfoo", None),
        ("devin", None),
    ],
)
def test_agent_matchers(argv: str, harness: str | None) -> None:
    assert match_agent(argv) == harness


def test_is_agent_chain_and_short_chain() -> None:
    assert is_agent_chain(["python", "-zsh", "claude --resume"])
    assert not is_agent_chain(
        ["python", "-zsh", "/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"]
    )
    chain = proc.ancestry(os.getpid(), 3)
    s = short_chain(chain)
    assert isinstance(s, str) and s
    assert short_chain(chain[:1]) == "unknown"


def _pi(pid: int, start: float) -> proc.ProcInfo:
    return proc.ProcInfo(pid=pid, ppid=1, start=start, uid=os.getuid())


def test_socketpair_peer_helpers() -> None:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert peer_pid(a) == os.getpid()
        assert peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()
    time.sleep(0)
