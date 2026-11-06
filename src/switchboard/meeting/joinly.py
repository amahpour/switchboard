"""Driving joinly over MCP: what it is told, what it keeps, what it answers (DESIGN.md §52, §54).

Two programs drive a joinly. The broker's own bot (``bridge.py``) does, beside the broker, and the relay of a
call hosted on a person's machine (``relay.py``) does, beside a joinly of its own. What either sends and
reads is the same, so it is here once: the ``joinly-settings`` header (the bot's name, the speech-to-text
and text-to-speech arguments, the size of a snapshot), the transcript resource and how its segments are
read, how a tool's answer becomes text, the client that connects and subscribes, and the redaction that keeps
a meeting link out of the MCP libraries' debug logs. None of it decides anything about a room: the bridge
posts the lines itself, and the relay pushes them to the broker.

The meeting link is an argument of ``join_meeting`` and is never stored or logged here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import warnings
from collections.abc import Sequence
from typing import Any

from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.exceptions import MCPDeprecationWarning

from switchboard.meeting.lines import Segment

PARTICIPANT = "switchboard (transcribing)"
# joinly screenshots its 1280x720 page, trims a tenth off each edge and fits the rest to this size. Its
# own default, 512x288, is too small to read text on a shared screen; 1024x576 keeps the 16:9 frame near
# the trimmed page's own width, as a JPEG of a few hundred KB (checked against joinly v0.5.4, which takes
# it as a BrowserMeetingProvider argument through meeting_provider_args).
SNAPSHOT_SIZE = (1024, 576)
# joinly's Whisper cuts the call into pieces: after `min_silence` seconds of quiet, once a piece holds
# `min_audio` seconds. Its defaults (0.2 s and 0.4 s) cut "Hi Codex," off from the request after it, and
# a piece that short is misheard ("High Codex"). Longer ones (#620): on the recorded call of #617, with the
# pre-roll fix, every call-out woke the right agent (17 of 17, against 16), for about 0.14 s more delay.
# Both are arguments of joinly v0.5.4's WhisperSTT, so a joinly without our fixes takes them (a key it did
# not have would break every call).
STT_MIN_SILENCE_S = 0.35
STT_MIN_AUDIO_S = 1.0
HOTWORDS_MAX = 100  # the room's names sent as the transcriber's hotwords, each cut to 40 characters
RESOURCE = "transcript://live/segments"
LEAVE_TIMEOUT_S = 5.0
POLL_S = 3.0  # how long to go without reading the transcript when joinly says nothing changed
SPEAK_TIMEOUT_S = 180.0  # speak_text returns when the speech has ended; 600 characters is under a minute
INTERRUPTED = "Interrupted"  # how joinly's reply to speak_text starts when someone spoke over it
_SECRET = re.compile(r"https?://\S+|pwd=\S+", re.IGNORECASE)

_REDACT_LOGGERS = ("mcp", "fastmcp", "httpx", "httpcore", "sse_starlette", "switchboard.meeting")
_redact_installed = False


def install_redact() -> None:
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


def settings_header(
    *, hotwords: Sequence[str], model: str = "", voice: str, participant: str = PARTICIPANT
) -> str:
    """The ``joinly-settings`` header of a session: the bot's name, the speech-to-text arguments (the room's
    names as hotwords, and longer pieces of speech, ``STT_MIN_SILENCE_S``), the voice, and the size of a
    snapshot. ``model`` is only sent when the operator named one: a ``model_name`` that is given at all, even
    an empty one, makes joinly look for the model on the network, where leaving it out keeps its own files
    (§52). Each hotword is cut to 40 characters and at most ``HOTWORDS_MAX`` are sent."""
    stt_args: dict[str, Any] = {
        "hotwords": [w[:40] for w in list(hotwords)[:HOTWORDS_MAX]],
        "min_silence": STT_MIN_SILENCE_S,
        "min_audio": STT_MIN_AUDIO_S,
    }
    if model:
        stt_args["model_name"] = model
    return json.dumps(
        {
            "name": participant,
            "stt_args": stt_args,
            "tts_args": {"voice": voice},  # the Kokoro voice (joinly's settings, §52)
            # a frame whose text can be read
            "meeting_provider_args": {"snapshot_size": list(SNAPSHOT_SIZE)},
        },
        ensure_ascii=True,
    )


class Updates(MessageHandler):
    """Sets ``updated`` whenever joinly says the transcript changed."""

    def __init__(self) -> None:
        self.updated = asyncio.Event()

    async def on_resource_updated(self, message: Any) -> None:
        self.updated.set()


def open_client(url: str, header: str) -> tuple[Client, Updates]:
    """A client for the joinly at ``url`` that sends ``header`` as its settings, and the handler that learns
    of changes to the transcript. Enter it with ``async with`` and call ``subscribe``."""
    handler = Updates()
    client = Client(
        StreamableHttpTransport(url, headers={"joinly-settings": header}),
        message_handler=handler,
        mode="legacy",
    )
    return client, handler


async def subscribe(client: Any) -> bool:
    """Ask to be told when the transcript changes. False when joinly refuses: the transcript can still be read
    on a timer, which is slower."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", MCPDeprecationWarning)
            await client.session.subscribe_resource(RESOURCE)
    except Exception:
        return False
    return True


def segments(contents: Any) -> list[Segment]:
    """The segments in what ``read_resource`` gave back: the whole transcript so far, in whatever shape the
    client library returned it. An item without a numeric start and end is skipped, a speaker that is not
    text is ``None``."""
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


def reply(result: Any) -> str:
    """The text of a tool result, whatever shape the client library gave it."""
    data = getattr(result, "data", None)
    if isinstance(data, str):
        return data
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return ""


async def wait_for(handler: Updates, stop: asyncio.Event, timeout: float) -> None:
    """Return when the transcript changed, ``stop`` was set, or ``timeout`` seconds passed."""
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
