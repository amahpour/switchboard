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

    async def evaluate(self, js: str, arg: Any = None) -> Any:
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

    async def evaluate(self, js: str, arg: Any = None) -> Any:
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


@pytest.mark.parametrize("initials", ["", "S("])
async def test_self_caption_stays_quiet_as_initials_appear(initials: str) -> None:
    """A bot caption must leave the barge-in signal quiet even on its first frame without initials."""
    c = controller(Page([None]))
    await c._take([{"i": "", "c": "rgb(2, 2, 2)", "t": "First", "own_active": True}], 1.0)
    first_frame_quiet = not c._rule.changing(1.0, 0.6, c._is_own_key)
    if initials:
        await c._take([{"i": initials, "c": "rgb(2, 2, 2)", "t": "First words"}], 1.25)
    assert first_frame_quiet
    assert not c._rule.changing(1.25, 0.6, c._is_own_key)


async def test_playback_interruption_logs_row_metadata_once(caplog: pytest.LogCaptureFixture) -> None:
    """A human can still interrupt playback. The transition log gives row metadata without spoken words."""
    c = controller(Page([None]))
    c.attach(c._provider, c._bot, lambda: True)
    with caplog.at_level("INFO"):
        await c._take([{"i": "AB", "c": "rgb(1, 2, 3)", "t": "private phrase", "own_active": True}], 1.0)
        await c._take([{"i": "AB", "c": "rgb(1, 2, 3)", "t": "private phrase again"}], 1.25)
    assert not c.no_speech_event.is_set()
    assert caplog.text.count("no-speech cleared during playback") == 1
    assert "initials='AB' colour='rgb(1, 2, 3)' label_length=0 picture='' words=2" in caplog.text
    assert "private phrase" not in caplog.text


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


# ------------------------------------------------------------------ a speaker with a picture (#810)
def _speakers_of(c: Any, keys: list[str]) -> list[Any]:
    ts = Transcript()
    c._clock, c._transcript, c._events = Clock(), ts, Bus()
    c._emit([cap.Final(k, "Hello there", 0.0, 1.0) for k in keys])
    return [s.speaker for s in ts.segments]


def test_a_row_with_a_picture_and_a_label_is_the_one_participant_with_that_name(joinly_types: None) -> None:
    """A picture has no initials; Zoom may put the name on the image. The label names the speaker when it is
    exactly one participant (case and spacing aside). Fails if such a row stays "call" (no speaker), or if the
    label of the bot itself is posted as a line."""
    c = controller(Page([None]))
    c._participants = ["Alice Brown", "bob smith"]
    assert _speakers_of(c, ["|rgb(1, 1, 1)|alice  brown", "|rgb(1, 1, 1)|switchboard (transcribing)"]) == [
        "Alice Brown"
    ]  # the second is the bot's own voice: dropped


def test_two_speakers_with_pictures_are_two_keys_and_two_names() -> None:
    """Both rows have empty initials and the same colour; only the label tells them apart. Fails if the key
    leaves the label out (the two would be one speaker, and their text one line)."""
    rule = cap.FinalRule()
    a, b = "|rgb(1, 1, 1)|alice brown", "|rgb(1, 1, 1)|bob smith"
    out = rule.feed([(a, "One."), (b, "Two.")], 0) + rule.feed([(a, "One."), (b, "Two.")], 2.1)
    assert sorted((f.key, f.text) for f in out) == [(a, "One."), (b, "Two.")]
    assert [cap.key_parts(f.key)[1] for f in out] == ["alice brown", "bob smith"]


async def test_a_picture_row_key_holds_its_label_and_a_row_with_initials_does_not(
    joinly_types: None,
) -> None:
    """`_take` puts the label in the key only for a row with no initials, cut to 80 characters with the key's
    separator removed. Fails if a label is trusted next to real initials, is not cut, or can split the key."""
    c = controller(Page([None]))
    seen: list[list[tuple[str, str]]] = []
    c._rule = types.SimpleNamespace(feed=lambda e, n: seen.append(e) or [], changing_rows=lambda *a: iter(()))
    await c._take(
        [
            {"i": "", "c": "rgb(1, 1, 1)", "n": "al|ice " + "x" * 200, "t": "hi"},
            {"i": "BS", "c": "rgb(2, 2, 2)", "n": "someone else", "t": "yo"},
        ],
        0.0,
    )
    keys = [k for k, _ in seen[0]]
    assert cap.key_parts(keys[0]) == ("", ("al ice " + "x" * 200)[:80])
    assert keys[1] == "BS|rgb(2, 2, 2)||"


def test_a_label_that_matches_nobody_or_two_people_names_no_one(joinly_types: None) -> None:
    """The label is a claim: "Profile picture" or a name no one has is not a speaker, and a name two people
    share is not guessed. Fails if either line gets a speaker."""
    c = controller(Page([None]))
    c._participants = ["alice brown", "Alice Brown", "bob smith"]
    assert _speakers_of(c, ["|c|alice brown", "|c|Profile picture", "|c|carol"]) == [None, None, None]


def test_with_no_participant_list_the_label_counts_only_when_the_active_speaker_is_that_name(
    joinly_types: None,
) -> None:
    """The panel could not be read (no list). A label alone is not trusted, since it may be "avatar"; it
    counts when the call's own active-speaker name, sampled while that row changed, is the same name. Fails
    if a label is used with no support, or if an active speaker with another name is used for the row."""
    c = controller(Page([None]))
    plat = c._provider._platform_controller
    key = "|rgb(1, 1, 1)|alice brown"
    plat.active_speaker = None
    c._sample_active([(key, "Hello")])
    assert c._name_for(*cap.key_parts(key)) is None  # a label nothing supports
    plat.active_speaker = "Carol"
    c._sample_active([(key, "Hello there")])
    assert c._name_for(*cap.key_parts(key)) is None  # the call shows someone else
    plat.active_speaker = "Alice Brown"
    for t in ("Hello there you", "Hello there you all"):
        c._sample_active([(key, t)])
    assert c._name_for(*cap.key_parts(key)) == "Alice Brown"  # the most named is the label's name now


def test_markup_is_logged_with_its_names_and_classes_but_no_text_or_attribute_values() -> None:
    """The diagnostic shows where Zoom puts a name without the name: tag, attribute and class names stay;
    text and every other attribute value become their length. Fails if "Alice Brown" or a src survives."""
    html = (
        '<div class="zmu-data-selector-item__icon bg-1" aria-label="Alice Brown" title=\'Alice\'>'
        '<img alt="Alice Brown" src="https://example.com/a.png">Alice Brown</div>'
    )
    got = cap.redact_markup(html)
    assert "Alice" not in got and "example.com" not in got
    assert got == (
        '<div class="zmu-data-selector-item__icon bg-1" aria-label="[11]" title="[5]">'
        '<img alt="[11]" src="[25]">[11]</div>'
    )
    assert len(cap.redact_markup("<p>" * 500)) == 600


async def test_a_row_without_initials_is_logged_once_per_call_and_the_active_speaker_count_at_the_end(
    joinly_types: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """For the live check: once, the redacted markup of a row with no initials; at the end, how often the
    call's active speaker was named. Fails if the log repeats, holds the name, or lacks the count."""
    caplog.set_level("INFO", logger=cap.logger.name)
    c = controller(Page([None]))
    c._provider._platform_controller.active_speaker = "Alice Brown"
    row = {
        "i": "",
        "c": "x",
        "n": "",
        "t": "hi",
        "h": '<span class="icon"><img alt="Alice Brown"></span>\0<div>Alice</div>',
    }
    await c._take([row], 0.0)
    await c._take([{**row, "t": "hi there"}], 1.0)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text.count("a row with no initials") == 1
    assert '<img alt="[11]">' in text and "Alice" not in text
    c._clock, c._transcript, c._events = Clock(), Transcript(), Bus()
    task = asyncio.create_task(c._run())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert "active_speaker was named in 2 of 2 samples" in "\n".join(r.getMessage() for r in caplog.records)


# ------------------------------------------------- a picture with no name on the row (#810, found live)
PIC_A, PIC_B = "a" * 12, "b" * 12


def _pic_key(pic: str, colour: str = "rgb(1, 1, 1)") -> str:
    return f"|{colour}||{pic}"


def _sample(c: Any, key: str, active: str | None, n: int) -> None:
    c._provider._platform_controller.active_speaker = active
    for i in range(n):
        c._sample_active([(key, f"words {i} {key}")])


def test_a_picture_row_with_no_name_is_named_by_the_active_speaker_when_it_always_agrees(
    joinly_types: None,
) -> None:
    """Live, a speaker with a picture had no name on the row, no list could be read, and the call's active
    speaker was named in 7 of 7 samples. Fails if that row stays "call" (no speaker): the old `_name_for`
    returned None for a row with no initials and no label before looking at the counts."""
    c = controller(Page([None]))
    _sample(c, _pic_key(PIC_A), "Alice Brown", 7)
    assert _speakers_of(c, [_pic_key(PIC_A)]) == ["Alice Brown"]


def test_too_few_samples_or_a_split_count_name_no_one(joinly_types: None) -> None:
    """Two samples are too few, and 3 against 2 is not sure enough (80% is the bar). Fails if a thin or split
    count names someone: the name would be a guess."""
    c = controller(Page([None]))
    _sample(c, _pic_key(PIC_A), "Alice Brown", 2)
    _sample(c, _pic_key(PIC_B), "Alice Brown", 3)
    _sample(c, _pic_key(PIC_B), "Carol", 2)
    assert _speakers_of(c, [_pic_key(PIC_A), _pic_key(PIC_B)]) == [None, None]
    _sample(c, _pic_key(PIC_A), "Alice Brown", 1)  # 3 of 3 now
    assert _speakers_of(c, [_pic_key(PIC_A)]) == ["Alice Brown"]


def test_the_bots_own_name_never_names_a_picture_row(joinly_types: None) -> None:
    """The active speaker can be the bot itself (its own voice). Fails if a picture row is named with the
    bot's name, or dropped as the bot's own voice on that evidence (it would silence a person)."""
    c = controller(Page([None]))
    own = c._bot
    assert own
    _sample(c, _pic_key(PIC_A), own.upper(), 9)
    assert _speakers_of(c, [_pic_key(PIC_A)]) == [None]
    assert not c._is_own_key(_pic_key(PIC_A))


def test_two_pictures_and_a_picture_and_initials_never_share_counts(joinly_types: None) -> None:
    """Counts are kept for each picture id, apart from every other picture and from any initials. Fails if
    two people with pictures are given one name, or a row with initials "AB" adds to a picture's counts."""
    c = controller(Page([None]))
    _sample(c, _pic_key(PIC_A), "Alice Brown", 4)
    _sample(c, _pic_key(PIC_B), "Bob Smith", 4)
    _sample(c, "AB|rgb(1, 1, 1)||", "Zed", 9)
    assert _speakers_of(c, [_pic_key(PIC_A), _pic_key(PIC_B), _pic_key(PIC_A, "rgb(9, 9, 9)")]) == [
        "Alice Brown",
        "Bob Smith",
        "Alice Brown",
    ]  # the colour is not the picture: the id is
    assert c._active_id("", "", PIC_A) != c._active_id("", "", PIC_B) != c._active_id("AB", "", PIC_A)
    assert set(c._active) == {c._active_id("", "", PIC_A), c._active_id("", "", PIC_B), "AB"}


async def test_the_picture_address_is_never_logged_and_the_key_holds_only_its_id(
    joinly_types: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The row's markup (with the avatar's src) reaches Python for the one-time diagnostic, and the id is
    all the key keeps. Fails if the address, or a name, shows in any log record, or the key holds more than
    12 hex digits of the id (or an id for a row that has initials)."""
    caplog.set_level("DEBUG", logger=cap.logger.name)
    src = "https://example.com/avatar/secret-person-123.png"
    c = controller(Page([None]))
    seen: list[list[tuple[str, str]]] = []
    c._rule = types.SimpleNamespace(feed=lambda e, n: seen.append(e) or [], changing_rows=lambda *a: iter(()))
    row = {
        "i": "",
        "c": "x",
        "n": "",
        "p": PIC_A + "ff",
        "t": "hi",
        "h": f'<span><img alt="" src="{src}"></span>\0<div/>',
    }
    await c._take([row, {**row, "i": "AB"}], 0.0)
    assert [cap.key_pic(k) for k, _ in seen[0]] == [PIC_A, ""]
    text = "\n".join(r.getMessage() + str(r.args) for r in caplog.records)
    assert "secret-person" not in text and "example.com" not in text
    assert 'src="[' in text  # the diagnostic still ran, with the value as a length
    c._clock, c._transcript, c._events = Clock(), Transcript(), Bus()
    c._emit([cap.Final(seen[0][0][0], "hi", 0.0, 1.0)])
    assert "secret-person" not in "\n".join(r.getMessage() for r in caplog.records)


# ------------------------------------- a row with no identity yet, during playback (#852, found live)

# The row the live call recorded when the bot cut its own line: a picture, no initials, a transparent
# colour, no label (hash shortened to the key's 12 hex digits).
LIVE_OWN_ROW = {"i": "", "c": "rgba(0, 0, 0, 0)", "n": "", "p": "137b8afd3423", "t": "one two three"}


def _playing(c: Any) -> Any:
    c.attach(c._provider, c._bot, lambda: True)
    return c


async def test_an_anonymous_first_row_of_the_bots_own_does_not_cut_its_playback(joinly_types: None) -> None:
    """The bot's own first caption row can show a picture with no initials and no label, before the call has
    named it. While the bot plays, such a row must not count as a person talking over it (#852). Fails on a
    filter that only knows the bot's initials: the row clears the no-speech signal and playback stops."""
    c = _playing(controller(Page([None])))
    await c._take([dict(LIVE_OWN_ROW)], 1.0)
    assert c.no_speech_event.is_set()


async def test_a_person_with_initials_still_cuts_playback(joinly_types: None) -> None:
    """Initials name a row at once, so a person with them talking over the bot stops it. Fails if the fix
    waits for anything more than an identity."""
    c = _playing(controller(Page([None])))
    await c._take([{"i": "AB", "c": "rgb(1, 2, 3)", "t": "hold on"}], 1.0)
    assert not c.no_speech_event.is_set()


async def test_a_picture_person_is_heard_once_the_active_speaker_names_them(joinly_types: None) -> None:
    """On Zoom a person with a profile picture has no initials and no label (#810): only the call's
    active-speaker detection names them. While the bot plays, their row does not count until that evidence is
    enough (PIC_MIN_SAMPLES), then it does. Fails if a picture row can never interrupt (a person is unheard)
    or if it counts from its first frame (the bug)."""
    c = _playing(controller(Page([None])))
    c._provider._platform_controller.active_speaker = "Alice Brown"
    for n in range(1, cap.PIC_MIN_SAMPLES):
        await c._take([{**LIVE_OWN_ROW, "t": "word " * n}], float(n))
        assert c.no_speech_event.is_set()  # not yet named
    await c._take([{**LIVE_OWN_ROW, "t": "word " * cap.PIC_MIN_SAMPLES}], float(cap.PIC_MIN_SAMPLES))
    assert not c.no_speech_event.is_set()


async def test_an_anonymous_row_that_resolves_to_the_bots_own_never_cuts_playback(joinly_types: None) -> None:
    """The bot's row starts anonymous (picture, nothing else) while the call's active speaker is the bot
    itself, then shows its initials. At no step may it clear the no-speech signal. Fails if the picture row,
    named by the bot's own name, counts as a person, or if the later initials row does."""
    c = _playing(controller(Page([None])))
    c._provider._platform_controller.active_speaker = c._bot
    for n in range(1, cap.PIC_MIN_SAMPLES + 3):
        await c._take([{**LIVE_OWN_ROW, "t": "word " * n}], float(n))
        assert c.no_speech_event.is_set()
    await c._take([{"i": "S(", "c": "rgb(2, 2, 2)", "t": "word " * 9}], 10.0)
    assert c.no_speech_event.is_set()
