#!/usr/bin/env python3
"""Cursor HTTPS command hook: report one session and bind each MCP call to its own ID.

``post-other`` and ``failure-other`` run after every tool but switchboard's own (docs/DESIGN.md §49.6, #493): they
tell the broker that the session finished a tool, so a busy agent hears its messages after a shell command or an
edit too. They send the event, the session, the call's id and the tool's name, and never the tool's input or output."""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# a delivery's batch token, as switchboard writes it into a tool's answer
TOKEN = re.compile(r"yk:b\d{1,12}\.[0-9a-f]{8}")
MAX_TOKENS = 20
EVENTS = {
    "post": "postToolUse",
    "failure": "postToolUseFailure",
    "post-other": "postToolUse",
    "failure-other": "postToolUseFailure",
    "start": "sessionStart",
    "stop": "stop",
    "end": "sessionEnd",
}
OTHER = ("post-other", "failure-other")
OWN_TOOL = "MCP:switchboard_"  # how Cursor names the tools of the plugin's server
ID_MAX = 128  # an id or a tool's name longer than this is not relayed (the broker takes no more)
OTHER_TIMEOUT = 1.5  # under the group's own timeout (2 s): the script ends itself, Cursor never kills it


def send(path, body, timeout):
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


def short(value):
    return value if isinstance(value, str) and 0 < len(value) <= ID_MAX else None


def report_other(kind, p, sid):
    """Tell the broker that this session finished a tool that isn't switchboard's own, and print what it
    answers if that is context for the model (the broker's pending priority messages, or ``{}``).

    The report is the event, the session, the call's id and the tool's name. Never the tool's input or output,
    the working directory, the transcript's path or the person's address, all of which Cursor hands the hook:
    they are the user's, and a hosted broker is another machine (docs/DESIGN.md §11). A switchboard tool is not
    reported again: the group with its answer already did. What is printed is `additional_context` and nothing
    else, so this hook, which runs after every tool call, can give the model words and nothing more."""
    tool = short(p.get("tool_name"))
    if tool is None or tool.startswith(OWN_TOOL):
        return
    body = {"harness": "cursor", "event": EVENTS[kind], "session_id": sid, "tool_name": tool}
    use = short(p.get("tool_use_id"))
    if use is not None:
        body["tool_use_id"] = use
    reply = send("/mcp/event", body, OTHER_TIMEOUT)
    if isinstance(reply, dict) and set(reply) == {"additional_context"} and isinstance(
        reply["additional_context"], str
    ):
        print(json.dumps(reply))


def main():
    kind = sys.argv[1]
    try:
        p = json.load(sys.stdin)
    except ValueError:
        return
    sid = p.get("conversation_id") or p.get("session_id")
    if not isinstance(sid, str) or not 0 < len(sid) <= 128:
        return
    if kind in OTHER:
        report_other(kind, p, sid)
        return
    if kind == "pre":
        tool = p.get("tool_name", "")
        use = p.get("tool_use_id")
        inp = p.get("tool_input")
        if not (
            isinstance(tool, str)
            and tool.startswith("MCP:switchboard_")
            and isinstance(use, str)
            and 0 < len(use) <= 128
            and isinstance(inp, dict)
        ):
            print(json.dumps({"permission": "deny"}))
            return
        name = tool[4:]
        ok = send(
            "/mcp/pretool",
            {
                "harness": "cursor",
                "event": "preToolUse",
                "session_id": sid,
                "tool_use_id": use,
                "tool_name": name,
            },
            5,
        )
        if ok is None:
            print(json.dumps({"permission": "deny"}))
            return
        print(
            json.dumps(
                {
                    "permission": "allow",
                    "updated_input": {**inp, "switchboard_session_id": sid, "switchboard_tool_use_id": use},
                }
            )
        )
        return
    event = EVENTS.get(kind)
    if event is None:
        return
    body = {"harness": "cursor", "event": event, "session_id": sid}
    for key in ("generation_id", "tool_use_id", "tool_name", "stop_hook_active", "model", "reason"):
        if key in p:
            body["prompt_id" if key == "generation_id" else key] = p[key]
    if kind == "post":
        # only the batch tokens: the broker reads nothing else of the answer, and a long one (a
        # wait() with several messages) would not fit the request it accepts
        out = p.get("tool_output")
        text = out if isinstance(out, str) else json.dumps(out)
        body["tool_response"] = " ".join(list(dict.fromkeys(TOKEN.findall(text)))[:MAX_TOKENS])
    if kind == "stop":
        body["wait_s"] = 630
    reply = send("/mcp/event", body, 650 if kind == "stop" else 5)
    if isinstance(reply, dict) and (
        set(reply) == {"followup_message"} or set(reply) == {"additional_context"}
    ):
        print(json.dumps(reply))


if __name__ == "__main__":
    main()
