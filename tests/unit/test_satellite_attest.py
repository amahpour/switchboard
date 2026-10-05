"""The satellite's attest is the broker's own check, run on the remote host (DESIGN.md §27.5.3)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import child_env

from switchboard.broker.peer import McpRefused, Peer, verify_mcp_peer
from switchboard.paths import Paths
from switchboard.remote import proto
from switchboard.remote.satellite import Satellite

CHILD = textwrap.dedent("""
    import socket, sys
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sys.argv[1])
    s.recv(1)
""")
PARENT = textwrap.dedent("""
    import json, os, subprocess, sys
    sock, sessions, inbox, child = sys.argv[1:5]
    if sessions != "-":
        with open(os.path.join(sessions, f"{os.getpid()}.json"), "w") as f:
            json.dump({"pid": os.getpid(), "messagingSocketPath": inbox, "status": "idle"}, f)
    subprocess.run([sys.executable, "-c", child, sock])
""")


@pytest.fixture
def workdir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="yk-at-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def connect_from(
    workdir: Path, parent_name: str | None, sessions: Path | None, inbox: str
) -> tuple[Peer, list]:
    """A child process (the "MCP server") under a parent named ``parent_name`` (a stand-in
    harness) or directly under pytest, connected to a socket here: its kernel peer."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = str(workdir / "s.sock")
    srv.bind(path)
    srv.listen(1)
    if parent_name is None:
        p = subprocess.Popen([sys.executable, "-c", CHILD, path], env=child_env())
    else:
        exe = workdir / parent_name
        exe.write_text(PARENT)
        p = subprocess.Popen(
            [sys.executable, str(exe), path, str(sessions) if sessions else "-", inbox, CHILD],
            env=child_env(),
        )
    srv.settimeout(20)
    conn, _ = srv.accept()
    return Peer.from_socket(conn), [conn, srv, p]


def done(keep: list) -> None:
    conn, srv, p = keep
    conn.close()
    srv.close()
    p.wait(10)


def sat(tmp_path: Path, sessions: str, *, test_mode: bool = False, shift: int = 0) -> Satellite:
    return Satellite(
        Paths.from_home(tmp_path),
        "fpga-pi",
        test_mode=test_mode,
        sessions_dir=sessions,
        harden_state="none",
        pid_shift=shift,
    )


def test_attest_equals_broker_verdict_for_same_process(workdir: Path, tmp_path: Path) -> None:
    sessions = workdir / "sessions"
    sessions.mkdir()
    inbox = str(workdir / "inbox.sock")
    peer, keep = connect_from(workdir, "claude", sessions, inbox)
    try:
        for claimed, sock in (
            ("claude", inbox),
            ("claude", "/elsewhere.sock"),
            ("codex", None),
            ("unknown", None),
        ):
            ident = verify_mcp_peer(
                peer, claimed, claude_socket=sock, sessions_dir=str(sessions), test_mode=False
            )
            a = sat(tmp_path, str(sessions)).attest(peer, {"harness": claimed, "claude_socket": sock})
            proto.check_attest(a)  # it passes the link's strict check
            assert (a["harness"], a["evidence"], a["tier_note"], a["claude_socket"]) == (
                ident.harness,
                ident.evidence,
                ident.tier_note,
                ident.claude_socket,
            )
            assert a["mcp"] == [ident.mcp_pid, ident.mcp_start] and a["agent"] == [
                ident.agent_pid,
                ident.agent_start,
            ]
        verified = sat(tmp_path, str(sessions)).attest(peer, {"harness": "claude", "claude_socket": inbox})
        assert verified["harness"] == "claude" and verified["evidence"] == "parent:claude+registry"
        # test mode shifts every pid it reports, never a start time
        shifted = sat(tmp_path, str(sessions), shift=1_000_000_000).attest(
            peer, {"harness": "claude", "claude_socket": inbox}
        )
        assert shifted["mcp"] == [verified["mcp"][0] + 1_000_000_000, verified["mcp"][1]]
        assert shifted["agent"] == [verified["agent"][0] + 1_000_000_000, verified["agent"][1]]
        # another host's registry dir: the same claim is unverified
        other = workdir / "other-sessions"
        other.mkdir()
        assert (
            sat(tmp_path, str(other)).attest(peer, {"harness": "claude", "claude_socket": inbox})["harness"]
            == "unknown"
        )
    finally:
        done(keep)


def test_test_harness_refused_without_test_mode(workdir: Path, tmp_path: Path) -> None:
    peer, keep = connect_from(workdir, None, None, "")
    try:
        with pytest.raises(McpRefused) as ei:
            sat(tmp_path, "/nonexistent").attest(peer, {"harness": "test"})
        assert ei.value.code == "forbidden"
        a = sat(tmp_path, "/nonexistent", test_mode=True).attest(peer, {"harness": "test"})
        assert (a["harness"], a["evidence"]) == ("test", "flag:test")
    finally:
        done(keep)


def test_parent_mismatch_is_unknown(workdir: Path, tmp_path: Path) -> None:
    peer, keep = connect_from(workdir, None, None, "")  # its parent is pytest, not a harness
    try:
        for claimed, evidence in (
            ("claude", "claude-claim,parent-mismatch"),
            ("codex", "codex-claim,parent-mismatch"),
            ("devin", "devin-claim,no-acp"),
            ("cursor", "cursor-claim,no-ancestor"),
            ("bogus", "unknown"),
        ):
            a = sat(tmp_path, "/nonexistent").attest(peer, {"harness": claimed, "claude_socket": "/x.sock"})
            assert (a["harness"], a["evidence"], a["claude_socket"]) == ("unknown", evidence, None), claimed
        # a peer the satellite can't identify is refused, never attested
        with pytest.raises(McpRefused):
            sat(tmp_path, "/nonexistent").attest(Peer(pid=None, uid=os.getuid()), {"harness": "claude"})
    finally:
        done(keep)


def test_hook_chain_has_verdicts_and_shifted_pids(workdir: Path, tmp_path: Path) -> None:
    peer, keep = connect_from(workdir, "claude", None, "")
    try:
        s = sat(tmp_path, "/nonexistent", shift=1_000_000_000)
        chain = s.chain(peer)
        assert chain is not None and len(chain) <= proto.MAX_CHAIN
        proto.check_chain(chain)
        assert chain[0][0] == peer.pid + 1_000_000_000 and chain[0][2] == "-"  # python -c ...: no agent
        assert chain[1][2] == "claude"  # the stand-in harness above it
        assert json.dumps(chain).count("/") == 0  # verdicts only: no path, no argv
        assert s.chain(Peer(pid=None, uid=None)) is None
        # a peer whose start time doesn't match the process now at that pid gets no chain
        assert s.chain(Peer(pid=peer.pid, uid=peer.uid, start=(peer.start or 0) + 50.0)) is None
    finally:
        done(keep)
