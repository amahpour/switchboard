#!/usr/bin/env python3
"""Devin HTTPS hook: put this hook's session and call IDs into its MCP call."""

import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

SERVER = "mcp__switchboard_https__"
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


def main():
    kind = sys.argv[1]
    try:
        p = json.load(sys.stdin)
    except ValueError:
        return
    sid = p.get("session_id")
    if not isinstance(sid, str) or not 0 < len(sid) <= 128:
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
        body["tool_response"] = p.get("tool_response", {})
    reply = send("/mcp/event", body)
    if isinstance(reply, dict) and (
        set(reply) == {"decision", "reason"} or set(reply) == {"hookSpecificOutput"}
    ):
        print(json.dumps(reply))


if __name__ == "__main__":
    main()
