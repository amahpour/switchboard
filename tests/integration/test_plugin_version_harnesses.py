"""Cursor's, Devin's and OpenCode's plugins say which version they are, like Claude Code's and Codex's (#887).

Their MCP connection is the person's own config and carries no version, so each plugin sends
``X-Switchboard-Plugin-Version`` on the requests it makes itself: Cursor's and Devin's hooks on
``/mcp/event``,
OpenCode's listener on ``/mcp/listen`` and ``/mcp/status``. The broker keeps it as a claim and compares majors
the way it does for the other two (DESIGN.md §49.13). A plugin that sends none stays unsaid, and an MCP call
(which sends none) never erases what a hook reported.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_agents_http import Codex, make_token
from test_agents_http_cursor import SID_A, invoke
from test_agents_http_devin import Devin
from test_agents_http_opencode import SESSION_A, OpenCode, listen
from test_plugin_version import HEADER, inspector

from switchboard import plugin_versions

NOW = plugin_versions.PINNED["cursor"]
OLD = "0.1.0"  # an earlier major than any release


def _post(
    b: InProcBroker, token: str, path: str, body: dict[str, Any], version: str | None
) -> httpx.Response:
    headers = {"authorization": f"Bearer {token}", **({HEADER: version} if version is not None else {})}
    return httpx.post(f"{b.base}{path}", json=body, headers=headers, timeout=10)


@pytest.fixture
def build(broker: InProcBroker) -> InProcBroker:
    broker.call("room.create", {"name": "#build"})
    return broker


def cursor_member(b: InProcBroker) -> tuple[str, Codex]:
    token = make_token(b)
    c = Codex(b, token, client="Cursor")
    assert c.start().status_code == 200
    pre = {"harness": "cursor", "event": "preToolUse", "session_id": SID_A, "tool_use_id": "u1"}
    r = _post(b, token, "/mcp/pretool", {**pre, "tool_name": "switchboard_join"}, None)
    assert r.status_code == 200
    args = {"room": "#build", "screen_name": "cursor-1", "switchboard_session_id": SID_A}
    assert invoke(c, "switchboard_join", {**args, "switchboard_tool_use_id": "u1"})["ok"]
    return token, c


def cursor_event(b: InProcBroker, token: str, version: str | None) -> httpx.Response:
    body = {"harness": "cursor", "event": "postToolUse", "session_id": SID_A, "tool_name": "Shell"}
    return _post(b, token, "/mcp/event", body, version)


def devin_member(b: InProcBroker) -> tuple[str, Devin]:
    token = make_token(b)
    dv = Devin(b, token)
    args = dv.pre("devin-sid", "use-1", {"room": "#build", "screen_name": "devin-1"})
    assert dv.call("join", args)["ok"]
    return token, dv


def devin_event(b: InProcBroker, token: str, version: str | None) -> httpx.Response:
    body = {"harness": "devin", "event": "PostToolUse", "session_id": "devin-sid", "tool_name": "exec"}
    return _post(b, token, "/mcp/event", body, version)


def opencode_member(b: InProcBroker) -> str:
    token = make_token(b)
    c = OpenCode(b, token)
    c.start()
    assert c.call("join", SESSION_A, room="#build", screen_name="opencode-1")["ok"]
    return token


def opencode_status(b: InProcBroker, token: str, version: str | None) -> httpx.Response:
    return _post(
        b, token, "/mcp/status", {"event": "session.execution.started", "session_id": SESSION_A}, version
    )


def opencode_listen(b: InProcBroker, token: str, version: str | None) -> httpx.Response:
    return listen(b, token, **({HEADER: version} if version is not None else {}))


def seen(b: InProcBroker, name: str, send: Any, version: str | None) -> dict[str, Any]:
    assert send(version).status_code == 200
    return inspector(b, name)


CASES = {
    "cursor": ("cursor-1", cursor_member, cursor_event),
    "devin": ("devin-1", devin_member, devin_event),
}


@pytest.mark.parametrize("harness", ["cursor", "devin"])
def test_a_hook_that_reports_a_plugin_version_is_checked_by_major(build: InProcBroker, harness: str) -> None:
    """Cursor's and Devin's hooks send the header on their own request: an earlier major is said, with that
    harness's own update words, a later minor of this major is current, and a later major is said without
    the update. Fails if the broker reads the header only from MCP calls, or checks these harnesses against
    another's command."""
    name, joiner, event = CASES[harness]
    token, _ = joiner(build)
    member = seen(build, name, lambda v: event(build, token, v), OLD)
    assert (member["plugin_state"], member["plugin_version"]) == ("major", OLD)
    assert plugin_versions.UPDATE[harness] in member["plugin_note"]
    major, minor, _ = (int(x) for x in NOW.split("."))
    member = seen(build, name, lambda v: event(build, token, v), f"{major}.{minor + 1}.0")
    assert (member["plugin_state"], member["plugin_note"]) == ("current", "")
    ahead = f"{major + 1}.0.0"
    member = seen(build, name, lambda v: event(build, token, v), ahead)
    assert (member["plugin_state"], member["plugin_version"]) == ("major", ahead)
    assert (
        "different major" in member["plugin_note"]
        and plugin_versions.UPDATE[harness] not in member["plugin_note"]
    )


def test_opencodes_status_and_listener_report_the_plugin_version(build: InProcBroker) -> None:
    """OpenCode's plugin sends the header on ``/mcp/status`` and on ``/mcp/listen``, so either one is how
    the broker learns it. Fails if either route drops the header."""
    token = opencode_member(build)
    member = seen(build, "opencode-1", lambda v: opencode_status(build, token, v), OLD)
    assert (member["plugin_state"], member["plugin_version"]) == ("major", OLD)
    assert plugin_versions.UPDATE["opencode"] in member["plugin_note"]
    member = seen(build, "opencode-1", lambda v: opencode_status(build, token, v), NOW)
    assert member["plugin_state"] == "current"
    wake = httpx.post(  # an idle report, then a listener round with the old version, whose header counts
        f"{build.base}/mcp/status",
        json={"event": "session.execution.succeeded", "session_id": SESSION_A},
        headers={"authorization": f"Bearer {token}"},
        timeout=10,
    )
    assert wake.status_code == 200
    r = httpx.post(
        f"{build.base}/mcp/listen",
        headers={
            "authorization": f"Bearer {token}",
            "x-switchboard-session": SESSION_A,
            "x-switchboard-harness": "opencode",
            "x-switchboard-wait": "1",
            HEADER: OLD,
        },
        timeout=20,
    )
    assert r.status_code in (200, 408, 409), r.text  # whichever the round ends with, the header was read
    assert inspector(build, "opencode-1")["plugin_state"] == "major"


@pytest.mark.parametrize("harness", ["cursor", "devin", "opencode"])
def test_no_version_stays_unsaid_and_an_mcp_call_never_erases_one(build: InProcBroker, harness: str) -> None:
    """A plugin from before the header sends none, and its MCP calls never do: neither is a note (these
    harnesses are not nagged for silence, as today). A version a hook or the listener reported is kept
    through the next MCP call, which carries an empty header. Fails if silence becomes ``unknown`` over a
    reported version, or if a note appears for a plugin that never said."""
    client: Any = None
    if harness == "opencode":
        token = opencode_member(build)
        name, send = "opencode-1", lambda v: opencode_status(build, token, v)
    else:
        name, joiner, event = CASES[harness]
        token, client = joiner(build)
        send = lambda v: event(build, token, v)  # noqa: E731
    silent = seen(build, name, send, None)
    assert "plugin_state" not in silent or silent["plugin_note"] == ""
    said = seen(build, name, send, OLD)
    assert said["plugin_state"] == "major"
    assert mcp_who(build, harness, token, client)["ok"]
    member = inspector(build, name)
    assert (member["plugin_state"], member["plugin_version"]) == ("major", OLD)


def mcp_who(b: InProcBroker, harness: str, token: str, client: Any) -> dict[str, Any]:
    """A ``who`` call the broker accepts, over MCP and with no plugin header, as the harness makes it."""
    if harness == "cursor":
        pre = {"harness": "cursor", "event": "preToolUse", "session_id": SID_A, "tool_use_id": "u2"}
        report = _post(b, token, "/mcp/pretool", {**pre, "tool_name": "switchboard_who"}, None)
        assert report.status_code == 200
        args = {"room": "#build", "switchboard_session_id": SID_A, "switchboard_tool_use_id": "u2"}
        return invoke(client, "switchboard_who", args)
    if harness == "devin":
        # the pretool report by hand: the real script would send its version, and this call must not
        pre = {"harness": "devin", "event": "PreToolUse", "session_id": "devin-sid", "tool_use_id": "use-2"}
        assert _post(b, token, "/mcp/pretool", {**pre, "tool_name": "who"}, None).status_code == 200
        args = {"room": "#build", "switchboard_session_id": "devin-sid", "switchboard_tool_use_id": "use-2"}
        return client.call("who", args)
    c = OpenCode(b, token)
    c.start()
    return c.call("who", SESSION_A, room="#build")


def test_every_harness_has_a_versioned_plugin_with_its_own_update_words() -> None:
    """All five harnesses are checked by the one major rule, each with a distinct update instruction naming
    that harness (§49.13, #887). Fails if a harness is left out of either table, or two share words."""
    five = {"claude", "codex", "cursor", "devin", "opencode"}
    assert set(plugin_versions.PINNED) == set(plugin_versions.UPDATE) == five
    assert len(set(plugin_versions.UPDATE.values())) == 5
    for harness in five:
        assert harness in plugin_versions.UPDATE[harness].lower()


def test_cursors_real_hook_sends_the_plugin_version(broker: InProcBroker) -> None:
    """The plugin's own script (``bin/hook post-other``, with the recorded Shell payload) puts its manifest's
    version in the header, so a joined Cursor shows as current right after a tool call. Fails if the script
    sends none (the member would carry no plugin fields at all)."""
    from test_agents_http_any_tool import joined_cursor, recorded
    from test_agents_http_cursor import run_hook

    _, token = joined_cursor(broker)
    member = inspector(broker, "cur")  # the join's own post hook already reported it
    assert (member["plugin_state"], member["plugin_version"]) == ("current", NOW)
    shell = recorded("cursor/https_postToolUse_shell.json", conversation_id=SID_A, session_id=SID_A)
    run_hook(broker, token, "post-other", shell)
    assert inspector(broker, "cur")["plugin_version"] == NOW


def test_devins_real_hook_sends_the_plugin_version(broker: InProcBroker) -> None:
    """Devin's script (``bin/hook post-other``) sends its manifest's version the same way. Fails if the script
    sends none."""
    from test_agents_http_any_tool import devin_tool, joined_devin

    dv, sid = joined_devin(broker)
    member = inspector(broker, "dev")
    assert (member["plugin_state"], member["plugin_version"]) == ("current", NOW)  # its own join's report
    dv.run_hook("post-other", devin_tool(sid))
    assert inspector(broker, "dev")["plugin_version"] == NOW
