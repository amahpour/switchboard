"""A hint the broker gives an agent names a tool parameter only if that agent's session has it
(#340, DESIGN.md §6.1). Each MCP server says its version in ``mcp.hello``; a session started
before a parameter existed keeps its old tool schema until it is restarted, so a hint to use that
parameter sends it to a call its own tool refuses. Here: the join answer's ``read(before=)`` line
(first released in 0.25.0) and the refusal of a ``fix`` without ``proof`` (0.26.0). The ``resume``
hint of a refused name is tested with the other resume tests (test_name_resume.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard import __version__, envelope
from switchboard.config import Config
from switchboard.mcp.client import RpcError, Stream

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
OLDER = "this session's switchboard is older than the broker's"
PR = "https://example.com/shop/pull/7"
OLD_CLAIMS = [
    pytest.param({"version": "0.24.9"}, id="0.24.9"),
    pytest.param({"version": "0.23.0"}, id="0.23.0"),
    pytest.param({}, id="no version"),
    pytest.param({"version": "0.25"}, id="not a release number"),
]


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


def say_in_build(b: InProcBroker, n: int) -> None:
    """``n`` chat messages from the person, so a join shows the last 8 and counts the rest."""
    for i in range(n):
        r = b.web.post(  # type: ignore[attr-defined]
            "/api/rooms/build/say", json={"text": f"message {i}"}, headers=b.write_headers()
        )
        assert r.status_code == 200, r.text


def join_text(b: InProcBroker, session: str, claim: dict[str, Any]) -> str:
    """The join answer a session gets when its ``mcp.hello`` claims ``claim``."""
    with Stream(b.paths.sock, timeout=5) as s:
        s.call("mcp.hello", {"harness": "test", "test_session": session, **claim}, 5)
        return s.call("agent.join", {"room": "#build", "screen_name": "claude-1"}, 5)["text"]


@pytest.mark.parametrize("version", [__version__, "0.25.0"])
def test_a_session_that_can_page_back_is_given_the_read_call(broker: InProcBroker, version: str) -> None:
    """A session with ``read(before=)`` (0.25.0 and later) is told the exact call that shows the
    messages the join left out, as since #300. A fix that dropped the line for everyone would
    leave current agents unable to see what they missed."""
    say_in_build(broker, 12)
    text = join_text(broker, "pages", {"version": version})
    assert '4 earlier messages: read("#build", before=' in text and "shows them." in text, text
    assert OLDER not in text, text


@pytest.mark.parametrize("claim", OLD_CLAIMS)
def test_a_session_that_cannot_page_back_is_not_sent_to_before(
    broker: InProcBroker, claim: dict[str, Any]
) -> None:
    """The bug (#340): the join answer told every session to call
    ``read(room, before=<id>)`` for the messages it left out, but a session from before 0.25.0 has no
    ``before`` and its tool refuses the call. It is told its switchboard is older than the broker's,
    that it can't page back, and that a restart updates it; the line still counts what was left
    out. An unknown or unreadable version gets the same, since it can't be shown to have the
    parameter."""
    say_in_build(broker, 12)
    text = join_text(broker, "no-paging", claim)
    assert "before=" not in text, text
    assert (
        f"4 earlier messages: {OLDER} and can't page back to them. Ask your user to restart this session"
        in text
    ), text
    assert "Recent messages, oldest first (8)." in text  # the rest of the answer is unchanged
    assert "\njoin yk:j" in text, text  # the join code still ends the answer, for resume later


def test_a_room_with_nothing_earlier_says_nothing_of_paging(broker: InProcBroker) -> None:
    """With no earlier message there is nothing to page to, so an old session is not told its
    switchboard is old either: it has no use for the news."""
    say_in_build(broker, 3)
    text = join_text(broker, "short", {"version": "0.24.0"})
    assert OLDER not in text and "earlier message" not in text, text


# ----------------------------------------------------------------- review: a fix needs proof
async def refused_fix(broker: InProcBroker, claim: dict[str, Any]) -> RpcError:
    """A session claiming ``claim`` is made the owner of F1 and tries to fix it with no proof."""
    async with FakeAgent(broker.home, "raiser") as raiser:
        await raiser.join("#build", "codex-1")
        await raiser.call("review", room="#build", action="open", url=PR)
        await raiser.call("review", room="#build", action="raise", title="wrong total")
        with Stream(broker.paths.sock, timeout=5) as owner:
            owner.call("mcp.hello", {"harness": "test", "test_session": "owner", **claim}, 5)
            cred = owner.call("agent.join", {"room": "#build", "screen_name": "claude-1"}, 5)["cred"]
            owner.call("agent.review", {"cred": cred, "action": "concede", "item": "F1"}, 5)
            with pytest.raises(RpcError) as refused:
                owner.call(
                    "agent.review", {"cred": cred, "action": "fix", "item": "F1", "commit": "abc1234"}, 5
                )
    assert refused.value.code == "bad_request", refused.value
    return refused.value


@pytest.mark.parametrize("version", [__version__, "0.26.0"])
async def test_a_session_that_has_proof_is_told_a_fix_needs_it(broker: InProcBroker, version: str) -> None:
    """A session with the ``proof`` field (0.26.0 and later) is refused with "proof is required",
    as since #251: it can supply it. A fix that rewrote the refusal for everyone would hide the
    rule from the sessions that can follow it."""
    err = await refused_fix(broker, {"version": version})
    assert err.message == "proof is required", err.message


@pytest.mark.parametrize("claim", [*OLD_CLAIMS, pytest.param({"version": "0.25.9"}, id="0.25.9")])
async def test_a_session_without_proof_is_told_to_update_not_to_send_it(
    broker: InProcBroker, claim: dict[str, Any]
) -> None:
    """The bug (#340): a fix without proof was refused with "proof is required", but a session
    from before 0.26.0 has no ``proof`` field on its ``review`` tool, so it could never satisfy the
    rule and kept retrying. It is told why: the broker needs proof, this session's switchboard
    is older and can't send it, and a restart updates it."""
    err = await refused_fix(broker, claim)
    assert err.message.startswith("fix needs proof, a test's name or an http(s) link, but " + OLDER), (
        err.message
    )
    assert envelope.RESTART_UPDATES in err.message
