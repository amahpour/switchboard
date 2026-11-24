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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

QUIET_S = 2.0  # a caption that has not changed for this long is final (the issue's rule)
SENTENCE_QUIET_S = 0.8  # ... or this long when it ends a sentence (. ? !): a pause, #801 (a starting point)
MAX_HOLD_S = 8.0  # finished sentences are held for the speaker's pause no longer than this
POLL_S = 0.25  # how often the caption box is read
BOX_GONE_S = 6.0  # a box missing this long after it was on: turn captions on again
REENABLE_GAP_S = 30.0  # ... and not more often than this
LOOKUP_GAP_S = 15.0  # the participant list is asked for no more often than this
ENABLE_WAIT_S = 12.0  # how long to wait for the box once captions were asked for
DECIDE_WAIT_S = 40.0  # how long the status tool waits for captions to be turned on
HISTORY_WORDS = 60  # how much of what was said is kept to tell new words from old ones
MIN_MATCH_WORDS = 3  # a shorter overlap must start the box's text to count
# A picture-only speaker (#810) is named from the call's active-speaker detection only when it agrees with
# itself: enough samples, and nearly all of them one name. One live call named the speaker in 7 of 7 samples;
# 3 and 80% leave room for a stray sample and still refuse a split or a thin count (never a guess).
PIC_MIN_SAMPLES = 3
PIC_MIN_SHARE = 0.8

_SENTENCE = re.compile(r".+?(?:[.?!…。？！]+[\"'”’)\]]*)(?=\s|$)|.+$", re.S)
_QUOTES = "\"'“”‘’"
_TERMINAL = ".?!…。？！"

BOX = ".live-transcription-subtitle__box"
# one entry for each speaker the box shows: the avatar's initials and colour, the text beside it, and (#810)
# the name label of an avatar with a picture (no initials): the picture's alt, or aria-label or title on the
# icon or the row (live, Zoom has none); `p`, a short id of the picture's address, hashed here so the address
# itself never leaves the page (FNV-1a, 64 bit, 12 hex digits: SubtleCrypto is asynchronous, and this only
# has to tell the people on one call apart); and, only for a row with no initials, its markup (cut), for the
# one-time diagnostic
READ_JS = """(bot) => {
  const box = document.querySelector('.live-transcription-subtitle__box');
  if (!box) return null;
  const out = [];
  // Joinly's active_speaker deliberately filters out the bot. Check active tiles directly.
  const name = (s) => s.trim().toLowerCase();
  const ownActive = bot && [
    'div.speaker-active-container__video-frame span',
    'div.speaker-bar-container__video-frame--active span',
    'div.single-main-container__video-frame span',
  ].some((sel) => [...document.querySelectorAll(sel)].some(
    (el) => name(el.textContent) === name(bot)));
  for (const it of box.querySelectorAll('.live-transcription-subtitle__item')) {
    const row = it.parentElement;
    const icon = row ? row.querySelector('.zmu-data-selector-item__icon') : null;
    const initials = icon ? icon.textContent.trim() : '';
    let label = '', pic = '';
    if (icon && !initials) {
      // Zoom's picture is the icon element itself (live, 2026-11-17); an <img> inside it is the fallback
      const img = icon.tagName === 'IMG' ? icon : icon.querySelector('img');
      const from = [img && img.getAttribute('alt'), icon.getAttribute('aria-label'), icon.getAttribute('title'),
                    row && row.getAttribute('aria-label'), row && row.getAttribute('title')];
      label = (from.find((v) => v && v.trim()) || '').trim().slice(0, 80);
      const src = (img && (img.currentSrc || img.getAttribute('src'))) || '';
      if (src) {
        let h = 0xcbf29ce484222325n;
        for (let k = 0; k < src.length; k++) {
          h = BigInt.asUintN(64, (h ^ BigInt(src.charCodeAt(k))) * 0x100000001b3n);
        }
        pic = h.toString(16).padStart(16, '0').slice(0, 12);
      }
    }
    out.push({i: initials,
              c: icon ? getComputedStyle(icon).backgroundColor : '',
              n: label,
              p: pic,
              own_active: Boolean(ownActive),
              t: it.textContent,
              h: initials ? '' : [icon, row].map((e) => (e ? e.outerHTML.slice(0, 1500) : '')).join('\\u0000')});
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


def is_edit(before: list[str] | None, now: list[str]) -> bool:
    """Whether a row's words are only a revision of what it showed when the bot began to speak (#852). Zoom
    revises an old caption in place (a word's case, punctuation or spelling) and drops a row's oldest words
    when the speaker is quiet; neither is someone speaking over the bot. Compared by position against the END
    of the baseline: an edit has no more words than before, and at least two thirds of them equal the baseline's
    last words at the same places. A row with no baseline (``None``), more words, a scroll (old words moved
    forward, new ones after) or a new utterance does not line up, so it is speech. A speaker who repeats
    exactly the last words of their previous caption is not heard until they say something new."""
    if before is None or not now or len(now) > len(before):
        return False
    tail = before[-len(now) :]
    return 3 * sum(1 for a, b in zip(tail, now, strict=True) if a == b) >= 2 * len(now)


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
    raw = [w for w in re.split(r"[\s_.\-]+", name) if w]
    words = [re.sub(r"^\W+", "", w) for w in raw]
    words = [w for w in words if w]
    if not words:
        return set()
    out = {words[0][:2].upper()}
    if len(words) >= 2:
        out.add((words[0][0] + words[-1][0]).upper())
        out.add((words[0][0] + words[1][0]).upper())
    # Zoom takes the first character of each word as written: "switchboard (transcribing)" shows "S("
    if len(raw) >= 2:
        out.add((raw[0][0] + raw[1][0]).upper())
        out.add((raw[0][0] + raw[-1][0]).upper())
    return out


SELF = "\0self"  # the bot's own voice: never a call line


def norm_name(name: str) -> str:
    return " ".join(name.casefold().split())


def key_parts(key: str) -> tuple[str, str]:
    """The initials and the name label an entry key holds: ``initials|colour|label|picture``. The label and the
    picture's id are in the key so two speakers with pictures (no initials) are two keys, not one."""
    parts = key.split("|", 3)
    return parts[0], (parts[2] if len(parts) > 2 else "")


def key_pic(key: str) -> str:
    """The id of the avatar's picture an entry key holds (#810), or ``""``: a row with initials has none."""
    parts = key.split("|", 3)
    return parts[3] if len(parts) > 3 else ""


def speaker_for_label(label: str, participants: list[str], own_name: str) -> str | None:
    """The participant a picture's name label belongs to (#810). The label is a claim Zoom's page makes, so it
    counts only when it is, ignoring case and spacing, exactly one participant's name (or the bot's own: ``SELF``).
    No match, or two people with that name, and the line has no speaker: never guessed."""
    want = norm_name(label[:80])
    if not want:
        return None
    if want == norm_name(own_name):
        return SELF
    names = {p for p in participants if p and norm_name(p) == want}
    return next(iter(names)) if len(names) == 1 else None


_TAG = re.compile(r"<[^>]*(?:>|$)")
_ATTR = re.compile(r"([^\s=/\"'<>]+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+))?")


def redact_markup(html: str, limit: int = 600) -> str:
    """``html`` for a log (#810): tag names, attribute names and class names are kept; every other attribute
    value and all text become their length, so the markup shows where Zoom puts a name and the log holds none.
    Cut to ``limit`` characters."""

    def tag(m: re.Match[str]) -> str:
        body = m.group(0)[1:].rstrip(">").strip()
        if body.startswith(("/", "!")):
            return "<" + body.split()[0] + ">" if body.split() else "<>"
        closing = body.endswith("/")
        parts = body.rstrip("/").strip().split(None, 1)
        name = parts[0] if parts else ""
        attrs = []
        for a in _ATTR.finditer(parts[1] if len(parts) > 1 else ""):
            val = a.group(2)
            if val is None:
                attrs.append(a.group(1))
                continue
            inner = val[1:-1] if val[:1] in "\"'" and len(val) >= 2 else val
            attrs.append(f'{a.group(1)}="{inner if a.group(1) == "class" else f"[{len(inner)}]"}"')
        return "<" + " ".join([name, *attrs]) + ("/" if closing else "") + ">"

    out, pos = [], 0
    for m in _TAG.finditer(html):
        if m.start() > pos and html[pos : m.start()].strip():
            out.append(f"[{len(html[pos : m.start()].strip())}]")
        out.append(tag(m))
        pos = m.end()
    if pos < len(html) and html[pos:].strip():
        out.append(f"[{len(html[pos:].strip())}]")
    return "".join(out)[:limit]


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
    paused: bool = False  # finalised because the speaker paused or left, not because the next sentence began


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

    def __init__(self, quiet_s: float = QUIET_S, sentence_quiet_s: float = SENTENCE_QUIET_S) -> None:
        self.quiet_s = quiet_s
        self.sentence_quiet_s = min(sentence_quiet_s, quiet_s)
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
            out += self._finals(key, sp, now, quiet=self._settled(sp, now))
        for key, sp in self._by_key.items():  # gone from the box: what is left will not be revised
            if key not in present and sp.last_text:
                out += self._finals(key, sp, now, quiet=True)
                sp.last_text = ""
        return out

    def _settled(self, sp: _Speaker, now: float) -> bool:
        """A pause: no change for ``quiet_s``, or for the shorter ``sentence_quiet_s`` when the text ends a
        sentence (a speaker who has finished one and stopped does not need two seconds to be sure of)."""
        idle = now - sp.changed_at
        return idle >= self.quiet_s or (idle >= self.sentence_quiet_s and ends_sentence(sp.last_text))

    def flush(self, now: float) -> list[Final]:
        """The call is over: everything pending is final."""
        out: list[Final] = []
        for key, sp in self._by_key.items():
            if sp.last_text:
                out += self._finals(key, sp, now, quiet=True)
                sp.last_text = ""
        return out

    def changing(self, now: float, within: float, ignore: Callable[[str], bool] | None = None) -> bool:
        """Whether any speaker's text changed in the last ``within`` seconds. ``ignore`` is a predicate on a
        key: a row it is true for does not count (the bot's own caption, which must not stop its own voice)."""
        return next(self.changing_rows(now, within, ignore), None) is not None

    def changing_rows(
        self, now: float, within: float, ignore: Callable[[str], bool] | None = None
    ) -> Iterator[tuple[str, str]]:
        for key, sp in self._by_key.items():
            if sp.last_text and now - sp.changed_at < within and (ignore is None or not ignore(key)):
                yield key, sp.last_text

    @staticmethod
    def _before_is_sent(have: list[str], k: int, before: list[str]) -> bool:
        """Whether the box's words ``before`` a match of the last ``k`` sent words are what was sent just
        before them. The box is a rolling window over what was said, so words in front of the tail are older
        sent words; others mean a new sentence that only ends like the last one. Only history this short is
        checked when it was not cut at ``HISTORY_WORDS``: older words are gone, so they are taken as sent."""
        if not before:
            return True
        older = have[: len(have) - k]  # what was sent before the matched tail
        n = min(len(before), len(older))
        if n and before[-n:] != older[-n:]:
            return False
        return len(before) <= len(older) or len(have) >= HISTORY_WORDS

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
                    if not FinalRule._before_is_sent(have, k, words[:i]):
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
            out.append(Final(key, s, sp.first_at, now, paused=quiet))
        if out:
            sp.first_at = now
        return out


class PauseHold:
    """Holds each speaker's finished sentences until that speaker pauses, then gives them out together as one
    ``Final`` (#801), so the bridge posts one line a moment after the pause and not a second wait later. A
    sentence final only because the next began is held; one final because the speaker paused or left (or the
    call ended) releases everything held for that key with it. Another speaker's text changing releases it at
    once (the first one has stopped), and nothing is held longer than ``MAX_HOLD_S``. The clock is an
    argument."""

    def __init__(self, max_hold_s: float = MAX_HOLD_S) -> None:
        self.max_hold_s = max_hold_s
        self._held: dict[str, list[Final]] = {}
        self._texts: dict[str, str] = {}

    def step(self, finals: list[Final], entries: list[tuple[str, str]], now: float) -> list[Final]:
        changed = {k for k, t in entries if self._texts.get(k) != " ".join(t.split())}
        self._texts = {k: " ".join(t.split()) for k, t in entries}
        out: list[Final] = []
        for f in finals:
            held = self._held.setdefault(f.key, [])
            held.append(f)
            if f.paused:
                out.append(self._merge(self._held.pop(f.key)))
        for key in list(self._held):
            held = self._held[key]
            if now - held[0].t_end >= self.max_hold_s or (changed - {key}):
                out.append(self._merge(self._held.pop(key)))
        return out

    def flush(self) -> list[Final]:
        out = [self._merge(h) for h in self._held.values() if h]
        self._held = {}
        return out

    @staticmethod
    def _merge(held: list[Final]) -> Final:
        first, last = held[0], held[-1]
        return Final(first.key, " ".join(f.text for f in held), first.t_start, last.t_end, paused=last.paused)


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
        self._playback_active: Callable[[], bool] = lambda: False
        self._anonymous_own: dict[str, str] = {}
        # what each row showed at the last read, and (while the bot plays) when this line began (#852)
        self._last_texts: dict[str, list[str]] = {}
        self._baseline: dict[str, list[str]] = {}
        self._was_playing = False
        self._edit_logged = False
        self._task: asyncio.Task[None] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._no_speech = asyncio.Event()
        self._no_speech.set()
        self._decided = asyncio.Event()
        self.status: dict[str, Any] = {"ok": None, "reason": "pending"}
        self._rule = FinalRule()
        self._hold = PauseHold()
        self._participants: list[str] = []
        self._looked_at = float("-inf")
        self._lookup_failures = 0
        self._seen_text: dict[str, str] = {}
        # initials (or, with a picture and no initials, the label) -> who the call showed as speaking meanwhile
        self._active: dict[str, dict[str, int]] = {}
        self._active_samples = 0
        self._active_named = 0
        self._icon_logged = False
        self._dumped = False
        self._clock: Any = None
        self._transcript: Any = None
        self._events: Any = None

    def attach(self, provider: Any, bot_name: str, playback_active: Callable[[], bool] | None = None) -> None:
        """Called once by the patched server with the session's meeting provider and the bot's own name."""
        self._provider = provider
        self._bot = bot_name
        if playback_active is not None:
            self._playback_active = playback_active

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
            logger.info("captions: on; no speech model is used")
            self._decide(True, "on")
            last_on, last_try = time.monotonic(), float("-inf")
            while True:
                now = time.monotonic()
                page = self._page()
                items = None
                if page is not None:
                    try:
                        items = await page.evaluate(READ_JS, self._bot)
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
                await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            logger.info(
                "captions: active_speaker was named in %d of %d samples of a changing caption",
                self._active_named,
                self._active_samples,
            )
            with contextlib.suppress(Exception):
                self._emit(self._hold.step(self._rule.flush(time.monotonic()), [], 0.0) + self._hold.flush())
            raise
        except Exception:
            logger.exception("captions: reading stopped")
            if not self._decided.is_set():
                self._decide(False, "no_box")

    def _is_own_key(self, key: str) -> bool:
        return self._name_of(key) == SELF

    def _name_of(self, key: str) -> str | None:
        return self._name_for(*key_parts(key), key_pic(key))

    async def _take(self, items: list[dict[str, Any]], now: float) -> None:
        entries = []
        anonymous_own = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            ini = str(it.get("i", ""))[:8]
            label = " ".join(str(it.get("n", "") if not ini else "").replace("|", " ").split())[:80]
            pic = "" if ini else re.sub(r"[^0-9a-f]", "", str(it.get("p", "")).lower())[:12]
            colour = str(it.get("c", "")).replace("|", "")[:40]
            key = f"{ini.replace('|', '')}|{colour}|{label}|{pic}"
            caption = str(it.get("t", ""))[:4000]
            # A new anonymous row can precede the bot's initials. Keep an unchanged self caption
            # suppressed even if the active speaker has since changed.
            if not (ini or label or pic) and (
                self._anonymous_own.get(key) == caption
                or (it.get("own_active") is True and self._seen_text.get(key) != caption)
            ):
                anonymous_own[key] = caption
                continue
            entries.append((key, caption))
            if not ini and not self._icon_logged and it.get("h"):
                self._icon_logged = True  # once a call: where Zoom puts the name of a speaker with a picture
                icon, _, row = str(it["h"]).partition("\0")
                logger.info(
                    "captions: a row with no initials; icon %s; row %s",
                    redact_markup(icon),
                    redact_markup(row),
                )
        self._anonymous_own = anonymous_own
        playing = self._playback_active()
        if playing and not self._was_playing:  # a new line: what the rows showed just before it is the baseline
            self._baseline = dict(self._last_texts)
            self._edit_logged = False
        self._was_playing = playing
        self._last_texts = {key: words_of(caption) for key, caption in entries}
        self._sample_active(entries)
        finals = self._hold.step(self._rule.feed(entries, now), entries, now)
        self._update_no_speech(now, playing)
        if finals:
            await self._dump_buttons()
            await self._look_up(now, {f.key for f in finals})
        self._emit(finals)

    def _update_no_speech(self, now: float, playing: bool = False) -> None:
        """Clear the no-speech event while a person's row says something new. While the bot plays, a row that
        only revises what it showed when the line began does not count (#852): see ``is_edit``. Only counts
        of words are logged, never the words."""
        rows = []
        for key, caption in self._rule.changing_rows(now, 0.6, self._is_own_key):
            if playing:
                before, words = self._baseline.get(key), words_of(caption)
                if is_edit(before, words):
                    # The row as it last stood is the next baseline: a 0.25 s poll can miss the gap between a
                    # line's sentences, and revisions must not add up against a list from before the first.
                    self._baseline[key] = words
                    if not self._edit_logged:
                        self._edit_logged = True
                        logger.info(
                            "captions: an edit to words already shown, not speech over the bot; "
                            "words_before=%d words=%d",
                            len(before or ()),
                            len(words),
                        )
                    continue
            rows.append((key, caption))
        if not rows:
            self._no_speech.set()
            return
        if self._no_speech.is_set() and playing:
            for key, caption in rows:
                initials, colour, label, picture = key.split("|", 3)
                before = self._baseline.get(key)
                logger.info(
                    "captions: no-speech cleared during playback; "
                    "initials=%r colour=%r label_length=%d picture=%r words=%d baseline_words=%s",
                    initials,
                    colour,
                    len(label),
                    picture,
                    len(words_of(caption)),
                    "none" if before is None else len(before),
                )
        self._no_speech.clear()

    def _sample_active(self, entries: list[tuple[str, str]]) -> None:
        """While a speaker's caption changes, note who the call's own active-speaker detection names (joinly's
        Zoom fix reads the speaking tile). It names a speaker without opening Zoom's participant panel."""
        name = getattr(getattr(self._provider, "_platform_controller", None), "active_speaker", None)
        for key, text in entries:
            if text != self._seen_text.get(key):
                self._seen_text[key] = text
                self._active_samples += 1
                if isinstance(name, str) and name:
                    self._active_named += 1
                    counts = self._active.setdefault(self._active_id(*key_parts(key), key_pic(key)), {})
                    counts[name[:80]] = counts.get(name[:80], 0) + 1

    @staticmethod
    def _active_id(initials: str, label: str, pic: str = "") -> str:
        """Whose counts a row's samples go in: its initials, else its picture (``\0p:``), else its label
        (``\0``). The prefixes keep a picture's counts apart from any initials' and from another picture's."""
        if initials.strip():
            return initials.strip().upper()
        if pic:
            return "\0p:" + pic
        return "\0" + norm_name(label) if label else ""

    def _by_picture(self, pic: str) -> str | None:
        """The name the call's active-speaker detection gave this picture's row, only when it is sure: at
        least ``PIC_MIN_SAMPLES`` samples, ``PIC_MIN_SHARE`` of them one name, and not the bot's own."""
        counts = self._active.get(self._active_id("", "", pic)) or {}
        total = sum(counts.values())
        if not counts or total < PIC_MIN_SAMPLES:
            return None
        top, n = max(counts.items(), key=lambda kv: kv[1])
        if n < PIC_MIN_SHARE * total or norm_name(top) == norm_name(self._bot):
            return None
        return top

    def _name_for(self, initials: str, label: str = "", pic: str = "") -> str | None:
        """The speaker for ``initials``: the participant list's one match, else the name the call showed as
        speaking most while the caption changed, if its initials are these. Never a guess.

        A picture has no initials (#810), so a row with only a ``label`` is named by it only when the label is
        exactly one participant's name; and when the list cannot be read, only when it is also the name the
        call's own active-speaker detection showed most while that row changed. A label that is neither
        (Zoom's "avatar", say) names no one."""
        if not initials.strip():
            if label:
                who = speaker_for_label(label, self._participants, self._bot)
                if who is not None or self._participants:
                    return who
            if pic:
                return self._by_picture(pic)
            if not label:
                return None
            counts = self._active.get(self._active_id("", label)) or {}
            top = max(counts.items(), key=lambda kv: kv[1])[0] if counts else ""
            return top if top and norm_name(top) == norm_name(label) else None
        who = speaker_for(initials, self._participants, self._bot)
        if who is not None:
            return who
        counts = self._active.get(initials.strip().upper()) or self._active.get(initials) or {}
        for name, _ in sorted(counts.items(), key=lambda kv: -kv[1]):
            if initials.strip().upper() in initials_of(name):
                return name
            break
        return None

    async def _look_up(self, now: float, wanted: set[str]) -> None:
        """The participant list, when a speaker's initials do not map yet. Not more often than ``LOOKUP_GAP_S``
        because asking opens Zoom's participant panel."""
        unknown = [k for k in wanted if self._name_of(k) is None]
        if not unknown or now - self._looked_at < LOOKUP_GAP_S or self._provider is None:
            return
        if self._lookup_failures >= 2:  # the panel can't be opened here: the active speaker is the way
            return
        self._looked_at = now
        try:
            people = await asyncio.wait_for(self._provider.get_participants(), 20)
            self._participants = [str(getattr(p, "name", "")) for p in people if getattr(p, "name", "")]
        except Exception:
            self._lookup_failures += 1
            logger.warning("captions: the participant list could not be read")
            await self._dump_buttons()

    async def _dump_buttons(self) -> None:
        """Once, for a person to read: the labels of the page's buttons as they are, and again after the click
        that joinly uses to bring up Zoom's control bar, to see why a control was not found (#773)."""
        page = self._page()
        if self._dumped or page is None:
            return
        self._dumped = True
        js = """() => [...document.querySelectorAll('button')].slice(0, 60).map(
          (b) => (b.getAttribute('aria-label') || b.innerText || '').trim().slice(0, 40)
          + (b.offsetParent === null ? ' [hidden]' : ''))"""
        try:
            logger.info("captions: buttons at rest: %s", await page.evaluate(js))
            await page.mouse.click(640, 360)
            await page.wait_for_timeout(300)
            logger.info("captions: buttons after joinly's click: %s", await page.evaluate(js))
        except Exception:
            logger.info("captions: the page's buttons could not be listed")

    def _emit(self, finals: list[Final]) -> None:
        if not finals or self._transcript is None or self._clock is None:
            return
        from joinly.types import TranscriptSegment

        end = self._clock.now_s
        for f in finals:
            initials, label = key_parts(f.key)
            pic = key_pic(f.key)
            who = self._name_of(f.key)
            logger.info(
                "captions: line from initials %r%s as %s (%d participants known)",
                initials,
                " with a name label" if label else (" with a picture" if pic else ""),
                "the bot itself" if who == SELF else repr(who),
                len(self._participants),
            )
            if who == SELF:
                continue
            start = max(0.0, end - max(0.0, f.t_end - f.t_start))
            self._transcript.add_segment(TranscriptSegment(text=f.text, start=start, end=end, speaker=who))
            if self._events is not None:
                self._events.publish("segment")
        if self._events is not None:
            self._events.publish("utterance")
