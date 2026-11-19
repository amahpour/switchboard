"""A plugin works with any server of the same major version (#850, DESIGN.md §49.13), checked in both
directions against the oldest release of the current major, without a network or Docker.

- The newest server against the oldest plugin: the oldest release's plugin files are kept as fixtures
  (``tests/fixtures/compat/plugin-1.0.0``, the bytes of git tag ``v1.0.0``) and run as Claude Code runs
  them, against an in-process broker of this checkout.
- The newest plugin against the oldest server: the oldest release's tool list and listener contract are
  recorded in ``tests/fixtures/compat/server-1.0.0.json``, and this checkout's plugin is checked against
  them (a stand-in server that answers as that release's listener did, and the tool surface the skills
  name).

A real old release is run by ``tests/compat`` (opt-in, ``-m compat``). When the major goes up, the
fixtures move to the new major's first release (CONTRIBUTING.md, "Releases"): a test here fails if the
fixtures are for a major other than the package's, so the move is not forgotten."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker, child_env
from test_agent_plugins import CLAUDE_PLUGIN, Stub, claude_hooks, impl, run_listen  # noqa: F401
from test_agents_http import make_token
from test_agents_http_claude import SESSION_1, Claude
from test_agents_http_wake import participant, until

from switchboard import __version__
from switchboard import board as board_module
from switchboard.broker.agent_http import HOOK_FIELDS, LISTEN_PATH
from switchboard.broker.reviews import ACTIONS as REVIEW_ACTIONS
from switchboard.mcp.server import TOOL_NAMES

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "compat"
OLD_PLUGIN = FIXTURES / "plugin-1.0.0"
OLD_SERVER = json.loads((FIXTURES / "server-1.0.0.json").read_text())
TOKEN = "sbat_" + "x" * 43


def major(version: str) -> int:
    return int(version.split(".")[0])


def test_the_fixtures_are_the_oldest_release_of_the_current_major() -> None:
    """The fixtures are for this package's major, and are the release's bytes where the tag is in
    the checkout (a developer's; CI's shallow clone has no tags). A fixture for another major would
    check nothing; one edited by hand would check what nobody ever shipped. Fails after a major bump
    until the fixtures are replaced, and if a fixture file differs from the tag."""
    assert major(OLD_SERVER["release"]) == major(__version__), (
        "move the fixtures to the new major's first release"
    )
    assert OLD_SERVER["release"] == f"{major(__version__)}.0.0"
    tag = f"v{OLD_SERVER['release']}"
    have = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], capture_output=True
    )
    if have.returncode != 0:
        pytest.skip(f"{tag} is not in this checkout")
    for rel in ("bin/listen", "bin/session", "bin/headers", "hooks/hooks.json"):
        shown = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{tag}:plugins/claude/{rel}"], capture_output=True, check=True
        ).stdout
        assert (OLD_PLUGIN / rel).read_bytes() == shown, rel


def test_the_newest_server_still_offers_everything_the_oldest_release_did() -> None:
    """The contract's first half (#850): a release of the same major removes or renames no tool, no
    action and no required parameter, keeps the listen path and takes every field a hook may send.
    The oldest release's surface is recorded; this checkout must still hold all of it. Fails when a
    change removes or renames one of them without a major version."""
    import asyncio

    from fastmcp import Client

    from switchboard.mcp.server import build_server

    class Backend:
        harness = "claude"

    async def listed() -> dict[str, Any]:
        async with Client(build_server(Backend()), mode="legacy") as c:  # type: ignore[arg-type]
            return {t.name: t.input_schema for t in await c.list_tools()}

    now = asyncio.run(listed())
    for name, old in OLD_SERVER["tools"].items():
        assert name in now, f"the tool {name} existed in {OLD_SERVER['release']}"
        props = set(now[name]["properties"])
        assert set(old["properties"]) <= props, (name, set(old["properties"]) - props)
        # a parameter that was required then is still named; it may only have become optional
        assert set(old["required"]) <= props, name
        # a parameter that is required now would have broken an agent written for the old tool
        assert set(now[name].get("required", [])) <= set(old["required"]) | {"ctx"}, (
            f"{name} newly requires {set(now[name].get('required', [])) - set(old['required'])}"
        )
    assert set(OLD_SERVER["review_actions"]) <= set(REVIEW_ACTIONS)
    assert set(OLD_SERVER["board_actions"]) <= set(board_module.ACTIONS)
    assert set(OLD_SERVER["hook_fields"]) <= set(HOOK_FIELDS)
    assert LISTEN_PATH == OLD_SERVER["listen_path"]
    assert set(OLD_SERVER["tools"]) <= set(TOOL_NAMES)


def old_plugin_hooks() -> dict[str, list[dict[str, Any]]]:
    return json.loads((OLD_PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]


def test_the_oldest_plugins_hooks_send_only_what_the_newest_server_takes() -> None:
    """Every mcp_tool hook of the oldest release's plugin calls the hook tool with fields this
    server still takes, and the same tool name. Fails when a server drops a field an old plugin's
    template still sends, which would make every old session's hook an error."""
    seen = 0
    for groups in old_plugin_hooks().values():
        for group in groups:
            for h in group["hooks"]:
                if h["type"] != "mcp_tool":
                    continue
                seen += 1
                assert h["tool"] == "hook"
                assert set(h["input"]) <= set(HOOK_FIELDS), set(h["input"]) - set(HOOK_FIELDS)
    assert seen


def run_old_listen(
    broker: InProcBroker, token: str, session: str, *, round_s: int | None = None
) -> subprocess.CompletedProcess[str]:
    """The oldest plugin's ``bin/listen``, started as Claude Code starts it (``sh``, the hook's input on
    stdin, the plugin's options in the environment)."""
    env = {
        "PATH": child_env()["PATH"],
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": broker.base,
        "CLAUDE_PLUGIN_OPTION_TOKEN": token,
        "CLAUDE_PID": str(__import__("os").getpid()),
    }
    if round_s is not None:
        env["SWITCHBOARD_LISTEN_S"] = str(round_s)
    stop = json.dumps(
        {"session_id": session, "hook_event_name": "Stop", "source": "", "stop_hook_active": False}
    )
    return subprocess.run(
        ["sh", str(OLD_PLUGIN / "bin" / "listen")],
        input=stop,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="the listener needs curl")
def test_the_oldest_plugins_listener_is_woken_by_the_newest_server(broker: InProcBroker) -> None:
    """The oldest release's listener script, unchanged, parks on this server and exits 2 with the
    message that wakes the session; with a wrong token, or for a session that never joined, it wakes
    nothing. Fails when a protocol change (the endpoint, a header, a status) strands old plugins."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    claude = Claude(broker, token, SESSION_1)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    claude.call("read", room="#build")
    pid = participant(broker, "claude-1").id
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(run_old_listen, broker, token, SESSION_1)
        until(lambda: bool(broker.on_loop(broker.state.engine.sinks.parks_for, pid)), timeout=30)
        broker.call("human.say", {"room": "#build", "text": "@claude-1 wake up"})
        r = waiting.result(60)
    assert r.returncode == 2 and "wake up" in r.stderr, r
    refused = run_old_listen(broker, "sbat_" + "y" * 43, SESSION_1)
    assert (refused.returncode, refused.stderr) == (0, ""), refused
    nobody = run_old_listen(broker, token, "00000000-0000-4000-8000-0000000000ff")
    assert (nobody.returncode, nobody.stderr) == (0, ""), nobody


@pytest.mark.usefixtures("impl")
def test_the_newest_plugins_listener_speaks_the_oldest_servers_protocol(tmp_path: Path) -> None:
    """A stand-in answers as the oldest release's listener did (``server-1.0.0.json``: 408 means
    listen again, 200 is the message, 409 ends the listener quietly), and the newest plugin's listener
    is run against it. It must wake on the message after a 408, must send only headers that release
    read, and must end without waking on a 409. Fails when the plugin's script depends on a status
    or a header that an older server of the same major does not have."""
    read = set(OLD_SERVER["listen"]["headers_read"])
    stub = Stub([(408, ""), (200, "the message")])
    try:
        r = run_listen(None, TOKEN, SESSION_1, url=stub.url, round_s=10)
    finally:
        stub.close()
    assert (r.returncode, r.stderr) == (2, "the message"), r
    assert stub.again == ["0", "1"]
    for method, path, headers, _ in stub.requests:
        assert (method, path) == (OLD_SERVER["listen"]["method"], OLD_SERVER["listen_path"])
        sent = {
            k.lower()
            for k in headers
            if k.lower().startswith("x-switchboard") or k.lower() == "authorization"
        }
        assert sent <= read, sent - read
    quiet = Stub([(409, '{"error":"nothing","message":"nothing to wake for"}')])
    try:
        r = run_listen(None, TOKEN, SESSION_1, url=quiet.url, round_s=10)
    finally:
        quiet.close()
    assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), r
    ended = Stub([(410, "another listener ended this session")])
    try:
        (tmp_path / "data").mkdir()
        r = run_listen(None, TOKEN, SESSION_1, url=ended.url, round_s=10, data=tmp_path / "data")
    finally:
        ended.close()
    assert r.returncode == 2 and "another listener ended this session" in r.stderr, r


def test_the_newest_plugins_skills_name_only_tools_the_oldest_server_has() -> None:
    """Every switchboard tool a skill of this checkout's plugins names (in backticks, as the tool or
    ``mcp__..``) exists on the oldest release of the major. A new tool needs a minor release that adds
    it, and a skill that names it would steer an agent on an older server to a tool it lacks: it fails
    until the skill is generic about it (#850, "Feature guidance lives on the server")."""
    named: dict[str, set[str]] = {}
    for plugin in ("claude", "codex", "cursor"):
        for skill in (ROOT / "plugins" / plugin / "skills").glob("*/SKILL.md"):
            text = skill.read_text()
            words = set(re.findall(r"`(?:switchboard_)?([a-z_]+)(?:\(|`)", text))
            for tool in words & set(TOOL_NAMES):
                named.setdefault(tool, set()).add(f"{plugin}/{skill.parent.name}")
    assert named  # the skills do name tools
    missing = {t: sorted(w) for t, w in named.items() if t not in OLD_SERVER["tools"]}
    assert not missing, missing


def test_the_plugins_hook_templates_are_what_the_oldest_plugin_sent_or_add_fields_the_server_takes() -> None:
    """The newest plugin's mcp_tool hooks send only fields the oldest server's hook tool took, or the
    newest server adds: a hook field the old server did not know is refused by it (a plugin ahead of
    its server). Fails when a hook template gains a field with no major version."""
    old = set(OLD_SERVER["hook_fields"])
    for groups in claude_hooks().values():
        for group in groups:
            for h in group["hooks"]:
                if h["type"] == "mcp_tool":
                    assert set(h["input"]) <= old, (
                        f"{h['input']} sends a field release {OLD_SERVER['release']} refuses; "
                        "that is a major, not a minor (DESIGN.md §49.13)"
                    )
    assert CLAUDE_PLUGIN.is_dir()
