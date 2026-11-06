"""Codex's independently installed host, with a token and without one (DESIGN.md §54, #623).

The two plugins are installed as separate subdirectories. This tests an isolated Codex package,
with the same stand-in Docker and HTTP server the Claude host tests use.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from test_agent_plugins import impl  # noqa: F401
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
