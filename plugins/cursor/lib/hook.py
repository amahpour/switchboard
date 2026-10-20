#!/usr/bin/env python3
"""Cursor HTTPS command hook: report one session and bind each MCP call to its own ID."""

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
    "start": "sessionStart",
    "stop": "stop",
    "end": "sessionEnd",
}


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


def main():
    kind = sys.argv[1]
    try:
        p = json.load(sys.stdin)
    except ValueError:
        return
    sid = p.get("conversation_id") or p.get("session_id")
    if not isinstance(sid, str) or not 0 < len(sid) <= 128:
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
