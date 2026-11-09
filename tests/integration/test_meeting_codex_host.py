"""Codex's independently installed host, with a token and without one (DESIGN.md §54, #623).

The two plugins are installed as separate subdirectories. This tests an isolated Codex package,
with the same stand-in Docker and HTTP server the Claude host tests use.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Iterator

import pytest
from test_agent_plugins import (
    CURRENT_IMPL,
    PWSH,
    impl,  # noqa: F401
)
from test_meeting_host import CLAUDE_PID, LISTENER, TOKEN, Machine, Stub, stub  # noqa: F401


@pytest.fixture
def machine(stub: Stub, impl: str) -> Iterator[Machine]:  # noqa: F811
    m = Machine(stub, plugin_name="codex")
    try:
        yield m
    finally:
        m.close()


def test_codexs_isolated_plugin_starts_and_stops_with_an_agent_token(machine: Machine) -> None:
    """An installed Codex plugin needs no sibling plugin: token registration starts the same project,
    with private files and no credential in output, and stop removes it. Fails without its packaged host.
    """
    result = machine.start(env=machine.env(CLAUDE_PID="", CODEX_THREAD_ID="thread-1"))
    assert result.returncode == 0, result.stderr
    assert machine.stub.requests[0]["auth"] == f"Bearer {TOKEN}"
    assert machine.compose("up") and machine.call_dirs()
    output = result.stdout + result.stderr
    assert TOKEN not in output and "CPU" in output
    stopped = machine.run(["stop", "--data", str(machine.data)], stdin="")
    assert stopped.returncode == 0 and not machine.call_dirs()


def test_codex_browser_sign_in_cannot_borrow_a_claude_credential(machine: Machine) -> None:
    """Browser-signed-in Codex has no script credential. Refuse before registration, even if a Claude
    listener key and Claude token happen to exist; no new credential or permission is invented.
    """
    (machine.data / f"listener-key-{CLAUDE_PID}").write_text(LISTENER)
    result = machine.start(env=machine.env(SWITCHBOARD_AGENT_TOKEN="", CLAUDE_PLUGIN_OPTION_TOKEN=TOKEN))
    assert result.returncode == 1
    assert "SWITCHBOARD_AGENT_TOKEN" in result.stderr and "browser" in result.stderr
    assert not machine.stub.requests and not machine.compose("up") and not machine.call_dirs()
    assert TOKEN not in result.stdout + result.stderr and LISTENER not in result.stdout + result.stderr


def test_codex_helper_makes_a_private_rotating_key_and_prints_only_its_hash(machine: Machine) -> None:
    """The local HTTP helper creates a fresh 0600 key, never reads a login, and replaces a planted link
    without writing through it. The printed header carries only the hash (#637)."""
    first = run_codex_headers(machine)
    assert first.returncode == 0, first.stderr
    f = machine.data / f"codex-listener-key-{os.getpid()}"
    key = f.read_text().strip()
    assert key.startswith("sbl_") and len(key) == 68
    assert json.loads(first.stdout) == {"X-Switchboard-Listener": hashlib.sha256(key.encode()).hexdigest()}
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert key not in first.stdout + first.stderr
    victim = machine.root / "victim"
    victim.write_text("unchanged")
    f.unlink()
    f.symlink_to(victim)
    second = run_codex_headers(machine)
    assert second.returncode == 0, second.stderr
    assert not f.is_symlink() and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert victim.read_text() == "unchanged"
    assert f.read_text().strip() != key and second.stdout != first.stdout


def test_codex_host_uses_its_process_key_and_sends_its_thread_without_a_token(machine: Machine) -> None:
    """A shell uses only this Codex's key, not a nearby Claude key. Naming CODEX_THREAD_ID in the existing
    session field lets the broker refuse a subagent trying its parent's key."""
    key = "sbl_" + "ab" * 32
    f = machine.data / f"codex-listener-key-{os.getpid()}"
    f.write_text(key + "\n")
    f.chmod(0o600)
    for invalid in ("", "x" * 129, 'bad"thread'):
        refused = machine.start(
            env=machine.env(SWITCHBOARD_AGENT_TOKEN="", CLAUDE_PID="", CODEX_THREAD_ID=invalid)
        )
        assert refused.returncode == 1 and not machine.stub.requests and not machine.compose("up")
        assert key not in refused.stdout + refused.stderr
    result = machine.start(
        env=machine.env(SWITCHBOARD_AGENT_TOKEN="", CLAUDE_PID="", CODEX_THREAD_ID="thread-1")
    )
    assert result.returncode == 0, result.stderr
    assert machine.stub.requests[0]["auth"] == f"Bearer {key}"
    assert machine.stub.requests[0]["body"]["session"] == "thread-1"
    assert key not in result.stdout + result.stderr + machine.everything_a_process_was_given()
    assert machine.run(["stop", "--data", str(machine.data)], stdin="").returncode == 0


def run_codex_headers(machine: Machine) -> subprocess.CompletedProcess[str]:
    """The installed package's helper, run as a child of this stand-in Codex process."""
    if CURRENT_IMPL[0] == "sh":
        argv = ["sh", str(machine.plugin / "bin/headers"), str(machine.data)]
    else:
        argv = [
            PWSH,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(machine.plugin / "bin/windows/headers.ps1"),
            str(machine.data),
        ]
    return subprocess.run(argv, env=machine.env(), capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("command", ["plan", "status"])
def test_codex_host_reports_a_missing_configured_headers_helper_without_showing_config(
    machine: Machine, command: str
) -> None:
    """A removed versioned helper gets a useful diagnosis before hosting, without exposing config secrets."""
    config = machine.home / ".codex"
    config.mkdir()
    missing = machine.home / "removed-plugin/bin/headers"
    text = (
        f"[mcp_servers.switchboard]\nhttp_headers_helper = '\"{missing}\"'\n"
        "[mcp_servers.other]\nhttp_headers_helper = '\"/missing/other\"'\n"
        f'secret = "{TOKEN}"\n'
    )
    (config / "config.toml").write_text(text)
    result = machine.run([command], stdin="")
    assert result.returncode == 0, result.stderr
    assert "configured http_headers_helper is missing" in result.stderr
    assert "headers-launcher" in result.stderr and "restart Codex" in result.stderr
    assert TOKEN not in result.stdout + result.stderr
    assert (config / "config.toml").read_text() == text
    assert not machine.stub.requests and not machine.compose("up")
