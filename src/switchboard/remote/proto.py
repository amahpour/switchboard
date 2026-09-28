"""The link protocol between the broker and a satellite (DESIGN.md §27.4.4-§27.4.6).

UTF-8 JSON, one object per line, at most ``MAX_FRAME`` bytes. Every frame has
``t``, its type. Until the satellite's ``hello`` the broker skips lines that are
not a JSON object (a chatty login shell on the Pi): at most ``HELLO_MAX_LINES``
lines and ``HELLO_MAX_BYTES`` bytes within ``HELLO_TIMEOUT_S``; after ``hello``
any malformed frame ends the link.

This module does no I/O except ``read_hello`` (an asyncio reader). The satellite
imports it, so it has no spawn site, no pty and no network socket
(``tests/unit/test_satellite_static.py``).

Frames (s = satellite, b = broker):

=====  ==========  ==================================================================
dir    ``t``       fields
=====  ==========  ==================================================================
s→b    hello       proto, version, name, now, hook_state, test_mode, harden
b→s    welcome     proto, version, link, rooms, harnesses, limits
b→s    refuse      why, message
s→b    open        c
s→b    req         c, line, facts?  (``{attest}`` on mcp.hello, ``{chain}`` on hook.event,
                                     ``{lastmile}`` on the satellite's own mcp.posted)
b→s    out         c, line, chk?
both   close       c
b→s    watch       n, procs, claude
s→b    alive       n, dead
s→b    reg         views, read_age
b→s    ping        n
s→b    pong        n
s→b    status      hook_state
s→b    bye         why
=====  ==========  ==================================================================

``n`` on ``watch``/``alive`` (an addition to the design's field list) is the
watch's sequence number, echoed by the ``alive`` that answers it, so the broker
knows which watched pairs an ``alive`` frame has vouched for.

Only ages cross the link (§27.4.6): the satellite turns the Pi's timestamps into
ages on its own clock (``to_ages``) and the broker rebases them to its receive
time with per-field clamps (``from_ages``).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from switchboard.models import HARNESSES, HOST_RE, ROOM_RE

LINK_PROTO = 1
MAX_FRAME = 2 * 1024 * 1024  # a client line is at most 1 MiB (rpc.MAX_LINE), plus the envelope
MAX_LINE = 1 << 20  # a client line, as on the broker's own socket (rpc.MAX_LINE; a unit test checks)
# a ``req`` frame: one client line re-encoded by the satellite, plus its envelope and facts. The
# satellite refuses a request that would be larger; the broker closes a link that sends one.
MAX_REQ_FRAME = MAX_LINE + 64 * 1024
HELLO_MAX_LINES = 64
HELLO_MAX_BYTES = 64 * 1024
HELLO_TIMEOUT_S = 10.0
PID_MAX = 2**31 - 1
MAX_CHAIN = 8
MAX_PAIRS = 4096  # watch/alive/reg entries
SOCKET_MAX_BYTES = 1023

# The methods a remote connection may call (DESIGN.md §27.5.2). Everything else
# (sys.*, room.*, human.*, remote.*) is refused before any role check, under every
# peer policy. ``broker/rpc.py`` enforces it; the satellite refuses the rest itself
# only to give the Pi a clear message.
REMOTE_METHODS = frozenset(
    {
        "mcp.hello", "mcp.attach", "mcp.posted", "mcp.bye",
        "agent.join", "agent.leave", "agent.who", "agent.say", "agent.read", "agent.wait",
        "agent.unwait", "agent.pass", "agent.away",
        "hook.event", "hook.ack",
    }
)

S2B = frozenset({"hello", "open", "req", "close", "alive", "reg", "pong", "status", "bye"})
B2S = frozenset({"welcome", "refuse", "out", "close", "watch", "ping"})
BYE_WHY = frozenset({"eof", "shutdown", "replaced", "local_broker", "busy"})
HARDEN = frozenset({"prctl", "none", "failed"})
WANT = frozenset({"idle", "busy"})
# mcp.posted's err when the satellite's last-mile check drops a Claude ``deliver`` push
# (§27.5.6), in its own report marked ``facts.lastmile``: the session's registry no longer
# says ``chk.want`` (an uncounted re-route on the broker); the push carried no ``chk``; or
# ``chk`` doesn't name the Claude this connection's MCP server was attested under (both
# counted failures: the broker never sends either)
STALE_STATUS = "stale_status"
NO_CHK = "no_chk"
BAD_CHK = "bad_chk"

# A hook chain's per-process verdict (§27.5.5): the agent a readable argv runs, '-' for a
# readable argv that runs no agent, '?' for an unreadable one. No argv leaves the Pi.
VERDICT_ARGV: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "cursor": "cursor-agent",
    "devin": "devin acp",
    "-": "-",
    "?": "",
}

# What verify_mcp_peer can say (broker/peer.py): evidence -> the harness it goes with.
# tests/unit/test_remote_proto.py drives every branch of verify_mcp_peer against this table.
EVIDENCE: dict[str, str] = {
    "flag:test": "test",
    "parent:claude+registry": "claude",
    "parent:claude,registry-mismatch": "unknown",
    "claude-claim,parent-mismatch": "unknown",
    "parent:codex": "codex",
    "codex-claim,parent-mismatch": "unknown",
    "ancestor:devin-acp": "devin",
    "devin-claim,no-acp": "unknown",
    "ancestor:cursor": "cursor",
    "cursor-claim,no-ancestor": "unknown",
    "unknown": "unknown",
}
TIER_NOTES = frozenset({"unverified claude", "unverified codex", "unverified devin", "unverified cursor"})

# Ages (§27.4.6): name -> (lo, hi) clamp, seconds.
AGE_CLAMPS: dict[str, tuple[float, float]] = {
    "t_age": (0.0, 30.0),  # a hook's start time
    "t_post_age": (0.0, 30.0),  # mcp.posted's post time
    "since_age": (0.0, 86400.0),  # a Claude registry status's age: may be old (never faked short)
    "read_age": (0.0, 5.0),  # when the satellite read the registry
}
# request param -> its age field, per method
TIME_PARAMS: dict[str, tuple[str, str]] = {
    "hook.event": ("t", "t_age"),
    "mcp.posted": ("t_post", "t_post_age"),
}

_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VERSION = re.compile(r"^[A-Za-z0-9.+_-]{1,40}$")


class FrameError(ValueError):
    """A frame that isn't valid: ``code`` names why (no frame content in it)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class ShellNoise(Exception):
    """More text than the login shell may print before the satellite's hello."""


class LinkEOF(Exception):
    """The link ended before a hello."""


# ------------------------------------------------------------------ values
def is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pid(v: Any) -> int:
    if not is_int(v) or not 1 <= v <= PID_MAX:
        raise FrameError("bad_pid")
    return int(v)


def _start(v: Any) -> float:
    if not is_num(v) or v < 0:
        raise FrameError("bad_start")
    return float(v)


def _pair(v: Any) -> tuple[int, float]:
    if not isinstance(v, list) or len(v) != 2:
        raise FrameError("bad_pair")
    return _pid(v[0]), _start(v[1])


def _cid(v: Any) -> int:
    if not is_int(v) or not 1 <= v <= PID_MAX:
        raise FrameError("bad_conn")
    return int(v)


def _seq(v: Any) -> int:
    if not is_int(v) or not 0 <= v <= 2**53:
        raise FrameError("bad_seq")
    return int(v)


def _text(v: Any, n: int) -> str:
    if not isinstance(v, str) or len(v) > n:
        raise FrameError("bad_text")
    return v


def _socket(v: Any) -> str | None:
    if v is None:
        return None
    if (not isinstance(v, str) or not v.startswith("/") or "\0" in v
            or len(v.encode("utf-8", "surrogatepass")) > SOCKET_MAX_BYTES):
        raise FrameError("bad_socket")
    return v


def _keys(obj: Mapping[str, Any], required: set[str], optional: frozenset[str] = frozenset()) -> None:
    have = set(obj)
    if not required <= have:
        raise FrameError("missing_field", ",".join(sorted(required - have)))
    extra = have - required - optional
    if extra:
        raise FrameError("unknown_field", ",".join(sorted(extra)))


def check_attest(a: Any) -> dict[str, Any]:
    """``facts.attest`` (§27.5.3): what the satellite's ``verify_mcp_peer`` said about
    its own kernel peer. Strict: pids in 1..2^31-1, finite starts, the fixed evidence
    vocabulary (which names the harness), an absolute ``claude_socket`` of at most
    1023 bytes, present exactly when the Claude registry check passed."""
    if not isinstance(a, dict):
        raise FrameError("bad_attest")
    _keys(a, {"harness", "mcp", "agent", "evidence", "tier_note", "claude_socket"})
    harness, evidence, note = a["harness"], a["evidence"], a["tier_note"]
    if harness not in HARNESSES or not isinstance(evidence, str) or EVIDENCE.get(evidence) != harness:
        raise FrameError("bad_attest", "harness/evidence")
    if note is not None and note not in TIER_NOTES:
        raise FrameError("bad_attest", "tier_note")
    sock = _socket(a["claude_socket"])
    if (sock is not None) != (evidence == "parent:claude+registry"):
        raise FrameError("bad_attest", "claude_socket")
    mcp = _pair(a["mcp"])
    agent = None if a["agent"] is None else _pair(a["agent"])
    return {"harness": harness, "mcp": mcp, "agent": agent, "evidence": evidence, "tier_note": note,
            "claude_socket": sock}


def check_chain(c: Any) -> list[tuple[int, float, str]]:
    """``facts.chain`` (§27.5.5): ``[pid, start, verdict]`` from the hook process up,
    at most ``MAX_CHAIN`` entries; no argv, env, cwd or path."""
    if not isinstance(c, list) or not 1 <= len(c) <= MAX_CHAIN:
        raise FrameError("bad_chain")
    out = []
    for e in c:
        if not isinstance(e, list) or len(e) != 3 or e[2] not in VERDICT_ARGV:
            raise FrameError("bad_chain")
        out.append((_pid(e[0]), _start(e[1]), e[2]))
    return out


def _facts(f: Any) -> dict[str, Any]:
    if not isinstance(f, dict) or len(f) != 1:
        raise FrameError("bad_facts")
    if "attest" in f:
        return {"attest": check_attest(f["attest"])}
    if "chain" in f:
        return {"chain": check_chain(f["chain"])}
    if "lastmile" in f and f["lastmile"] is True:
        # the satellite's own mcp.posted after its last-mile check dropped a push (§27.5.6)
        return {"lastmile": True}
    raise FrameError("bad_facts")


def _pairs(v: Any) -> list[tuple[int, float]]:
    if not isinstance(v, list) or len(v) > MAX_PAIRS:
        raise FrameError("bad_pairs")
    return [_pair(x) for x in v]


# ------------------------------------------------------------ validators
def _v_hello(f: dict[str, Any]) -> dict[str, Any]:
    # the proto is checked first: a satellite of another protocol is refused by name (blocked(proto)),
    # whatever the rest of its hello looks like
    if not is_int(f.get("proto")):
        raise FrameError("bad_proto")
    if f["proto"] != LINK_PROTO:
        v = f.get("version")
        return {"t": "hello", "proto": f["proto"], "version": v if isinstance(v, str) and _VERSION.match(v) else "?"}
    _keys(f, {"t", "proto", "version", "name", "now", "hook_state", "test_mode", "harden"})
    if not isinstance(f["version"], str) or not _VERSION.match(f["version"]):
        raise FrameError("bad_version")
    if not isinstance(f["name"], str) or not HOST_RE.match(f["name"]):
        raise FrameError("bad_name")
    if not is_num(f["now"]):
        raise FrameError("bad_now")
    _text(f["hook_state"], 200)
    if not isinstance(f["test_mode"], bool) or f["harden"] not in HARDEN:
        raise FrameError("bad_hello")
    return dict(f)


def _v_welcome(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "proto", "version", "link", "rooms", "harnesses", "limits"})
    if f["proto"] != LINK_PROTO or not is_int(f["proto"]):
        raise FrameError("bad_proto")
    if not isinstance(f["version"], str) or not _VERSION.match(f["version"]):
        raise FrameError("bad_version")
    if not isinstance(f["link"], str) or not _HEX16.match(f["link"]):
        raise FrameError("bad_link")
    rooms, hs, lim = f["rooms"], f["harnesses"], f["limits"]
    if not isinstance(rooms, list) or len(rooms) > 64 or not all(isinstance(r, str) and ROOM_RE.match(r)
                                                                  for r in rooms):
        raise FrameError("bad_rooms")
    if not isinstance(hs, list) or not all(h in HARNESSES for h in hs):
        raise FrameError("bad_harnesses")
    if not isinstance(lim, dict) or not all(isinstance(k, str) and is_int(v) and v >= 0 for k, v in lim.items()):
        raise FrameError("bad_limits")
    return dict(f)


def _v_refuse(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "why", "message"})
    if not isinstance(f["why"], str) or not _REASON.match(f["why"]):
        raise FrameError("bad_why")
    _text(f["message"], 300)
    return dict(f)


def _v_conn(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "c"})
    return {"t": f["t"], "c": _cid(f["c"])}


def _v_req(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "c", "line"}, frozenset({"facts"}))
    if not isinstance(f["line"], dict):
        raise FrameError("bad_line")
    out = {"t": "req", "c": _cid(f["c"]), "line": f["line"]}
    if "facts" in f:
        out["facts"] = _facts(f["facts"])
    return out


def _v_out(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "c", "line"}, frozenset({"chk"}))
    if not isinstance(f["line"], dict):
        raise FrameError("bad_line")
    out = {"t": "out", "c": _cid(f["c"]), "line": f["line"]}
    if "chk" in f:
        chk = f["chk"]
        if not isinstance(chk, dict):
            raise FrameError("bad_chk")
        _keys(chk, {"pid", "start", "want"})
        if chk["want"] not in WANT:
            raise FrameError("bad_chk")
        out["chk"] = {"pid": _pid(chk["pid"]), "start": _start(chk["start"]), "want": chk["want"]}
    return out


def _v_watch(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "n", "procs", "claude"})
    cl = f["claude"]
    if not isinstance(cl, list) or len(cl) > MAX_PAIRS:
        raise FrameError("bad_claude")
    claude = []
    for e in cl:
        if not isinstance(e, list) or len(e) != 3:
            raise FrameError("bad_claude")
        claude.append((_pid(e[0]), _start(e[1]), _socket(e[2])))
    return {"t": "watch", "n": _seq(f["n"]), "procs": _pairs(f["procs"]), "claude": claude}


def _v_alive(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "n", "dead"})
    return {"t": "alive", "n": _seq(f["n"]), "dead": _pairs(f["dead"])}


def _v_reg(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "views", "read_age"})
    vs = f["views"]
    if not isinstance(vs, list) or len(vs) > MAX_PAIRS or not is_num(f["read_age"]):
        raise FrameError("bad_reg")
    views = []
    for e in vs:
        if not isinstance(e, list) or len(e) != 4:
            raise FrameError("bad_reg")
        status, since = e[2], e[3]
        if status is not None and (not isinstance(status, str) or len(status) > 32):
            raise FrameError("bad_reg")
        if since is not None and not is_num(since):
            raise FrameError("bad_reg")
        views.append((_pid(e[0]), _start(e[1]), status, since))
    return {"t": "reg", "views": views, "read_age": float(f["read_age"])}


def _v_n(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "n"})
    return {"t": f["t"], "n": _seq(f["n"])}


def _v_status(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "hook_state"})
    return {"t": "status", "hook_state": _text(f["hook_state"], 200)}


def _v_bye(f: dict[str, Any]) -> dict[str, Any]:
    _keys(f, {"t", "why"})
    if f["why"] not in BYE_WHY:
        raise FrameError("bad_why")
    return {"t": "bye", "why": f["why"]}


_VALIDATORS = {
    "hello": _v_hello, "welcome": _v_welcome, "refuse": _v_refuse, "open": _v_conn, "req": _v_req,
    "out": _v_out, "close": _v_conn, "watch": _v_watch, "alive": _v_alive, "reg": _v_reg,
    "ping": _v_n, "pong": _v_n, "status": _v_status, "bye": _v_bye,
}


def validate(frame: Any, direction: str) -> dict[str, Any]:
    """A frame from ``direction`` (``"s2b"`` or ``"b2s"``), normalized, or FrameError."""
    if not isinstance(frame, dict):
        raise FrameError("not_object")
    t = frame.get("t")
    allowed = S2B if direction == "s2b" else B2S
    if not isinstance(t, str) or t not in allowed:
        raise FrameError("bad_type")
    return _VALIDATORS[t](frame)


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def loads(line: bytes | str) -> Any:
    """JSON without NaN or Infinity."""
    return json.loads(line, parse_constant=_no_constants)


def decode(line: bytes, direction: str) -> dict[str, Any]:
    """One received line -> a validated frame, or FrameError."""
    if len(line) > MAX_FRAME + 1:
        raise FrameError("oversize")
    try:
        obj = loads(line)
    except (ValueError, UnicodeDecodeError):
        raise FrameError("bad_json") from None
    return validate(obj, direction)


def encode(frame: dict[str, Any]) -> bytes:
    """A frame as one line (FrameError if it would be over ``MAX_FRAME``)."""
    data = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(data) > MAX_FRAME + 1:
        raise FrameError("oversize")
    return data


# ------------------------------------------------------------- builders
def hello(*, version: str, name: str, now: float, hook_state: str, test_mode: bool, harden: str,
          proto: int = LINK_PROTO) -> dict[str, Any]:
    return {"t": "hello", "proto": proto, "version": version, "name": name, "now": now,
            "hook_state": hook_state[:200], "test_mode": test_mode, "harden": harden}


def welcome(*, version: str, link: str, rooms: list[str], harnesses: list[str],
            limits: dict[str, int]) -> dict[str, Any]:
    return {"t": "welcome", "proto": LINK_PROTO, "version": version, "link": link, "rooms": rooms,
            "harnesses": harnesses, "limits": limits}


def refuse(why: str, message: str) -> dict[str, Any]:
    return {"t": "refuse", "why": why, "message": message[:300]}


def open_(c: int) -> dict[str, Any]:
    return {"t": "open", "c": c}


def req(c: int, line: dict[str, Any], facts: dict[str, Any] | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {"t": "req", "c": c, "line": line}
    if facts:
        f["facts"] = facts
    return f


def out(c: int, line: dict[str, Any], chk: dict[str, Any] | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {"t": "out", "c": c, "line": line}
    if chk is not None:
        f["chk"] = chk
    return f


def close(c: int) -> dict[str, Any]:
    return {"t": "close", "c": c}


def watch(n: int, procs: list[tuple[int, float]], claude: list[tuple[int, float, str | None]]) -> dict[str, Any]:
    return {"t": "watch", "n": n, "procs": [list(p) for p in procs], "claude": [list(c) for c in claude]}


def alive(n: int, dead: list[tuple[int, float]]) -> dict[str, Any]:
    return {"t": "alive", "n": n, "dead": [list(d) for d in dead]}


def reg(views: list[tuple[int, float, str | None, float | None]], read_age: float) -> dict[str, Any]:
    """The relayed Claude registry: ``[pid, start, status | None, since_age | None]`` per
    watched Claude, and how long ago (``read_age``) the satellite read it (§27.5.6)."""
    return {"t": "reg", "views": [list(v) for v in views], "read_age": read_age}


def ping(n: int) -> dict[str, Any]:
    return {"t": "ping", "n": n}


def pong(n: int) -> dict[str, Any]:
    return {"t": "pong", "n": n}


def status(hook_state: str) -> dict[str, Any]:
    return {"t": "status", "hook_state": hook_state[:200]}


def bye(why: str) -> dict[str, Any]:
    return {"t": "bye", "why": why}


# ------------------------------------------------------------------ hello
class HelloScanner:
    """Skips what a login shell prints before the satellite starts (§27.4.4).

    ``feed`` returns the first line that is a JSON object with ``t`` of ``hello``
    or ``bye``, None for a noise line, and raises ShellNoise past the limits."""

    def __init__(self, max_lines: int = HELLO_MAX_LINES, max_bytes: int = HELLO_MAX_BYTES):
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self.lines = 0
        self.bytes = 0

    def feed(self, line: bytes) -> dict[str, Any] | None:
        s = line.strip()
        if s.startswith(b"{"):
            try:
                obj = loads(s)
            except (ValueError, UnicodeDecodeError):
                obj = None
            if isinstance(obj, dict) and obj.get("t") in ("hello", "bye"):
                return obj
        self.lines += 1
        self.bytes += len(line)
        if self.lines > self.max_lines or self.bytes > self.max_bytes:
            raise ShellNoise(f"{self.lines} lines, {self.bytes} bytes before the hello")
        return None


async def read_hello(reader: asyncio.StreamReader, timeout: float = HELLO_TIMEOUT_S,
                     max_lines: int = HELLO_MAX_LINES, max_bytes: int = HELLO_MAX_BYTES) -> dict[str, Any]:
    """The satellite's first frame (``hello`` or ``bye``), skipping shell noise.

    Raises ShellNoise (over the limits), LinkEOF (the link ended first) or
    TimeoutError (nothing within ``timeout``)."""
    scan = HelloScanner(max_lines, max_bytes)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        left = deadline - loop.time()
        if left <= 0:
            raise TimeoutError("no hello")
        try:
            line = await asyncio.wait_for(reader.readline(), left)
        except (asyncio.TimeoutError, TimeoutError):
            raise TimeoutError("no hello") from None
        except (ValueError, asyncio.LimitOverrunError):
            raise ShellNoise("a line over the frame limit before the hello") from None
        if not line:
            raise LinkEOF("eof before hello")
        got = scan.feed(line)
        if got is not None:
            return got


# ------------------------------------------------------------------- ages
def age(t: Any, now: float) -> float | None:
    """``now - t`` for a timestamp on the same clock, or None if ``t`` isn't a number."""
    return float(now - t) if is_num(t) else None


def rebase(a: Any, recv: float, lo: float, hi: float) -> float | None:
    """``recv - clamp(a, lo, hi)``: an age from the far clock as a time on this one."""
    if not is_num(a):
        return None
    return float(recv - min(max(float(a), lo), hi))


def rebase_field(name: str, a: Any, recv: float) -> float | None:
    lo, hi = AGE_CLAMPS[name]
    return rebase(a, recv, lo, hi)


def to_ages(method: str, params: dict[str, Any], now: float) -> dict[str, Any]:
    """The satellite's side: a request's Pi timestamps become ages on the Pi's clock."""
    tp = TIME_PARAMS.get(method)
    if tp is None:
        return params
    field, age_field = tp
    out = {k: v for k, v in params.items() if k not in (field, age_field)}
    a = age(params.get(field), now)
    if a is not None:
        out[age_field] = a
    return out


def from_ages(method: str, params: dict[str, Any], recv: float) -> dict[str, Any]:
    """The broker's side: ages rebased to the receive time, clamped per field. Any
    wall-clock value the far side sent (``t``, ``t_post``) is dropped: the broker never
    uses a remote host's clock in delivery logic (§27.4.6)."""
    out = {k: v for k, v in params.items() if k not in ("t", "t_post", "t_age", "t_post_age")}
    tp = TIME_PARAMS.get(method)
    if tp is not None:
        field, age_field = tp
        v = rebase_field(age_field, params.get(age_field), recv)
        if v is not None:
            out[field] = v
    return out


# --------------------------------------------------------------- verdicts
def verdict(argv: str, match: Any) -> str:
    """A process's verdict from its argv (``match`` is ``peer.match_agent``)."""
    if not argv:
        return "?"
    return match(argv) or "-"


def verdict_argv(v: str) -> str:
    """The canonical argv a verdict stands for, for ``resolve_hook_participant``'s argv_fn."""
    return VERDICT_ARGV.get(v, "")
