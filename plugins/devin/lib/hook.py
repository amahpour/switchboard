#!/usr/bin/env python3
"""Devin HTTPS hook: put this hook's session and call IDs into its MCP call.

``post-other`` runs after every tool (docs/DESIGN.md §49.7, #493), switchboard's own included because Devin's
matchers can't leave them out, and reports the ones that aren't switchboard's: that the session finished a tool,
so a busy agent hears its messages after a shell command or an edit too. It sends the event, the session, the
call's id and the tool's name, and never the tool's input or output."""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

SERVER = "mcp__switchboard_https__"
# a delivery's batch token, as switchboard writes it into a tool's answer
TOKEN = re.compile(r"yk:b\d{1,12}\.[0-9a-f]{8}")
MAX_TOKENS = 20
ID_MAX = 128  # an id or a tool's name longer than this is not relayed (the broker takes no more)
OTHER_TIMEOUT = 1.5  # under the group's own timeout (2 s): the script ends itself, Devin never kills it
EVENTS = {
    "post": "PostToolUse",
    "start": "SessionStart",
    "stop": "Stop",
    "end": "SessionEnd",
    "prompt": "UserPromptSubmit",
}


def send(path, body, timeout=5):
    base = os.environ.get("SWITCHBOARD_URL", "").rstrip("/")
    token = os.environ.get("SWITCHBOARD_AGENT_TOKEN", "")
    u = urlsplit(base)
    if not token or (
        u.scheme != "https"
        and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "switchboard.localhost"))
    ):
        return None
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.HTTPError):
        return None


def report_permission(sid):
    """Tell switchboard that this session has an approval prompt open (docs/DESIGN.md §49.9, #472).

    In Devin a PermissionRequest hook's output is a permission decision, so this one cannot make one:
    it prints nothing and returns nothing, the server's reply is dropped (never printed), and whatever goes
    wrong (no server, a refusal, a crash) is swallowed, so Devin shows its own prompt as it always does.
    It sends the session's id and the event's name, never the tool or its input.
    tests/unit/test_permission_hooks_static.py runs it against a server that answers every decision."""
    try:
        send("/mcp/event", {"harness": "devin", "event": "PermissionRequest", "session_id": sid}, timeout=2)
    except Exception:  # noqa: BLE001 - nothing this hook does may reach Devin as a result
        pass


def short(value):
    return value if isinstance(value, str) and 0 < len(value) <= ID_MAX else None


def report_other(p, sid):
    """Tell the broker that this session finished a tool that isn't switchboard's own, and print what it
    answers if that is context for the model (the broker's pending priority messages, or ``{}``).

    The report is the event, the session, the call's id and the tool's name. Never the tool's input or its
    result, which Devin hands the hook: they are the user's (a command, its output, a file), and a hosted broker
    is another machine (docs/DESIGN.md §11). A switchboard tool is not reported here: the group with its answer
    already did, and a second report could take the context that one was about to carry. What is printed is
    a PostToolUse `additionalContext` and nothing else, so this hook, which runs after every tool call, can give
    the model words and nothing more."""
    tool = short(p.get("tool_name"))
    if tool is None or tool.startswith(SERVER):
        return
    body = {"harness": "devin", "event": "PostToolUse", "session_id": sid, "tool_name": tool}
    use = short(p.get("tool_use_id"))
    if use is not None:
        body["tool_use_id"] = use
    reply = send("/mcp/event", body, OTHER_TIMEOUT)
    out = reply.get("hookSpecificOutput") if isinstance(reply, dict) and len(reply) == 1 else None
    if (
        isinstance(out, dict)
        and set(out) == {"hookEventName", "additionalContext"}
        and out["hookEventName"] == "PostToolUse"
        and isinstance(out["additionalContext"], str)
    ):
        print(json.dumps(reply))


def main():
    kind = sys.argv[1]
    try:
        p = json.load(sys.stdin)
    except ValueError:
        return
    sid = p.get("session_id")
    if not isinstance(sid, str) or not 0 < len(sid) <= 128:
        return
    if kind == "permission":
        report_permission(sid)
        return
    if kind == "post-other":
        report_other(p, sid)
        return
    if kind == "pre":
        tool = p.get("tool_name", "")
        use = p.get("tool_use_id")
        inp = p.get("tool_input")
        if not (
            isinstance(tool, str)
            and tool.startswith(SERVER)
            and isinstance(use, str)
            and 0 < len(use) <= 128
            and isinstance(inp, dict)
        ):
            print(json.dumps({"decision": "block"}))
            return
        name = tool[len(SERVER) :]
        ok = send(
            "/mcp/pretool",
            {
                "harness": "devin",
                "event": "PreToolUse",
                "session_id": sid,
                "tool_use_id": use,
                "tool_name": name,
            },
        )
        if ok is None:
            print(json.dumps({"decision": "block"}))
            return
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "updatedInput": {
                            **inp,
                            "switchboard_session_id": sid,
                            "switchboard_tool_use_id": use,
                        },
                    }
                }
            )
        )
        return
    event = EVENTS.get(kind)
    if event is None:
        return
    body = {"harness": "devin", "event": event, "session_id": sid}
    for key in ("prompt_id", "tool_use_id", "stop_hook_active", "source", "reason"):
        if key in p:
            body[key] = p[key]
    if kind == "post":
        tool = p.get("tool_name", "")
        if not isinstance(tool, str) or not tool.startswith(SERVER):
            return
        body["tool_name"] = tool
        # only the batch tokens: the broker reads nothing else of the answer, and a long one would
        # not fit the request it accepts
        resp = p.get("tool_response")
        text = resp if isinstance(resp, str) else json.dumps(resp)
        body["tool_response"] = " ".join(list(dict.fromkeys(TOKEN.findall(text)))[:MAX_TOKENS])
    reply = send("/mcp/event", body)
    if isinstance(reply, dict) and (
        set(reply) == {"decision", "reason"} or set(reply) == {"hookSpecificOutput"}
    ):
        print(json.dumps(reply))


if __name__ == "__main__":
    main()
