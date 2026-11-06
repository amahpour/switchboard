"""A relay ends only its own call, with a fixed public reason (DESIGN.md §54, #623)."""

from __future__ import annotations

import asyncio
import secrets

import pytest
from test_meeting_hosted import (
    KEY,
    LKEY,
    PUSH,
    SPEECH,
    Host,
    World,
    auth,
    host,
    hosted_call,
    poll_waiting,
    seg,
    until,
)
from test_meeting_hosted import world as world

from switchboard.meeting import keys

LEFT = "/mcp/meeting/leave"


@pytest.mark.parametrize(
    ("reason", "notice"),
    [
        ("stopped", "The call ended: its bot stopped."),
        ("join_failed", "The call ended: its bot could not join."),
        ("joinly_lost", "The call ended: its bot lost the call."),
        ("ended", "The call ended: its bot left."),
        ("server_lost", "The call ended: its bot left."),
        (None, "The call ended: its bot left."),
        (["stopped"], "The call ended: its bot left."),
    ],
)
async def test_departure_flushes_the_call_and_revokes_its_key(
    world: World, reason: object, notice: str
) -> None:
    """A departure clears the bar and wakes a poll immediately, without dropping the last line. Unknown
    reasons become generic text, never a claim repeated into history. Fails without the new route."""
    w = world
    claude, codex = await hosted_call(w)
    try:
        assert w.relay.push([seg("the last words", 0, 1)]).status_code == 200
        poll = asyncio.get_running_loop().run_in_executor(None, lambda: w.relay.speech(wait=25))
        await until(lambda: poll_waiting(w), what="a waiting speech poll")
        got = w.relay.post(LEFT, {"reason": reason, "room": "#uploads", "meeting": 999})
        assert got.status_code == 200
        assert got.json() == {"ended": True}
        assert (await asyncio.wait_for(poll, 10)).json()["ended"] is True
        assert w.settings()["meeting"] is None
        assert w.notices()[-1] == notice
        assert [m["text"] for m in w.call_lines()] == ["the last words"]
        for path, body in ((LEFT, {}), (PUSH, {"segments": []}), (SPEECH, {"wait": 0})):
            assert w.relay.post(path, body).status_code == 410
        assert w.notices().count(notice) == 1
    finally:
        await claude.close()
        await codex.close()


async def test_departure_door_refuses_other_credentials_and_browser(world: World) -> None:
    """An actual agent token or listener key, a cookie, Origin and wrong methods cannot end the call."""
    w = world
    a = Host(w.b, listener_key=LKEY)
    try:
        assert (await a.join("#build", "claude-1"))["ok"]
        assert (await host(a))["ok"]
        for credential in (a.token, LKEY, "sbc_" + secrets.token_hex(32)):
            assert w.relay.post(LEFT, {}, key=credential).status_code == 401
        assert w.web.post(LEFT, json={}).status_code in (401, 403)
        assert w.relay.post(LEFT, {}, origin=w.b.origin).status_code == 403
        for method in ("GET", "PUT", "DELETE"):
            assert w.relay.c.request(method, LEFT, headers=auth(KEY)).status_code == 405
        assert w.relay.post(LEFT, ["stopped"]).status_code == 400
        assert w.settings()["meeting"] is not None
        assert w.relay.post(LEFT, {"reason": "join_failed"}).status_code == 200
        assert w.settings()["meeting"] is None
    finally:
        await a.close()


async def test_old_call_key_cannot_end_the_next_call_in_its_room(world: World) -> None:
    """A key is tied to one call for ever, even when its room holds a newer call."""
    w = world
    a = Host(w.b)
    try:
        assert (await a.join("#build", "claude-1"))["ok"]
        assert (await host(a))["ok"]
        w.command("/meeting leave")
        newer = "sbc_" + secrets.token_hex(32)
        assert (await host(a, key_hash=keys.key_hash(newer)))["ok"]
        assert w.relay.post(LEFT, {"key": newer, "reason": "stopped"}).status_code == 410
        assert w.settings()["meeting"] is not None
        assert w.relay.post(LEFT, {}, key=newer).status_code == 200
    finally:
        await a.close()
