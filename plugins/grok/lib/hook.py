#!/usr/bin/env python3
"""Grok Build's command hook for switchboard over HTTPS (docs/DESIGN.md §49.15).

Grok runs this for each event its plugin registers (``hooks/hooks.json``), with the event's JSON on stdin.
It reports the session's status to the broker at ``/mcp/event`` and prints only what the broker may answer:

- after a tool call (``PostToolUse``, ``PostToolUseFailure``), a busy session's priority messages as
  ``hookSpecificOutput.additionalContext``, which Grok shows the model as a system reminder;
- at ``Stop``, after waiting up to an hour for a message, ``{"decision": "block", "reason": ...}``, which
  Grok shows the model as Stop hook feedback and continues the turn with. Grok ends a turn after 8 blocks
  and fires no Stop for the last one, so this listens for 7 at most; then it reports a plain Stop and the
  session is idle and not listening until its person types.

It prints nothing else, ever: no permission decision, and no output at all for an event that could carry
one. Grok's ``PreToolUse`` and ``PermissionDenied`` are not registered: only Codex's and Devin's plugins
register a hook for a permission event (§49.9). A permission prompt (``Notification``) is only reported,
and a declined one ends the turn with ``StopCancelled``, which is. It never reports a subagent's hook (Grok
gives a subagent its own session id and a ``subagentType``), nor the observe-only Stop at session end.

What it sends is the event, the session's id, the prompt's id, the permission mode, the stop reason and
flag, the notification's type, and a tool's name and call id; for switchboard's own tools also the batch
tokens in the answer. Never a tool's input or output, the working directory, the transcript's path or the
prompt's text, all of which Grok hands the hook: they are the person's, and a hosted broker is another
machine (§11). The token comes from ``SWITCHBOARD_AGENT_TOKEN`` in Grok's environment: switchboard writes it
into no file. It goes only to https, or plain http on this machine, directly (no proxy of the environment), and
never follows a redirect: a 3xx is a failure, so the token never reaches an address the person did not set.

Everything it reads is bounded: its stdin and the broker's reply to ``INPUT_MAX`` and ``REPLY_MAX`` bytes (more
is no event, or no answer), and a tool's answer is walked for batch tokens to a size and a depth, never
serialized whole.
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
# a delivery's batch token, as switchboard writes it into a tool's answer
TOKEN = re.compile(r"yk:b\d{1,12}\.[0-9a-f]{8}")
MAX_TOKENS = 20
OWN_TOOL = "switchboard__"  # how Grok names the tools of the plugin's server (``<server>__<tool>``)
ID_MAX = 128  # an id or a tool's name longer than this is not relayed (the broker takes no more)
# the events this script reports, by Grok's name for them (``hook_event_name``)
EVENTS = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "PostToolUseFailure",
        "Notification",
        "Stop",
        "StopCancelled",
        "StopFailure",
        "SessionEnd",
    }
)
APPROVAL = "permission_prompt"  # the Notification type of an open tool approval prompt
WAIT_S = 3630  # how long a Stop waits for a message; hooks.json gives the hook 3660 s
MAX_WAKES = 7  # Grok allows 8 Stop blocks in one turn and fires no Stop after the 8th
REPORT_TIMEOUT = 4  # under the 5 s hooks.json gives every other event
RETRY_STATUSES = (429, 500, 502, 503, 504)
OUTAGE_LIMIT_S = 120
INPUT_MAX = 1024 * 1024  # a hook's stdin past this is not read as an event
REPLY_MAX = 1024 * 1024  # nor a broker's answer past this as an answer
SCAN_MAX = 256 * 1024  # characters of a switchboard tool's answer searched for batch tokens
SCAN_NODES = 10000  # values of that answer visited, at most
SCAN_DEPTH = 32  # and how deep


def plugin_version():
    """This plugin's own version, from its manifest, for X-Switchboard-Plugin-Version (DESIGN.md §49.13).
    Only ``X.Y.Z`` of digits is sent: a missing or odd manifest sends no header, and never breaks a hook."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin.json")
        with open(path, encoding="utf-8") as f:
            version = json.load(f).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and VERSION.fullmatch(version) else None


def opener():
    """urllib's handlers without its redirect handler and with no proxy: a 3xx answer is a failure
    (``HTTPError``) and is never followed, and no ``http_proxy`` or ``https_proxy`` of the environment is used,
    so the token in the request's header goes nowhere but ``SWITCHBOARD_URL``, directly."""
    o = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        o.add_handler(handler)
    return o


def send(body, timeout, retry_stop=False):
    """POST one report to ``/mcp/event`` and return the broker's JSON answer, or None. Only https, or http to
    this machine, and only with a token: the token is never sent anywhere else."""
    base = os.environ.get("SWITCHBOARD_URL", "").rstrip("/")
    token = os.environ.get("SWITCHBOARD_AGENT_TOKEN", "")
    u = urlsplit(base)
    if not token or (
        u.scheme != "https"
        and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "switchboard.localhost"))
    ):
        return None
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    version = plugin_version()
    if version:
        headers["X-Switchboard-Plugin-Version"] = version
    req = urllib.request.Request(
        base + "/mcp/event", data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with opener().open(req, timeout=timeout) as response:
            if response.status != 200:
                return None  # only a 200 answers; nothing else is read
            raw = response.read(REPLY_MAX + 1)
        return json.loads(raw) if len(raw) <= REPLY_MAX else None
    except urllib.error.HTTPError as err:
        err.close()
        if retry_stop and err.code in RETRY_STATUSES:
            raise
        return None
    except OSError:
        if retry_stop:
            raise
        return None
    except (ValueError, RecursionError):
        return None


def park(body):
    """Wait at Stop for a message, within the hook's own time. A round cut short by a proxy is not an outage
    (#838); only time the broker could not be reached counts toward giving up."""
    stop_deadline = time.monotonic() + WAIT_S
    retry_deadline = None
    backoff = 1
    while True:
        began = time.monotonic()
        remaining = int(stop_deadline - began)
        if remaining < 10:
            return None
        try:
            return send({**body, "wait_s": remaining}, remaining + 10, retry_stop=True)
        except OSError as err:
            now = time.monotonic()
            if now - began >= 10:
                retry_deadline = None
                backoff = 1
                continue
            delay = backoff
            if isinstance(err, urllib.error.HTTPError):
                retry_deadline = None
            else:
                if retry_deadline is None:
                    retry_deadline = now + OUTAGE_LIMIT_S
                if now >= retry_deadline:
                    print(
                        "[switchboard] Switchboard has been unreachable for at least 2 minutes. "
                        "The listener stopped. Type in Grok to listen again.",
                        file=sys.stderr,
                    )
                    return None
                delay = min(delay, retry_deadline - now)
            time.sleep(min(delay, max(0, stop_deadline - now - 10)))
            backoff = min(backoff * 2, 60)


def short(value):
    return value if isinstance(value, str) and 0 < len(value) <= ID_MAX else None


def batch_tokens(value):
    """The batch tokens in a switchboard tool's answer, in the order they appear: its strings, walked to
    ``SCAN_DEPTH`` levels, ``SCAN_NODES`` values and ``SCAN_MAX`` characters in all, and never serialized
    whole, however large Grok's ``tool_response`` is."""
    found = []
    budget, nodes = SCAN_MAX, 0
    stack = [(value, 0)]
    while stack and budget > 0 and nodes < SCAN_NODES and len(found) < MAX_TOKENS:
        v, depth = stack.pop()
        nodes += 1
        if isinstance(v, str):
            chunk = v[:budget]
            budget -= len(chunk)
            found.extend(TOKEN.findall(chunk))
        elif depth < SCAN_DEPTH and isinstance(v, dict):
            stack.extend((x, depth + 1) for x in reversed(list(v.values())[:SCAN_NODES]))
        elif depth < SCAN_DEPTH and isinstance(v, list):
            stack.extend((x, depth + 1) for x in reversed(v[:SCAN_NODES]))
    return list(dict.fromkeys(found))[:MAX_TOKENS]


def wakes_file(sid):
    """Where this session's count of wakes in its current turn is kept: Grok's data directory for the plugin,
    one small file a session, named by a hash so no id reaches a path."""
    data = os.environ.get("GROK_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".grok", "switchboard")
    return os.path.join(data, "wakes", hashlib.sha256(sid.encode()).hexdigest()[:32])


def wakes_so_far(path, prompt_id, continuing):
    """How many times this turn was already woken. A turn's first Stop (``stopHookActive`` false) starts at
    0. A continuing Stop's count is known only from a file this hook wrote for the same prompt: a file that is
    missing or can't be read, or that holds anything but an object with this prompt's id and a count, is
    unknown, and unknown counts as the cap, so a turn is never woken past it."""
    if not continuing:
        return 0
    if prompt_id is None:
        return MAX_WAKES
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError, RecursionError):
        return MAX_WAKES
    if not isinstance(got, dict) or got.get("prompt_id") != prompt_id:
        return MAX_WAKES
    n = got.get("wakes")
    return n if isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= MAX_WAKES else MAX_WAKES


def reserve_wake(path, prompt_id, n):
    """Write the count with this wake in it before the hook parks, durably (a temporary file, fsync, rename),
    so a wake the broker hands out is always counted, even if the hook is then killed. False if it could not
    be written: then the hook does not park, and no message is handed to it."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"prompt_id": prompt_id, "wakes": n}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    try:
        fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # the rename is done; a directory that can't be synced (some filesystems) still holds it
    return True


def watch_parent():
    """End this hook when Grok goes away (it quit, or the person sent a queued prompt and Grok dropped the
    hook): the broker then sees the request close and ends the wait, so no message is answered to nobody."""
    parent = os.getppid()

    def run():
        while True:
            time.sleep(1)
            if os.getppid() != parent:
                os._exit(0)

    threading.Thread(target=run, daemon=True).start()


def main():
    try:
        raw = sys.stdin.buffer.read(INPUT_MAX + 1)
        if len(raw) > INPUT_MAX:
            return  # past any event Grok sends: not read as one
        p = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return
    if not isinstance(p, dict):
        return
    event = p.get("hook_event_name")
    if event not in EVENTS:
        return
    if "subagentType" in p or "subagentId" in p or "subagent_type" in p:
        return  # a subagent's hook: it is not in the room, and nothing it does is the session's
    sid = short(p.get("session_id")) or short(p.get("sessionId"))
    if sid is None:
        return
    reason = p.get("reason")
    if event == "Stop" and reason in ("shutdown", "channel_closed"):
        return  # the observe-only Stop at session end: SessionEnd says the session ended
    if event == "Notification" and p.get("notification_type", p.get("notificationType")) != APPROVAL:
        return  # only an approval prompt says anything about the session's turn
    body = {"harness": "grok", "event": event, "session_id": sid}
    prompt_id = short(p.get("promptId"))
    if prompt_id is not None:
        body["prompt_id"] = prompt_id
    for key in ("permission_mode", "source"):
        if short(p.get(key)) is not None:
            body[key] = p[key]
    if short(reason) is not None:
        body["reason"] = reason
    if event == "Notification":
        body["notification_type"] = APPROVAL
    tool = short(p.get("tool_name"))
    if event in ("PostToolUse", "PostToolUseFailure") and tool is not None:
        body["tool_name"] = tool
        use = short(p.get("tool_use_id"))
        if use is not None:
            body["tool_use_id"] = use
        if tool.startswith(OWN_TOOL):
            # only the batch tokens: the broker reads nothing else of the answer
            body["tool_response"] = " ".join(batch_tokens(p.get("tool_response")))
    if event == "Stop":
        continuing = p.get("stop_hook_active") is True
        body["stop_hook_active"] = continuing
        path = wakes_file(sid)
        woken = wakes_so_far(path, prompt_id, continuing)
        if reason != "end_turn" or woken >= MAX_WAKES or not reserve_wake(path, prompt_id, woken + 1):
            # idle, and not listening: the person's next prompt starts again. A count that could not be kept
            # is the cap reached, so the 8th block Grok would end the turn on is never handed out.
            send(body, REPORT_TIMEOUT)
            return
        watch_parent()
        reply = park(body)
        if (
            isinstance(reply, dict)
            and set(reply) == {"decision", "reason"}
            and reply["decision"] == "block"
            and isinstance(reply["reason"], str)
            and reply["reason"]
        ):
            print(json.dumps(reply))
        return
    reply = send(body, REPORT_TIMEOUT)
    if (
        event in ("PostToolUse", "PostToolUseFailure")
        and isinstance(reply, dict)
        and set(reply) == {"hookSpecificOutput"}
        and isinstance(reply["hookSpecificOutput"], dict)
        and set(reply["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}
        and reply["hookSpecificOutput"]["hookEventName"] == event
        and isinstance(reply["hookSpecificOutput"]["additionalContext"], str)
    ):
        print(json.dumps(reply))


if __name__ == "__main__":
    main()
