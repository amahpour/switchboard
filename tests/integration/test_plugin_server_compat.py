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

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker, child_env
from test_agent_plugins import (  # noqa: F401
    CLAUDE_PLUGIN,
    Stub,
    claude_hooks,
    fill,
    impl,
    load,
    run_listen,
    run_session,
)
from test_agent_plugins import (
    FIXTURES as PAYLOADS,
)
from test_agents_http import make_token, sse_result
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
# Optional headers the newest listener may add that the oldest release never read. Adding one is a minor
# change, not a major one, when an older server ignores it and the plugin still works without it: the
# maintainer's ruling of 2026-11-19 ("As long as backwards compatible ignores it then it is NOT a breaking
# change"). Each entry says what it is for; the stand-in, like the old server, ignores it.
ADDITIVE_HEADERS = {
    "x-switchboard-resume-idle": "#838: this listener was parked before a broker restart",
}


def major(version: str) -> int:
    return int(version.split(".")[0])


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)


def tag_is_here() -> bool:
    """Whether the oldest release's tag is in this checkout. A developer's usually is; CI fetches it and
    sets SWITCHBOARD_REQUIRE_COMPAT_TAG=1 (as for PWSH), so a missing tag fails there."""
    if git("rev-parse", "-q", "--verify", f"refs/tags/{TAG}").returncode == 0:
        return True
    if os.environ.get("SWITCHBOARD_REQUIRE_COMPAT_TAG") == "1":
        pytest.fail(
            f"{TAG} is not in this checkout, and SWITCHBOARD_REQUIRE_COMPAT_TAG=1 says CI must have it"
        )
    return False


TAG = f"v{OLD_SERVER['release']}"
SURFACE = """
import asyncio, json, sys
sys.path.insert(0, sys.argv[1] + "/src")
from fastmcp import Client
import switchboard
from switchboard.mcp import server as srv
from switchboard.broker import agent_http
from switchboard.broker.reviews import ACTIONS as R
from switchboard.board import ACTIONS as B
class Backend:
    harness = "claude"
async def main():
    async with Client(srv.build_server(Backend()), mode="legacy") as c:
        tools = await c.list_tools()
    print(json.dumps({"release": switchboard.__version__,
      "tools": {t.name: {"properties": sorted(t.input_schema["properties"]),
                         "required": sorted(t.input_schema.get("required", []))} for t in tools},
      "review_actions": list(R), "board_actions": list(B), "hook_fields": sorted(agent_http.HOOK_FIELDS),
      "listen_path": agent_http.LISTEN_PATH, "session_path": agent_http.SESSION_PATH,
      "session_fields": list(agent_http.SESSION_FIELDS)}))
asyncio.run(main())
"""


def test_the_fixtures_are_the_oldest_release_of_the_current_major(tmp_path: Path) -> None:
    """The fixtures are for this package's major, and are what the release shipped: where the tag is in the
    checkout (CI fetches it and requires it), its plugin files are compared byte for byte, and the recorded
    tool surface and listener contract are derived again from the tag's own source and must equal the JSON.
    A fixture edited by hand, to make a compatibility check pass, fails here. A fixture for another major
    fails after a major bump, until the fixtures move to the new major's first release."""
    assert major(OLD_SERVER["release"]) == major(__version__), (
        "move the fixtures to the new major's first release"
    )
    assert OLD_SERVER["release"] == f"{major(__version__)}.0.0"
    if not tag_is_here():
        pytest.skip(f"{TAG} is not in this checkout")
    for rel in ("bin/listen", "bin/session", "bin/headers", "hooks/hooks.json"):
        shown = git("show", f"{TAG}:plugins/claude/{rel}").stdout
        assert (OLD_PLUGIN / rel).read_bytes() == shown, rel
    archive = tmp_path / "src.tar"
    assert git("archive", "-o", str(archive), TAG, "src").returncode == 0
    subprocess.run(["tar", "-x", "-C", str(tmp_path), "-f", str(archive)], check=True)
    out = subprocess.run(
        [sys.executable, "-c", SURFACE, str(tmp_path)],
        env={**child_env(), "PYTHONPATH": str(tmp_path / "src")},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    derived = json.loads(out)
    for key, value in derived.items():
        assert OLD_SERVER[key] == value, key
    # the listener's statuses and the headers it reads, from the release's own source
    listener = git("show", f"{TAG}:src/switchboard/broker/agent_http.py").stdout.decode()
    for status in OLD_SERVER["listen"]["answers"]:
        assert re.search(rf"\b{status}\b", listener), status
    # the headers the listener reads are exactly the recorded ones: the constants its class names, the
    # Origin it refuses, and the token header and Authorization that the door reads
    constants = dict(re.findall(r'^(\w+_HEADER) = b"([\w-]+)"', listener, re.M))
    body = listener[listener.index("class Listener:") : listener.index("class HideHook")]
    used = {constants[n] for n in re.findall(r"_header\(scope, (\w+_HEADER)\)", body)}
    assert "origin" in body and 'b"origin"' in body
    assert set(OLD_SERVER["listen"]["headers_read"]) == used | {
        "origin",
        "authorization",
        constants["TOKEN_HEADER"],
    }


def test_the_newest_server_still_offers_everything_the_oldest_release_did() -> None:
    """The contract's first half (#850): a release of the same major removes or renames no tool, no
    action and no required parameter, keeps the listen and session paths and takes every field a hook may
    send. The oldest release's surface is recorded (and checked against the tag, above); this checkout must
    still hold all of it. Fails when a change removes or renames one of them without a major version."""
    import asyncio

    from fastmcp import Client

    from switchboard.broker.agent_http import SESSION_FIELDS, SESSION_PATH
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
        assert set(old["required"]) <= props, name
        # a parameter that is required now would have broken an agent written for the old tool
        assert set(now[name].get("required", [])) <= set(old["required"]) | {"ctx"}, (
            f"{name} newly requires {set(now[name].get('required', [])) - set(old['required'])}"
        )
    assert set(OLD_SERVER["review_actions"]) <= set(REVIEW_ACTIONS)
    assert set(OLD_SERVER["board_actions"]) <= set(board_module.ACTIONS)
    assert set(OLD_SERVER["hook_fields"]) <= set(HOOK_FIELDS)
    assert LISTEN_PATH == OLD_SERVER["listen_path"]
    assert SESSION_PATH == OLD_SERVER["session_path"]
    assert set(OLD_SERVER["session_fields"]) <= set(SESSION_FIELDS)
    assert set(OLD_SERVER["tools"]) <= set(TOOL_NAMES)


def old_plugin_hooks() -> dict[str, list[dict[str, Any]]]:
    return json.loads((OLD_PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]


# the recorded Claude Code payload each hook event of the old plugin is filled from (tests/fixtures/payloads)
OLD_HOOK_PAYLOADS = {
    "UserPromptSubmit": "UserPromptSubmit.json",
    "PreToolUse": "PreToolUse_bash.json",
    "PostToolUse": "PostToolUse_bash.json",
    "PostToolUseFailure": "PostToolUseFailure_bash.json",
    "Notification": "Notification_permission_prompt.json",
    "Stop": "Stop.json",
}


def test_the_oldest_plugins_hooks_are_answered_by_the_newest_server(broker: InProcBroker) -> None:
    """Every mcp_tool hook of the oldest release's plugin, filled from the recorded payload of its event
    (empty where Claude Code leaves a field out) and sent as Claude Code sends a hook call, is answered by
    this server's hook tool without an error, and uses only fields it still takes. Fails when the hook
    tool's signature or an event's handling stops accepting what an old session's hooks send, which would
    make every old session's hook an error."""
    broker.call("room.create", {"name": "#build"})
    claude = Claude(broker, make_token(broker, "laptop"), SESSION_1)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    claude.call("read", room="#build")
    rec = load(PAYLOADS / "mcp_http" / "claude_hook_post_tool.json")
    sent = 0
    for event, groups in old_plugin_hooks().items():
        for group in groups:
            for h in group["hooks"]:
                if h["type"] != "mcp_tool":
                    continue
                assert h["tool"] == "hook" and set(h["input"]) <= set(HOOK_FIELDS), (event, h["input"])
                payload = {**load(PAYLOADS / "payloads" / "claude" / OLD_HOOK_PAYLOADS[event])}
                payload["session_id"] = SESSION_1
                args = fill(h["input"], payload, missing="")
                body = copy.deepcopy(rec["body"])
                body["id"] = next(claude.ids)
                body["params"] = {**body["params"], "arguments": args}
                r = claude.post(body, rec["headers"], "hook")
                assert r.status_code == 200, (event, r.text)
                result = sse_result(r)
                assert "error" not in result and not result["result"].get("isError"), (event, result)
                sent += 1
    assert sent >= 6


def isolated_home(path: Path) -> Path:
    """A home for a script under test with an empty ``.curlrc`` in it: curl reads that one and goes no
    further, where an empty or missing folder lets it fall back to the account's own home (``getpwuid``)
    and the developer's real configuration."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".curlrc").write_text("")
    return path


def old_env(url: str, token: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment Claude Code gives a command hook, with curl's own configuration pointed at an empty
    folder: the archived scripts are byte-exact and call curl with its defaults, so a developer's
    ``~/.curlrc`` must not reach them."""
    home = isolated_home(Path(tempfile.mkdtemp(prefix="sb-compat-home-")))
    return {
        "PATH": child_env()["PATH"],
        "HOME": str(home),
        "CURL_HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": url,
        "CLAUDE_PLUGIN_OPTION_TOKEN": token,
        "CLAUDE_PID": str(os.getpid()),
        **(extra or {}),
    }


def run_old_script(
    name: str, url: str, token: str, session: str, event: str, *, round_s: int | None = None, **fields: str
) -> subprocess.CompletedProcess[str]:
    """The oldest plugin's ``bin/<name>``, started as Claude Code starts it (``sh``, the hook's input on
    stdin, the plugin's options in the environment)."""
    env = old_env(url, token, {"SWITCHBOARD_LISTEN_S": str(round_s)} if round_s is not None else None)
    hook = json.dumps(
        {"session_id": session, "hook_event_name": event, "source": "", "stop_hook_active": False, **fields}
    )
    try:
        return subprocess.run(
            ["sh", str(OLD_PLUGIN / "bin" / name)],
            input=hook,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        shutil.rmtree(env["HOME"], ignore_errors=True)


@pytest.mark.skipif(shutil.which("curl") is None, reason="the listener needs curl")
def test_the_oldest_plugins_scripts_work_against_the_newest_server(broker: InProcBroker) -> None:
    """The oldest release's listener and session scripts, unchanged, against this server. The listener
    parks and exits 2 with the message that wakes the session; with a wrong token, or for a session that
    never joined, it wakes nothing. The session script's SessionEnd takes the session offline through
    ``/mcp/session``. Fails when a protocol change (an endpoint, a header, a status) strands old plugins."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    claude = Claude(broker, token, SESSION_1)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    claude.call("read", room="#build")
    pid = participant(broker, "claude-1").id
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(run_old_script, "listen", broker.base, token, SESSION_1, "Stop")
        until(lambda: bool(broker.on_loop(broker.state.engine.sinks.parks_for, pid)), timeout=30)
        broker.call("human.say", {"room": "#build", "text": "@claude-1 wake up"})
        r = waiting.result(60)
    assert r.returncode == 2 and "wake up" in r.stderr, r
    refused = run_old_script("listen", broker.base, "sbat_" + "y" * 43, SESSION_1, "Stop")
    assert refused.returncode == 2 and "401" in refused.stderr, refused  # with a home, a refusal is said once
    nobody = run_old_script("listen", broker.base, token, "00000000-0000-4000-8000-0000000000ff", "Stop")
    assert (nobody.returncode, nobody.stderr) == (0, ""), nobody
    ended = run_old_script("session", broker.base, token, SESSION_1, "SessionEnd", reason="prompt_input_exit")
    assert (ended.returncode, ended.stdout, ended.stderr) == (0, "", ""), ended
    until(lambda: participant(broker, "claude-1").status == "offline")


def stood_in(status: int) -> tuple[int, str]:
    """The oldest release's answer for a status, as the fixture records it."""
    return status, OLD_SERVER["listen"]["answers"][str(status)]["body"]


def check_old_server_would_accept(requests: list[tuple[str, str, dict[str, str], bytes]], path: str) -> None:
    """What the oldest release's listener checked before it answered a wait: POST, its path, a bearer
    token, the session header, and no Origin; its wait header a number. A stand-in that answered any
    request would let a listener that dropped one of these pass."""
    assert requests
    for method, got_path, headers, _ in requests:
        h = {k.lower(): v for k, v in headers.items()}
        assert (method, got_path) == (OLD_SERVER["listen"]["method"], path)
        assert h.get("authorization") == f"Bearer {TOKEN}", "the old server needs the bearer token"
        assert h.get("x-switchboard-session") == SESSION_1, "the old server needs the session header"
        assert h.get("x-switchboard-wait", "").isdigit()
        assert h.get("x-switchboard-again") in ("0", "1")
        assert "origin" not in h
        sent = {k for k in h if k.startswith("x-switchboard") or k == "authorization"}
        allowed = set(OLD_SERVER["listen"]["headers_read"]) | set(ADDITIVE_HEADERS)
        assert sent <= allowed, sent - allowed


def test_an_additive_header_is_one_the_oldest_release_never_read() -> None:
    """An optional header is listed as additive only while the oldest release of the major ignores it
    (the maintainer's ruling of 2026-11-19). A header the old server reads is part of the protocol it
    checks, so it can't hide in that list. Fails if a protocol header is moved into ADDITIVE_HEADERS."""
    assert not set(ADDITIVE_HEADERS) & set(OLD_SERVER["listen"]["headers_read"])
    assert all(name.startswith("x-switchboard-") for name in ADDITIVE_HEADERS)


def listen_once(
    tmp_path: Path, replies: list[tuple[int, str]]
) -> tuple[subprocess.CompletedProcess[str], Stub]:
    """The newest ``bin/listen`` against a stand-in that answers ``replies``, with an isolated home and
    a plugin data folder (where a notice is kept), and every request it made checked as the oldest release
    would have checked it."""
    home = isolated_home(tmp_path / "home")
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    stub = Stub(list(replies))
    try:
        r = run_listen(None, TOKEN, SESSION_1, url=stub.url, round_s=10, data=data, home=home)
    finally:
        stub.close()
    check_old_server_would_accept(stub.requests, OLD_SERVER["listen_path"])
    return r, stub


@pytest.mark.usefixtures("impl")
def test_the_newest_plugins_listener_speaks_the_oldest_servers_protocol(tmp_path: Path) -> None:
    """A stand-in answers with the statuses the fixture records for the oldest release's listener (408
    listen again, 200 the message, 409 end quietly, 410 say who ended it; the bodies are examples), and the
    newest plugin's listener is run against it in each scenario. Every request, in every scenario, is
    checked as that release would have checked it (``check_old_server_would_accept``). Fails when the
    plugin's script depends on a status or a header an older server of the same major lacks, or drops
    what that server requires."""
    again, wake = stood_in(408), stood_in(200)
    r, stub = listen_once(tmp_path / "a", [again, wake])
    assert (r.returncode, r.stderr) == (2, wake[1]), r
    assert stub.again == ["0", "1"]
    r, stub = listen_once(tmp_path / "b", [stood_in(409)])
    assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), r
    assert len(stub.requests) == 1
    r, stub = listen_once(tmp_path / "c", [stood_in(410)])
    assert r.returncode == 2 and stood_in(410)[1] in r.stderr, r
    assert len(stub.requests) == 1


@pytest.mark.usefixtures("impl")
def test_the_newest_plugins_session_script_posts_what_the_oldest_server_reads(tmp_path: Path) -> None:
    """The newest ``bin/session`` posts SessionStart and SessionEnd to the route and with the fields the
    oldest release's ``/mcp/session`` read, and its bearer token. Fails when the script moves the route or
    sends a field an older server of the same major would not know."""
    isolated_home(tmp_path / "h")
    stub = Stub([(200, "{}"), (200, "{}")])
    try:
        run_session(
            None, TOKEN, SESSION_1, "SessionStart", url=stub.url, source="startup", home=tmp_path / "h"
        )
        run_session(
            None,
            TOKEN,
            SESSION_1,
            "SessionEnd",
            url=stub.url,
            reason="prompt_input_exit",
            home=tmp_path / "h",
        )
    finally:
        stub.close()
    assert len(stub.requests) == 2
    for method, path, headers, body in stub.requests:
        assert (method, path) == ("POST", OLD_SERVER["session_path"])
        assert {k.lower(): v for k, v in headers.items()}.get("authorization") == f"Bearer {TOKEN}"
        sent = json.loads(body)
        assert set(sent) <= set(OLD_SERVER["session_fields"]), set(sent) - set(OLD_SERVER["session_fields"])
        assert sent["session_id"] == SESSION_1
    assert [json.loads(b)["event"] for *_, b in stub.requests] == ["SessionStart", "SessionEnd"]


def tools_named_by(text: str) -> set[str]:
    """The switchboard tools a skill names: a backticked ``name`` or ``name(``, with the prefix a harness
    gives it (Cursor's ``switchboard_``, Claude Code's and Codex's ``mcp__..__``)."""
    names: set[str] = set()
    for word in re.findall(r"`([A-Za-z0-9_]+)(?:\(|`)", text):
        for prefix in ("mcp__plugin_switchboard_switchboard__", "mcp__switchboard__", "switchboard_"):
            word = word.removeprefix(prefix)
        if word in TOOL_NAMES:
            names.add(word)
    return names


def test_the_skill_scan_sees_a_tool_behind_a_harnesss_prefix() -> None:
    """The scan below must not lose a tool because a harness prefixes it: a tool added after the oldest
    release is found whether a skill writes it plain, as Cursor lists it, or as Claude Code names it."""
    for written in (
        "`rooms`",
        "`switchboard_rooms`",
        "`mcp__switchboard__rooms`",
        "`mcp__plugin_switchboard_switchboard__rooms(`",
    ):
        assert tools_named_by(f"call {written} first") == {"rooms"}, written
    assert "rooms" not in OLD_SERVER["tools"]  # so such a skill would be refused by the check below


SKILL_TOOL = {"review-board": "review", "board": "board", "claim": "claim", "talk-on-a-call": "say"}
# Skills whose bare ``name=value`` spans are parameters of the tool (the board skill's are style keys).
LOOSE_PARAMS = {"review-board", "claim"}
# What a skill may name that the oldest server lacks, only with a fallback the skill itself gives: each
# entry is (tool, kind, name) -> text a skill naming it must contain, which says to use what the server
# has. The check fails when the text is gone from a skill, so an entry cannot outlive its fallback.
FALLBACKS = {
    ("review", "action", name): "if an action here is not in that list, the server is older"
    for name in ("edit", "abstain", "take", "reopen")
}


def skill_uses(skill: str, text: str) -> set[tuple[str, str, str]]:
    """What a skill names of the tool surface, as (tool, kind, name) with kind ``tool``, ``action`` or
    ``param``: a call written ``name(...)`` (its ``action="x"`` and its arguments), a bullet of the
    ``## Actions`` list with the parameters in its parentheses, and, for skills that use them, a backticked
    ``param=value``. Conservative: only these shapes, so prose is never read as a call."""
    uses: set[tuple[str, str, str]] = set()
    own = SKILL_TOOL.get(skill)
    for span in re.findall(r"`([^`\n]*)`", text):
        call = re.fullmatch(r"(?:mcp__[a-z_]+__|switchboard_)?([a-z_]+)\((.*)\)", span)
        if call and (call.group(1) in TOOL_NAMES or call.group(1) == own):
            tool = call.group(1)
            uses.add((tool, "tool", tool))
            for arg in (a.strip() for a in call.group(2).split(",")):
                name, _, value = arg.partition("=")
                if not re.fullmatch(r"[a-z_]+", name):
                    continue  # "...", "<sha>"
                if name == "action":
                    if value.strip('"'):
                        uses.add((tool, "action", value.strip('"')))
                else:
                    uses.add((tool, "param", name))
        elif own and skill in LOOSE_PARAMS:
            for name in re.findall(r"(?<![\w-])([a-z_]+)=", span):
                uses.add((own, "param", name))
    section = re.search(r"^## Actions\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if own in ("review", "board") and section:
        for bullet in re.finditer(r"^- `([a-z_]+)`(?: \(([^)]*)\))?", section.group(1), re.M):
            uses.add((own, "action", bullet.group(1)))
            for item in (bullet.group(2) or "").split(";"):
                # "`shape` is `rectangle`, ...": the words after "is" are values, not parameters
                for name in re.findall(r"`([a-z_]+)`", item.split(" is ")[0]):
                    uses.add((own, "param", name))
    return uses


def beyond_the_oldest_server(skill: str, text: str) -> dict[tuple[str, str, str], str]:
    """Each use in ``text`` that the oldest release lacks and no fallback in the text covers, with why."""
    lacking: dict[tuple[str, str, str], str] = {}
    tools = OLD_SERVER["tools"]
    for use in sorted(skill_uses(skill, text)):
        tool, kind, name = use
        if kind == "tool":
            have = name in tools
        elif tool not in tools:
            have = False
        elif kind == "param":
            have = name in tools[tool]["properties"]
        else:
            have = name in OLD_SERVER.get(f"{tool}_actions", [])
        if have:
            continue
        fallback = FALLBACKS.get(use)
        if fallback and fallback in text:
            continue
        lacking[use] = f"release {OLD_SERVER['release']} lacks the {kind} {name!r} of {tool!r}"
    return lacking


def plugin_skills() -> list[tuple[str, str, str]]:
    return [
        (f"{p.parents[2].name}/{p.parent.name}", p.parent.name, p.read_text())
        for p in sorted((ROOT / "plugins").glob("*/skills/*/SKILL.md"))
    ]


def test_the_newest_plugins_skills_name_only_tools_the_oldest_server_has() -> None:
    """Every switchboard tool a skill of this checkout's plugins names exists on the oldest release of the
    major. A skill that names a tool added since would steer an agent on an older server to a tool it lacks:
    it fails until the skill is generic about it (#850, "Feature guidance lives on the server")."""
    named: dict[str, set[str]] = {}
    for where, _, text in plugin_skills():
        for tool in tools_named_by(text):
            named.setdefault(tool, set()).add(where)
    assert named  # the skills do name tools
    missing = {t: sorted(w) for t, w in named.items() if t not in OLD_SERVER["tools"]}
    assert not missing, missing


def test_the_newest_plugins_skills_name_only_actions_and_parameters_the_oldest_server_has() -> None:
    """Every ``action`` and parameter a skill of this checkout's plugins names exists on the oldest release
    of the major, unless the skill gives a fallback (``FALLBACKS``). A skill that tells an agent on an older
    server of the same major to call an action it never had, or to pass a parameter it refuses (a
    ``state`` to ``close``, before ``close`` took one), fails here until the skill says what to do on the
    older server (#850, #954)."""
    missing: dict[str, list[str]] = {}
    for where, skill, text in plugin_skills():
        for why in beyond_the_oldest_server(skill, text).values():
            missing.setdefault(where, []).append(why)
    assert not missing, missing
    assert any(skill_uses(skill, text) for _, skill, text in plugin_skills())  # the scan sees something


def test_the_skill_scan_finds_an_action_and_a_parameter_the_oldest_server_lacks() -> None:
    """The scan above is not blind: a planted line naming a tool, an action and a parameter that the oldest
    release lacks is found, a fallback in the text excuses only the action it was written for, and a plain
    call of what the release has is clean."""
    ok = 'Call `review(action="close", state="merged")` and `claim(room, key, ttl, note)`.'
    assert beyond_the_oldest_server("review-board", ok) == {}
    bad = 'Call `review(action="shelve", state="merged", sparkle="x")` and `rooms()`.'
    assert set(beyond_the_oldest_server("review-board", bad)) == {
        ("review", "action", "shelve"),
        ("review", "param", "sparkle"),
        ("rooms", "tool", "rooms"),
    }
    actions = "## Actions\n\n- `take` (`item`, `sparkle`): x\n\n## Next\n"
    assert set(beyond_the_oldest_server("review-board", actions)) == {
        ("review", "action", "take"),
        ("review", "param", "sparkle"),
    }
    covered = actions + "\nif an action here is not in that list, the server is older"
    assert set(beyond_the_oldest_server("review-board", covered)) == {("review", "param", "sparkle")}


def test_the_plugins_hook_templates_send_only_fields_the_oldest_server_takes() -> None:
    """The newest plugin's mcp_tool hooks send only fields the oldest server's hook tool took: a field it
    did not know is refused by it (a plugin ahead of its server). Fails when a hook template gains a field
    with no major version."""
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
