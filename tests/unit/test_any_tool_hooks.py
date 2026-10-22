"""Cursor and Devin report every tool, but never what a tool did (#493, DESIGN.md §49.6, §49.7).

A busy agent hears a priority message only through a hook that runs after a tool call. Cursor's and Devin's
tool hooks matched switchboard's own tools only, so an agent that works with a shell and its files heard
nothing until its next switchboard call: the problem #422 fixed for Claude Code and Codex. The fix is the
same: a second hook group for every other tool, whose script sends the event, the session, the call's id and
the tool's name, and nothing else. A tool's input and output are the user's (a shell command, its output, a
file, a secret) and a hosted broker is another machine (DESIGN.md §11).

These tests read the two plugins' hook files and run their scripts, as the harnesses run them, against a
stand-in server: the files say which tools reach the script, the script says what leaves the machine.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAYLOADS = ROOT / "tests" / "fixtures" / "payloads"
CURSOR_PLUGIN = ROOT / "plugins" / "cursor"
DEVIN_PLUGIN = ROOT / "plugins" / "devin"
SECRET = "SECRET-OF-THE-TOOL-9f3a"
TOKEN = "sbat_" + "a1" * 20

CURSOR_OWN = [f"MCP:switchboard_{t}" for t in ("join", "say", "read", "wait", "status")]
DEVIN_OWN = [f"mcp__switchboard_https__{t}" for t in ("join", "say", "read", "wait", "status")]
CURSOR_OTHER = [
    "Shell",
    "Read",
    "Write",
    "StrReplace",
    "Grep",
    "Glob",
    "Task",
    "WebFetch",
    "MCP:github_create_issue",
]
DEVIN_OTHER = [
    "exec",
    "read",
    "edit",
    "grep",
    "glob",
    "run_subagent",
    "mcp_call_tool",
    "mcp__github__create_issue",
]


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def groups_for(groups: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    """The groups that run for ``tool``: no matcher or an empty one matches every tool; any other is a
    regular expression searched in the tool's name."""
    return [g for g in groups if g.get("matcher") in (None, "", "*") or re.search(g["matcher"], tool)]


def cursor_hooks() -> dict[str, list[dict[str, Any]]]:
    return dict(load(CURSOR_PLUGIN / "hooks" / "hooks.json")["hooks"])


def devin_hooks() -> dict[str, list[dict[str, Any]]]:
    return dict(load(DEVIN_PLUGIN / "config.example.json")["hooks"])


def test_cursors_plugin_runs_a_group_of_its_own_after_every_tool_but_switchboards() -> None:
    """After a tool, Cursor runs `bin/hook post` for switchboard's own tools (it carries the batch tokens
    of their answers) and `bin/hook post-other` for every other tool, never both, for the success event
    and the failure event alike. The other group is short, because it runs on every tool call, and it is
    the only way a shell command or an edit gets a busy agent its messages. preToolUse stays on
    switchboard's tools: it only rewrites their input. Fails if a tool matches both groups or neither
    (so Cursor runs the script twice for a switchboard call, or none after a shell command), or the other
    group waits as long as the rest."""
    hooks = cursor_hooks()
    for event, mine, others in (
        ("postToolUse", "post", "post-other"),
        ("postToolUseFailure", "failure", "failure-other"),
    ):
        for tool in CURSOR_OWN:
            [g] = groups_for(hooks[event], tool)
            assert g["command"].endswith(f'/bin/hook" {mine}'), (event, tool, g)
        for tool in CURSOR_OTHER:
            [g] = groups_for(hooks[event], tool)
            assert g["command"].endswith(f'/bin/hook" {others}'), (event, tool, g)
            assert 0 < g["timeout"] <= 2, (event, tool)  # on every tool call: a slow server must not hold it
    for tool in CURSOR_OTHER:
        assert groups_for(hooks["preToolUse"], tool) == [], tool
    assert [g["command"].rsplit(" ", 1)[1] for g in hooks["preToolUse"]] == ["pre"]


def test_devins_template_runs_a_group_of_its_own_after_every_tool() -> None:
    """Devin's matchers are regular expressions with no look-around (3000.11.92, live: a look-ahead matched
    no tool at all, and Devin said nothing), so the group for every other tool has the matcher "" and runs
    after switchboard's own tools too; `bin/hook post-other` sends nothing for those (the group with their
    results does the work, tested below). PreToolUse stays on switchboard's tools. Fails if the group is
    missing, waits as long as the rest, or any matcher uses look-around, which would silently never run."""
    hooks = devin_hooks()
    for tool in DEVIN_OWN:
        assert [g["hooks"][0]["command"].split()[-1] for g in groups_for(hooks["PostToolUse"], tool)] == [
            "post",
            "post-other",
        ], tool
    for tool in DEVIN_OTHER:
        [g] = groups_for(hooks["PostToolUse"], tool)
        [h] = g["hooks"]
        assert h["command"].endswith("/bin/hook post-other") and g["matcher"] == "", tool
        assert h["type"] == "command" and 0 < h["timeout"] <= 2, tool
        assert groups_for(hooks["PreToolUse"], tool) == [], tool
    for event, groups in hooks.items():
        for g in groups:
            assert not re.search(r"\(\?[=!<]", g.get("matcher", "")), (event, g["matcher"])


class Stand(BaseHTTPRequestHandler):
    """A server that records each request and answers with whatever the test set."""

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        self.server.seen.append(  # type: ignore[attr-defined]
            {"path": self.path, "body": body, "auth": self.headers.get("authorization", "")}
        )
        status, data = self.server.reply  # type: ignore[attr-defined]
        if data is None:  # never answers: a hook that waits for it would hold the tool call up
            self.server.release.wait(30)  # type: ignore[attr-defined]
            return
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Any:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stand)
    srv.seen, srv.reply, srv.release = [], (200, b"{}"), threading.Event()  # type: ignore[attr-defined]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.release.set()  # type: ignore[attr-defined]
    srv.shutdown()
    srv.server_close()
    thread.join(10)


def js(obj: object) -> bytes:
    return json.dumps(obj).encode()


# each case: the harness, the script's kind, the recorded payload it is run with, the event it reports
# and the fields it may send (the issue's four, #493)
CASES = {
    "cursor shell": ("cursor", "post-other", "cursor/https_postToolUse_shell.json", "postToolUse"),
    "cursor read": ("cursor", "post-other", "cursor/https_postToolUse_read.json", "postToolUse"),
    "cursor failure": (
        "cursor",
        "failure-other",
        "cursor/https_postToolUseFailure_shell.json",
        "postToolUseFailure",
    ),
    "devin exec": ("devin", "post-other", "devin/https_PostToolUse_exec.json", "PostToolUse"),
}
FIELDS = {"harness", "event", "session_id", "tool_use_id", "tool_name"}


def planted(fixture: str) -> dict[str, Any]:
    """A recorded payload with everything the tool did or saw replaced by ``SECRET``: its input, its
    result or error, the directory, the transcript and the person's address. Whatever of it reaches the
    server is a leak."""
    doc = {k: v for k, v in load(PAYLOADS / fixture).items() if not k.startswith("_")}
    for key in ("tool_input", "tool_output", "tool_response", "error_message"):
        if key in doc:
            doc[key] = {"command": SECRET} if key == "tool_input" else SECRET
    for key in ("cwd", "transcript_path"):
        if key in doc:
            doc[key] = f"/{SECRET}"
    if fixture.startswith("cursor/"):
        # Cursor hands every hook the person's address; the fixtures leave it out (test_fixtures_scan)
        doc["user_email"] = f"{SECRET}@example.com"
    doc["workspace_roots"] = [f"/{SECRET}"] if "workspace_roots" in doc else None
    return {k: v for k, v in doc.items() if v is not None}


def run(
    case: str, url: str | None, payload: dict[str, Any] | None = None, *, timeout: float = 30
) -> subprocess.CompletedProcess[bytes]:
    harness, kind, fixture, _event = CASES[case]
    plugin = CURSOR_PLUGIN if harness == "cursor" else DEVIN_PLUGIN
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
    if url is not None:
        env.update(SWITCHBOARD_URL=url, SWITCHBOARD_AGENT_TOKEN=TOKEN)
    return subprocess.run(
        [str(plugin / "bin" / "hook"), kind],
        input=json.dumps(payload or planted(fixture)).encode(),
        capture_output=True,
        timeout=timeout,
        env=env,
    )


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_script_sends_the_event_the_session_the_call_and_the_tools_name_and_nothing_else(
    server: Any, case: str
) -> None:
    """Each script is run with the harness's own recorded payload (Cursor Agent 2026.10.18, Devin CLI
    3000.11.92) in which the tool's input, its output, the failure's message, the working directory, the
    transcript and the person's address are all the same secret. The one request to `/mcp/event` has
    exactly five fields (the harness's name and the four of the issue), the token is in the header
    and nowhere else, and the secret is in no byte of the request. Fails if the script sends the output,
    the input, an error, a path or the address."""
    harness, _kind, fixture, event = CASES[case]
    r = run(case, f"http://127.0.0.1:{server.server_port}")
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b""), r
    [req] = server.seen
    assert req["path"] == "/mcp/event" and req["auth"] == f"Bearer {TOKEN}"
    sent = json.loads(req["body"])
    recorded = load(PAYLOADS / fixture)
    assert sent == {
        "harness": harness,
        "event": event,
        "session_id": recorded["conversation_id" if harness == "cursor" else "session_id"],
        "tool_use_id": recorded["tool_use_id"],
        "tool_name": recorded["tool_name"],
    }, sent
    assert set(sent) == FIELDS
    assert SECRET.encode() not in req["body"] and TOKEN.encode() not in req["body"]


@pytest.mark.parametrize("case", ["cursor shell", "devin exec"])
def test_the_script_sends_nothing_for_a_switchboard_tool_whatever_group_ran_it(
    server: Any, case: str
) -> None:
    """Devin's group for every other tool also runs after switchboard's tools (its matcher can't leave them
    out), and Cursor's would in a project that merged the groups by hand: the group with the result already
    reported that call, so a second report would take the context the first was about to carry. Fails if
    the script reports a call of switchboard's own server."""
    harness = CASES[case][0]
    own = CURSOR_OWN if harness == "cursor" else DEVIN_OWN
    for tool in own:
        r = run(
            case, f"http://127.0.0.1:{server.server_port}", {**planted(CASES[case][2]), "tool_name": tool}
        )
        assert (r.returncode, r.stdout) == (0, b""), (tool, r)
    assert server.seen == []


@pytest.mark.parametrize("case", ["cursor shell", "devin exec"])
def test_the_script_leaves_out_an_id_it_cannot_vouch_for_and_a_tool_with_no_name(
    server: Any, case: str
) -> None:
    """A call's id or a tool's name that isn't a string, or is longer than the broker takes (128), is
    not sent: the report still goes without the id (the session is what it needs), and a call with no usable
    name isn't reported. Fails if a long or odd value is relayed as it is."""
    url = f"http://127.0.0.1:{server.server_port}"
    base = planted(CASES[case][2])
    for bad in ("x" * 129, 7, None, ""):
        assert run(case, url, {**base, "tool_use_id": bad}).returncode == 0
    assert [json.loads(r["body"]).get("tool_use_id") for r in server.seen] == [None] * 4
    server.seen.clear()
    for bad in ("x" * 129, 7, None, ""):
        assert run(case, url, {**base, "tool_name": bad}).returncode == 0
    assert server.seen == []


CURSOR_CONTEXT = {"additional_context": "[switchboard] alice: check the tests"}
DEVIN_CONTEXT = {
    "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "[switchboard] alice: check"}
}
# what the server may answer, and what the script prints for it: context in the harness's own shape, and
# nothing else, whatever else the server says (this hook runs after every tool call)
REPLIES = [
    ("cursor shell", CURSOR_CONTEXT, CURSOR_CONTEXT),
    ("cursor failure", CURSOR_CONTEXT, CURSOR_CONTEXT),
    ("devin exec", DEVIN_CONTEXT, DEVIN_CONTEXT),
    ("cursor shell", {}, None),
    ("devin exec", {}, None),
    ("cursor shell", {"followup_message": "go on"}, None),
    ("cursor shell", {"permission": "deny"}, None),
    ("cursor shell", {"updated_input": {"command": "id"}}, None),
    ("cursor shell", {**CURSOR_CONTEXT, "permission": "allow"}, None),
    ("cursor shell", {"additional_context": 7}, None),
    ("devin exec", {"decision": "block", "reason": "stop"}, None),
    ("devin exec", {"decision": "approve"}, None),
    ("devin exec", {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedInput": {"x": 1}}}, None),
    ("devin exec", {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "x"}}, None),
    (
        "devin exec",
        {"hookSpecificOutput": {**DEVIN_CONTEXT["hookSpecificOutput"], "permissionDecision": "allow"}},
        None,
    ),
    ("devin exec", {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": 7}}, None),
]


@pytest.mark.parametrize(("case", "answer", "printed"), REPLIES)
def test_the_script_prints_context_in_the_harnesss_own_shape_and_nothing_else(
    server: Any, case: str, answer: dict[str, Any], printed: dict[str, Any] | None
) -> None:
    """The reply is the only thing the script prints. Context for the model goes out exactly as the harness
    takes it (Cursor's `additional_context`, Devin's PostToolUse `additionalContext`); a continue, a
    decision, a rewrite or a field beside the context is dropped, because this hook runs after every
    tool call and a hostile server must not get more from it than words for the model (DESIGN.md §11).
    Fails if the script prints another shape."""
    server.reply = (200, js(answer))
    r = run(case, f"http://127.0.0.1:{server.server_port}")
    assert r.returncode == 0 and r.stderr == b"", r
    assert (json.loads(r.stdout) if r.stdout else None) == printed, r.stdout


@pytest.mark.parametrize("case", ["cursor shell", "devin exec"])
def test_the_script_prints_nothing_and_exits_0_without_a_server_a_token_or_an_answer(
    server: Any, case: str
) -> None:
    """A server that refuses, errors, answers with a page or never answers, or no address or token in
    the environment: the tool call goes on, with no output and exit 0. The silent server holds the script
    for less than the group's own timeout (2 s), so it ends itself and Cursor or Devin never has to kill
    it. Fails if the script prints on an error, hangs on a silent server, or crashes."""
    url = f"http://127.0.0.1:{server.server_port}"
    for status, data in ((500, js(CURSOR_CONTEXT)), (401, b"{}"), (200, b"<html>no</html>"), (200, b"")):
        server.reply = (status, data)
        r = run(case, url)
        assert (r.returncode, r.stdout, r.stderr) == (0, b"", b""), (status, r)
    r = run(case, None)  # no address, no token
    assert (r.returncode, r.stdout) == (0, b"")
    assert run(case, "http://127.0.0.1:9").returncode == 0  # nothing is listening there
    server.reply = (200, None)
    seen = len(server.seen)
    r = run(case, url, timeout=10)
    assert (r.returncode, r.stdout) == (0, b"") and len(server.seen) == seen + 1
