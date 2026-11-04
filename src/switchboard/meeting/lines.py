"""Merge a call's transcript segments into lines (DESIGN.md §52).

Segments arrive in batches about every 3 seconds, and one spoken request is often
two or three of them. This keeps a call-out and the rest of its sentence in one
line. The clock is an argument: no ``time`` call, so a test steps it.
"""

from __future__ import annotations

from dataclasses import dataclass

PAUSE_S = 2.0  # audio-clock gap that ends a line
WALL_S = 2.5  # wall-clock silence that ends a line
CAP = 1000  # characters in one line


@dataclass(frozen=True)
class Segment:
    text: str
    start: float
    end: float
    speaker: str | None = None
    role: str = "participant"


@dataclass(frozen=True)
class Line:
    text: str
    speaker: str | None
    start: float
    end: float


class Assembler:
    """Consecutive participant segments, until a pause or the length cap.

    ``role`` other than ``participant`` is the bot's own speech and is skipped.
    A wall-clock pause is decided by ``poll``; a new segment extends the line
    even when the batches were a few seconds apart, as long as the audio clock
    has no gap.
    """

    def __init__(self, *, pause_s: float = PAUSE_S, wall_s: float = WALL_S, cap: int = CAP):
        self.pause_s = pause_s
        self.wall_s = wall_s
        self.cap = cap
        self._parts: list[Segment] = []
        self._last_wall: float | None = None

    def add(self, seg: Segment, now: float) -> list[Line]:
        """Take one segment. Returns any line the segment closed."""
        if seg.role != "participant":
            return self.poll(now)
        text = " ".join((seg.text or "").split())
        if not text:
            return self.poll(now)
        if len(text) > self.cap:
            text = text[: self.cap]
        out: list[Line] = []
        if self._parts:
            prev = self._parts[-1]
            gap = seg.start - prev.end
            speaker_break = bool(prev.speaker and seg.speaker and prev.speaker != seg.speaker)
            if gap >= self.pause_s or speaker_break or len(self._text()) + 1 + len(text) > self.cap:
                out.append(self._emit())
        self._parts.append(
            Segment(text=text, start=seg.start, end=seg.end, speaker=seg.speaker, role=seg.role)
        )
        self._last_wall = now
        if len(self._text()) >= self.cap:
            out.append(self._emit())
        return out

    def poll(self, now: float) -> list[Line]:
        """The open line, once the wall clock has been quiet, else nothing."""
        if self._parts and self._last_wall is not None and now - self._last_wall >= self.wall_s:
            return [self._emit()]
        return []

    def flush(self) -> list[Line]:
        """The open line now. The call ended, so it does not wait out the pause."""
        if not self._parts:
            return []
        return [self._emit()]

    def _text(self) -> str:
        return " ".join(p.text for p in self._parts)

    def _speaker(self) -> str | None:
        names = [p.speaker for p in self._parts if p.speaker]
        if names and len(set(names)) == 1:
            return names[0]
        return None

    def _emit(self) -> Line:
        line = Line(
            text=self._text(),
            speaker=self._speaker(),
            start=self._parts[0].start,
            end=self._parts[-1].end,
        )
        self._parts = []
        self._last_wall = None
        return line
