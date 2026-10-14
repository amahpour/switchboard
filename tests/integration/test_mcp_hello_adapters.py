"""``mcp.hello`` on a real broker, with the harness questions answered by the adapters (#170,
DESIGN.md §6.4): the test agent's session key and ``--ack`` follow its hello and its
reconnects, and every adapter hears every hello with the sessions of that MCP process that
are its own harness's."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import InProcBroker

from switchboard.mcp.client import RpcError, Stream


def join(s: Stream, room: str, name: str) -> dict[str, Any]:
    return s.call("agent.join", {"room": room, "screen_name": name}, 5)


def participant(b: InProcBroker, session: str) -> Any:
    return b.on_loop(lambda: b.state.store.find_participant("test", f"test:{session}"))


@pytest.mark.parametrize(
    "params", [{}, {"test_session": ""}, {"test_session": "has space"}, {"test_session": 7}]
)
def test_a_test_agents_hello_without_a_usable_session_key_is_a_bad_request(
    broker: InProcBroker, params: dict[str, Any]
) -> None:
    """The refusal is the test adapter's (``hello_claims``), reported to the MCP server as
    ``bad_request`` with the sentence its user reads, and nothing is recorded for the connection:
    it can't go on to join. Fails if ``AgentService`` lets the hello through, or turns the
    adapter's refusal into some other code."""
    with Stream(broker.paths.sock, timeout=5) as s:
        with pytest.raises(RpcError) as ei:
            s.call("mcp.hello", {"harness": "test", **params}, 5)
        assert ei.value.code == "bad_request" and "--test-session KEY" in ei.value.message
        with pytest.raises(RpcError) as ei:
            join(s, "#build", "worker")
        assert ei.value.code == "unauthorized"


def test_the_test_agents_ack_mode_follows_its_join_and_each_reconnect(broker: InProcBroker) -> None:
    """The ``--ack`` an MCP server's hello claims is how its pull answers are confirmed (§8.7): set
    when it joins, and set again by each later hello of the same MCP process (a reconnect), the
    newest claim winning; one the adapter doesn't know is ``next_call``. Fails if a reconnect
    keeps the old mode, or a harness with no ``--ack`` gets an entry."""
    broker.on_loop(lambda: broker.state.service.create_room("#build"))
    modes = broker.state.engine.ack_modes
    with Stream(broker.paths.sock, timeout=5) as s:
        s.call("mcp.hello", {"harness": "test", "test_session": "ack-a", "test_ack": "immediate"}, 5)
        join(s, "#build", "worker")
    p = participant(broker, "ack-a")
    assert broker.on_loop(lambda: modes[p.id]) == "immediate"
    with Stream(broker.paths.sock, timeout=5) as s:  # the MCP process reconnects, claiming never
        s.call("mcp.hello", {"harness": "test", "test_session": "ack-a", "test_ack": "never"}, 5)
    assert broker.on_loop(lambda: modes[p.id]) == "never"
    with Stream(broker.paths.sock, timeout=5) as s:
        s.call("mcp.hello", {"harness": "test", "test_session": "ack-a", "test_ack": "nonsense"}, 5)
    assert broker.on_loop(lambda: modes[p.id]) == "next_call"
    with Stream(broker.paths.sock, timeout=5) as s:  # no --ack at all: next_call too
        s.call("mcp.hello", {"harness": "test", "test_session": "ack-a"}, 5)
        join(s, "#build", "worker")
    assert broker.on_loop(lambda: modes[p.id]) == "next_call"


def test_every_adapter_hears_every_hello_with_its_own_harnesss_sessions(
    broker: InProcBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Codex adapter used to be the only one told of a hello (``AgentService`` looked it up
    by name), with the Codex sessions of that MCP process. Now each adapter is told, with the
    sessions of that MCP process that are its own harness's: here the test agent's adapter hears
    the test agent's earlier session, and every other adapter an empty list. Fails if an adapter
    isn't told, or is handed another harness's sessions."""
    broker.on_loop(lambda: broker.state.service.create_room("#build"))
    with Stream(broker.paths.sock, timeout=5) as s:
        s.call("mcp.hello", {"harness": "test", "test_session": "hear-a"}, 5)
        join(s, "#build", "worker")
    earlier = participant(broker, "hear-a")
    heard: dict[str, list[tuple[str, list[str]]]] = {}
    for key, adapter in broker.state.engine.adapters.items():

        def spy(ident: Any, mine: list[Any], key: str = key) -> None:
            heard.setdefault(key, []).append((ident.harness, [p.session_key for p in mine]))

        monkeypatch.setattr(adapter, "on_mcp_hello", spy)
    with Stream(broker.paths.sock, timeout=5) as s:
        s.call("mcp.hello", {"harness": "test", "test_session": "hear-b"}, 5)
    assert set(heard) == set(broker.state.engine.adapters)
    for key, calls in heard.items():
        want = [earlier.session_key] if key == "test" else []
        assert calls == [("test", want)], key
