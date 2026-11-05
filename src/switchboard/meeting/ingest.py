"""What becomes of a call's finished line, whoever heard it (DESIGN.md §52, §54).

A call's segments are merged into lines (``lines.py``). Each line is then checked and posted the same way
whether the broker's own bot heard it (``bridge.py``) or a person's machine pushed it (``hosted.py``): the
hint list a transcriber writes back is dropped, a spoken name picks out who was called out, the speaker is cut
and treated as text, and the line is posted as a ``meeting`` message, which agents never read as their own
person's. So every source gets the same fixes, and there is one place to change them.

These are plain functions over a line and a few names. Nothing here waits: the callers own the clock, the
queue's pacing and any picture to take first.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from switchboard.envelope import clean
from switchboard.meeting.lines import Line
from switchboard.meeting.names import Match, match

LINE_GAP_S = 1.0  # the room posts at most one call line a second
QUEUE_MAX = 50  # lines waiting for their turn; past it the oldest is dropped, with one warning
SENDER_MAX = 40  # the speaker's name, cut: it is a claim from outside
LINE_MAX = 1000  # characters of one posted line
OVERFLOW_NOTICE = "The call is coming in faster than the room can show. Some lines were dropped."


def listed(names: Sequence[str]) -> str:
    """``a``, ``a and b`` or ``a, b and c``: names, as a sentence says them."""
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + " and " + names[-1]


def enqueue(queue: deque[Line], line: Line, warned: bool, on_overflow: Callable[[], None]) -> bool:
    """Put ``line`` last in ``queue``. Past ``QUEUE_MAX`` the oldest goes, and ``on_overflow`` is called
    the first time in a burst. Returns whether the burst has been warned of."""
    queue.append(line)
    if len(queue) <= QUEUE_MAX:
        return warned
    queue.popleft()
    if not warned:
        on_overflow()
    return True


@dataclass(frozen=True)
class Prepared:
    """A line that will be posted: its text as posted, the one member it addressed (or none), and what
    the match said (an ambiguous name is told to the room after the line)."""

    text: str
    mentions: list[str]
    found: Match


def prepare(text: str, members: Sequence[str]) -> Prepared | None:
    """The text of a line as the room should read it, or None when nothing is to be posted: no words
    left, or only the transcriber's hint list written back, which nobody said (#616). An echoed lead-in
    is taken off and the real words kept (#602)."""
    text = clean(text).strip()
    if not text:
        return None
    found = match(text, members)
    if found.echo:
        return None
    text = found.text
    mentions = [found.name] if found.outcome == "one" and found.name else []
    return Prepared(text, mentions, found)


def post(
    state: Any,
    room_id: int,
    line: Line,
    prepared: Prepared,
    *,
    meeting_id: int | None = None,
    host: str | None = None,
) -> Any:
    """Post ``prepared`` as a call line by ``line``'s speaker (cut to ``SENDER_MAX``, or ``call`` when it
    gave none). ``meeting_id`` and ``host`` are a hosted call and the machine that heard the line: it is
    stored with them. An ambiguous name is told to the room, after the line."""
    sender = clean(line.speaker).strip()[:SENDER_MAX] if line.speaker else ""
    posted = state.service.post_meeting_line(
        room_id,
        sender_name=sender or "call",
        text=prepared.text[:LINE_MAX],
        mentions=prepared.mentions,
        meeting_id=meeting_id,
        sender_host=host,
    )
    found = prepared.found
    if found.outcome == "ambiguous" and found.heard and found.names:
        heard = found.heard.replace('"', "'")[:40]
        who = listed(found.names)
        state.service.notice_in(
            room_id,
            f'Someone on the call said "{heard}", and this room has {who}. Say the number.',
        )
    return posted
