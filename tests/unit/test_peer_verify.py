"""verify_mcp_peer: the broker's own check of an MCP server's ancestry (DESIGN.md §5.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switchboard.broker.peer import McpRefused, Peer, verify_mcp_peer
from switchboard.broker.proc import ProcInfo


def fake(chain_argv: list[str]):
    """chain: [mcp(100), parent(99), grandparent(98), ...] with these argvs (index 0 = mcp)."""
    procs = [ProcInfo(pid=100 - i, ppid=99 - i, start=1000.0 + i, uid=0, comm="x") for i in range(len(chain_argv))]
    argvs = {p.pid: a for p, a in zip(procs, chain_argv)}
    return (lambda pid, depth: procs[:depth]), (lambda ps: {p.pid: argvs.get(p.pid, "") for p in ps})


def verify(claimed: str, argvs: list[str], *, sock=None, sd="/nonexistent", test_mode=False):
    cf, af = fake(argvs)
    return verify_mcp_peer(Peer(pid=100, uid=0), claimed, claude_socket=sock, sessions_dir=sd,
                           test_mode=test_mode, chain_fn=cf, argv_fn=af)


MCP = "/venv/bin/python -I -m switchboard mcp --home /h"


def test_test_harness_needs_test_mode() -> None:
    with pytest.raises(McpRefused):
        verify("test", [MCP, "/usr/bin/python3 -m pytest"])
    ident = verify("test", [MCP, "/usr/bin/python3 -m pytest"], test_mode=True)
    assert ident.harness == "test" and ident.agent_pid == 99 and ident.mcp_pid == 100


def test_claude_verified_only_with_parent_and_registry(tmp_path: Path) -> None:
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "99.json").write_text(json.dumps({"messagingSocketPath": "/tmp/cc-socks/99.sock"}))
    ok = verify("claude", [MCP, "/x/claude/versions/2.1.282"], sock="/tmp/cc-socks/99.sock", sd=str(d))
    assert ok.harness == "claude" and ok.agent_pid == 99 and ok.claude_socket == "/tmp/cc-socks/99.sock"
    bad = verify("claude", [MCP, "/x/claude/versions/2.1.282"], sock="/tmp/cc-socks/1.sock", sd=str(d))
    assert bad.harness == "unknown" and bad.tier_note == "unverified claude"
    shell = verify("claude", [MCP, "/bin/zsh", "/x/claude/versions/2.1.282"], sock="/tmp/cc-socks/99.sock",
                   sd=str(d))
    assert shell.harness == "unknown"  # started from an agent's shell: not the harness itself


def test_codex_devin_cursor() -> None:
    assert verify("codex", [MCP, "/usr/local/bin/codex app-server"]).harness == "codex"
    assert verify("codex", [MCP, "/bin/bash", "/usr/local/bin/codex"]).harness == "unknown"
    d = verify("devin", [MCP, "/bin/sh", "/opt/devin acp"])
    assert d.harness == "devin" and d.agent_pid == 98
    assert verify("devin", [MCP, "/a", "/b", "/opt/devin acp"]).harness == "unknown"  # beyond depth 2
    assert verify("cursor", [MCP, "node x", "/opt/cursor-agent"]).harness == "cursor"
    assert verify("cursor", [MCP, "node"]).tier_note == "unverified cursor"
    assert verify("unknown", [MCP, "whatever"]).harness == "unknown"


@pytest.mark.parametrize("relay", ["/usr/bin/ssh -R /tmp/x.sock:/h/run/broker.sock pi", "socat UNIX-LISTEN:/tmp/x",
                                   "sshd-session: alice@notty", "python3 /tmp/k/ssh -L a:b pi"])
def test_a_relay_on_the_socket_is_no_mcp_server(relay: str) -> None:
    """Through a forward of the socket every process behind it would be one "MCP server"
    (DESIGN.md §27.4.2): refused, whatever it claims (M8c; deferred from M8a)."""
    for claimed in ("claude", "codex", "unknown", "test"):
        with pytest.raises(McpRefused) as ei:
            verify(claimed, [relay, "-zsh", "/sbin/launchd"], test_mode=True)
        assert ei.value.code == "forbidden" and "remote link" in ei.value.message
    # an MCP server that merely mentions ssh in its arguments is not one
    assert verify("unknown", ["/venv/bin/python -m switchboard mcp --home /tmp/ssh", "-zsh"]).harness == "unknown"
