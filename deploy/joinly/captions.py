# switchboard (#764, DESIGN.md section 52.7): read a call's own captions instead of running a speech model.
# This file is switchboard's own, MIT licensed like the rest of the repository (see deploy/joinly/NOTICE). It is
# mounted into joinly v0.5.4 as joinly/captions.py, and the patched server.py (the same one that gives
# `speak_text` a voice) points joinly's `stt`, `vad` and `transcription_controller` settings at the three
# classes here when a client asks for `captions`. With captions on, no speech model is loaded or run.
#
# The module has no joinly import at its top, only inside the methods that need joinly's types, so the two
# pure parts (the rule for when a caption line is final, and the mapping from a caption's initials to a
# participant) can be tested without joinly.
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

QUIET_S = 2.0  # a caption that has not changed for this long is final (the issue's rule)
POLL_S = 0.25  # how often the caption box is read
BOX_GONE_S = 6.0  # a box missing this long after it was on: turn captions on again
REENABLE_GAP_S = 30.0  # ... and not more often than this
LOOKUP_GAP_S = 15.0  # the participant list is asked for no more often than this
ENABLE_WAIT_S = 12.0  # how long to wait for the box once captions were asked for
DECIDE_WAIT_S = 40.0  # how long the status tool waits for captions to be turned on
HISTORY_WORDS = 60  # how much of what was said is kept to tell new words from old ones
MIN_MATCH_WORDS = 3  # a shorter overlap must start the box's text to count

_SENTENCE = re.compile(r".+?(?:[.?!…。？！]+[\"'”’)\]]*)(?=\s|$)|.+$", re.S)
_QUOTES = "\"'“”‘’"
_TERMINAL = ".?!…。？！"

BOX = ".live-transcription-subtitle__box"
# one entry for each speaker the box shows: the avatar's initials and colour, and the text beside it
READ_JS = """() => {
  const box = document.querySelector('.live-transcription-subtitle__box');
  if (!box) return null;
  const out = [];
  for (const it of box.querySelectorAll('.live-transcription-subtitle__item')) {
    const row = it.parentElement;
    const icon = row ? row.querySelector('.zmu-data-selector-item__icon') : null;
    out.push({i: icon ? icon.textContent.trim() : '',
              c: icon ? getComputedStyle(icon).backgroundColor : '',
              t: it.textContent});
  }
  return out;
}"""
# Zoom: More > Show Captions (a guest at 1280x720 has it), then Save in the language dialog. Answers the
# step reached, so the reason a caller reports is the one that happened.
ENABLE_JS = """async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  if (document.querySelector('.live-transcription-subtitle__box')) return 'on';
  window.scrollTo(0, 0);
  const more = [...document.querySelectorAll('button')].find(
    (b) => (b.getAttribute('aria-label') || '').startsWith('More meeting control'));
  if (!more) return 'no_button';
  more.click();
  await sleep(700);
  const show = [...document.querySelectorAll('button.more-button__item-box')].find(
    (b) => /Show Captions/.test(b.innerText || ''));
  if (!show) {
    const hide = [...document.querySelectorAll('button.more-button__item-box')].find(
      (b) => /Hide Captions/.test(b.innerText || ''));
    more.click();
    return hide ? 'on' : 'no_button';
  }
  show.click();
  await sleep(1500);
  const save = [...document.querySelectorAll('button')].find((b) => (b.innerText || '').trim() === 'Save');
  if (save) save.click();
  await sleep(1500);
  return 'clicked';
}"""


# ------------------------------------------------------------------------------------------ pure parts
def words_of(text: str) -> list[str]:
    """The words of ``text`` for comparing, lower case with no punctuation, and "1" and "one" left alone."""
    out = []
    for w in text.split():
        w = re.sub(r"[^\w]+", "", w, flags=re.UNICODE).lower()
        if w:
            out.append(w)
    return out


def sentences(text: str) -> list[str]:
    """``text`` split after each ``. ? !`` that ends a word (so "3.5" and "e.g.x" stay whole), with the
    stray quotation marks Zoom puts around a caption cut off the ends."""
    out = []
    for m in _SENTENCE.finditer(" ".join(text.split())):
        s = m.group(0).strip().strip(_QUOTES).strip()
        if s:
            out.append(s)
    return out


def ends_sentence(text: str) -> bool:
    return text.rstrip(_QUOTES + ")] ").endswith(tuple(_TERMINAL))


def initials_of(name: str) -> set[str]:
    """What Zoom may show on a participant's avatar: the first letters of the first and last word, of the
    first two words, and the first two letters of the first word. A set, because the rule is not published."""
    words = [re.sub(r"^\W+", "", w) for w in re.split(r"[\s_.\-]+", name)]
    words = [w for w in words if w]
    if not words:
        return set()
    out = {words[0][:2].upper()}
    if len(words) >= 2:
        out.add((words[0][0] + words[-1][0]).upper())
        out.add((words[0][0] + words[1][0]).upper())
    return out


SELF = "\0self"  # the bot's own voice: never a call line


def speaker_for(initials: str, participants: list[str], own_name: str) -> str | None:
    """The participant a caption's initials belong to. ``SELF`` when they are the bot's own (its spoken lines
    come back as captions, and a line it said must not be posted as if the call said it). The one name that
    has them, or None when no one or two people share them: a speaker is never guessed."""
    key = initials.strip().upper()
    if not key:
        return None
    if key in initials_of(own_name):
        return SELF
    names = {p for p in participants if p and key in initials_of(p)}
    return next(iter(names)) if len(names) == 1 else None


@dataclass
class Final:
    """One caption line that is done: who (the box's key), what, and when it first and last changed."""

    key: str
    text: str
    t_start: float
    t_end: float


@dataclass
class _Speaker:
    last_text: str = ""
    changed_at: float = 0.0
    first_at: float = 0.0
    history: list[str] = field(default_factory=list)  # the words already sent, most recent last


class FinalRule:
    """When a caption line is final (#764). Each speaker's text in the box is a rolling window of the last
    sentences, revised in place as the words arrive. A sentence is final once the next one has begun, or the
    text has not changed for ``QUIET_S``, or its speaker has left the box. Words already sent are remembered
    (``HISTORY_WORDS`` of them) and are never sent again, whether the window scrolled or a sentence without a
    full stop grew. The clock is an argument: no ``time`` call, so a test steps it."""

    def __init__(self, quiet_s: float = QUIET_S) -> None:
        self.quiet_s = quiet_s
        self._by_key: dict[str, _Speaker] = {}

    def feed(self, entries: list[tuple[str, str]], now: float) -> list[Final]:
        """One read of the box: ``(key, text)`` for each speaker shown. Returns what became final, in order."""
        out: list[Final] = []
        present = set()
        for key, text in entries:
            present.add(key)
            sp = self._by_key.setdefault(key, _Speaker())
            text = " ".join(text.split())
            if text != sp.last_text:
                if not sp.last_text or not self._pending(sp, sp.last_text):
                    sp.first_at = now
                sp.last_text, sp.changed_at = text, now
            out += self._finals(key, sp, now, quiet=now - sp.changed_at >= self.quiet_s)
        for key, sp in self._by_key.items():  # gone from the box: what is left will not be revised
            if key not in present and sp.last_text:
                out += self._finals(key, sp, now, quiet=True)
                sp.last_text = ""
        return out

    def flush(self, now: float) -> list[Final]:
        """The call is over: everything pending is final."""
        out: list[Final] = []
        for key, sp in self._by_key.items():
            if sp.last_text:
                out += self._finals(key, sp, now, quiet=True)
                sp.last_text = ""
        return out

    def changing(self, now: float, within: float) -> bool:
        """Whether any speaker's text changed in the last ``within`` seconds."""
        return any(sp.last_text and now - sp.changed_at < within for sp in self._by_key.values())

    @staticmethod
    def _pending(sp: _Speaker, text: str) -> list[str]:
        """The sentences of ``text`` not yet sent: those after the words already sent."""
        have = sp.history
        words = words_of(text)
        start = 0
        for k in range(min(len(have), len(words)), 0, -1):
            tail = have[-k:]
            for i in range(0, len(words) - k + 1):
                if words[i : i + k] == tail and (k >= MIN_MATCH_WORDS or i == 0 or k == len(have)):
                    if k < MIN_MATCH_WORDS and i != 0:
                        continue
                    start = i + k
                    break
            else:
                continue
            break
        if start == 0:
            return sentences(text)
        # drop the first ``start`` words, then split what is left
        rest, seen = [], 0
        for token in text.split():
            if seen < start:
                if words_of(token):
                    seen += 1
                continue
            rest.append(token)
        return sentences(" ".join(rest))

    def _finals(self, key: str, sp: _Speaker, now: float, *, quiet: bool) -> list[Final]:
        pending = self._pending(sp, sp.last_text) if sp.last_text else []
        if not pending:
            return []
        done = pending if quiet else pending[:-1]
        out = []
        for s in done:
            w = words_of(s)
            if not w:
                continue
            sp.history = (sp.history + w)[-HISTORY_WORDS:]
            out.append(Final(key, s, sp.first_at, now))
        if out:
            sp.first_at = now
        return out


# --------------------------------------------------------------------------------- joinly's three parts
class CaptionsVAD:
    """A voice-activity service that does nothing: no model is loaded, and no audio is looked at. joinly's
    container asks for its audio format, and the transcription controller below never calls ``stream``."""

    def __init__(self, **_: Any) -> None:
        from joinly.types import AudioFormat

        self.audio_format = AudioFormat(sample_rate=16000, byte_depth=2)

    def stream(self, chunks: Any) -> Any:  # pragma: no cover - never called: the controller reads no audio
        raise RuntimeError("captions mode has no voice activity detection")


class CaptionsSTT:
    """A speech-to-text service that does nothing, for the same reason."""

    def __init__(self, **_: Any) -> None:
        from joinly.types import AudioFormat

        self.audio_format = AudioFormat(sample_rate=16000, byte_depth=2)

    def stream(self, windows: Any) -> Any:  # pragma: no cover - never called
        raise RuntimeError("captions mode has no speech-to-text")


class CaptionsTranscriptionController:
    """Turns a call's captions into transcript segments, in place of voice detection and a speech model.

    The patched server hands it the meeting provider (``attach``) so it can reach the call's page. It turns
    on Zoom's captions (for everyone in the call: Zoom shows them to all), reads the box every ``POLL_S``,
    and adds one segment for each line ``FinalRule`` says is final, with the speaker the initials stand for.
    ``status`` says whether the captions could be turned on, for the ``transcription_status`` tool."""

    reader: Any
    vad: Any
    stt: Any

    def __init__(self, **_: Any) -> None:
        self._provider: Any = None
        self._bot = ""
        self._task: asyncio.Task[None] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._no_speech = asyncio.Event()
        self._no_speech.set()
        self._decided = asyncio.Event()
        self.status: dict[str, Any] = {"ok": None, "reason": "pending"}
        self._rule = FinalRule()
        self._participants: list[str] = []
        self._looked_at = float("-inf")
        self._clock: Any = None
        self._transcript: Any = None
        self._events: Any = None

    def attach(self, provider: Any, bot_name: str) -> None:
        """Called once by the patched server with the session's meeting provider and the bot's own name."""
        self._provider = provider
        self._bot = bot_name

    @property
    def no_speech_event(self) -> asyncio.Event:
        return self._no_speech

    async def __aenter__(self) -> CaptionsTranscriptionController:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def start(self, clock: Any, transcript: Any, event_bus: Any) -> None:
        if self._task is not None:
            raise RuntimeError("Transcription controller already started")
        self._clock, self._transcript, self._events = clock, transcript, event_bus
        self._no_speech.set()
        self._task = asyncio.create_task(self._run())
        self._drain_task = asyncio.create_task(self._drain())

    async def stop(self) -> None:
        for task in (self._task, self._drain_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._task = self._drain_task = None
        self._no_speech.clear()
        self._clock = self._transcript = self._events = None

    async def wait_decided(self, timeout: float = DECIDE_WAIT_S) -> dict[str, Any]:
        """``status`` once captions were turned on or could not be (it waits for that, up to ``timeout``)."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._decided.wait(), timeout)
        return self.status

    def _decide(self, ok: bool, reason: str) -> None:
        self.status = {"ok": ok, "reason": reason}
        self._decided.set()

    async def _drain(self) -> None:
        """Keeps joinly's clock moving (it runs on audio) and the speaker's pipe from filling, as the speech
        model's controller does by reading it. The audio itself is thrown away."""
        offset: int | None = None
        while True:
            chunk = await self.reader.read()
            if offset is None:
                offset = chunk.time_ns
            with contextlib.suppress(ValueError):
                if self._clock is not None:
                    self._clock.update(chunk.time_ns - offset)

    def _page(self) -> Any:
        page = getattr(self._provider, "_page", None)
        return page if page is not None and not page.is_closed() else None

    async def _enable(self) -> str:
        """Turn the captions on. "on" or the reason word they could not be."""
        if (
            type(getattr(self._provider, "_platform_controller", None)).__name__
            != "ZoomBrowserPlatformController"
        ):
            return "unsupported_platform"
        page = self._page()
        if page is None:
            return "no_page"
        try:
            step = await page.evaluate(ENABLE_JS)
        except Exception:
            logger.warning("captions: turning them on failed", exc_info=True)
            return "no_button"
        if step == "no_button":
            return "no_button"
        deadline = time.monotonic() + ENABLE_WAIT_S
        while time.monotonic() < deadline:
            if await page.query_selector(BOX):
                return "on"
            await asyncio.sleep(0.5)
        return "no_box"

    async def _run(self) -> None:
        try:
            reason = await self._enable()
            if reason != "on":
                self._decide(False, reason)
                return
            self._decide(True, "on")
            last_on, last_try = time.monotonic(), float("-inf")
            while True:
                now = time.monotonic()
                page = self._page()
                items = None
                if page is not None:
                    try:
                        items = await page.evaluate(READ_JS)
                    except Exception:
                        items = None
                if items is None:
                    if now - last_on >= BOX_GONE_S and now - last_try >= REENABLE_GAP_S:
                        last_try = now
                        logger.info("captions: the box is gone, turning captions on again")
                        await self._enable()
                else:
                    last_on = now
                    await self._take(items, now)
                    quiet = not self._rule.changing(now, 0.6)
                    self._no_speech.set() if quiet else self._no_speech.clear()
                await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                self._emit(self._rule.flush(time.monotonic()))
            raise
        except Exception:
            logger.exception("captions: reading stopped")
            if not self._decided.is_set():
                self._decide(False, "no_box")

    async def _take(self, items: list[dict[str, Any]], now: float) -> None:
        entries = [
            (f"{str(it.get('i', ''))[:8]}|{str(it.get('c', ''))[:40]}", str(it.get("t", ""))[:4000])
            for it in items
            if isinstance(it, dict)
        ]
        finals = self._rule.feed(entries, now)
        if finals:
            await self._look_up(now, {f.key.split("|", 1)[0] for f in finals})
        self._emit(finals)

    async def _look_up(self, now: float, wanted: set[str]) -> None:
        """The participant list, when a speaker's initials do not map yet. Not more often than ``LOOKUP_GAP_S``
        because asking opens Zoom's participant panel."""
        unknown = [i for i in wanted if speaker_for(i, self._participants, self._bot) is None]
        if not unknown or now - self._looked_at < LOOKUP_GAP_S or self._provider is None:
            return
        self._looked_at = now
        try:
            people = await asyncio.wait_for(self._provider.get_participants(), 20)
            self._participants = [str(getattr(p, "name", "")) for p in people if getattr(p, "name", "")]
        except Exception:
            logger.warning("captions: the participant list could not be read")

    def _emit(self, finals: list[Final]) -> None:
        if not finals or self._transcript is None or self._clock is None:
            return
        from joinly.types import TranscriptSegment

        end = self._clock.now_s
        for f in finals:
            who = speaker_for(f.key.split("|", 1)[0], self._participants, self._bot)
            if who == SELF:
                continue
            start = max(0.0, end - max(0.0, f.t_end - f.t_start))
            self._transcript.add_segment(TranscriptSegment(text=f.text, start=start, end=end, speaker=who))
            if self._events is not None:
                self._events.publish("segment")
        if self._events is not None:
            self._events.publish("utterance")
