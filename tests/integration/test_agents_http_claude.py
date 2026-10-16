"""Claude Code over HTTPS (#382, DESIGN.md §49.3): its calls name the call, never the session, and
its PreToolUse hook, a call of the hidden ``hook`` tool just before, says which session made each.
Driven with what Claude Code 2.1.300 sends (``tests/fixtures/mcp_http/claude_*.json``, #380's spike):
stateless requests, each carrying the client's ``_meta``."""

from __future__ import annotations

import copy
import itertools
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_agents_http import Codex, make_token, members, sse_result

from switchboard import __version__
from switchboard.broker import agent_http
from switchboard.mcp.server import TOOL_NAMES

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_http"
DISCOVER = json.loads((FIX / "claude_discover.json").read_text())
CALL = json.loads((FIX / "claude_tools_call.json").read_text())
HOOK = json.loads((FIX / "claude_hook_call.json").read_text())
SESSION_1 = "00000000-0000-4000-8000-0000000000d1"
SESSION_2 = "00000000-0000-4000-8000-0000000000d2"
USES = (f"toolu_01{n:024d}" for n in itertools.count(100))


class Claude:
    """One Claude Code session's MCP client: every request stands alone, with its ``_meta``."""

    def __init__(self, b: InProcBroker, token: str, session: str) -> None:
        self.c = httpx.Client(base_url=b.base, timeout=30.0)
        self.token = token
        self.session = session
        self.ids = itertools.count(10)

    def post(self, body: dict[str, Any], recorded: dict[str, str], name: str | None = None) -> httpx.Response:
        h = {**recorded, "authorization": f"Bearer {self.token}"}
        if name is not None:
            h["mcp-name"] = name
        return self.c.post("/mcp", json=body, headers=h)

    def discover(self) -> httpx.Response:
        return self.post(DISCOVER["body"], DISCOVER["headers"])

    def hook(self, use: str, *, event: str = "PreToolUse", tool: str = "mcp__switchboard__join") -> str:
        body = copy.deepcopy(HOOK["body"])
        body["id"] = next(self.ids)
        body["params"]["arguments"] = {
            "event": event,
            "session_id": self.session,
            "tool_use_id": use,
            "tool_name": tool,
        }
        r = self.post(body, HOOK["headers"], "hook")
        assert r.status_code == 200, r.text
        return str(sse_result(r)["result"]["content"][0]["text"])

    def raw(self, tool: str, use: str, **arguments: Any) -> httpx.Response:
        body = copy.deepcopy(CALL["body"])
        body["id"] = next(self.ids)
        body["params"]["name"] = tool
        body["params"]["arguments"] = arguments
        body["params"]["_meta"]["claudecode/toolUseId"] = use
        return self.post(body, CALL["headers"], tool)

    def call(self, tool: str, *, hook: bool = True, **arguments: Any) -> dict[str, Any]:
        """A tool call as Claude Code makes one: its PreToolUse hook first, then the call."""
        use = next(USES)
        if hook:
            assert self.hook(use, tool=f"mcp__switchboard__{tool}") == "{}"
        r = self.raw(tool, use, **arguments)
        assert r.status_code == 200, (r.status_code, r.text)
        return json.loads(sse_result(r)["result"]["content"][0]["text"])

    def tools(self) -> list[str]:
        body = {
            "jsonrpc": "2.0",
            "id": next(self.ids),
            "method": "tools/list",
            "params": {"_meta": CALL["body"]["params"]["_meta"]},
        }
        r = self.post(body, {**CALL["headers"], "mcp-method": "tools/list"})
        return sorted(t["name"] for t in sse_result(r)["result"]["tools"])


@pytest.fixture
def build(broker: InProcBroker) -> InProcBroker:
    broker.call("room.create", {"name": "#build"})
    return broker


def test_two_claude_sessions_on_one_device_are_two_members(build: InProcBroker) -> None:
    """Each call is matched to the session its hook named, so two Claude Code sessions on one
    device join, talk and read as two members. Fails if the hook's report isn't used, or the two
    sessions collapse into one."""
    token = make_token(build, "laptop")
    one, two = Claude(build, token, SESSION_1), Claude(build, token, SESSION_2)
    assert one.discover().status_code == 200
    assert one.call("join", room="#build", screen_name="claude-1")["ok"]
    assert two.call("join", room="#build", screen_name="claude-2")["ok"]
    assert members(build, "#build") == {"claude-1": ("claude", "laptop"), "claude-2": ("claude", "laptop")}
    assert one.call("say", room="#build", text="hello from session one")["ok"]
    got = two.call("read", room="#build")
    assert got["ok"] and "hello from session one" in got["text"], got
    assert two.call("leave", room="#build")["ok"]
    assert set(members(build, "#build")) == {"claude-1"}
    st = build.state
    room = build.on_loop(st.store.get_room, "#build")
    [m] = build.on_loop(st.store.members, room.id)
    p = build.on_loop(st.store.get_participant, m.participant_id)
    assert (p.session_key, p.tier) == (f"claude@laptop:{SESSION_1}", "claude:https")


def test_a_call_waits_a_moment_for_its_hook(build: InProcBroker) -> None:
    """The hook's report and the call are two requests, and the call can come first: it waits
    for the report rather than failing. Fails if a call that beat its hook is refused."""
    claude = Claude(build, make_token(build, "laptop"), SESSION_1)
    use = next(USES)
    with ThreadPoolExecutor(1) as pool:
        joining = pool.submit(claude.raw, "join", use, room="#build", screen_name="claude-1")
        claude.hook(use)
        r = joining.result(30)
    assert json.loads(sse_result(r)["result"]["content"][0]["text"])["ok"]
    assert set(members(build, "#build")) == {"claude-1"}


def test_a_call_with_no_hook_report_is_refused_with_why(
    build: InProcBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the PreToolUse hook nothing names the session, so the call is refused, and the
    answer says what to set up. Fails if such a call joins anyone, or the refusal doesn't say."""
    monkeypatch.setattr(agent_http, "HOOK_WAIT_S", 0.2)
    claude = Claude(build, make_token(build, "laptop"), SESSION_1)
    got = claude.call("join", hook=False, room="#build", screen_name="claude-1")
    assert not got["ok"] and "PreToolUse hook" in got["error"], got
    assert members(build, "#build") == {}


def test_another_tokens_hook_names_nothing_here(build: InProcBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook's report is its token's: one device can't say which session another device's call
    belongs to. Fails if a report under one token names the session of a call under another."""
    monkeypatch.setattr(agent_http, "HOOK_WAIT_S", 0.2)
    mine = Claude(build, make_token(build, "laptop"), SESSION_1)
    theirs = Claude(build, make_token(build, "desk"), SESSION_2)
    use = next(USES)
    theirs.hook(use)
    r = mine.raw("join", use, room="#build", screen_name="claude-1")
    got = json.loads(sse_result(r)["result"]["content"][0]["text"])
    assert not got["ok"] and "PreToolUse hook" in got["error"], got


def test_only_pretooluse_names_a_session(build: InProcBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    """Other events report nothing yet (#383): a PostToolUse with the same ids doesn't name the
    call's session. Fails if any hook event is taken as the call's."""
    monkeypatch.setattr(agent_http, "HOOK_WAIT_S", 0.2)
    claude = Claude(build, make_token(build, "laptop"), SESSION_1)
    use = next(USES)
    assert claude.hook(use, event="PostToolUse") == "{}"
    r = claude.raw("join", use, room="#build", screen_name="claude-1")
    assert not json.loads(sse_result(r)["result"]["content"][0]["text"])["ok"]


def test_the_hook_tool_is_hidden_and_decides_nothing(build: InProcBroker) -> None:
    """The model never sees the hook tool, and the hook's answer is ``{}``: no decision, no
    context, nothing a harness acts on. Fails if it is listed, or answers anything else."""
    claude = Claude(build, make_token(build, "laptop"), SESSION_1)
    assert claude.tools() == sorted(TOOL_NAMES)
    assert claude.hook(next(USES)) == "{}"


def test_the_server_tells_an_agent_its_version_behind_the_token(build: InProcBroker) -> None:
    """The version of the server an agent talks to comes in its serverInfo (#402): in Codex's
    initialize answer, and under `_meta` in Claude Code's discover. It comes only with a token:
    /healthz stays "ok" for anyone, so the version isn't a fingerprint. Fails if serverInfo lacks
    the version, or a request without a valid token learns it."""
    codex = Codex(build, make_token(build, "desk"))
    info = sse_result(codex.start())["result"]["serverInfo"]
    assert (info["name"], info["version"]) == ("switchboard", __version__)
    claude = Claude(build, make_token(build, "laptop"), SESSION_1)
    info = sse_result(claude.discover())["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert (info["name"], info["version"]) == ("switchboard", __version__)
    for bad in (None, "sbat_" + "x" * 43):
        r = Codex(build, bad).start()
        assert r.status_code == 401 and __version__ not in r.text
    assert httpx.get(f"{build.base}/healthz").text == "ok\n"
