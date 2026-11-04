"""One live call: a joinly session, and the lines it posts (DESIGN.md §52).

This is the only async part of a meeting. The broker's lifespan owns one bridge;
each open call is one task and one MCP session. The task always calls
``leave_meeting`` and then closes the client, including when the join fails,
the connection drops, the room closes, or the broker stops. The meeting link
lives only in the task, long enough to join, and is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import warnings
from collections import deque
from typing import Any

from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.exceptions import MCPDeprecationWarning

from switchboard.envelope import clean
from switchboard.meeting.lines import Assembler, Line, Segment
from switchboard.meeting.links import PLATFORM_LABEL
from switchboard.meeting.names import hotwords, match

log = logging.getLogger("switchboard.meeting")

PARTICIPANT = "switchboard (transcribing)"
RESOURCE = "transcript://live/segments"
JOIN_TIMEOUT_S = 45.0
LEAVE_TIMEOUT_S = 5.0
POLL_S = 3.0
LINE_GAP_S = 1.0
QUEUE_MAX = 50
_SECRET = re.compile(r"https?://\S+|pwd=\S+", re.IGNORECASE)


_REDACT_LOGGERS = ("mcp", "fastmcp", "httpx", "httpcore", "sse_starlette", "switchboard.meeting")
_redact_installed = False


def _install_redact() -> None:
    """The MCP client logs a tool call's arguments at DEBUG, and that call carries the
    link. A filter on the root logger does not see a child logger's records, so the
    redaction runs when the record is created. Only those libraries are touched."""
    global _redact_installed
    if _redact_installed:
        return
    _redact_installed = True
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if not record.name.startswith(_REDACT_LOGGERS):
            return record
        try:
            rendered = record.getMessage()
        except Exception:
            return record
        if "://" not in rendered and "pwd=" not in rendered.lower():
            return record
        record.msg = _SECRET.sub("[redacted]", rendered)
        record.args = ()
        return record

    logging.setLogRecordFactory(factory)


class _Updates(MessageHandler):
    def __init__(self) -> None:
        self.updated = asyncio.Event()

    async def on_resource_updated(self, message: Any) -> None:
        self.updated.set()


def _listed(names: tuple[str, ...]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + " and " + names[-1]


def _segments(contents: Any) -> list[Segment]:
    raw = ""
    if isinstance(contents, str):
        raw = contents
    else:
        for block in contents or []:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                raw = text
                break
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    out: list[Segment] = []
    for item in data.get("segments") or []:
        if not isinstance(item, dict):
            continue
        start_v, end_v = item.get("start"), item.get("end")
        if isinstance(start_v, bool) or not isinstance(start_v, (int, float)):
            continue
        if isinstance(end_v, bool) or not isinstance(end_v, (int, float)):
            continue
        start, end = float(start_v), float(end_v)
        speaker = item.get("speaker")
        out.append(
            Segment(
                text=str(item.get("text") or ""),
                start=start,
                end=end,
                speaker=speaker if isinstance(speaker, str) and speaker.strip() else None,
                role=str(item.get("role") or "participant"),
            )
        )
    return out


class MeetingBridge:
    """The calls the broker has open. ``start`` and ``stop`` are called on the broker's loop."""

    def __init__(self, state: Any):
        self.state = state
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._stops: dict[int, asyncio.Event] = {}
        self._reasons: dict[int, str] = {}
        _install_redact()

    def recover(self) -> None:
        """A call that was open when the process died has no session left to leave.
        Mark it ended so the room is not stuck on 'live'."""
        for row in self.state.store.open_meetings():
            self.state.store.end_meeting(row.id, "the broker restarted during the call")
            self.state.service.notice_in(row.room_id, "The call ended because the broker restarted.")
            self.state.service.publish_settings(row.room_id)

    def start(self, room_id: int, url: str) -> None:
        """Join ``url``. The string is not stored; the task holds it until ``join_meeting`` returns."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._finish(room_id, "the meeting bridge is not running")
            return
        stop = asyncio.Event()
        self._stops[room_id] = stop
        self._tasks[room_id] = loop.create_task(self._run(room_id, url, stop))

    def stop(self, room_id: int, reason: str) -> None:
        """Ask the call's task to leave. With no task, the row ends now."""
        self._reasons[room_id] = reason
        ev = self._stops.get(room_id)
        if ev is None:
            self._finish(room_id, None)
            return
        ev.set()

    async def shutdown(self) -> None:
        """Broker stop: every open call leaves, then its client closes."""
        for room_id, ev in list(self._stops.items()):
            self._reasons.setdefault(room_id, "the broker stopped")
            ev.set()
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.wait(tasks, timeout=20)

    async def _run(self, room_id: int, url: str, stop: asyncio.Event) -> None:
        errors: list[str] = []
        try:
            await self._session(room_id, url, stop, errors)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("meeting room %s ended on an error", room_id)
            if not errors:
                errors.append("the call dropped")
        finally:
            self._tasks.pop(room_id, None)
            self._stops.pop(room_id, None)
            self._finish(room_id, errors[0] if errors else None)

    async def _session(self, room_id: int, url: str, stop: asyncio.Event, errors: list[str]) -> None:
        names = self.state.store.active_names(room_id)
        words = [w[:40] for w in hotwords(names)[:100]]
        header = json.dumps({"name": PARTICIPANT, "stt_args": {"hotwords": words}}, ensure_ascii=True)
        transport = StreamableHttpTransport(
            self.state.cfg.meeting.joinly_url, headers={"joinly-settings": header}
        )
        handler = _Updates()
        client = Client(transport, message_handler=handler, mode="legacy")
        async with client:
            try:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", DeprecationWarning)
                        warnings.simplefilter("ignore", MCPDeprecationWarning)
                        await client.session.subscribe_resource(RESOURCE)
                except Exception:
                    log.warning("meeting room %s: transcript subscribe failed", room_id)
                try:
                    await client.call_tool(
                        "join_meeting",
                        {"meeting_url": url, "participant_name": PARTICIPANT},
                        timeout=JOIN_TIMEOUT_S,
                    )
                except Exception:
                    errors.append("couldn't join the call")
                    return
                self._mark_live(room_id)
                await self._listen(client, handler, room_id, stop, errors)
            finally:
                await self._leave(client, room_id)

    async def _leave(self, client: Any, room_id: int) -> None:
        try:
            await asyncio.wait_for(
                client.call_tool("leave_meeting", {}, timeout=LEAVE_TIMEOUT_S), timeout=LEAVE_TIMEOUT_S + 3
            )
        except Exception:
            log.warning("meeting room %s: leave_meeting failed", room_id)

    def _mark_live(self, room_id: int) -> None:
        row = self.state.store.open_meeting(room_id)
        if row is None:
            return
        self.state.store.set_meeting_live(row.id)
        self.state.service.publish_settings(room_id)
        label = PLATFORM_LABEL.get(row.platform, "call")
        self.state.service.notice_in(room_id, f'Transcribing a {label} call as "{PARTICIPANT}".')

    async def _listen(
        self, client: Any, handler: _Updates, room_id: int, stop: asyncio.Event, errors: list[str]
    ) -> None:
        seen: set[tuple[Any, ...]] = set()
        asm = Assembler()
        queue: deque[Line] = deque()
        warned = False
        last = 0.0
        while not stop.is_set():
            handler.updated.clear()
            try:
                contents = await client.read_resource(RESOURCE)
            except Exception:
                errors.append("the call dropped")
                return
            now = time.monotonic()
            for seg in _segments(contents):
                key = (seg.start, seg.end, seg.text, seg.speaker, seg.role)
                if key in seen:
                    continue
                seen.add(key)
                if seg.role != "participant":
                    continue
                for line in asm.add(seg, now):
                    warned = self._enqueue(queue, line, room_id, warned)
            for line in asm.poll(now):
                warned = self._enqueue(queue, line, room_id, warned)
            if queue and (not last or now - last >= LINE_GAP_S):
                self._post_line(room_id, queue.popleft())
                last = time.monotonic()
            await _wait_for(handler, stop, POLL_S if not queue else min(POLL_S, LINE_GAP_S))
        for line in asm.flush():
            queue.append(line)
        while queue:
            self._post_line(room_id, queue.popleft())

    def _enqueue(self, queue: deque[Line], line: Line, room_id: int, warned: bool) -> bool:
        queue.append(line)
        if len(queue) <= QUEUE_MAX:
            return warned
        queue.popleft()
        if warned:
            return True
        self.state.service.notice_in(
            room_id, "The call is coming in faster than the room can show. Some lines were dropped."
        )
        return True

    def _post_line(self, room_id: int, line: Line) -> None:
        text = clean(line.text).strip()
        if not text:
            return
        members = self.state.store.active_names(room_id)
        found = match(text, members)
        mentions = [found.name] if found.outcome == "one" and found.name else []
        sender = clean(line.speaker).strip()[:40] if line.speaker else ""
        self.state.service.post_meeting_line(
            room_id, sender_name=sender or "call", text=text[:1000], mentions=mentions
        )
        if found.outcome == "ambiguous" and found.heard and found.names:
            heard = found.heard.replace('"', "'")[:40]
            who = _listed(found.names)
            self.state.service.notice_in(
                room_id,
                f'Someone on the call said "{heard}", and this room has {who}. Say the number.',
            )

    def _finish(self, room_id: int, error: str | None) -> None:
        try:
            row = self.state.store.open_meeting(room_id)
            if row is not None:
                self.state.store.end_meeting(row.id, error)
            self.state.service.publish_settings(room_id)
            note = self._notice(room_id, error)
            if note:
                self.state.service.notice_in(room_id, note)
        except Exception:
            log.warning("meeting room %s: could not record the end", room_id)

    def _notice(self, room_id: int, error: str | None) -> str | None:
        reason = self._reasons.pop(room_id, None)
        if reason == "leave":
            return None
        if error:
            return f"The call ended: {error}."
        if reason == "the room closed":
            return "The call ended because the room closed."
        if reason == "the broker stopped":
            return "The call ended because the broker stopped."
        return None


async def _wait_for(handler: _Updates, stop: asyncio.Event, timeout: float) -> None:
    if stop.is_set() or handler.updated.is_set():
        return
    stop_t = asyncio.ensure_future(stop.wait())
    upd_t = asyncio.ensure_future(handler.updated.wait())
    try:
        await asyncio.wait({stop_t, upd_t}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (stop_t, upd_t):
            task.cancel()
        await asyncio.gather(stop_t, upd_t, return_exceptions=True)
