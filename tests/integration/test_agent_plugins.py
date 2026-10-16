"""The plugins that connect an agent over HTTPS (#384, DESIGN.md §49.5): their files agree with the
server they talk to, and Claude Code's listener script, run as Claude Code runs it, wakes the
session with a message and stays quiet otherwise."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker, child_env
from test_agents_http import make_token, sse_result
from test_agents_http_claude import SESSION_1, Claude
from test_agents_http_wake import parked, participant, until

from switchboard.broker.agent_http import HOOK_FIELDS, HOOK_TOOL, LISTEN_PATH, PATH

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_PLUGIN = ROOT / "plugins" / "claude"
LISTEN = CLAUDE_PLUGIN / "bin" / "listen"
SESSION = CLAUDE_PLUGIN / "bin" / "session"


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
    result leaves the machine; SessionStart and SessionEnd are command hooks (Claude Code 2.1.283
    and 2.1.299 refuse an mcp_tool hook there: "no MCP client context", #399); and Stop runs the
    listener in the background. Fails if a hook names a field the server doesn't take, matches every
    tool, is an mcp_tool hook where Claude Code won't run one, or the listener can't wake the session."""
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
    for event in ("SessionStart", "SessionEnd"):  # Claude Code runs no mcp_tool hook at these (#399)
        [h] = [h for g in hooks[event] for h in g["hooks"]]
        assert h["type"] == "command" and h["command"] == '"${CLAUDE_PLUGIN_ROOT}"/bin/session', event
    assert os.access(SESSION, os.X_OK)
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
    b: InProcBroker | None,
    token: str,
    session: str,
    url: str | None = None,
    owner: int | None = None,
    round_s: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """``bin/listen`` as Claude Code runs it at Stop: the hook's input on stdin, the plugin's settings
    in ``CLAUDE_PLUGIN_OPTION_*``, and Claude Code's own pid in ``CLAUDE_PID`` (here the test's, or
    ``owner``). ``round_s`` shortens a round. The test's timeout is a safety net."""
    env = {
        "PATH": os.environ["PATH"],
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": url or (b.base if b else ""),
        "CLAUDE_PLUGIN_OPTION_TOKEN": token,
        "CLAUDE_PID": str(owner if owner is not None else os.getpid()),
    }
    if round_s is not None:
        env["SWITCHBOARD_LISTEN_S"] = str(round_s)
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


@pytest.mark.skipif(shutil.which("curl") is None, reason="the listener needs curl")
def test_the_listener_waits_again_when_a_round_runs_out(broker: InProcBroker) -> None:
    """An idle session stays reachable past one round (#384): when a round runs out with nothing,
    the listener starts the next, and a message then wakes it. Fails if the listener gives up after
    its first round, as it did when one wait was all it had."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    claude = Claude(broker, token, SESSION_1)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    claude.call("read", room="#build")
    pid = participant(broker, "claude-1").id

    def park_ids() -> set[int]:
        return {s.id for s in broker.on_loop(broker.state.engine.sinks.parks_for, pid)}

    with ThreadPoolExecutor(1) as pool:
        run = pool.submit(run_listen, broker, token, SESSION_1, None, None, 10)  # rounds of 5 s
        until(lambda: bool(park_ids()))
        first = park_ids()
        until(lambda: bool(park_ids()) and park_ids() != first, timeout=20)  # the second round
        broker.call("human.say", {"room": "#build", "text": "@claude-1 are you there"})
        r = run.result(60)
    assert r.returncode == 2 and "are you there" in r.stderr, r


class Stub:
    """A stand-in server for the listener: answers each POST with the next scripted reply (a status
    and a body; ``None`` holds the request until the test lets it go), and records each request's
    ``X-Switchboard-Again``."""

    def __init__(self, replies: list[tuple[int, str] | None]) -> None:
        self.replies = list(replies)
        self.again: list[str] = []
        self.release = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                stub.again.append(self.headers.get("X-Switchboard-Again", ""))
                reply = stub.replies.pop(0) if stub.replies else (409, "")
                if reply is None:
                    stub.release.wait(30)
                    reply = (409, "")
                status, body = reply
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()


@pytest.mark.skipif(shutil.which("curl") is None, reason="the listener needs curl")
def test_the_listener_rides_out_a_server_that_is_away_and_says_which_round_it_is() -> None:
    """A server restarting, or a proxy that cut a long wait, answers 5xx (or nothing): the listener
    tries again, then wakes on the message that comes. Its first round is the session's Stop, and
    every later one says so (``X-Switchboard-Again: 1``). Fails if a 5xx ends the listener, or a
    later round passes for a new Stop."""
    stub = Stub([(503, "restarting"), (408, ""), (200, "the message")])
    try:
        r = run_listen(None, "sbat_" + "x" * 43, SESSION_1, url=stub.url, round_s=10)
    finally:
        stub.close()
    assert (r.returncode, r.stderr) == (2, "the message"), r
    assert stub.again == ["0", "1", "1"]


@pytest.mark.skipif(shutil.which("curl") is None, reason="the listener needs curl")
def test_the_listener_ends_with_claude_code() -> None:
    """A background hook outlives Claude Code when it quits (seen with 2.1.300), so the listener
    watches Claude Code's pid (``CLAUDE_PID``): it never starts for one that's gone, and stops within
    seconds of it going mid-wait, waking nothing. Fails if a listener keeps waiting for a session
    whose Claude Code has quit."""
    gone = subprocess.Popen(["sleep", "0"])
    gone.wait()
    stub = Stub([(200, "never sent")])
    try:
        r = run_listen(None, "sbat_" + "x" * 43, SESSION_1, url=stub.url, owner=gone.pid)
        assert (r.returncode, r.stderr, stub.again) == (0, "", []), r
    finally:
        stub.close()
    owner = subprocess.Popen(["sleep", "60"])
    stub = Stub([None])  # holds the wait open
    try:
        with ThreadPoolExecutor(1) as pool:
            run = pool.submit(run_listen, None, "sbat_" + "x" * 43, SESSION_1, stub.url, owner.pid)
            until(lambda: stub.again == ["0"])
            owner.kill()
            owner.wait()
            t0 = time.monotonic()
            r = run.result(30)
        assert (r.returncode, r.stderr) == (0, ""), r
        assert time.monotonic() - t0 < 10
    finally:
        owner.kill()
        stub.close()


# ------------------------------------------------------- a change to a plugin bumps its version
MANIFESTS = {
    "plugins/claude": ".claude-plugin/plugin.json",
    "plugins/codex": ".codex-plugin/plugin.json",
}


def vcs(repo: Path, *args: str) -> str | None:
    """Git's output in ``repo``, or None where git isn't there, it isn't a repository, or the
    command fails."""
    try:
        r = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=repo, capture_output=True, text=True, timeout=60, env=child_env(),
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def base_commit(repo: Path) -> str | None:
    """What this work is compared with: the merge base with origin/main, or, in CI's checkout of a
    pull request's merge commit (which has no origin/main), that commit's first parent, which is
    main as it is. None where neither is known (the Docker test image has no .git)."""
    base = vcs(repo, "merge-base", "HEAD", "origin/main")
    if base:
        return base.strip()
    parents = vcs(repo, "rev-list", "--parents", "-n", "1", "HEAD")
    if parents and len(parents.split()) == 3:
        return parents.split()[1]
    return None


def version_at(repo: Path, rev: str, path: str) -> str | None:
    text = vcs(repo, "show", f"{rev}:{path}")
    return json.loads(text)["version"] if text else None


def unbumped_plugins(repo: Path, base: str) -> list[str]:
    """The plugins with a file changed since ``base`` (committed, edited or new) whose manifest
    ``version`` is still what it was there."""
    out = []
    for folder, manifest in MANIFESTS.items():
        changed = (vcs(repo, "diff", "--name-only", base, "--", folder) or "").split()
        changed += (vcs(repo, "ls-files", "--others", "--exclude-standard", folder) or "").split()
        old = version_at(repo, base, f"{folder}/{manifest}")
        if changed and old is not None and old == load(repo / folder / manifest)["version"]:
            out.append(f"{folder} ({', '.join(sorted(changed))}) is still at {old}")
    return out


def test_a_change_to_a_plugin_bumps_its_version() -> None:
    """Claude Code and Codex keep an installed plugin by its `version`, so a change to its hooks or
    listener that leaves the version alone never reaches anyone who has installed it (DESIGN.md
    §49.5, #402). Compares each plugin's files with main and fails if one changed and its manifest
    version didn't. Skipped where there is no git history to compare with."""
    base = base_commit(ROOT)
    if base is None:
        pytest.skip("no git history to compare the plugins with")
    assert unbumped_plugins(ROOT, base) == []


def test_the_version_check_sees_a_changed_file_and_a_new_one_and_accepts_a_bump(tmp_path: Path) -> None:
    """The check above, on a throwaway repository: an edit to a plugin file, or a new file in the
    plugin, without a bump is found; the same with a bump, and a change outside the plugins, are
    not. Fails if the check can't tell them apart (so it would pass whatever the plugins do)."""
    if vcs(tmp_path, "--version") is None:
        pytest.skip("git is not available")
    repo = tmp_path / "repo"
    for folder, manifest in MANIFESTS.items():
        (repo / folder / Path(manifest).parent).mkdir(parents=True)
        (repo / folder / manifest).write_text('{"name": "switchboard", "version": "1.0.0"}')
        (repo / folder / "hooks.json").write_text("{}")
    assert vcs(repo, "init", "-q", "-b", "main") is not None
    ident = ["-c", "user.name=alice", "-c", "user.email=alice@example.com"]
    assert vcs(repo, "add", "-A") is not None
    assert vcs(repo, *ident, "commit", "-q", "-m", "start") is not None
    base = (vcs(repo, "rev-parse", "HEAD") or "").strip()
    assert base and unbumped_plugins(repo, base) == []

    (repo / "plugins/claude/hooks.json").write_text('{"hooks": {}}')  # an edit, no bump
    (repo / "plugins/codex/bin.sh").write_text("exit 0")  # a new file, no bump
    (repo / "README.md").write_text("outside the plugins")
    found = unbumped_plugins(repo, base)
    assert [f.split(" ")[0] for f in found] == ["plugins/claude", "plugins/codex"], found

    for folder, manifest in MANIFESTS.items():
        (repo / folder / manifest).write_text('{"name": "switchboard", "version": "1.0.1"}')
    assert unbumped_plugins(repo, base) == []
    assert vcs(repo, "add", "-A") is not None  # committed work is compared the same way
    assert vcs(repo, *ident, "commit", "-q", "-m", "change") is not None
    assert unbumped_plugins(repo, base) == []


def run_session(
    b: InProcBroker, token: str, session: str, event: str, **fields: str
) -> subprocess.CompletedProcess[str]:
    """``bin/session`` as Claude Code runs it: the hook's input on stdin (compact JSON, as Claude Code
    writes it), the plugin's settings in ``CLAUDE_PLUGIN_OPTION_*``."""
    env = {
        "PATH": os.environ["PATH"],
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": b.base,
        "CLAUDE_PLUGIN_OPTION_TOKEN": token,
    }
    hook = json.dumps(
        {"session_id": session, "hook_event_name": event, "cwd": "/x/y", **fields}, separators=(",", ":")
    )
    return subprocess.run(
        ["sh", str(SESSION)], input=hook, env=env, capture_output=True, text=True, timeout=30
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="the script needs curl")
def test_the_session_script_takes_a_quit_session_offline(broker: InProcBroker) -> None:
    """Claude Code won't run an mcp_tool hook at SessionEnd (#399), so the plugin's ``bin/session``
    sends it: the session goes offline at once, and the script exits 0 and prints nothing. A
    SessionStart reports too, and an event that isn't one of the two sends nothing. Fails if a quit
    session stays idle in the room, or the script prints anything for a SessionEnd."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    claude = Claude(broker, token, SESSION_1)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    r = run_session(broker, token, SESSION_1, "SessionStart", source="resume", permission_mode="default")
    assert (r.returncode, r.stderr) == (0, ""), r
    assert participant(broker, "claude-1").status != "offline"
    assert run_session(broker, token, SESSION_1, "Stop").returncode == 0
    assert participant(broker, "claude-1").status != "offline"  # not one of its events
    r = run_session(broker, token, SESSION_1, "SessionEnd", reason="prompt_input_exit")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), r
    until(lambda: participant(broker, "claude-1").status == "offline")


def test_the_session_script_keeps_the_token_off_its_command_line() -> None:
    """As the listener does: the token reaches curl on stdin (``-K -``). Fails if it ever goes in
    curl's arguments."""
    text = SESSION.read_text()
    assert "-K -" in text
    curl_lines = [line for line in text.splitlines() if "curl " in line and not line.lstrip().startswith("#")]
    assert curl_lines and all("token" not in line.lower() for line in curl_lines)
