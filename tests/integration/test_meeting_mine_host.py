"""Installed hosts take a one-use link from registration, never through the model (DESIGN.md §54)."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator

import pytest
from fakes.fake_joinly import FakeJoinly
from test_agent_plugins import impl as impl
from test_meeting_host import Machine, Stub
from test_meeting_host import stub as stub
from test_meeting_hosted import Host, World, until
from test_meeting_hosted import world as world
from test_meeting_mine import LINK as BROKER_LINK
from test_meeting_mine import assert_private
from test_relay_and_broker import Run
from test_relay_and_broker import joinly as joinly

from switchboard.meeting.relay import Exit, read_secrets

LINK = 'https://example.com.zoom.us/j/123456789?pwd=example-only&x=one%20two+three/="four"'


@pytest.fixture(params=["claude", "codex"])
def machine(stub: Stub, impl: str, request: pytest.FixtureRequest) -> Iterator[Machine]:
    m = Machine(stub, plugin_name=request.param)
    try:
        yield m
    finally:
        m.close()


def test_from_room_gets_the_link_only_from_the_registration_answer(machine: Machine) -> None:
    """Each isolated sh/PowerShell package starts from empty stdin, writes an exact private link, and
    gives no child process or model output the secret, including JSON-sensitive punctuation."""
    m = machine
    m.stub.handoff = base64.b64encode(LINK.encode()).decode()
    got = m.start("--from-room", stdin="")
    assert got.returncode == 0, got.stderr
    assert m.stub.requests[0]["body"]["from_room"] is True
    [directory] = m.call_dirs()
    assert (directory / "link").read_text().strip() == LINK
    assert (directory / "link").stat().st_mode & 0o077 == 0
    assert m.compose("up")
    visible = got.stdout + got.stderr + m.everything_a_process_was_given()
    assert LINK not in visible and m.stub.handoff not in visible
    assert LINK not in json.dumps(m.stub.requests, default=str)
    assert "example-only" not in (directory / "info").read_text()


@pytest.mark.parametrize("answer", [None, "%%%", "A" * 4000], ids=["missing", "invalid", "too_long"])
def test_missing_or_malformed_handoff_starts_nothing_and_prints_no_response(
    machine: Machine, answer: str | None
) -> None:
    """An older broker or unusable response fails closed before Compose starts, without response data
    reaching the model. The registered call's files are removed."""
    m = machine
    m.stub.handoff = answer
    got = m.start("--from-room", stdin="")
    assert got.returncode == 1
    assert not m.compose("up") and not m.call_dirs()
    assert LINK not in got.stdout + got.stderr


def test_handoff_refusal_does_not_repeat_the_servers_message(machine: Machine) -> None:
    """A failed handoff is described locally: even a server error containing a link is never printed."""
    m = machine
    m.stub.answer = (409, {"error": "conflict", "message": LINK})
    got = m.start("--from-room", stdin="")
    assert got.returncode == 1 and not m.call_dirs() and not m.compose("up")
    assert LINK not in got.stdout + got.stderr and "example-only" not in got.stdout + got.stderr


def test_setup_check_cannot_consume_a_pending_link(machine: Machine) -> None:
    """--no-join and --from-room cannot combine: a setup check must not spend a person's link."""
    got = machine.start("--from-room", "--no-join", stdin="")
    assert got.returncode == 2 and "cannot combine" in got.stderr
    assert not machine.stub.requests


async def test_room_command_to_installed_script_to_joinly_keeps_the_link_private(
    machine: Machine, world: World, joinly: FakeJoinly
) -> None:
    """The actual registration response feeds the installed script's private file and the real relay's
    joinly client. Neither model output nor the broker's entire database receives the secret."""
    w, m = world, machine
    a = Host(w.b)
    run = None
    try:
        assert (await a.join("#build", "claude-1"))["ok"]
        w.command(f"/meeting {BROKER_LINK} mine")
        m.stub.url = w.b.base
        got = m.start("--from-room", room="#build", stdin="", env=m.env(SWITCHBOARD_AGENT_TOKEN=a.token))
        assert got.returncode == 0, got.stderr
        [directory] = m.call_dirs()
        key, link = read_secrets(str(directory / "key"), str(directory / "link"))
        run = Run(w, joinly, key=key.reveal(), link=link.reveal())
        await until(lambda: bool(joinly.joins), what="the host's link reaching joinly")
        assert joinly.joins[0]["meeting_url"] == BROKER_LINK
        assert BROKER_LINK not in got.stdout + got.stderr + m.everything_a_process_was_given()
        assert_private(w)
        w.command("/meeting leave")
        assert await run.code() == Exit.ENDED
        assert BROKER_LINK not in "\n".join(run.said)
    finally:
        if run is not None and not run.task.done():
            run.relay.finish(Exit.STOPPED)
            await run.code()
        await a.close()
