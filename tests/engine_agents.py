"""Sessions over HTTPS for engine tests (DESIGN.md §49.3, §49.4): what each harness's tests build.

Every real harness is a session over HTTPS on a device (``laptop``), woken by its own Stop hook, which
the broker holds (a park) until a message comes, or by a ``wait()`` call it makes; a busy one gets
priority messages as hook context after its tool calls. The scripted ``test`` agent has no device: it
only waits (§9.6). These helpers build those sessions on a ``World`` (``engine_world.py``) and read what
the engine did, with no broker and no I/O.
"""

from __future__ import annotations

from typing import Any

from engine_world import World

from switchboard.models import HookEvent, Membership, Participant, ResolveSink

# a Stop hook's ``max_wait_s``: about a day, the longest a park is held (``adapters.https.LISTEN_S``)
STOP_MAX_WAIT_S = 86390.0


def session(
    w: World,
    name: str = "claude-1",
    harness: str = "claude",
    *,
    status: str = "idle",
    mode: str | None = None,
    room_id: int | None = None,
) -> tuple[Participant, Membership]:
    """A session of ``harness`` over HTTPS that has called a hook already (``hooks_seen_at`` set),
    with an approval ``mode`` (``prompting``, ``bypass``, ...) when the test needs one."""
    p, m = w.agent(name, harness=harness, status=status, hooks=True, room_id=room_id)
    if mode is not None:
        p = w.store.update_participant(p.id, approval_mode=mode)
    return p, m


def stop(w: World, p: Participant, **fields: Any) -> Any:
    """The session's Stop hook, which may wait about a day: the broker parks it and the hook gets a
    ``park`` answer naming the sink, or ``None`` when nothing can park (the room is paused)."""
    fields.setdefault("max_wait_s", STOP_MAX_WAIT_S)
    return w.hook(p, "Stop", **fields)


def listener(
    w: World,
    name: str = "claude-1",
    harness: str = "claude",
    *,
    status: str = "busy",
    mode: str | None = None,
    room_id: int | None = None,
) -> tuple[Participant, Membership, Any]:
    """A session whose turn ended and whose Stop hook waits on the broker: idle and listening.
    Returns ``(participant, membership, park)``; ``park.sink_id`` is what a wake resolves."""
    p, m = session(w, name, harness, status=status, mode=mode, room_id=room_id)
    out = stop(w, p)
    assert out is not None and out.kind == "park", out
    return p, m, out


def waiting(
    w: World, name: str = "bot", harness: str = "test", wait_s: float = 3600.0
) -> tuple[Any, Any, Any]:
    """A session in its own ``wait()`` call: ``(participant, membership, sink)``."""
    p, m = w.agent(name, harness=harness, status="idle", hooks=harness != "test")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), f"w-{name}", wait_s)
    w.actions += acts
    return p, m, sink


def result(w: World, sink_or_id: Any) -> dict[str, Any] | None:
    """What a closed wait or park was answered with, or ``None`` while it is open."""
    sid = sink_or_id if isinstance(sink_or_id, int) else getattr(sink_or_id, "sink_id", None) or sink_or_id.id
    s = w.engine.sinks.get(sid)
    return None if s is None or s.open else (s.result or {})


def woke(w: World, sink_or_id: Any) -> bool:
    """Whether a wait or park was answered with messages."""
    res = result(w, sink_or_id)
    return bool(res) and res.get("status") == "messages"


def fills(w: World, sink_or_id: Any = None) -> list[dict[str, Any]]:
    """The answers the engine has put on sinks since the last ``take`` (of one sink, or any)."""
    sid = None
    if sink_or_id is not None:
        sid = (
            sink_or_id
            if isinstance(sink_or_id, int)
            else getattr(sink_or_id, "sink_id", None) or sink_or_id.id
        )
    return [
        a.result
        for a in w.take()
        if isinstance(a, ResolveSink)
        and (sid is None or a.sink_id == sid)
        and a.result.get("status") == "messages"
    ]


def post(w: World, p: Participant, **fields: Any) -> Any:
    """A PostToolUse hook of the session: the priority context its turn is handed after a tool call."""
    fields.setdefault("ok", True)
    return w.hook(p, "PostToolUse", **fields)


def stop_event(p: Participant, **fields: Any) -> HookEvent:
    return HookEvent(harness=p.harness, event="Stop", max_wait_s=STOP_MAX_WAIT_S, **fields)
