"""A client that gives up on a ``wait()`` by closing its connection (#559, DESIGN.md §6.1).

The broker holds a ``wait()`` open as a sink. When the client is gone, the sink has to go with it: a
message that arrives meanwhile would be handed to a wait nobody can read, expire ten seconds later as
``no_confirm`` and be offered again. How the broker finds out depends on the wire the client speaks:

- a 2026-07-28 request (Claude Code, Devin) is one self-contained POST, and the MCP library cancels the
  tool call when its response stream closes;
- a 2025-06-18 session (Codex, Cursor, OpenCode) cancels a call with a ``notifications/cancelled`` post,
  and the library treats a stream that closes without one as a dropped line, not a cancel: the tool call
  ran on. ``HttpTools`` watches the request itself for that.

Each test drives a real client wire against the in-process broker over a real socket, and ends the call
the way a process dies or a proxy gives up: it closes the connection and sends nothing else.
"""

from __future__ import annotations

import copy
import json
import socket
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from conftest import InProcBroker
from test_agents_http import CALL as CODEX_CALL
from test_agents_http import THREAD_A, Codex, make_token
from test_agents_http_claude import CALL as CLAUDE_CALL
from test_agents_http_claude import SESSION_1, USES, Claude
from test_agents_http_devin import open_waits
from test_agents_http_wake import until
from test_mcp_agent import q, states

WAIT_S = 60  # long: the tests end it themselves, so the timeout never does


def hold_open(b: InProcBroker, headers: dict[str, str], body: dict[str, Any]) -> socket.socket:
    """POST ``body`` to ``/mcp`` on a socket of our own and leave it open, unread: a client in the middle of
    a call, which is the only way to close the connection from the client's side with nothing else sent."""
    payload = json.dumps(body).encode()
    sent = {k.lower(): v for k, v in headers.items()} | {
        "host": f"switchboard.localhost:{b.port}",
        "content-length": str(len(payload)),
    }
    head = "".join(f"{k}: {v}\r\n" for k, v in sent.items())
    s = socket.create_connection(("127.0.0.1", b.port), timeout=10)
    s.sendall(f"POST /mcp HTTP/1.1\r\n{head}\r\n".encode() + payload)
    return s


class Wire:
    """One client wire: how a member joins and how its ``wait()`` request is made."""

    def __init__(self, b: InProcBroker, name: str) -> None:
        self.b, self.name = b, name

    def join(self) -> None:
        raise NotImplementedError

    def wait_request(self) -> tuple[dict[str, str], dict[str, Any]]:
        raise NotImplementedError

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        raise NotImplementedError


class CodexWire(Wire):
    """Codex 0.158.0: an ``initialize`` opens an MCP session (2025-06-18), and a call names its thread."""

    def __init__(self, b: InProcBroker, name: str) -> None:
        super().__init__(b, name)
        self.codex = Codex(b, make_token(b, "desk"))
        assert self.codex.start().status_code == 200

    def join(self) -> None:
        assert self.codex.call("join", THREAD_A, room="#build", screen_name=self.name)["ok"]
        self.codex.call("read", THREAD_A, room="#build")

    def wait_request(self) -> tuple[dict[str, str], dict[str, Any]]:
        body = copy.deepcopy(CODEX_CALL["body"])
        body["id"] = next(self.codex.ids)
        body["params"]["name"] = "wait"
        body["params"]["arguments"] = {"room": "#build", "timeout_s": WAIT_S}
        meta = body["params"]["_meta"]
        meta["threadId"] = meta["sessionId"] = THREAD_A
        meta["x-codex-turn-metadata"]["thread_id"] = THREAD_A
        return self.codex.headers(CODEX_CALL["headers"]), body

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        return self.codex.call(tool, THREAD_A, **arguments)


class ClaudeWire(Wire):
    """Claude Code 2.1.300: stateless requests (2026-07-28), the session named by a PreToolUse hook."""

    def __init__(self, b: InProcBroker, name: str) -> None:
        super().__init__(b, name)
        self.claude = Claude(b, make_token(b, "laptop"), SESSION_1)

    def join(self) -> None:
        assert self.claude.call("join", room="#build", screen_name=self.name)["ok"]
        self.claude.call("read", room="#build")

    def wait_request(self) -> tuple[dict[str, str], dict[str, Any]]:
        use = next(USES)
        assert self.claude.hook(use, tool="mcp__switchboard__wait") == "{}"
        body = copy.deepcopy(CLAUDE_CALL["body"])
        body["id"] = next(self.claude.ids)
        body["params"]["name"] = "wait"
        body["params"]["arguments"] = {"room": "#build", "timeout_s": WAIT_S}
        body["params"]["_meta"]["claudecode/toolUseId"] = use
        headers = {
            **CLAUDE_CALL["headers"],
            "authorization": f"Bearer {self.claude.token}",
            "mcp-name": "wait",
        }
        return headers, body

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        return self.claude.call(tool, **arguments)


WIRES: dict[str, Callable[[InProcBroker, str], Wire]] = {"codex": CodexWire, "claude": ClaudeWire}


@pytest.fixture(params=sorted(WIRES))
def wire(request: pytest.FixtureRequest, broker: InProcBroker) -> Iterator[Wire]:
    broker.call("room.create", {"name": "#build"})
    w = WIRES[request.param](broker, f"{request.param}-1")
    w.join()
    yield w


def say_to(b: InProcBroker, name: str, text: str) -> int:
    return int(b.call("human.say", {"room": "#build", "text": f"@{name} {text}"})["id"])


def test_a_wait_whose_client_closes_its_connection_is_closed_at_once(
    broker: InProcBroker, wire: Wire
) -> None:
    """A client that closes its connection in the middle of ``wait()`` and sends nothing else has gone: the
    broker closes the wait at once, so the next message stays pending for the next way it can be delivered
    and is not handed to a wait nobody can read (it would expire as ``no_confirm`` and come back late).
    Fails, for the Codex wire, if the broker lets the wait run on to its own timeout after the connection is
    closed: the MCP library takes that for a dropped line, not a cancel. (The Claude wire is cancelled by the
    library and passes without the change: it is here so a library that stops doing so is noticed.)"""
    headers, body = wire.wait_request()
    line = hold_open(broker, headers, body)
    try:
        until(lambda: open_waits(broker, wire.name) == 1)
    finally:
        line.close()
    until(lambda: open_waits(broker, wire.name) == 0)
    mid = say_to(broker, wire.name, "anyone there?")
    assert states(broker, wire.name)[mid] == "pending"  # nobody was listening
    got = wire.call("read", room="#build")
    assert got["ok"] and "anyone there?" in got["text"], got  # and nothing was lost


def test_a_wait_that_is_answered_is_not_taken_back(broker: InProcBroker, wire: Wire) -> None:
    """The watch on the request must not fire for a wait that ends the ordinary way: a message arrives, the
    wait answers, and the message stays offered to the agent that has just been given it. Fails if the
    request counts as hung up when the answer is on its way, which would take the message back (``unwait``)
    from an agent that has it."""
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(wire.call, "wait", room="#build", timeout_s=WAIT_S)
        until(lambda: open_waits(broker, wire.name) == 1)
        mid = say_to(broker, wire.name, "you may stop waiting")
        got = waiting.result(30)
    assert got["ok"] and got["status"] == "messages" and "you may stop waiting" in got["text"], got
    assert states(broker, wire.name)[mid] == "offered"
    assert not q(broker, "SELECT id FROM batches WHERE expire_reason = 'unwait'")
    assert open_waits(broker, wire.name) == 0


def test_closing_a_connection_ends_only_the_wait_it_carried(broker: InProcBroker) -> None:
    """Two members of one broker wait; one's connection closes. The other's wait stays open and is still
    answered. Fails if a hang-up closes waits other than the one on its request."""
    broker.call("room.create", {"name": "#build"})
    gone, kept = CodexWire(broker, "codex-1"), ClaudeWire(broker, "claude-1")
    gone.join()
    kept.join()
    headers, body = gone.wait_request()
    line = hold_open(broker, headers, body)
    try:
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(kept.call, "wait", room="#build", timeout_s=WAIT_S)
            until(lambda: open_waits(broker, "codex-1") == 1 and open_waits(broker, "claude-1") == 1)
            line.close()
            until(lambda: open_waits(broker, "codex-1") == 0)
            assert open_waits(broker, "claude-1") == 1
            say_to(broker, "claude-1", "still here?")
            got = waiting.result(30)
    finally:
        line.close()
    assert got["ok"] and "still here?" in got["text"], got
