"""Pure turn-taking for spoken call lines (DESIGN.md §52.2, #697, #713).

The broker's bridge and hosted-call polls share one slot for speech in progress, and one line
may wait behind it. Times come from their caller's clock; no I/O, sleeping or speech service
lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GAP_S = 1.5
STALE_S = 60.0
INTERVAL_S = 30.0  # between two starts of one agent's lines
ANSWER_INTERVAL_S = 2.0  # a line that answers a question put to its speaker (#713)
CHAIN_MAX = 4  # agent lines in a row, until a person speaks on the call or in the room (#713)
ACTIVE_MAX_S = 90.0  # a 600-character line is under a minute; an end never reported frees the slot then
MAX_ID = 2**63 - 1
BUSY_NOTICE = "Another @call line is already waiting; this line was not spoken."
CHAIN_NOTICE = (
    f"Agents have spoken {CHAIN_MAX} lines in a row; this @call line was not spoken. "
    "It is spoken again once a person speaks on the call or in the room."
)
STALE_NOTICE = "A queued @call line waited 60 seconds and was dropped instead of being spoken late."


@dataclass(frozen=True)
class Line:
    id: int
    text: str
    queued_at: float
    membership_id: int | None
    answering: bool = False
    voice: str | None = None  # the Kokoro voice of its speaker, or None: the session's (§52.6)


@dataclass
class Turns:
    """One call's speech slot. ``pending`` is the line waiting for its turn (at most one) and ``active``
    the line being spoken; a line may wait while another is spoken. After ``offer`` returns False,
    ``refusal`` says why: ``busy`` (a line is already waiting) or ``chain`` (``CHAIN_MAX`` agent lines
    in a row)."""

    pending: Line | None = None
    active: Line | None = None
    last_heard: float | None = None
    last_started: float | None = None
    started_by: dict[int | None, float] = field(default_factory=dict)  # each agent's last start
    chain: int = 0
    refusal: str = "busy"
    sequence: int = 0

    def hear(self, now: float) -> None:
        """Only a new participant segment moves this clock, never a replay or the bot's voice. A person
        speaking on the call also ends the run of agent lines."""
        self.last_heard = now
        self.chain = 0

    @property
    def refusal_notice(self) -> str:
        return CHAIN_NOTICE if self.refusal == "chain" else BUSY_NOTICE

    def person_posted(self) -> None:
        """A person wrote in the room: the run of agent lines ends, as when one speaks on the call."""
        self.chain = 0

    def offer(
        self,
        text: str,
        now: float,
        membership_id: int | None = None,
        *,
        answering: bool = False,
        voice: str | None = None,
    ) -> bool:
        """Queue ``text``. ``answering``: the broker knows this agent's last batch put a question to it.
        Never decided from the text."""
        self._release_lost(now)
        if self.chain >= CHAIN_MAX:
            self.refusal = "chain"
            return False
        if self.pending is not None:
            self.refusal = "busy"
            return False
        self.sequence += 1
        self.chain += 1
        self.pending = Line(self.sequence, text, now, membership_id, answering, voice)
        return True

    def ready_at(self) -> float | None:
        """When the waiting line may start; while another line is spoken, no sooner than the 90 s backstop."""
        p = self.pending
        if p is None:
            return None
        if p.answering:
            wait_for = self.last_started + ANSWER_INTERVAL_S if self.last_started is not None else None
        else:
            own = self.started_by.get(p.membership_id)
            wait_for = own + INTERVAL_S if own is not None else None
        at = max(
            p.queued_at,
            self.last_heard + GAP_S if self.last_heard is not None else p.queued_at,
            wait_for if wait_for is not None else p.queued_at,
        )
        if self.active is not None and self.last_started is not None:
            at = max(at, self.last_started + ACTIVE_MAX_S)
        return at

    def expire(self, now: float) -> Line | None:
        if self.pending is None or now - self.pending.queued_at < STALE_S:
            return None
        return self.clear_pending()

    def take(self, now: float) -> Line | None:
        self._release_lost(now)
        at = self.ready_at()
        if at is None or now < at or self.pending is None or now - self.pending.queued_at >= STALE_S:
            return None
        if self.active is not None:
            return None
        line = self.clear_pending()
        if line is None:
            return None
        self.active = line
        self.last_started = now
        self.started_by[line.membership_id] = now
        return line

    def finish(self, line_id: int) -> bool:
        if type(line_id) is not int or self.active is None or self.active.id != line_id:
            return False
        self.active = None
        return True

    def _release_lost(self, now: float) -> None:
        """A relay that crashed mid-line, or lost its receipt, must not silence the call for good."""
        if (
            self.active is not None
            and self.last_started is not None
            and now - self.last_started >= ACTIVE_MAX_S
        ):
            self.active = None

    def clear_pending(self) -> Line | None:
        dropped, self.pending = self.pending, None
        return dropped
