"""One live call: a joinly session, the lines it posts, and what is said back (DESIGN.md §52).

This is the only async part of a meeting. The broker's lifespan owns one bridge;
each open call is one task and one MCP session. The task always calls
``leave_meeting`` and then closes the client, including when the join fails,
the connection drops, the room closes, or the broker stops. The meeting link
lives only in the task, long enough to join, and is never logged.

Two things go back into the call through the same session, both started by a person. While
voice is on, an agent's ``@call`` line is spoken (``speak_text``), one line at a time off the
request that posted it. A snapshot (``get_video_snapshot``) is stored as a room file and posted.
Whether voice is on is memory only: a call does not outlive the process, so neither does it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from switchboard.meeting import ingest, joinly, voices
from switchboard.meeting.ingest import LINE_GAP_S
from switchboard.meeting.joinly import (
    INTERRUPTED,
    LEAVE_TIMEOUT_S,
    PARTICIPANT,
    POLL_S,
    RESOURCE,
    SPEAK_TIMEOUT_S,
)
from switchboard.meeting.lines import Assembler, Line
from switchboard.meeting.links import PLATFORM_LABEL
from switchboard.meeting.names import hotwords
from switchboard.meeting.screen import SNAPSHOT_GAP_S, Asker, NoFrame, frame_from, says_screen
from switchboard.meeting.speech import call_text, speakable
from switchboard.meeting.turns import STALE_NOTICE, STALE_S, Turns
from switchboard.meeting.voices import voice_of

log = logging.getLogger("switchboard.meeting")

JOIN_TIMEOUT_S = 45.0
SNAPSHOT_TIMEOUT_S = 8.0  # a call-out's line waits no longer than this for its picture


@dataclass
class _Live:
    """What a joined call holds that a command or a say reaches: its client, the agents' lines
    waiting to be spoken, and when a snapshot last began (the broker's clock)."""

    client: Any
    turns: Turns = field(default_factory=Turns)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task[None] | None = None
    last_snapshot: float | None = None
    voices: bool = False  # this joinly's speak_text takes a voice (the patch, §52.5)
    missing: set[str] = field(default_factory=set)  # voices Kokoro lacked, already told to the room


class MeetingBridge:
    """The calls the broker has open. ``start`` and ``stop`` are called on the broker's loop."""

    def __init__(self, state: Any):
        self.state = state
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._stops: dict[int, asyncio.Event] = {}
        self._reasons: dict[int, str] = {}
        self._voice: set[int] = set()  # rooms whose call is speaking agents' @call lines
        self._live: dict[int, _Live] = {}  # calls the bot has joined
        self._side: set[asyncio.Task[None]] = set()  # snapshots under way, kept from being collected
        joinly.install_redact()

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
        side = list(self._side)  # a snapshot still on its way is not worth waiting for
        for task in side:
            task.cancel()
        if side:
            await asyncio.gather(*side, return_exceptions=True)

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
        cfg = self.state.cfg.meeting
        header = joinly.settings_header(
            hotwords=hotwords(names), model=cfg.stt_model, voice=cfg.tts_voice, participant=PARTICIPANT
        )
        client, handler = joinly.open_client(cfg.joinly_url, header)
        async with client:
            try:
                if not await joinly.subscribe(client):
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
                # asked before the call is marked live: the bar must never say live while a command that needs
                # ``_live`` (a snapshot, a voice) still finds no call to act on
                takes_voice = await joinly.takes_voice(client)
                self._mark_live(room_id)
                live = self._live[room_id] = _Live(client, voices=takes_voice)
                try:
                    await self._listen(live, handler, room_id, stop, errors)
                finally:
                    self._live.pop(room_id, None)
                    await self._stop_speech(live)
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
        self, live: _Live, handler: joinly.Updates, room_id: int, stop: asyncio.Event, errors: list[str]
    ) -> None:
        client = live.client
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
            for seg in joinly.segments(contents):
                key = (seg.start, seg.end, seg.text, seg.speaker, seg.role)
                if key in seen:
                    continue
                seen.add(key)
                if seg.role != "participant":
                    continue
                live.turns.hear(self.state.clock.now())
                live.changed.set()
                for line in asm.add(seg, now):
                    warned = self._enqueue(queue, line, room_id, warned)
            for line in asm.poll(now):
                warned = self._enqueue(queue, line, room_id, warned)
            if queue and (not last or now - last >= LINE_GAP_S):
                await self._post_line(room_id, queue.popleft(), live)
                last = time.monotonic()
            await joinly.wait_for(handler, stop, POLL_S if not queue else min(POLL_S, LINE_GAP_S))
        for line in asm.flush():
            queue.append(line)
        while queue:
            await self._post_line(room_id, queue.popleft(), live, ending=True)

    def _enqueue(self, queue: deque[Line], line: Line, room_id: int, warned: bool) -> bool:
        return ingest.enqueue(
            queue, line, warned, lambda: self.state.service.notice_in(room_id, ingest.OVERFLOW_NOTICE)
        )

    async def _post_line(self, room_id: int, line: Line, live: _Live, *, ending: bool = False) -> None:
        row = self.state.store.open_meeting(room_id)
        if row is None:
            return
        incoming = ingest.receive(self.state, row, line)
        if incoming is None:
            return
        if incoming.prepared.mentions and says_screen(incoming.prepared.text) and not ending:
            # The picture comes first so the agent this line wakes gets both. A failed picture
            # never holds the line back; the checked line and its call stay together while it waits.
            await self._screen_for_line(room_id, live)
        incoming.post(self.state)

    # ------------------------------------------------------------------ voice (§52)
    def voice(self, room_id: int) -> bool:
        """Whether this room's call speaks agents' ``@call`` lines. Off until a person turns it on."""
        return room_id in self._voice

    def set_voice(self, room_id: int, on: bool) -> bool:
        """Turn voice on or off for this room's call. True when that changed it. Turning it off
        drops the lines still waiting; the one being spoken finishes."""
        if on == (room_id in self._voice):
            return False
        if on:
            self._voice.add(room_id)
        else:
            self._voice.discard(room_id)
            live = self._live.get(room_id)
            if live is not None:
                live.turns.clear_pending()
                live.changed.set()
        return True

    def person_posted(self, room_id: int) -> None:
        """A person wrote in the room: the run of agent lines on the call ends (§52.2)."""
        live = self._live.get(room_id)
        if live is not None:
            live.turns.person_posted()

    def speak(
        self, room_id: int, text: str, *, membership_id: int | None = None, answering: bool = False
    ) -> None:
        """Offer one line to the call without delaying its room post (§52.2). ``answering``: the broker
        knows this agent's last batch put a question to it; the line is never parsed for that."""
        if room_id not in self._voice:
            return
        live = self._live.get(room_id)
        body = call_text(text)
        if live is None or body is None:
            return
        line = speakable(body)
        if not line:
            return
        voice = voice_of(self.state.store, membership_id) if live.voices else None
        if not live.turns.offer(
            line, self.state.clock.now(), membership_id, answering=answering, voice=voice
        ):
            self.state.service.call_voice_notice(room_id, live.turns.refusal_notice, membership_id)
            return
        live.changed.set()
        if live.worker is None or live.worker.done():
            try:
                live.worker = asyncio.get_running_loop().create_task(self._speak(room_id, live))
            except RuntimeError:
                live.turns.clear_pending()

    async def _speak(self, room_id: int, live: _Live) -> None:
        """Wait for a speech gap and the interval; reserve the slot until speak_text finishes."""
        while live.turns.pending is not None:
            now = self.state.clock.now()
            stale = live.turns.expire(now)
            if stale is not None:
                self.state.service.call_voice_notice(room_id, STALE_NOTICE, stale.membership_id)
                return
            line = live.turns.take(now)
            if line is None:
                live.changed.clear()
                pending = live.turns.pending
                at = live.turns.ready_at()
                if pending is None or at is None:
                    return  # cleared meanwhile (voice off, call over): nothing left to wait for
                delay = min(at, pending.queued_at + STALE_S) - now
                try:
                    await asyncio.wait_for(live.changed.wait(), max(0.001, delay))
                except TimeoutError:
                    pass
                continue
            try:
                args: dict[str, str] = {"text": line.text}
                if line.voice is not None:
                    args["voice"] = line.voice  # the patched joinly reads it (deploy/joinly, §52.5)
                result = await live.client.call_tool("speak_text", args, timeout=SPEAK_TIMEOUT_S)
                said = joinly.reply(result)
                if line.voice is not None and voices.NOT_FOUND in said and line.voice not in live.missing:
                    live.missing.add(line.voice)  # once a call for each voice: the room is told, not nagged
                    self.state.service.notice_in(
                        room_id, voices.missing_notice(self.state.store, line.membership_id, line.voice)
                    )
                if said.startswith(INTERRUPTED):
                    self.state.service.notice_in(
                        room_id, "Someone spoke over the line in the call, so it stopped early."
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("meeting room %s: speak_text failed", room_id)
                self.state.service.notice_in(room_id, "Couldn't speak in the call.")
            finally:
                live.turns.finish(line.id)

    async def _stop_speech(self, live: _Live) -> None:
        live.turns.clear_pending()
        worker = live.worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    # --------------------------------------------------------------- snapshot (§52)
    def request_snapshot(self, room_id: int, asker: Asker) -> str | None:
        """A person asks for a picture of the call. None when it is under way: it is posted in the room
        as that person's message when it arrives. Otherwise why not, as a sentence for them."""
        live = self._live.get(room_id)
        if live is None:
            return "The bot is not in the call yet."
        why = self._gate(live)
        if why is not None:
            return why
        try:
            task = asyncio.get_running_loop().create_task(self._snapshot(room_id, live, asker))
        except RuntimeError:
            return "The meeting bridge is not running."
        self._side.add(task)
        task.add_done_callback(self._side.discard)
        return None

    def _gate(self, live: _Live) -> str | None:
        """At most one snapshot every ``SNAPSHOT_GAP_S`` in a call, from any of the three ways to ask."""
        now = self.state.clock.now()
        if live.last_snapshot is not None and now - live.last_snapshot < SNAPSHOT_GAP_S:
            return f"A snapshot was taken less than {SNAPSHOT_GAP_S:.0f} seconds ago. Try again in a moment."
        live.last_snapshot = now
        return None

    async def _screen_for_line(self, room_id: int, live: _Live) -> None:
        why = self._gate(live)
        if why is not None:
            self.state.service.notice_in(room_id, why)
            return
        await self._snapshot(room_id, live, None)

    async def _snapshot(self, room_id: int, live: _Live, asker: Asker | None) -> None:
        """Ask joinly for the picture, keep it as a room file and post it. ``asker`` is the person
        who asked, or None when a line on the call did: then the file and its message are from the
        call. Any failure is one sentence in the room."""
        att = self.state.attachments
        try:
            if att is None:
                raise NoFrame("This broker keeps no files.")
            result = await asyncio.wait_for(
                live.client.call_tool("get_video_snapshot", {}, timeout=SNAPSHOT_TIMEOUT_S),
                SNAPSHOT_TIMEOUT_S + 2,
            )
            data, kind = frame_from(result, att.max_bytes)
            room = self.state.store.room_by_id(room_id)
            if room is None:
                return
            clock = time.strftime("%H%M%S", time.localtime(self.state.clock.now()))
            name = f"screen-{clock}.{kind}"
            try:
                row = att.store_snapshot(
                    room_id,
                    asker.name if asker else "call",
                    "human" if asker else "meeting",
                    name,
                    data,
                )
            except ValueError as e:
                raise NoFrame(f"The snapshot was not kept: {e}.") from None
            artifact = f"artifact: {row.name} | {row.size} | sha256:{row.sha256} | switchboard:file/{row.id}"
            if asker is not None:
                self.state.service.human_say(
                    room.name, artifact, asker.via, person=(asker.name, asker.person_id)
                )
            else:
                self.state.service.post_meeting_line(room_id, sender_name="call", text=artifact, mentions=[])
        except NoFrame as e:
            self.state.service.notice_in(room_id, str(e))
        except Exception:
            log.warning("meeting room %s: snapshot failed", room_id)
            self.state.service.notice_in(room_id, "Couldn't get a snapshot of the call.")

    def _finish(self, room_id: int, error: str | None) -> None:
        # a call does not outlive the process, and its voice does not outlive the call
        self._voice.discard(room_id)
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
