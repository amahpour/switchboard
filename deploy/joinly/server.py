# joinly v0.5.4, server.py, changed by switchboard (#713, #763, #764, #820): see deploy/joinly/NOTICE for what.
# Copyright (c) 2025 Dominik Brockmann, Johannes Dittrich, Fynn Aurand. MIT License:
#
# Copyright (c) 2025 Dominik Brockmann, Johannes Dittrich, Fynn Aurand
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import asyncio
import base64
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, get_args

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from joinly.container import SessionContainer
from joinly.services.tts.kokoro import LINE_VOICE, VOICE_FELL_BACK
from joinly.session import MeetingSession
from joinly.settings import Settings, get_settings, reset_settings, set_settings
from joinly.types import (
    MeetingChatHistory,
    MeetingParticipantList,
    SpeakerRole,
    SpeechInterruptedError,
    Transcript,
    UIUpdate,
    Usage,
)
from joinly.utils.usage import get_usage, reset_usage, set_usage
from mcp import types as mcp_types
from mcp.types import ImageContent
from pydantic import AnyUrl, BaseModel, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

TRANSCRIPT_URL = AnyUrl("transcript://live")
SEGMENTS_URL = AnyUrl("transcript://live/segments")


class _UIUpdateNotification(BaseModel):
    method: Literal["notifications/joinly_ui_update"] = "notifications/joinly_ui_update"
    params: UIUpdate | None = None


def _patch_client_notifications(*types_: type) -> None:
    field = mcp_types.ClientNotification.model_fields["root"]
    current = get_args(field.annotation)
    field.annotation = Union[(*current, *types_)]  # type: ignore[assignment]
    mcp_types.ClientNotification.model_rebuild(force=True)


_patch_client_notifications(_UIUpdateNotification)


def _patch_experimental(server: FastMCP, extra: dict[str, dict[str, Any]]) -> None:
    """Advertise experimental capabilities on a FastMCP server."""
    _orig = server._mcp_server.get_capabilities  # noqa: SLF001

    def _get_capabilities(opts: Any, exp: dict | None = None) -> Any:  # noqa: ANN401
        return _orig(opts, {**(exp or {}), **extra})

    server._mcp_server.get_capabilities = _get_capabilities  # type: ignore[assignment]  # noqa: SLF001


# --- switchboard bot logo (#820, DESIGN.md section 52.10) -----------------------------------------------------
# joinly draws a logo on a virtual camera (joinly/providers/browser/camera_feed.py) but never turns the camera
# on, so the bot's tile in a call is grey. This swaps in switchboard's mark (favicon.svg of the switchboard
# repository, kept equal by tests/unit/test_joinly_voice_patch.py) and, after each platform's join, clicks that
# platform's camera button once. A failure there is logged once and never fails the join.
# SWITCHBOARD_BOT_VIDEO=0 (or false, off, no) leaves joinly as it was: a grey tile.
BOT_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7.5" '
    'fill="#0a64d8"/><g transform="translate(4 4) scale(1.5)" fill="none" stroke="#fff" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"><circle cx="4" cy="4" r="1.8"/><circle cx="12" cy="4" r="1.8"/>'
    '<circle cx="12" cy="12" r="1.8"/><path d="M4 5.8V8a4 4 0 0 0 4 4h2.2M12 5.8v4.4"/></g></svg>'
)
BOT_VIDEO_OFF = frozenset({"0", "false", "off", "no"})
# a platform's camera button while the camera is off (accessible names, case-insensitive)
BOT_CAMERA_ON = re.compile(r"^(start (my )?video|turn on (my )?camera|turn camera on)", re.IGNORECASE)
BOT_PLATFORMS = (
    ("joinly.providers.browser.platforms.zoom", "ZoomBrowserPlatformController"),
    ("joinly.providers.browser.platforms.google_meet", "GoogleMeetBrowserPlatformController"),
    ("joinly.providers.browser.platforms.teams", "TeamsBrowserPlatformController"),
)
_bot_camera_warned = False


def _bot_video_enabled(env: dict[str, str] | None = None) -> bool:
    """On unless SWITCHBOARD_BOT_VIDEO says off: the setting only turns it off, so an unknown value is on."""
    import os

    return (env if env is not None else os.environ).get("SWITCHBOARD_BOT_VIDEO", "1").strip().lower() not in (
        BOT_VIDEO_OFF
    )


# Zoom hides its control bar a moment after a join, and a hidden button is on the page but not visible, so
# get_by_role(...).is_visible() was False in the live check and the camera stayed off. One DOM click finds the first
# button whose aria-label (or text) matches and clicks it, visible or not; the pattern is the Python one's source.
_BOT_CAMERA_JS = """(src) => {
    const rx = new RegExp(src, 'i');
    for (const b of document.querySelectorAll('button')) {
        const t = (b.getAttribute('aria-label') || b.innerText || '').trim();
        if (rx.test(t)) { b.click(); return true; }
    }
    return false;
}"""
_bot_camera_logged = False


async def _bot_camera_on(page: Any) -> bool:  # noqa: ANN401
    """Click the page's camera-on button once if there is one; True when it was clicked. Never raises: an exception
    is logged once per process as a warning (a call that fails here fails the same way every time), and the outcome
    of the first attempt, clicked or not found, is logged once at info."""
    global _bot_camera_warned, _bot_camera_logged  # noqa: PLW0603
    try:
        clicked = bool(await page.evaluate(_BOT_CAMERA_JS, BOT_CAMERA_ON.pattern))
    except Exception:
        if not _bot_camera_warned:
            _bot_camera_warned = True
            logger.warning("bot logo: could not turn the camera on", exc_info=True)
        return False
    if not _bot_camera_logged:
        _bot_camera_logged = True
        logger.info("bot logo: camera button %s", "clicked" if clicked else "not found")
    return clicked


def _bot_after_join(original: Any) -> Any:  # noqa: ANN401
    async def join(self: Any, page: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        result = await original(self, page, *args, **kwargs)
        await _bot_camera_on(page)
        return result

    join.__wrapped__ = original  # type: ignore[attr-defined]
    return join


def _install_bot_logo(import_module: Any = None, env: dict[str, str] | None = None) -> bool:  # noqa: ANN401
    """Swap the logo and wrap each platform's join, once. False, touching nothing, when it is off. Never raises:
    a joinly without these modules is a grey tile."""
    if not _bot_video_enabled(env):
        return False
    import importlib
    from urllib.parse import quote

    load = import_module or importlib.import_module
    try:
        load("joinly.providers.browser.camera_feed")._LOGO_SVG = "data:image/svg+xml," + quote(
            BOT_LOGO_SVG, safe="/:=,;"
        )
    except Exception:
        logger.warning("bot logo: could not set the logo", exc_info=True)
        return False
    for module, name in BOT_PLATFORMS:
        try:
            cls = getattr(load(module), name)
        except (ImportError, AttributeError):
            logger.warning("bot logo: %s.%s not found", module, name)
            continue
        if not hasattr(cls.join, "__wrapped__"):
            cls.join = _bot_after_join(cls.join)
    return True


_install_bot_logo()
# --- end of the bot logo -------------------------------------------------------------------------------------

CAPTIONS = "captions"  # switchboard patch (#764): the token that selects joinly/captions.py's three classes


def _captions_settings(settings: Settings) -> Settings:
    """switchboard patch (#764): `captions` for `stt`, `vad` and `transcription_controller` means the classes of
    joinly/captions.py, which load and run no speech model. Anything else is left as it was."""
    if CAPTIONS not in (settings.stt, settings.vad, settings.transcription_controller):
        return settings
    from joinly.captions import CaptionsSTT, CaptionsTranscriptionController, CaptionsVAD

    return settings.model_copy(
        update={
            "stt": CaptionsSTT if settings.stt == CAPTIONS else settings.stt,
            "vad": CaptionsVAD if settings.vad == CAPTIONS else settings.vad,
            "transcription_controller": (
                CaptionsTranscriptionController
                if settings.transcription_controller == CAPTIONS
                else settings.transcription_controller
            ),
        }
    )


@dataclass
class SessionContext:
    """Context for the meeting session."""

    meeting_session: MeetingSession


def _extract_settings() -> Settings:
    """Extract settings from the HTTP headers."""
    current = get_settings()
    try:
        from fastmcp.server.http import _current_http_request

        request = _current_http_request.get()
        header = request.headers.get("joinly-settings") if request is not None else None
    except RuntimeError:
        logger.exception("Failed to get HTTP headers")
        header = None

    if not header:
        return current

    try:
        base = current.model_copy(deep=True).model_dump()
        patch = Settings.model_validate(json.loads(header)).model_dump(exclude_unset=True)
        for k, v in patch.items():
            base[k] = (base.get(k, {}) | v) if isinstance(v, dict) else v
        settings = Settings.model_validate(base)
    except (json.JSONDecodeError, ValidationError):
        msg = "Invalid joinly-settings."
        logger.exception(msg)
        logger.warning("Continuing with current settings")
        return current

    return _captions_settings(settings)


@asynccontextmanager
async def session_lifespan(server: FastMCP) -> AsyncIterator[SessionContext]:
    """Create and enter a MeetingSession once per client connection."""
    logger.info("Creating meeting session")
    settings = _extract_settings()
    settings_token = set_settings(settings)
    usage = Usage()
    usage_token = set_usage(usage)
    session_container = SessionContainer()
    meeting_session = await session_container.__aenter__()
    # switchboard patch (#764): the captions controller reaches the call's page through the provider
    _attach = getattr(meeting_session._transcription_controller, "attach", None)  # noqa: SLF001
    if _attach is not None:
        _attach(meeting_session._meeting_provider, settings.name)  # noqa: SLF001

    _remover: dict[AnyUrl, Callable[[], None]] = {}

    @server._mcp_server.subscribe_resource()  # noqa: SLF001
    async def _handle_subscribe_resource(url: AnyUrl) -> None:
        if url not in (TRANSCRIPT_URL, SEGMENTS_URL) or url in _remover:
            return
        logger.debug("Subscribing to resource: %s", url)
        session = server._mcp_server.request_context.session  # noqa: SLF001

        _event = "utterance" if url == TRANSCRIPT_URL else "segment"

        async def _push() -> None:
            logger.debug("Sending %s notification", _event)
            await session.send_resource_updated(url)

        _remover[url] = meeting_session.subscribe(_event, _push)

    @server._mcp_server.unsubscribe_resource()  # noqa: SLF001
    async def _handle_unsubscribe_resource(url: AnyUrl) -> None:
        if url in _remover:
            logger.debug("Unsubscribing from resource: %s", url)
            _remover[url]()
            _remover.pop(url)

    async def _handle_ui_update(notification: _UIUpdateNotification) -> None:
        if not notification.params:
            return
        logger.debug("UI update: %s", notification.params.content)
        await meeting_session.update_ui(notification.params)

    server._mcp_server.notification_handlers[_UIUpdateNotification] = _handle_ui_update  # noqa: SLF001

    try:
        yield SessionContext(meeting_session=meeting_session)
    finally:
        for _rem in _remover.values():
            _rem()

        # ensure proper cleanup
        from anyio import CancelScope

        with CancelScope(shield=True):
            await session_container.__aexit__()

        reset_settings(settings_token)
        reset_usage(usage_token)


mcp = FastMCP("joinly", lifespan=session_lifespan)
_patch_experimental(mcp, {"joinly_ui_update": {}})


@mcp.resource(
    str(TRANSCRIPT_URL),
    description="Live transcript of the meeting participant utterances.",
    mime_type="application/json",
)
async def get_transcript(ctx: Context) -> Transcript:
    """Get the live transcript of the meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    return ms.transcript.with_role(SpeakerRole.participant)


@mcp.resource(
    str(SEGMENTS_URL),
    description="Live transcript segments.",
    mime_type="application/json",
)
async def get_transcript_segments(ctx: Context) -> Transcript:
    """Get the live transcript segments of the meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    return ms.transcript


@mcp.resource(
    "usage://current",
    description="Current usage statistics of services",
    mime_type="application/json",
)
async def get_usage_report(_ctx: Context) -> Usage:
    """Get the current usage statistics."""
    return get_usage()


@mcp.tool(
    "join_meeting",
    description="Join a meeting with the given URL and participant name.",
)
async def join_meeting(
    ctx: Context,
    meeting_url: Annotated[str | None, Field(default=None, description="URL to join an online meeting")],
    participant_name: Annotated[
        str | None,
        Field(default=None, description="Name of the participant to join as"),
    ],
    passcode: Annotated[
        str | None,
        Field(
            default=None,
            description="Password or passcode for the meeting (if required)",
        ),
    ] = None,
) -> str:
    """Join a meeting with the given URL and participant name."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.join_meeting(meeting_url, participant_name, passcode)
    return "Joined meeting."


@mcp.tool(
    "leave_meeting",
    description="Leave the current meeting.",
)
async def leave_meeting(
    ctx: Context,
) -> str:
    """Leave the current meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.leave_meeting()
    return "Left the meeting."


@mcp.tool(
    "speak_text",
    description="Speak the given text in the meeting.",
)
async def speak_text(
    ctx: Context,
    text: Annotated[str, Field(description="Text to be spoken")],
    voice: Annotated[
        str | None,
        Field(
            description=("Voice for this text only (Kokoro voice name). Defaults to the session's voice."),
            pattern=r"^[a-z0-9_]{1,32}$",
        ),
    ] = None,
) -> str:
    """Speak the given text in the meeting using TTS."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    # switchboard patch (#713): the tasks that make the speech copy this context
    voice_token = LINE_VOICE.set(voice)
    fell_back: list[str] = []
    fell_token = VOICE_FELL_BACK.set(fell_back)
    try:
        await ms.speak_text(text)
    except SpeechInterruptedError as e:
        return str(e)
    finally:
        LINE_VOICE.reset(voice_token)
        VOICE_FELL_BACK.reset(fell_token)
    if fell_back:
        return "Finished speaking. The voice was not found, so the session's voice was used."
    return "Finished speaking."


@mcp.tool(
    "send_chat_message",
    description="Send a chat message in the meeting chat.",
)
async def send_chat_message(
    ctx: Context,
    message: Annotated[str, Field(description="Message to be sent")],
) -> str:
    """Send a chat message in the meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.send_chat_message(message)
    return "Sent message."


@mcp.tool(
    "get_chat_history",
    description="Get the chat history from the chat inside the meeting.",
)
async def get_chat_history(
    ctx: Context,
) -> MeetingChatHistory:
    """Get the chat history from the meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    return await ms.get_chat_history()


@mcp.tool(
    "get_transcript",
    description=(
        "Get the transcript of the meeting. By default, returns the full transcript. "
        "To get a slice, set mode to 'first' or 'latest' and provide a positive "
        "minutes value."
    ),
)
async def get_transcript_tool(
    ctx: Context,
    mode: Annotated[
        Literal["full", "first", "latest"],
        Field(
            default="full",
            description="Mode to get the transcript: 'full' for the entire transcript, "
            "'first' for the first N minutes, 'latest' for the last N minutes.",
        ),
    ] = "full",
    minutes: Annotated[
        int,
        Field(
            default=0,
            description="Number of minutes to slice the transcript. "
            "Only used if mode is 'first' or 'latest'.",
        ),
    ] = 0,
) -> Transcript:
    """Get the transcript of the meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    if mode == "first":
        return ms.transcript.before(minutes * 60).compact()
    if mode == "latest":
        return ms.transcript.after(ms.meeting_seconds - minutes * 60).compact()
    return ms.transcript.compact()


@mcp.tool(
    "get_participants",
    description="Get the list of participants in the meeting.",
)
async def get_participants(
    ctx: Context,
) -> MeetingParticipantList:
    """Get the list of participants in the meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    return MeetingParticipantList(await ms.get_participants())


@mcp.tool(
    "get_video_snapshot",
    description=(
        "Get a snapshot of the current video feed, including participant webcams and "
        "screenshares inside the meeting."
    ),
)
async def get_video_snapshot(ctx: Context) -> ImageContent:
    """Get a snapshot of the current video feed."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    snapshot = await ms.get_video_snapshot()
    return ImageContent(
        type="image",
        data=base64.b64encode(snapshot.data).decode(),
        mimeType=snapshot.media_type,
    )


@mcp.tool(
    "share_screen",
    description=(
        "Start sharing your screen in the meeting. Provide a URL to display "
        "while sharing. Participants will see the content at the given URL."
    ),
)
async def share_screen(
    ctx: Context,
    url: Annotated[
        str,
        Field(description="URL to open and display while sharing screen"),
    ],
) -> str:
    """Start sharing screen in the meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.share_screen(url)
    return "Started sharing screen."


@mcp.tool(
    "stop_sharing",
    description="Stop sharing your screen in the meeting.",
)
async def stop_sharing(
    ctx: Context,
) -> str:
    """Stop sharing screen in the meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.stop_sharing()
    return "Stopped sharing screen."


# --- switchboard patch (#763): share one picture in the call ----------------------------------------------
# The picture arrives as bytes (base64 of a PNG or JPEG), never as a URL, and is drawn on a canvas in the
# meeting page. Only that canvas is handed to the platform's own Share button, through an override of
# getDisplayMedia that lives for the length of the share: the browser's own screen picker, and so the bot's
# virtual display, is never reached. Zoom only (the one platform it was tried on); anything else is refused.
SHARE_MAX_BYTES = 8 * 1024 * 1024
SHARE_MAX_PIXELS = 50_000_000  # a PNG or JPEG that claims more is refused before the browser decodes it
SHARE_WAIT_S = 8.0
_SHARE_REFUSED = re.compile(
    r"host disabled attendee screen sharing|disabled share|screen sharing is disabled", re.IGNORECASE
)
_JPEG_FRAMES = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})

_SHARE_JS = """
async (dataUrl) => {
  const stale = window.__sbShare;
  if (stale && stale.stop) stale.stop();
  const img = new Image();
  await new Promise((ok, bad) => {
    img.onload = ok;
    img.onerror = () => bad(new Error('picture'));
    img.src = dataUrl;
  });
  const c = document.createElement('canvas');
  c.width = 1280; c.height = 720;
  const x = c.getContext('2d');
  const k = Math.min(1280 / img.naturalWidth, 720 / img.naturalHeight);
  const w = Math.round(img.naturalWidth * k), h = Math.round(img.naturalHeight * k);
  const draw = () => {
    x.fillStyle = '#111'; x.fillRect(0, 0, 1280, 720);
    x.drawImage(img, Math.round((1280 - w) / 2), Math.round((720 - h) / 2), w, h);
  };
  draw();
  const timer = setInterval(draw, 200);
  const md = navigator.mediaDevices;
  const original = md.getDisplayMedia;
  const state = { called: false, stream: null };
  state.stop = () => {
    clearInterval(timer);
    if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
    md.getDisplayMedia = original;
    window.__sbShare = null;
  };
  md.getDisplayMedia = async () => {
    state.called = true;
    state.stream = c.captureStream(10);
    return state.stream;
  };
  window.__sbShare = state;
  const buttons = [...document.querySelectorAll('button')];
  buttons.forEach((b) => { if ((b.innerText || '').trim() === 'Got it') b.click(); });
  const share = buttons.find((b) => /^share$/i.test(b.getAttribute('aria-label') || ''));
  if (!share) { state.stop(); return 'no-button'; }
  share.click();
  return 'clicked';
}
"""

_SHARE_STATUS_JS = """
() => {
  const s = window.__sbShare;
  const text = (document.body && document.body.innerText) || '';
  return { called: !!(s && s.called), text: text.slice(0, 20000) };
}
"""

_SHARE_STOP_JS = (
    "() => { const s = window.__sbShare; if (s && s.stop) { s.stop(); return true; } return false; }"
)


def _share_picture(data: bytes) -> str:
    """The data URL of a PNG or JPEG. The type is decided by the bytes alone.

    Raises ValueError with a short reason for anything else, for a body past SHARE_MAX_BYTES, and for a
    picture whose header cannot be read or claims more than SHARE_MAX_PIXELS."""
    if len(data) > SHARE_MAX_BYTES:
        msg = "The picture is too large to share."
        raise ValueError(msg)
    width = height = 0
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR" and len(data) >= 24:
        kind = "image/png"
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
    elif data.startswith(b"\xff\xd8\xff"):
        kind = "image/jpeg"
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker in _JPEG_FRAMES:
                height = int.from_bytes(data[i + 5 : i + 7], "big")
                width = int.from_bytes(data[i + 7 : i + 9], "big")
                break
            i += 2 + max(int.from_bytes(data[i + 2 : i + 4], "big"), 2)
    else:
        msg = "Only a PNG or JPEG picture can be shared."
        raise ValueError(msg)
    if width < 1 or height < 1 or width * height > SHARE_MAX_PIXELS:
        msg = "The picture's size could not be read, or is too large to share."
        raise ValueError(msg)
    return f"data:{kind};base64,{base64.b64encode(data).decode()}"


def _share_page(ms: MeetingSession) -> Any:  # noqa: ANN401
    """The meeting's own page, and only when it is a Zoom call."""
    page = getattr(getattr(ms, "_meeting_provider", None), "_page", None)
    if page is None or "zoom" not in str(getattr(page, "url", "")).lower():
        msg = "Sharing a picture is only supported in a Zoom call."
        raise ToolError(msg)
    return page


@mcp.tool(
    "share_image",
    description=(
        "Share one picture in the meeting as a screen share. The picture is given as base64 of a PNG or "
        "JPEG; everyone sees it until stop_share_image. Zoom only, and the host must allow participants "
        "to share."
    ),
)
async def share_image(
    ctx: Context,
    image: Annotated[str, Field(description="Base64 of a PNG or JPEG picture (at most 8 MB)")],
) -> str:
    """Share a picture from bytes, never from a URL."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    if len(image) > SHARE_MAX_BYTES * 4 // 3 + 8:
        msg = "The picture is too large to share."
        raise ToolError(msg)
    try:
        url = _share_picture(base64.b64decode(image, validate=True))
    except ValueError as e:
        raise ToolError(str(e)) from None
    page = _share_page(ms)
    if getattr(ms, "_sb_sharing", False):
        msg = "Already sharing. Stop the current share first."
        raise ToolError(msg)
    await page.bring_to_front()
    await page.mouse.move(640, 690)  # the toolbar hides until the pointer is over the page
    cdp = await page.context.new_cdp_session(page)
    try:
        # the platform's Share button needs a user gesture: Runtime.evaluate can say it is one
        reply = await cdp.send(
            "Runtime.evaluate",
            {
                "expression": f"({_SHARE_JS})({json.dumps(url)})",
                "awaitPromise": True,
                "userGesture": True,
                "returnByValue": True,
            },
        )
    finally:
        await cdp.detach()
    if reply.get("exceptionDetails") is not None or reply.get("result", {}).get("value") != "clicked":
        msg = "The Share button was not found in the call."
        raise ToolError(msg)
    for _ in range(int(SHARE_WAIT_S / 0.5)):
        await asyncio.sleep(0.5)
        status = await page.evaluate(_SHARE_STATUS_JS)
        if status["called"]:
            ms._sb_sharing = True  # noqa: SLF001
            return "Sharing the picture."
        if _SHARE_REFUSED.search(status["text"]):
            await page.evaluate(_SHARE_STOP_JS)
            for name in ("OK", "Got it", "Close"):
                button = page.get_by_role("button", name=name, exact=True)
                if await button.count():
                    await button.first.click(timeout=1000)
                    break
            msg = "Host disabled attendee screen sharing."
            raise ToolError(msg)
    await page.evaluate(_SHARE_STOP_JS)
    msg = "Zoom did not start the share."
    raise ToolError(msg)


@mcp.tool(
    "stop_share_image",
    description="Stop the picture that share_image is showing in the meeting.",
)
async def stop_share_image(ctx: Context) -> str:
    """Stop a picture share (stop_sharing is joinly's own, for a shared page)."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    page = _share_page(ms)
    await page.evaluate(_SHARE_STOP_JS)  # ends the canvas stream, and Zoom ends the share with it
    ms._sb_sharing = False  # noqa: SLF001
    stop = page.get_by_role("button", name=re.compile(r"stop share", re.IGNORECASE))
    try:
        if await stop.count():
            await stop.first.click(timeout=2000)
    except Exception:
        logger.warning("The Stop Share button could not be clicked")
    return "Stopped sharing the picture."


@mcp.tool(
    "mute_yourself",
    description="Mute yourself in the meeting.",
)
async def mute_yourself(
    ctx: Context,
) -> str:
    """Mute yourself in the meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.mute()
    return "Muted yourself."


@mcp.tool(
    "unmute_yourself",
    description="Unmute yourself in the meeting.",
)
async def unmute_yourself(
    ctx: Context,
) -> str:
    """Unmute yourself in the meeting."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.unmute()
    return "Unmuted yourself."


@mcp.tool(
    "transcription_status",
    description=(
        "Whether the call's own captions are on (switchboard patch). Waits for them to be turned on. "
        'Answers JSON: {"ok": true} or {"ok": false, "reason": "<word>"}; with a speech model it is always ok.'
    ),
)
async def transcription_status(ctx: Context) -> str:
    """Say whether captions could be turned on (switchboard patch, #764)."""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    wait = getattr(ms._transcription_controller, "wait_decided", None)  # noqa: SLF001
    if wait is None:
        return json.dumps({"ok": True, "reason": "speech_model"})
    return json.dumps(await wait())


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_req: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "healthy"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
