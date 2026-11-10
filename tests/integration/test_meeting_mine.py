"""A private, one-use room-to-host handoff without showing the link to a model (DESIGN.md §54)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import sqlite3
from typing import Any

import pytest
from test_meeting_hosted import LKEY, Host, World, enroll, host
from test_meeting_hosted import world as world

from switchboard.broker.commands import Actor
from switchboard.meeting import keys
from switchboard.meeting.hosted import HostedCalls

LINK = "https://example.com.zoom.us/j/123456789?pwd=example-only"


def claim(a: Host, room: str = "#build", **params: Any) -> Any:
    """A host script registering through the existing route, without a link in its request."""
    return enroll(
        a.b, a.cred, room, key_hash=keys.key_hash("sbc_" + secrets.token_hex(32)), from_room=True, **params
    )


def assert_private(w: World, logs: str = "") -> None:
    """Scan all SQLite tables, including events/delivery batches, plus public responses and logs."""
    with sqlite3.connect(w.b.paths.db) as con:
        dump = "\n".join(con.iterdump())
    public = json.dumps([w.messages(), w.settings(), w.notices()]) + dump + logs
    assert LINK not in public
    assert base64.b64encode(LINK.encode()).decode() not in public
    assert "example-only" not in public


@pytest.mark.parametrize("listener", [False, True])
@pytest.mark.parametrize("requester", ["owner", "person"])
async def test_mine_wakes_only_owned_agents_and_the_script_takes_the_link_once(
    world: World,
    listener: bool,
    requester: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person's room command wakes their own agent with no secret, and only its credential can take
    the link through registration. A different person's agent cannot take it, even with claimed owner ids.
    Fails if the server bot joins, if anyone else takes it, or if a model/database/log sees the link."""
    w = world
    caplog.set_level(logging.DEBUG, logger="switchboard")
    person = w.b.on_loop(lambda: w.b.state.store.person_add("bob", b"b" * 16, "not-a-real-hash", 300))
    mine = person.id if requester == "person" else None
    own = Host(w.b, person=mine, listener_key=LKEY if listener else None)
    other = Host(w.b, device="other-laptop", person=None if mine else person.id, session="other-session")

    def command(text: str) -> dict[str, Any]:
        if mine is None:
            return w.command(text)
        return w.b.on_loop(
            lambda: w.b.state.service.command(
                "#build", text, Actor("human", "web", name="bob", person_id=mine)
            )
        )

    try:
        assert (await own.join("#build", "claude-1"))["ok"]
        assert (await other.join("#build", "claude-2"))["ok"]

        def forbidden_join(*args: Any) -> None:
            raise AssertionError("mine must never start the server bot")

        monkeypatch.setattr(w.b.state.meetings, "start", forbidden_join)
        other.claude.call("read", room="#build")
        other_waiting = other.wait_task("#build", timeout_s=30)
        reply = command(f"/meeting {LINK} mine")
        assert LINK not in json.dumps(reply)
        assert w.rows() == [] and w.settings()["meeting"] is None
        waiting = own.wait_task("#build", timeout_s=30)
        got = await asyncio.wait_for(waiting, 10)
        assert got["ok"] and "host-meeting-bot" in got["text"] and "#build" in got["text"]
        assert "--from-room" in got["text"] and LINK not in got["text"]
        # a chat message under alice's name, so one short line (#685), not the instruction it used to be
        line = next(m for m in w.messages() if "--from-room" in m["text"])["text"]
        assert len(line) <= 100 and "\n" not in line, line
        assert "Do not" not in line and "stdin" not in line
        assert "@claude-2" not in got["text"]
        assert not other_waiting.done()
        assert w.b.on_loop(lambda: LINK not in repr(w.b.state.hosted_calls._requests))
        denied = claim(other, person_id=None, owner="alice")
        assert denied.status_code == 409 and LINK not in denied.text
        assert w.rows() == []
        accepted = claim(own, platform="unknown")
        assert accepted.status_code == 200
        assert accepted.headers["cache-control"] == "no-store"
        assert base64.b64decode(accepted.json()["link_b64"]).decode() == LINK
        assert accepted.json()["platform"] == "zoom"
        command("/meeting leave")
        assert claim(own).status_code == 409
        w.b.call("human.say", {"room": "#build", "text": "@claude-2 done"})
        await asyncio.wait_for(other_waiting, 10)
        assert_private(w, caplog.text)
    finally:
        await own.close()
        await other.close()


async def test_first_owned_agent_to_register_takes_the_handoff_and_other_rooms_cannot(world: World) -> None:
    """Wake all eligible owned agents; the first successful registration consumes one room's handoff.
    Failed membership/key validation does not consume it, and another room cannot take it."""
    w = world
    a = Host(w.b)
    b = Host(w.b, device="alice-desktop", session="second-session")
    try:
        assert (await a.join("#build", "claude-1"))["ok"]
        assert (await b.join("#build", "claude-2"))["ok"]
        assert (await a.join("#uploads", "claude-1"))["ok"]
        w.command(f"/meeting {LINK} mine")
        assert claim(a, "#uploads").status_code == 409
        invalid = enroll(w.b, a.cred, key_hash="not-a-hash", from_room=True)
        assert invalid.status_code == 400
        got = claim(b)
        assert got.status_code == 200 and "link_b64" in got.json()
        w.command("/meeting leave")
        assert claim(a).status_code == 409
        assert_private(w)
    finally:
        await a.close()
        await b.close()


@pytest.mark.parametrize("action", ["expires", "restart", "close", "replace"])
async def test_pending_handoff_is_short_lived_and_drops_on_restart_or_close(
    world: World, action: str
) -> None:
    """Unclaimed links expire after ten minutes, vanish with broker memory/room close, and a repeated
    request replaces the earlier link. None writes a call row or reveals its value."""
    w = world
    a = Host(w.b)
    try:
        assert (await a.join("#build", "claude-1"))["ok"]
        w.command(f"/meeting {LINK} mine")
        if action == "expires":
            w.tick(600)
        elif action == "restart":

            def restart() -> None:
                calls = HostedCalls(w.b.state)
                w.b.state.hosted_calls = w.b.state.service.hosted_calls = calls

            w.b.on_loop(restart)
        elif action == "close":
            w.command("/close")
            assert w.b.on_loop(lambda: not w.b.state.hosted_calls._requests)
            return
        else:
            replacement = LINK.replace("example-only", "replacement-only")
            w.command(f"/meeting {replacement} mine")
            got = claim(a)
            assert got.status_code == 200
            assert base64.b64decode(got.json()["link_b64"]).decode() == replacement
            return
        assert claim(a).status_code == 409
        assert w.rows() == []
        assert_private(w)
    finally:
        await a.close()


async def test_no_owned_member_holds_nothing_and_new_members_cannot_take_an_old_request(world: World) -> None:
    """No eligible agent means no handoff, even if an agent joins immediately afterwards."""
    w = world
    out = w.command(f"/meeting {LINK} mine")
    assert "no agent" in out["text"].lower()
    assert_private(w)
    a = Host(w.b)
    try:
        assert (await a.join("#build", "claude-1"))["ok"]
        assert claim(a).status_code == 409
    finally:
        await a.close()


@pytest.mark.parametrize("mode", ["wrong_room", "revoked", "left"])
async def test_agents_without_current_owned_membership_do_not_count(world: World, mode: str) -> None:
    """A member elsewhere, a revoked token, and a member that left cannot authorize a secret handoff."""
    w = world
    a = Host(w.b)
    try:
        assert (await a.join("#uploads" if mode == "wrong_room" else "#build", "claude-1"))["ok"]
        if mode == "revoked":
            w.b.on_loop(
                lambda: w.b.state.store.agent_token_revoke_id(
                    w.b.state.store.agent_token_for_device(a.device).id
                )
            )
        elif mode == "left":
            assert a.claude.call("leave", room="#build")["ok"]
        out = w.command(f"/meeting {LINK} mine")
        assert "no agent" in out["text"].lower()
        assert_private(w)
    finally:
        await a.close()


async def test_mine_needs_no_server_bot_and_ordinary_hosting_does_not_take_the_link(world: World) -> None:
    """The explicit handoff mode is required to receive a link. A normal host response stays unchanged."""
    w = world
    a = Host(w.b)
    try:
        assert (await a.join("#build", "claude-1"))["ok"]

        def no_server_bot() -> None:
            cfg = w.b.state.cfg.replace(meeting=w.b.state.cfg.meeting.__class__(max_calls=2))
            w.b.state.cfg = w.b.state.service.cfg = cfg

        w.b.on_loop(no_server_bot)
        w.command(f"/meeting {LINK} mine")
        got = await host(a)
        assert got["ok"] and "link_b64" not in got
        # Starting any call in the room cancels stale requests, so a later call cannot take an old link.
        w.command("/meeting leave")
        assert claim(a).status_code == 409
        assert_private(w)
    finally:
        await a.close()


async def test_repeating_without_owned_agents_discards_the_prior_request(world: World) -> None:
    """When no owned member remains, asking again must hold nothing, including an older pending link."""
    w = world
    a = Host(w.b)
    try:
        assert (await a.join("#build", "claude-1"))["ok"]
        w.command(f"/meeting {LINK} mine")
        assert a.claude.call("leave", room="#build")["ok"]
        assert "no agent" in w.command(f"/meeting {LINK} mine")["text"].lower()
        assert (await a.join("#build", "claude-1"))["ok"]
        assert claim(a).status_code == 409
        assert_private(w)
    finally:
        await a.close()


@pytest.mark.parametrize("mode", [None, "true", 1, []])
async def test_malformed_handoff_mode_is_refused_without_consuming_the_request(
    world: World, mode: Any
) -> None:
    """An unknown handoff mode must fail closed, leaving the valid one-use request available."""
    w = world
    a = Host(w.b)
    try:
        assert (await a.join("#build", "claude-1"))["ok"]
        w.command(f"/meeting {LINK} mine")
        got = a.claude.c.post(
            "/mcp/meeting/host",
            json={"room": "#build", "key_hash": "ab" * 32, "from_room": mode},
            headers={"authorization": f"Bearer {a.cred}"},
        )
        assert got.status_code == 400 and w.rows() == []
        assert claim(a).status_code == 200
        assert_private(w)
    finally:
        await a.close()


@pytest.mark.parametrize("platform", ["not-a-platform", ["zoom"]])
async def test_private_handoff_preserves_the_registration_platform_allowlist(
    world: World, platform: Any
) -> None:
    """The private link's platform is authoritative, but cannot bypass validation of a supplied claim.
    Refuse unknown values before consuming the pending link, as direct registration always does."""
    w = world
    a = Host(w.b)
    try:
        assert (await a.join("#build", "claude-1"))["ok"]
        w.command(f"/meeting {LINK} mine")
        refused = claim(a, platform=platform)
        assert refused.status_code == 400 and w.rows() == []
        assert claim(a).status_code == 200
        assert_private(w)
    finally:
        await a.close()


async def test_invalid_utf8_invite_is_refused_without_logging_or_holding_it(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """A JSON-escaped lone surrogate is not valid UTF-8 secret transport. Refuse it in words, without
    an internal encoding error, a pending request, or any secret in a log or the database."""
    w = world
    a = Host(w.b)
    try:
        assert (await a.join("#build", "claude-1"))["ok"]
        invalid = LINK + chr(0xD800)
        response = w.web.post(
            "/api/rooms/build/command",
            content=json.dumps({"text": f"/meeting {invalid} mine"}),
            headers={**w.b.write_headers(), "content-type": "application/json"},
        )
        assert response.status_code == 400
        assert "UTF-8" in response.text
        assert claim(a).status_code == 409
        assert_private(w, caplog.text)
    finally:
        await a.close()
