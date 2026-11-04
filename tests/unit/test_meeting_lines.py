"""Merging a call's transcript segments into one line (DESIGN.md §52).

A spoken request arrives as two or three segments a few seconds apart, on a batch
cadence of about 3 seconds. They stay one line until the audio clock has a gap, the
wall clock has been quiet, or the line hits its length cap. The clock is passed in.
"""

from __future__ import annotations

from conftest import FakeClock

from switchboard.meeting.lines import Assembler, Segment


def _seg(
    text: str, start: float, end: float, speaker: str | None = None, role: str = "participant"
) -> Segment:
    return Segment(text=text, start=start, end=end, speaker=speaker, role=role)


def test_a_request_split_across_the_batch_cadence_is_one_line() -> None:
    """The spike's shape: "Codex number two", then "Tell me how much.", then
    "gas prices are in Los Angeles.", each batch about 3 seconds later on the wall
    clock, with no gap on the audio clock. They become one line, and not before the
    pause. Fails if each segment is posted on its own, or the line is emitted early."""
    clock = FakeClock()
    asm = Assembler()
    parts = (
        ("Codex number two", 0.0, 1.2),
        ("Tell me how much.", 1.3, 2.4),
        ("gas prices are in Los Angeles.", 2.5, 4.0),
    )
    emitted: list[str] = []
    for text, start, end in parts:
        emitted += [ln.text for ln in asm.add(_seg(text, start, end), clock.now())]
        clock.advance(3.0)
    assert emitted == []
    # the last add advanced the clock; step back onto the moment it arrived, then wait
    clock.advance(-3.0)
    assert asm.poll(clock.now()) == []
    clock.advance(2.5)
    lines = asm.poll(clock.now())
    assert [ln.text for ln in lines] == ["Codex number two Tell me how much. gas prices are in Los Angeles."]
    assert lines[0].speaker is None


def test_an_audio_gap_ends_the_line_and_the_bots_own_speech_is_skipped() -> None:
    """2 seconds of silence on the audio clock is a new turn. The bot's own speech
    (any role but participant) is not a line. Fails if a gap is glued on, or the bot
    is transcribed back into the room."""
    clock = FakeClock()
    asm = Assembler()
    asm.add(_seg("hello there", 0.0, 1.0, speaker="Ada"), clock.now())
    clock.advance(0.2)
    skipped = asm.add(_seg("I am transcribing", 1.1, 2.0, role="assistant"), clock.now())
    assert skipped == []
    clock.advance(0.2)
    lines = asm.add(_seg("a new topic", 5.0, 6.0, speaker="Ada"), clock.now())
    assert [ln.text for ln in lines] == ["hello there"]
    assert lines[0].speaker == "Ada"
    clock.advance(2.5)
    assert [ln.text for ln in asm.poll(clock.now())] == ["a new topic"]


def test_the_length_cap_splits_a_long_run() -> None:
    """A line stops at 1,000 characters, and the rest starts the next line, in order.
    Fails if a line grows past the cap or the tail is dropped."""
    clock = FakeClock()
    asm = Assembler()
    first = "a" * 600
    second = "b" * 600
    asm.add(_seg(first, 0.0, 1.0), clock.now())
    lines = asm.add(_seg(second, 1.1, 2.0), clock.now())
    assert [ln.text for ln in lines] == [first]
    clock.advance(2.5)
    assert [ln.text for ln in asm.poll(clock.now())] == [second]
    huge = asm.add(_seg("c" * 1400, 10.0, 11.0), clock.now() + 10)
    assert huge == [] or all(len(ln.text) <= 1000 for ln in huge)
    rest = asm.poll(clock.now() + 20)
    assert all(len(ln.text) <= 1000 for ln in rest)
    assert sum(len(ln.text) for ln in (*huge, *rest)) == 1000
