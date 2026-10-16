"""The plugins that connect an agent over HTTPS (#384, DESIGN.md §49.5): their files agree with the
server they talk to, and Claude Code's listener script, run as Claude Code runs it, wakes the
session with a message and stays quiet otherwise."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from test_agents_http import make_token, sse_result
from test_agents_http_claude import SESSION_1, Claude
from test_agents_http_wake import parked, participant, until

from switchboard.broker.agent_http import HOOK_FIELDS, HOOK_TOOL, LISTEN_PATH, PATH

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_PLUGIN = ROOT / "plugins" / "claude"
LISTEN = CLAUDE_PLUGIN / "bin" / "listen"


def load(p: Path) -> Any:
    return json.loads(p.read_text())


def claude_hooks() -> dict[str, list[dict[str, Any]]]:
    return dict(load(CLAUDE_PLUGIN / "hooks" / "hooks.json")["hooks"])


def test_the_marketplace_lists_the_plugin_where_it_is() -> None:
    """`/plugin install switchboard --marketplace amahpour/switchboard` reads the repo's marketplace,
    which must point at the plugin's folder and name it as its manifest does. Fails if the two
    drift apart."""
    mk = load(ROOT / ".claude-plugin" / "marketplace.json")
    [entry] = mk["plugins"]
    folder = (ROOT / entry["source"]).resolve()
    assert folder == CLAUDE_PLUGIN.resolve()
    manifest = load(folder / ".claude-plugin" / "plugin.json")
    assert entry["name"] == manifest["name"] == mk["name"] == "switchboard"


def test_the_claude_plugin_asks_for_the_server_and_a_secret_token() -> None:
    """The plugin's two settings: the server's address, and the agent token, kept as a secret by
    Claude Code (``sensitive``), never in a settings file. Its MCP server is that address's /mcp
    with that token. Fails if the token isn't sensitive or the server's path is wrong."""
    cfg = load(CLAUDE_PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]
    assert cfg["server_url"]["required"] and cfg["token"]["required"]
    assert cfg["token"]["sensitive"] is True and "sensitive" not in cfg["server_url"]
    [(name, server)] = load(CLAUDE_PLUGIN / ".mcp.json")["mcpServers"].items()
    assert name == "switchboard" and server["type"] == "http"
    assert server["url"] == "${user_config.server_url}" + PATH
    assert server["headers"] == {"Authorization": "Bearer ${user_config.token}"}


def test_every_hook_calls_the_hook_tool_with_fields_it_takes() -> None:
    """Each mcp_tool hook calls this plugin's own server's hook tool, with only the fields that tool
    takes; the ones about tools match switchboard's own tools only, so no other tool's input or
    result leaves the machine; and Stop runs the listener in the background. Fails if a hook names
    a field the server doesn't take, matches every tool, or the listener can't wake the session."""
    hooks = claude_hooks()
    assert set(hooks) == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "SessionEnd",
    }
    for event, groups in hooks.items():
        for group in groups:
            for h in group["hooks"]:
                if h["type"] == "command":
                    continue
                assert (h["server"], h["tool"]) == ("plugin:switchboard:switchboard", HOOK_TOOL), event
                assert set(h["input"]) <= set(HOOK_FIELDS), (event, set(h["input"]) - set(HOOK_FIELDS))
                assert h["input"]["event"] == "${hook_event_name}"
                assert h["input"]["session_id"] == "${session_id}"
            if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
                assert group["matcher"] == "mcp__plugin_switchboard_switchboard__.*", event
    [stop] = [h for g in hooks["Stop"] for h in g["hooks"]]
    assert stop["type"] == "command" and stop["asyncRewake"] is True
    assert stop["command"] == '"${CLAUDE_PLUGIN_ROOT}"/bin/listen' and stop["timeout"] > 900
    assert os.access(LISTEN, os.X_OK)


def test_the_hook_tool_takes_every_field_a_hook_may_send(broker: InProcBroker) -> None:
    """The server's hook tool accepts each of ``HOOK_FIELDS`` at once, as text. Fails if the tool's
    parameters and the list the plugins are checked against drift apart."""
    claude = Claude(broker, make_token(broker, "laptop"), SESSION_1)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": HOOK_TOOL, "arguments": {k: "x" for k in HOOK_FIELDS}},
    }
    from test_agents_http_claude import HOOK

    body["params"]["_meta"] = HOOK["body"]["params"]["_meta"]
    r = claude.post(body, HOOK["headers"], HOOK_TOOL)
    msg = sse_result(r)
    assert "result" in msg and not msg["result"].get("isError"), msg


def run_listen(
    b: InProcBroker, token: str, session: str, url: str | None = None
) -> subprocess.CompletedProcess[str]:
    """``bin/listen`` as Claude Code runs it at Stop: the hook's input on stdin, the plugin's settings
    in ``CLAUDE_PLUGIN_OPTION_*``. Its own wait is the script's; the test's is a safety net."""
    env = {
        "PATH": os.environ["PATH"],
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": url or b.base,
        "CLAUDE_PLUGIN_OPTION_TOKEN": token,
    }
    stop = json.dumps({"session_id": session, "hook_event_name": "Stop", "stop_hook_active": False})
    return subprocess.run(
        ["sh", str(LISTEN)], input=stop, env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="the listener needs curl")
def test_the_listener_wakes_with_the_message_and_is_quiet_otherwise(broker: InProcBroker) -> None:
    """Run the plugin's own listener script against the broker: with a message for the session it
    exits 2 with the message on stderr (which wakes Claude Code); ended by a newer listener, with a
    wrong token, or for a session that never joined, it exits 0 and prints nothing (which wakes
    nothing). Fails if the script wakes on anything but a message, or misses one."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    claude = Claude(broker, token, SESSION_1)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    claude.call("read", room="#build")
    pid = participant(broker, "claude-1").id
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(run_listen, broker, token, SESSION_1)
        until(lambda: parked(broker, pid))
        second = pool.submit(run_listen, broker, token, SESSION_1)  # supersedes the first
        r = first.result(60)
        assert (r.returncode, r.stderr) == (0, ""), r
        until(lambda: parked(broker, pid))
        broker.call("human.say", {"room": "#build", "text": "@claude-1 wake up"})
        r = second.result(60)
    assert r.returncode == 2 and "wake up" in r.stderr, r
    assert run_listen(broker, "sbat_" + "x" * 43, SESSION_1).returncode == 0
    nobody = run_listen(broker, token, "00000000-0000-4000-8000-0000000000ff")
    assert (nobody.returncode, nobody.stderr) == (0, ""), nobody
    assert run_listen(broker, token, "not a session; rm -rf ~").returncode == 0  # never sent
    assert LISTEN_PATH == "/mcp/listen"  # the path the script posts to


def test_the_listener_keeps_the_token_off_its_command_line() -> None:
    """Other processes on the machine can read a command line; the token reaches curl on stdin
    (``-K -``). Fails if the script ever puts the token in curl's arguments."""
    text = LISTEN.read_text()
    assert "-K -" in text
    curl_lines = [line for line in text.splitlines() if "curl " in line]
    assert curl_lines and all("TOKEN" not in line for line in curl_lines)


CODEX_PLUGIN = ROOT / "plugins" / "codex"


def test_codexs_marketplace_lists_its_plugin_where_it_is() -> None:
    """`codex plugin marketplace add amahpour/switchboard` reads `.agents/plugins/marketplace.json`
    (first, before Claude Code's), whose entry must point at the Codex plugin's folder and match its
    manifest's name, or Codex refuses to install it. Fails if they drift apart."""
    mk = load(ROOT / ".agents" / "plugins" / "marketplace.json")
    [entry] = mk["plugins"]
    assert entry["source"] == {"source": "local", "path": "./plugins/codex"}
    manifest = load(CODEX_PLUGIN / ".codex-plugin" / "plugin.json")
    assert entry["name"] == manifest["name"] == mk["name"] == "switchboard"
    assert manifest["hooks"] == "./hooks/hooks.json" and "mcpServers" not in manifest
    assert not (CODEX_PLUGIN / ".mcp.json").exists()  # its server is the user's config.toml entry


def test_codexs_hooks_take_what_the_hook_tool_takes_and_what_codex_allows() -> None:
    """Codex refuses a hook file with a top-level key but `description` and `hooks`, or a null in an
    input; skips an mcp_tool hook at SessionEnd, with a warning; and fails a hook whose template
    names a field its event doesn't always carry. Each hook here calls the `switchboard` server's
    hook tool with fields it takes, PostToolUse matches switchboard's tools only, and Stop waits
    for less than its own timeout. Fails if a hook breaks any of these, which would break the file
    or the hook, or leak another tool's result."""
    doc = load(CODEX_PLUGIN / "hooks" / "hooks.json")
    assert set(doc) == {"description", "hooks"}
    hooks = doc["hooks"]
    assert set(hooks) == {"UserPromptSubmit", "PostToolUse", "Stop"}
    common = {
        "session_id",
        "turn_id",
        "hook_event_name",
        "permission_mode",
        "cwd",
        "model",
        "transcript_path",
    }
    allowed = {  # what each event always carries (codex-rs/hooks/schema/generated, rust-v0.158.0)
        "UserPromptSubmit": common | {"prompt"},
        "PostToolUse": common | {"tool_name", "tool_use_id", "tool_input", "tool_response"},
        "Stop": common | {"last_assistant_message", "stop_hook_active"},
    }
    for event, groups in hooks.items():
        for group in groups:
            for h in group["hooks"]:
                assert (h["type"], h["server"], h["tool"]) == ("mcp_tool", "switchboard", HOOK_TOOL)
                inp = h["input"]
                assert inp["event"] == event and None not in inp.values()
                assert set(inp) <= set(HOOK_FIELDS)
                named = {v[2:-1] for v in inp.values() if isinstance(v, str) and v.startswith("${")}
                assert named <= allowed[event], (event, named - allowed[event])
    [post] = hooks["PostToolUse"]
    assert post["matcher"] == "mcp__switchboard__.*"
    [stop] = [h for g in hooks["Stop"] for h in g["hooks"]]
    assert float(stop["input"]["wait_s"]) < stop["timeout"]
