"""Pure turn-taking for spoken call lines (DESIGN.md §52.2, #697).

The broker's bridge and hosted-call polls share one slot, including speech in progress.
Times come from their caller's clock; no I/O, sleeping or speech service lives here.
"""

from __future__ import annotations

from dataclasses import dataclass

GAP_S = 1.5
STALE_S = 60.0
INTERVAL_S = 30.0
MAX_ID = 2**63 - 1
BUSY_NOTICE = "Another @call line is waiting or being spoken; this line was not spoken."
STALE_NOTICE = "A queued @call line waited 60 seconds and was dropped instead of being spoken late."


@dataclass(frozen=True)
class Line:
    id: int
    text: str
    queued_at: float
    membership_id: int | None


@dataclass
class Turns:
    pending: Line | None = None
    active: Line | None = None
    last_heard: float | None = None
    last_started: float | None = None
    sequence: int = 0

    def hear(self, now: float) -> None:
        """Only a new participant segment moves this clock, never a replay or the bot's voice."""
        self.last_heard = now

    def offer(self, text: str, now: float, membership_id: int | None = None) -> bool:
        if self.pending is not None or self.active is not None:
            return False
        self.sequence += 1
        self.pending = Line(self.sequence, text, now, membership_id)
        return True

    def ready_at(self) -> float | None:
        if self.pending is None:
            return None
        return max(
            self.pending.queued_at,
            self.last_heard + GAP_S if self.last_heard is not None else self.pending.queued_at,
            self.last_started + INTERVAL_S if self.last_started is not None else self.pending.queued_at,
        )

    def expire(self, now: float) -> Line | None:
        if self.pending is None or now - self.pending.queued_at < STALE_S:
            return None
        return self.clear_pending()

    def take(self, now: float) -> Line | None:
        at = self.ready_at()
        if at is None or now < at or self.pending is None or now - self.pending.queued_at >= STALE_S:
            return None
        self.active = self.clear_pending()
        self.last_started = now
        return self.active

    def finish(self, line_id: int) -> bool:
        if type(line_id) is not int or self.active is None or self.active.id != line_id:
            return False
        self.active = None
        return True

    def clear_pending(self) -> Line | None:
        dropped, self.pending = self.pending, None
        return dropped
