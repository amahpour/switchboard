"""The captions patch's pure parts (DESIGN.md §52.7, #764): when a caption line is final, and which
participant
a caption's initials are. ``deploy/joinly/captions.py`` has no joinly import at its top, so it loads by
path."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sb_captions", ROOT / "deploy" / "joinly" / "captions.py")
assert spec and spec.loader
cap = importlib.util.module_from_spec(spec)
sys.modules["sb_captions"] = cap
spec.loader.exec_module(cap)

KEY = "SB|rgb(39, 174, 96)"


def run(reads: list[tuple[float, str]], rule: Any = None, key: str = KEY) -> list[str]:
    rule = rule or cap.FinalRule()
    out: list[str] = []
    for t, text in reads:
        out += [f.text for f in rule.feed([(key, text)], t)]
    return out


def test_a_sentence_is_final_once_the_next_has_begun_and_never_before() -> None:
    """The first sentence ends in a question mark, but is sent only when the next one starts (the box can
    still
    revise it). Fails if a sentence is sent while it is the last one and still changing."""
    assert run([(0, "Claude one, can you hear me?")]) == []
    assert run([(0, "Claude one, can you hear me?"), (0.5, "Claude one, can you hear me? Hey")]) == [
        "Claude one, can you hear me?"
    ]


def test_the_last_sentence_is_final_after_two_quiet_seconds() -> None:
    """With nothing changing for 2 s the last sentence is sent, even without a full stop. Fails at 1.9 s or if
    it never comes."""
    assert run([(0, "Hey Codex, run the tests"), (1.9, "Hey Codex, run the tests")]) == []
    assert run([(0, "Hey Codex, run the tests"), (2.0, "Hey Codex, run the tests")]) == [
        "Hey Codex, run the tests"
    ]


def test_the_recorded_rolling_box_gives_each_line_once_in_order_without_the_stray_quote() -> None:
    """The spike's real box: a rolling window that scrolls, grows a sentence with no full stop, and starts the
    first line with a quotation mark. Every line comes out once. Fails if a line is sent twice (window
    scroll or
    a growing sentence), half-sent, or keeps the quote."""
    data = json.loads((ROOT / "tests" / "fixtures" / "captions" / "zoom_rolling_box.json").read_text())
    reads = [(t, text) for t, text in data["reads"]]
    reads.append((reads[-1][0] + 2.5, reads[-1][1]))  # the speaker stops
    got = run(reads)
    assert got == [
        "Claude one, can you hear me?",
        "Hey Codex, run the tests",
        "Claude Kiln, what's the max temperature?",
        "The purple elephant is reading the",
    ]


def test_a_word_revised_in_place_is_not_sent_before_it_is_final() -> None:
    """Zoom changes "Claude won" to "Claude 1." as a sentence ends. The line is sent after, with the revision.
    Fails if the early form is sent."""
    got = run([(0, "Claude won"), (0.5, "Claude 1."), (1.0, "Claude 1. Please"), (3.5, "Claude 1. Please")])
    assert got == ["Claude 1.", "Please"]


def test_the_same_short_sentence_said_twice_is_two_lines_and_a_scroll_is_not_a_third() -> None:
    """ "Yes." twice in a row is two lines, and the window scrolling later does not send either again.
    Fails if a
    repeat is swallowed or resent."""
    rule = cap.FinalRule()
    reads = [(0, "Yes."), (3, "Yes."), (3.5, "Yes. Yes."), (6, "Yes. Yes."), (7, "Yes. Yes. Maybe")]
    got = run([*reads, (9.5, "Yes. Yes. Maybe")], rule)
    assert got == ["Yes.", "Yes.", "Maybe"]


def test_a_speaker_who_leaves_the_box_is_flushed_at_once() -> None:
    """When a speaker's text disappears (the box moved on to someone else), what is left is final now, since
    it will not be revised. Fails if the last words of a speaker are lost or wait for a timer that never
    runs."""
    rule = cap.FinalRule()
    assert rule.feed([("AB|x", "Ship it")], 0) == []
    out = rule.feed([("CD|y", "Okay")], 0.3)
    assert [f.text for f in out] == ["Ship it"] and out[0].key == "AB|x"


def test_two_speakers_in_the_box_are_kept_apart() -> None:
    rule = cap.FinalRule()
    out = rule.feed([("AB|x", "One."), ("CD|y", "Two.")], 0)
    out += rule.feed([("AB|x", "One."), ("CD|y", "Two.")], 2.1)
    assert sorted((f.key, f.text) for f in out) == [("AB|x", "One."), ("CD|y", "Two.")]


def test_flush_at_the_end_of_the_call_sends_what_is_pending() -> None:
    rule = cap.FinalRule()
    rule.feed([(KEY, "Bye for now")], 0)
    assert [f.text for f in rule.flush(0.1)] == ["Bye for now"]


@pytest.mark.parametrize(
    ("initials", "people", "expected"),
    [
        ("AB", ["alice brown", "bob smith"], "alice brown"),
        ("AB", ["alice brown", "Anna Bell"], None),  # two people share them: unknown, never guessed
        ("ZZ", ["alice brown"], None),
        ("", ["alice brown"], None),
        ("al", ["alice"], "alice"),  # a one-word name shows its first two letters
        ("ST", ["alice brown", "switchboard (transcribing)"], cap.SELF),  # the bot's own voice
        ("BS", ["bob smith"], "bob smith"),
    ],
)
def test_initials_map_to_the_one_participant_who_has_them(
    initials: str, people: list[str], expected: Any
) -> None:
    """The caption box shows initials only. One match is the speaker, none or two is unknown, and the
    bot's own
    initials are its own voice (an agent's spoken line must not be posted back as if the call said it). Fails
    if a speaker is guessed between two people."""
    assert cap.speaker_for(initials, people, "switchboard (transcribing)") == expected


# ------------------------------------------------------------------------------------------ the controller
class Page:
    def __init__(self, reads: list[Any]):
        self.reads = list(reads)
        self.enable = "clicked"
        self.box = True

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, js: str) -> Any:
        if js is cap.ENABLE_JS:
            return self.enable
        return self.reads.pop(0) if len(self.reads) > 1 else (self.reads[0] if self.reads else None)

    async def query_selector(self, sel: str) -> Any:
        return object() if self.box else None


class ZoomBrowserPlatformController:
    pass


class Provider:
    def __init__(self, page: Page, platform: Any = None):
        self._page = page
        self._platform_controller = platform if platform is not None else ZoomBrowserPlatformController()

    async def get_participants(self) -> list[Any]:
        names = ["alice brown", "switchboard (transcribing)"]
        return [types.SimpleNamespace(name=n) for n in names]


class Clock:
    now_s = 12.0

    def update(self, ns: int) -> None: ...


class Transcript:
    def __init__(self) -> None:
        self.segments: list[Any] = []

    def add_segment(self, seg: Any) -> None:
        self.segments.append(seg)


class Bus:
    def __init__(self) -> None:
        self.events: list[str] = []

    def publish(self, kind: str) -> None:
        self.events.append(kind)


@pytest.fixture
def joinly_types(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("joinly.types")
    mod.TranscriptSegment = lambda **kw: types.SimpleNamespace(**kw)  # type: ignore[attr-defined]
    mod.AudioFormat = lambda **kw: types.SimpleNamespace(**kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "joinly", types.ModuleType("joinly"))
    monkeypatch.setitem(sys.modules, "joinly.types", mod)


def controller(page: Page, platform: Any = None) -> Any:
    c = cap.CaptionsTranscriptionController()
    c.attach(Provider(page, platform), "switchboard (transcribing)")
    c.reader = types.SimpleNamespace(read=lambda: asyncio.sleep(3600))
    return c


async def test_the_status_says_which_step_failed_when_captions_cannot_be_turned_on(
    joinly_types: None,
) -> None:
    """Zoom with no Show Captions button (the host account has them off) is `no_button`; a platform other than
    Zoom is `unsupported_platform`; a box that never appears is `no_box`. Fails if a failure reads as ok."""
    page = Page([None])
    page.enable = "no_button"
    c = controller(page)
    await c.start(Clock(), Transcript(), Bus())
    assert (await c.wait_decided(5)) == {"ok": False, "reason": "no_button"}
    await c.stop()

    other = controller(Page([None]), platform=object())
    await other.start(Clock(), Transcript(), Bus())
    assert (await other.wait_decided(5))["reason"] == "unsupported_platform"
    await other.stop()


async def test_a_caption_that_goes_quiet_becomes_one_segment_with_the_speaker_the_initials_stand_for(
    joinly_types: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The controller turns the box into transcript segments: speaker from the participant list, the bot's own
    voice dropped, and a `segment` event for each. Fails if the speaker is not mapped, the bot's lines are
    posted, or no event is published."""
    monkeypatch.setattr(cap, "POLL_S", 0.01)
    monkeypatch.setattr(cap, "QUIET_S", 0.05)
    item = {"i": "AB", "c": "rgb(1, 1, 1)", "t": "Hey Codex, run the tests"}
    own = {"i": "ST", "c": "rgb(2, 2, 2)", "t": "Sure, running them"}
    page = Page([[item, own]])
    c = controller(page)
    c._rule = cap.FinalRule(0.05)
    ts, bus = Transcript(), Bus()
    await c.start(Clock(), ts, bus)
    assert (await c.wait_decided(5)) == {"ok": True, "reason": "on"}
    for _ in range(200):
        if ts.segments:
            break
        await asyncio.sleep(0.02)
    await c.stop()
    assert [(s.text, s.speaker) for s in ts.segments] == [("Hey Codex, run the tests", "alice brown")]
    assert "segment" in bus.events


def test_zoom_shows_the_first_character_of_each_word_as_written_so_the_bot_is_s_paren() -> None:
    """The live call (#773) showed "S(" for "switchboard (transcribing)": the parenthesis is not stripped.
    Fails if the bot's own initials are not recognised, which would post its lines back as call lines."""
    assert "S(" in cap.initials_of("switchboard (transcribing)")
    assert cap.speaker_for("S(", ["alice brown"], "switchboard (transcribing)") == cap.SELF


async def test_a_speaker_is_named_by_the_active_speaker_when_the_participant_list_cannot_be_read(
    joinly_types: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zoom's panel may not open for the bot (the live call showed "Participants button not found"), so the
    name comes from the call's active speaker while the caption changed, and only if its initials are the
    caption's. Fails if a line gets no speaker though the call showed one, or if a name whose initials
    differ is used (a guess)."""
    monkeypatch.setattr(cap, "POLL_S", 0.01)
    item = {"i": "SS", "c": "rgb(1, 1, 1)", "t": "Hey Codex, run the tests"}
    page = Page([[item]])
    c = controller(page)
    c._rule = cap.FinalRule(0.05)
    c._provider._platform_controller.active_speaker = "switchboard self-test speaker"

    async def no_panel() -> list[Any]:
        raise RuntimeError("Participants button not found or not visible.")

    c._provider.get_participants = no_panel
    ts = Transcript()
    await c.start(Clock(), ts, Bus())
    await c.wait_decided(5)
    for _ in range(200):
        if ts.segments:
            break
        await asyncio.sleep(0.02)
    await c.stop()
    assert [(s.text, s.speaker) for s in ts.segments] == [
        ("Hey Codex, run the tests", "switchboard self-test speaker")
    ]
    assert c._name_for("AB") is None  # nobody speaking has those initials: unknown, not guessed


def test_a_row_the_caller_ignores_does_not_count_as_changing() -> None:
    """The no-speech signal asks `changing` with the bot's own row ignored. Fails if an ignored row still
    counts, or if a row that is not ignored stops counting."""
    rule = cap.FinalRule()
    rule.feed([("ST|a", "Sure, running them"), ("AB|b", "Hello")], 0)
    own = lambda key: key.startswith("ST")  # noqa: E731
    assert rule.changing(0.1, 0.6) is True
    assert rule.changing(0.1, 0.6, own) is True  # the person's row is still speech
    rule.feed([("ST|a", "Sure, running them now"), ("AB|b", "Hello")], 1.0)
    assert rule.changing(1.1, 0.6) is True
    assert rule.changing(1.1, 0.6, own) is False  # only the bot's row changed: quiet


class Changing(Page):
    """A box whose one row grows on every read, as a caption does while someone speaks."""

    def __init__(self, initials: str) -> None:
        super().__init__([None])
        self.initials, self.n = initials, 0

    async def evaluate(self, js: str) -> Any:
        if js is cap.READ_JS:
            self.n += 1
            return [{"i": self.initials, "c": "rgb(2, 2, 2)", "t": "words " * self.n}]
        return await super().evaluate(js)


async def _no_speech_after_reads(initials: str, reads: int) -> bool:
    c = controller(Changing(initials))
    await c.start(Clock(), Transcript(), Bus())
    await c.wait_decided(5)
    while c._page().n < reads:
        await asyncio.sleep(0.005)
    held = c.no_speech_event.is_set()
    await c.stop()
    return held


async def test_the_bots_own_caption_never_clears_the_no_speech_signal_but_a_persons_does(
    joinly_types: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zoom captions the bot's own speech, and joinly stops the bot's voice when `no_speech_event` is clear.
    While only the bot's row changes the event stays set; when a person's row changes it clears. Fails if the
    bot interrupts itself (#801)."""
    monkeypatch.setattr(cap, "POLL_S", 0.01)
    assert await _no_speech_after_reads("S(", 20) is True  # the bot's own row, growing
    assert await _no_speech_after_reads("AB", 20) is False  # a person's row, growing


def test_a_new_sentence_that_ends_like_the_last_one_sent_is_not_dropped() -> None:
    """After "two plus two?" was sent, "Bob, what's two plus two?" ends with the same
    words. Fails if the tail of what was sent is taken as a match in the middle of the new text, which drops
    the whole sentence (#801)."""
    r = cap.FinalRule()
    k = "|"
    out: list[str] = []
    for t, text in [
        (0, "Claude, what's"),
        (2.1, "Claude, what's"),
        (3, "Claude, what's two plus two?"),
        (5.1, "Claude, what's two plus two?"),
        (10, "Bob, what's two plus two?"),
        (12.1, "Bob, what's two plus two?"),
    ]:
        out += [f.text for f in r.feed([(k, text)], t)]
    assert out == ["Claude, what's", "two plus two?", "Bob, what's two plus two?"]


def test_a_sentence_ending_in_a_stop_settles_sooner_than_one_without() -> None:
    """Text that ends a sentence is a pause after SENTENCE_QUIET_S (0.8 s); text with no stop keeps QUIET_S
    (2 s). Fails if "?" waits the full two seconds or an unfinished sentence is cut at 0.8 s (#801)."""
    ended = [(0, "Bob, what's the capital of France?")]
    assert run([*ended, (0.7, ended[0][1])]) == []
    assert run([*ended, (0.8, ended[0][1])]) == ["Bob, what's the capital of France?"]
    open_ = [(0, "Bob, what's the capital of")]
    assert run([*open_, (1.9, open_[0][1])]) == []
    assert run([*open_, (2.0, open_[0][1])]) == ["Bob, what's the capital of"]


def _held(reads: list[tuple[float, list[tuple[str, str]]]]) -> list[tuple[float, str, str]]:
    """Run reads through FinalRule and PauseHold; what comes out, with the time it came out."""
    rule, hold = cap.FinalRule(), cap.PauseHold()
    out = []
    for t, entries in reads:
        for f in hold.step(rule.feed(entries, t), entries, t):
            out.append((t, f.key, f.text))
    return out


def test_finished_sentences_are_held_until_the_speaker_pauses_and_come_out_as_one() -> None:
    """Two sentences, the first final because the second began: nothing comes out then, and at the pause both
    come out as one line. Fails if the first goes out alone (the bridge would post two lines, or wait for
    more) (#801)."""
    k = "|"
    got = _held(
        [
            (0, [(k, "Bob, can you help?")]),
            (0.5, [(k, "Bob, can you help? What is two")]),
            (1.0, [(k, "Bob, can you help? What is two plus two?")]),
            (1.5, [(k, "Bob, can you help? What is two plus two?")]),
            (1.8, [(k, "Bob, can you help? What is two plus two?")]),
        ]
    )
    assert got == [(1.8, k, "Bob, can you help? What is two plus two?")]


def test_a_speaker_change_a_departure_and_a_long_run_release_what_is_held_at_once() -> None:
    """Held sentences are released when another speaker's text changes, when their speaker leaves the box, and
    after MAX_HOLD_S without a pause. Fails if a held line waits for a pause that the speaker never makes."""
    a, b = "AB|x", "CD|y"
    got = _held([(0, [(a, "One.")]), (0.5, [(a, "One. Two")]), (0.6, [(a, "One. Two"), (b, "Hi")])])
    assert got == [(0.6, a, "One.")]
    left = _held([(0, [(a, "One.")]), (0.5, [(a, "One. Two")]), (0.6, [(b, "Hi")])])
    assert [(k, t) for _, k, t in left] == [(a, "One. Two")]
    long_run = [(i * 0.5, [(a, " ".join(f"S{n}." for n in range(i + 1)))]) for i in range(40)]
    assert _held(long_run)[0][0] < 9
