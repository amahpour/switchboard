"""What becomes of a call's finished line, whoever heard it (#623, DESIGN.md §52, §54).

``ingest`` is the one place the broker's own bot (``bridge.py``) and a call hosted on a person's machine
(``hosted.py``) both post a line through, so these are the rules every source gets: the same hint-list echo
dropped, the same name picked out, the same cut. Pure but for a stand-in service that records the posts.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

from switchboard.meeting import ingest
from switchboard.meeting.lines import Line

MEMBERS = ["claude-1", "codex-1"]


class Service:
    """Records what ``ingest.post`` asks the room service to do."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self.notices: list[str] = []

    def post_meeting_line(self, room_id: int, **kw: Any) -> dict[str, Any]:
        self.lines.append({"room_id": room_id, **kw})
        return kw

    def notice_in(self, room_id: int, text: str) -> None:
        self.notices.append(text)


def line(text: str, speaker: str | None = "bob") -> Line:
    return Line(text=text, speaker=speaker, start=0.0, end=1.0)


def post(text: str, members: list[str] = MEMBERS, **kw: Any) -> tuple[Service, Any]:
    svc = Service()
    prepared = ingest.prepare(text, members)
    assert prepared is not None
    ingest.post(
        SimpleNamespace(service=svc), 7, line(text, **{"speaker": kw.pop("speaker", "bob")}), prepared, **kw
    )
    return svc, prepared


def test_a_call_out_names_the_one_member_it_addressed_and_other_talk_names_nobody() -> None:
    """Both sources decide who was called out the same way: the name at the start of a line is an address,
    the name inside a sentence is not. Fails if the match moved out of the shared path and one source
    stopped picking out names."""
    one = ingest.prepare("Claude one, can you check the upload limit?", MEMBERS)
    assert one is not None and one.mentions == ["claude-1"]
    assert ingest.prepare("I asked Claude yesterday", MEMBERS).mentions == []  # type: ignore[union-attr]


def test_a_line_that_is_only_the_hint_list_written_back_is_not_posted() -> None:
    """Whisper sometimes writes its hotwords back as speech (#616). Nobody said that, so there is no line
    for any source. Fails if the echo check is not part of what both sources share."""
    assert ingest.prepare("Claude switchboard", MEMBERS) is None
    assert ingest.prepare("   \x1b  ", MEMBERS) is None  # nothing left once it is cleaned


def test_an_echoed_lead_in_is_taken_off_and_the_real_words_are_posted() -> None:
    """#602: "Claude-1 Claude switchboard Claude one can you hear me?" is a call-out with a hint list in
    front of it. The room reads only what was said, and the member is still addressed. Fails if the whole
    line is dropped or the echo is kept."""
    got = ingest.prepare("Claude-1 Claude switchboard Claude one can you hear me?", MEMBERS)
    assert got is not None
    assert got.text == "Claude one can you hear me?" and got.mentions == ["claude-1"]


def test_a_line_is_posted_with_its_speaker_cut_to_forty_characters_and_its_text_to_a_thousand() -> None:
    """A speaker's name is a claim from outside: cut, cleaned, and ``call`` when there is none. Fails if
    either bound moves, or the name keeps control characters."""
    svc, _ = post("hello " * 400, speaker="s" * 90 + "\x1b[2J")
    sent = svc.lines[0]
    assert sent["sender_name"] == "s" * 40
    assert len(sent["text"]) == ingest.LINE_MAX == 1000
    nobody = Service()
    prepared = ingest.prepare("hello", MEMBERS)
    assert prepared is not None
    ingest.post(SimpleNamespace(service=nobody), 7, line("hello", speaker=None), prepared)
    assert nobody.lines[0]["sender_name"] == "call"


def test_the_low_level_post_passes_on_provenance_when_supplied() -> None:
    """The low-level helper forwards supplied provenance and invents none when called without it.
    Production sources enter receive, which binds either kind of call before using this helper.
    Fails if supplied provenance is dropped on the way to the service.
    """
    hosted, _ = post("hello", meeting_id=9, host="alice-laptop")
    assert (hosted.lines[0]["meeting_id"], hosted.lines[0]["sender_host"]) == (9, "alice-laptop")
    server, _ = post("hello")
    assert (server.lines[0]["meeting_id"], server.lines[0]["sender_host"]) == (None, None)


def test_an_ambiguous_name_is_told_to_the_room_after_the_line() -> None:
    """Two members share "Claude": the line is posted with no mention and the room is told to say the
    number, as for the broker's own bot. Fails if the notice is not part of the shared path."""
    svc, prepared = post("Claude, what do you think?", members=["claude-1", "claude-2"])
    assert prepared.mentions == [] and svc.lines[0]["mentions"] == []
    assert (
        len(svc.notices) == 1
        and "claude-1 and claude-2" in svc.notices[0]
        and "Say the number" in svc.notices[0]
    )


def test_names_are_listed_as_a_sentence_says_them() -> None:
    assert ingest.listed([]) == "" and ingest.listed(["a"]) == "a"
    assert ingest.listed(["a", "b"]) == "a and b" and ingest.listed(["a", "b", "c"]) == "a, b and c"


def test_the_queue_drops_the_oldest_past_fifty_and_warns_once_a_burst() -> None:
    """Past ``QUEUE_MAX`` waiting lines the oldest goes and the first time only the warning fires, so a
    flood is one notice and not one per line. Fails if every drop warns, or none does."""
    queue: deque[Line] = deque()
    told: list[int] = []
    warned = False
    for i in range(ingest.QUEUE_MAX + 5):
        warned = ingest.enqueue(queue, line(str(i)), warned, lambda: told.append(1))
    assert (
        len(queue) == ingest.QUEUE_MAX
        and queue[0].text == "5"
        and queue[-1].text == str(ingest.QUEUE_MAX + 4)
    )
    assert told == [1] and warned is True
