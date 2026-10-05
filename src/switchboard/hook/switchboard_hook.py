"""switchboard hook script (DESIGN.md §7).

STANDALONE: this file imports nothing from switchboard and only uses the standard
library, because it runs as ``python -I -S <copy of this file>`` from
``$SWITCHBOARD_HOME/hooks/switchboard_hook-<sha12>.py`` behind a ``/bin/sh`` guard
(a missing file or interpreter exits 0, never 2).

It is also the single definition of ``sock_path()``; ``switchboard.paths``
imports it from here so the broker, the CLI, the MCP server and the hook
always agree on where the broker socket lives.

What it does, in order:
1. read all of stdin and parse it as JSON;
2. detect the harness that is really running it (Cursor and Devin also
   import ~/.claude hooks) and exit at once on a mismatch, or when the
   payload's event name differs from ``--event``;
3. relay an ALLOWLIST of fields to the broker (never stdin wholesale, never
   env values) and wait briefly for the reply;
4. print only what the fixed output table allows for this (harness, event),
   then acknowledge the batch it printed.

The broker returns only ``{kind, text}``; this script builds the JSON. It
has no way to print a permission decision, a tool-input rewrite, or a
``decision`` on any event except Devin's Stop. Every path exits 0.
"""

import hashlib
import json
import os
import re
import select
import signal
import socket
import stat
import sys
import time

SOCK_MAX_BYTES = 100  # macOS sun_path holds at most 103 bytes (FINDINGS §10 S6)
FIELD_SCAN_MAX = 256 * 1024
MAX_TOKENS = 20
HARD_GUARD_S = 1.5
CONNECT_TIMEOUT_S = 0.2

TOKEN_RE = re.compile(r"yk:b(\d{1,12})\.([0-9a-f]{8})")
NONCE_RE = re.compile(r"yk:j([0-9a-f]{16})")
# the model name a payload reports (Claude SessionStart, Codex, Cursor): only a
# short identifier-shaped value is relayed, for the report (DESIGN.md §12.6). An
# "@" may carry a version (Vertex: claude-...@20250514) but never a domain, so
# nothing email-shaped gets through.
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}(?:@[A-Za-z0-9_:+-]{1,32})?")
JOIN_TOOLS = ("MCP:join", "mcp__switchboard__join")

# Events each harness registers (DESIGN.md §7.4), in that harness's spelling.
HANDLED = {
    "claude": ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"),
    "codex": ("UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "SessionEnd"),
    "cursor": (
        "sessionStart",
        "beforeSubmitPrompt",
        "postToolUse",
        "postToolUseFailure",
        "stop",
        "sessionEnd",
    ),
    "devin": ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"),
}

# The fixed output table (DESIGN.md §7.3): which events may print what.
CONTEXT_EVENTS = {
    "claude": ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure"),
    "codex": ("PostToolUse",),
    "devin": ("PostToolUse",),
    "cursor": ("postToolUse", "postToolUseFailure"),
}
CONTINUE_EVENTS = {
    "devin": ("Stop",),
    "cursor": ("stop",),
}
CONTEXT_MAX = {"claude": 10000, "codex": 5000, "devin": 6000, "cursor": 8000}


def sock_path(home):
    """Return the broker's Unix socket path for ``home``.

    ``<home>/run/broker.sock`` when it fits in 100 bytes, otherwise
    ``/tmp/switchboard-<uid>/<sha256(realpath(home))[:12]>.sock``.
    """
    home = os.path.realpath(os.path.expanduser(str(home)))
    primary = os.path.join(home, "run", "broker.sock")
    if len(primary.encode("utf-8", "surrogateescape")) <= SOCK_MAX_BYTES:
        return primary
    digest = hashlib.sha256(home.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    return os.path.join("/tmp", "switchboard-%d" % os.getuid(), digest + ".sock")


# ---------------------------------------------------------------- arguments
def parse_args(argv):
    out = {"home": None, "harness": None, "event": None, "max_wait": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--home", "--harness", "--event", "--max-wait") and i + 1 < len(argv):
            key = a[2:].replace("-", "_")
            out[key] = argv[i + 1]
            i += 2
            continue
        i += 1
    if out["max_wait"] is not None:
        try:
            out["max_wait"] = max(0.0, float(out["max_wait"]))
        except ValueError:
            out["max_wait"] = None
    return out


# ---------------------------------------------------------------- detection
def detect_harness(payload, env, flag):
    """The harness really running this hook. Only env *presence* is read."""
    if "cursor_version" in payload:
        return "cursor"
    if "DEVIN_PROJECT_DIR" in env or "CHISEL_SESSION_DB" in env:
        return "devin"
    return flag


# ---------------------------------------------------------------- allowlist
def _as_text(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            v = json.dumps(v)
        except (TypeError, ValueError):
            return ""
    if not isinstance(v, str):
        v = str(v)
    return v[:FIELD_SCAN_MAX]


def _str(v, n=128):
    return v[:n] if isinstance(v, str) and v else None


def _model(v):
    return v if isinstance(v, str) and len(v) <= 64 and MODEL_RE.fullmatch(v) else None


def extract_tokens(payload):
    toks = []
    seen = set()
    for key in ("prompt", "tool_response", "tool_output", "tool_result"):
        for m in TOKEN_RE.finditer(_as_text(payload.get(key))):
            t = (int(m.group(1)), m.group(2))
            if t not in seen:
                seen.add(t)
                toks.append([t[0], t[1]])
            if len(toks) >= MAX_TOKENS:
                return toks
    return toks


def build_params(payload, harness, event, t0, max_wait):
    """Only these fields ever leave the hook (DESIGN.md §7.2 step 4)."""
    tool = _str(payload.get("tool_name"))
    ok = None
    tr = payload.get("tool_response")
    if "success" in payload and isinstance(payload.get("success"), bool):
        ok = payload["success"]
    elif harness == "devin" and isinstance(tr, dict) and isinstance(tr.get("success"), bool):
        ok = tr["success"]  # Devin: tool_response = {success, output, error}
    elif event.lower() in ("posttoolusefailure",):
        ok = False
    elif event.lower() == "posttooluse":
        ok = True
    gen = payload.get("generation_id") or payload.get("prompt_id") or payload.get("turn_id")
    p = {
        "harness": harness,
        "event": event,
        "sid": _str(payload.get("session_id") or payload.get("conversation_id")),
        "gen": _str(gen),
        "tool": tool,
        "tool_use_id": _str(payload.get("tool_use_id")),
        "ok": ok,
        "status": _str(payload.get("status"), 32),
        "loop_count": payload.get("loop_count") if isinstance(payload.get("loop_count"), int) else None,
        "stop_hook_active": payload.get("stop_hook_active")
        if isinstance(payload.get("stop_hook_active"), bool)
        else None,
        "source": _str(payload.get("source"), 32),
        "reason": _str(payload.get("reason"), 64),
        "permission_mode": _str(payload.get("permission_mode"), 32),
        "model": _model(payload.get("model")),
        "tokens": extract_tokens(payload),
        "t": t0,
        "max_wait_s": max_wait,
    }
    if tool in JOIN_TOOLS:
        m = NONCE_RE.search(
            _as_text(payload.get("tool_output")) + " " + _as_text(payload.get("tool_response"))
        )
        if m:
            p["join_nonce"] = m.group(1)
    if harness == "devin" and event == "PreToolUse" and tool == "run_subagent":
        ti = payload.get("tool_input")
        if isinstance(ti, dict) and ti.get("is_background") is True:
            p["subagent_bg"] = True
    return {k: v for k, v in p.items() if v is not None and v != []}


# ------------------------------------------------------------------- output
def render(harness, event, out):
    """The only shapes this script can print (DESIGN.md §7.3); None prints nothing."""
    if not isinstance(out, dict):
        return None
    kind = out.get("kind")
    text = out.get("text")
    if not isinstance(text, str) or not text:
        return None
    if kind == "context" and event in CONTEXT_EVENTS.get(harness, ()):
        if len(text) > CONTEXT_MAX.get(harness, 5000):
            # Never cut: a cut batch would be acked as if it had all been seen.
            # Print nothing and don't ack; the broker fits batches to this
            # limit, so this is only a safety net (the offer expires, retries).
            return None
        if harness == "cursor":
            return {"additional_context": text}
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if kind == "continue" and event in CONTINUE_EVENTS.get(harness, ()):
        if harness == "devin":
            return {"decision": "block", "reason": text}
        if harness == "cursor":
            return {"followup_message": text}
    return None


# ------------------------------------------------------------------ socket
def _connect(path):
    st = os.stat(path)
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT_S)
    s.connect(path)
    return s


def _agent_gpid():
    """Cursor stop parks: the agent is our grandparent (its bash wrapper gets reparented)."""
    try:
        import subprocess

        ps = "/bin/ps" if os.path.exists("/bin/ps") else "/usr/bin/ps"
        r = subprocess.run(
            [ps, "-o", "ppid=", "-p", str(os.getppid())], capture_output=True, text=True, timeout=2
        )
        v = int(r.stdout.strip())
        return v if v > 1 else None
    except Exception:
        return None


def ask_broker(path, params, max_wait, watch_pid=None):
    """Send hook.event and wait (select, 1 s ticks) up to ``max_wait`` seconds."""
    s = _connect(path)
    if s is None:
        return None, None
    ok = False
    try:
        req = json.dumps({"id": 1, "method": "hook.event", "params": params}) + "\n"
        s.sendall(req.encode())
        buf = b""
        deadline = time.monotonic() + max_wait
        while b"\n" not in buf:
            left = deadline - time.monotonic()
            if left <= 0:
                return None, None
            r, _, _ = select.select([s], [], [], min(1.0, left))
            if watch_pid is not None:
                try:
                    os.kill(watch_pid, 0)
                except OSError:
                    return None, None
            if not r:
                continue
            chunk = s.recv(65536)
            if not chunk:
                return None, None
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        obj = json.loads(line)
        ok = True
        return (obj.get("result") if isinstance(obj, dict) else None), s
    except Exception:
        return None, None
    finally:
        if not ok:
            try:
                s.close()
            except Exception:
                pass


def send_ack(s, batch_id, ack):
    try:
        req = json.dumps({"id": 2, "method": "hook.ack", "params": {"batch_id": batch_id, "ack": ack}}) + "\n"
        s.settimeout(0.2)
        s.sendall(req.encode())
    except Exception:
        pass


# -------------------------------------------------------------------- main
def run(argv, stdin, stdout, env):
    """Returns the text it printed (tests call this in-process); never raises."""
    t0 = time.time()
    args = parse_args(argv)
    try:
        raw = stdin.buffer.read() if hasattr(stdin, "buffer") else stdin.read()
    except Exception:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    flag = args["harness"]
    event = args["event"]
    if flag not in HANDLED or not event:
        return None
    harness = detect_harness(payload, env, flag)
    if harness != flag:
        return None  # another harness imported this hook: its own copy handles it
    name = payload.get("hook_event_name")
    if isinstance(name, str) and name.lower() != event.lower():
        return None
    if event not in HANDLED[harness]:
        return None
    home = args["home"]
    if not home:
        return None
    max_wait = args["max_wait"] if args["max_wait"] is not None else 1.0
    params = build_params(payload, harness, event, t0, max_wait)
    watch = _agent_gpid() if (harness == "cursor" and event == "stop" and max_wait > 1.5) else None
    try:
        result, s = ask_broker(sock_path(home), params, max_wait, watch)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    shaped = render(harness, event, result.get("out"))
    if shaped is None:
        if s is not None:
            s.close()
        return None
    text = json.dumps(shaped)
    try:
        stdout.write(text)
        stdout.flush()
    except Exception:
        return None
    bid, ack = result.get("batch_id"), result.get("ack")
    if s is not None:
        if isinstance(bid, int) and isinstance(ack, str):
            send_ack(s, bid, ack)
        s.close()
    return text


def _guard(_signum, _frame):
    os._exit(0)


def main(argv=None):
    try:
        argv = sys.argv[1:] if argv is None else argv
        args = parse_args(argv)
        if args["max_wait"] is None and hasattr(signal, "setitimer"):
            signal.signal(signal.SIGALRM, _guard)
            signal.setitimer(signal.ITIMER_REAL, HARD_GUARD_S)
        run(argv, sys.stdin, sys.stdout, os.environ)
    except BaseException:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
