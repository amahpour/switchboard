"""The delivery engine: a synchronous policy core (DESIGN.md §8.2).

The engine owns every delivery decision. It reads and writes the store and
reads the clock, but does no network or process I/O and never touches
asyncio: every method returns a list of ``Action``s (Push, ResolveSink,
Notice, Snapshot) for the async ``Runner`` to carry out. That keeps every
rule unit-testable with a FakeClock.

Offers are two-phase (§8.7): a batch is *offered* when it leaves (a wait()
result, a read()/say() answer, a hook reply, a push) and *confirmed* only on
evidence from the verified session that received it; otherwise it expires
and its deliveries go back to pending. Duplicates are possible; skips are not.

M3 adds the Claude inbox push path (confirmed by the UserPromptSubmit that
carries the batch token, expired by the adapter's event rules) and
re-deliver-once on Stop; M4 the Codex push paths. M5 adds stop-time
delivery: a Cursor stop hook *parks* (a ``park`` sink the engine fills with a
follow-up), a Devin Stop hook returns a ``decision: block`` (a wake batch, or
the "call wait()" re-arm). Both are *continuations*: confirmed by the hook's
ack plus the session's next hook, per the adapter's ``continue_verdict``.
M6 adds the watchdog (an unanswered @mention is brought back as a reminder
up to ``watchdog_max`` times, then the human is told; a mention waiting on a
parked member is escalated too), the rate-limit check behind say(), and no
Cursor park while every room of the session is paused. The read-first rule
behind pass() (§24): refused while a peer message reached the member only as
a "call read()" stub.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from switchboard import envelope
from switchboard.adapters.base import Adapter
from switchboard.adapters.cursor import FOLLOWUP_RACE_S
from switchboard.adapters.devin import rearm_text as devin_rearm_text
from switchboard.clock import Clock
from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.delivery.sinks import Sink, SinkRegistry
from switchboard.models import (
    CONTINUE_PATHS,
    HOOK_PATHS,
    PULL_PATHS,
    WATCHDOG_DONE,
    Action,
    Batch,
    HookEvent,
    HookOut,
    Item,
    Membership,
    Message,
    Notice,
    Participant,
    Push,
    Release,
    ResolveSink,
    Room,
    Snapshot,
    display_room,
)
from switchboard.store import Store

log = logging.getLogger("switchboard.engine")

TURN_BOUNDARY_EVENTS = frozenset({"UserPromptSubmit", "Stop", "SessionStart", "SessionEnd", "Interrupt"})
CONFIRMING_EVENTS = frozenset({"PostToolUse", "UserPromptSubmit"})
MAX_READ = 50
PUSH_EXPIRY_WARN = 3  # consecutive push-path expiries before a warn notice (§8.7)
WAIT_PRE_FRESH_S = 30.0  # a wait()'s PreToolUse is at most this much older than the call
# statuses a stop continuation (or a Devin re-arm) set; undone if no hook ever follows
STOP_BUSY_SRCS = frozenset({"stop:followup", "stop:block", "stop:rearm"})
TIMER_EXPIRIES = frozenset({"no_ack", "no_hook", "backstop"})  # expiries no hook caused
TAINTED_GENS_MAX = 16  # Devin prompts remembered as having started a background subagent
REARM_WINDOW_S = 3600.0  # the per-participant re-arm cap's window


@dataclass
class Cont:
    """An unconfirmed stop continuation (a Cursor follow-up, a Devin Stop block)."""

    participant_id: int
    loop_count: int | None  # Cursor: the stop's loop_count when the follow-up was sent
    created_at: float = 0.0
    acked_at: float | None = None  # the hook printed it (hook.ack)


class Engine:
    def __init__(
        self,
        store: Store,
        clock: Clock,
        cfg: Config,
        adapters: dict[str, Adapter],
        sinks: SinkRegistry | None = None,
        *,
        key: bytes | None = None,
        test_mode: bool = False,
    ):
        self.store = store
        self.clock = clock
        self.cfg = cfg
        self.adapters = adapters
        self.sinks = sinks or SinkRegistry()
        # batch-token HMAC key: random per broker start, memory only (§2)
        self.key = key or secrets.token_bytes(32)
        self.test_mode = test_mode
        self.parked: dict[int, str] = {}  # membership_id -> reason
        self.parked_since: dict[int, float] = {}  # membership_id -> when this parked spell began
        self._parked_at: dict[int, tuple[int, int]] = {}  # membership_id -> (room, participant) of its spell
        self.parked_escalated: set[int] = set()  # told the human about this parked spell (§8.5)
        # (membership_id, message_id): told the human this @mention is overdue while
        # its member isn't idle (busy, waiting on approval, offline; §20)
        self.stalled_told: set[tuple[int, int]] = set()
        self.peer_marks: dict[int, int] = {}  # offered batch with chatter -> the boundary it was made at
        self.hook_acks: dict[int, tuple[str, float]] = {}  # batch_id -> (ack nonce, deadline)
        self.pull_deadlines: dict[int, float] = {}  # batch_id -> expiry (PostToolUse expected)
        self.ack_modes: dict[int, str] = {}  # participant_id -> test --ack mode
        self.continues: dict[int, Cont] = {}  # batch_id -> unconfirmed stop continuation
        self.wait_calls: dict[int, tuple[str, float]] = {}  # participant_id -> its last wait() PreToolUse
        self.pull_tool_use: dict[int, str] = {}  # batch_id -> the tool_use_id whose PostToolUse confirms it
        self.first_action: dict[int, int] = {}  # participant_id -> wake batch awaiting its first action
        # Devin: prompt ids (gens) that started a background subagent, per participant.
        # The subagent's hooks carry the prompt id it started in (F§6 5.2) and it can
        # outlive that prompt, so the taint is kept per gen, not only for the current one.
        # Memory only: a broker restart forgets it (the current prompt's flag is persisted).
        self.tainted_gens: dict[int, dict[str, float]] = {}
        self.rearm_log: dict[int, list[float]] = {}  # participant_id -> recent re-arm times
        self.rearm_busy: dict[int, float] = {}  # participant_id -> when a re-arm set it busy
        # While a hook event is processed, evaluations are collected here and run
        # once its status and context are settled (see claim_for_hook).
        self._deferred: set[int] | None = None

    # ================================================================ helpers
    @property
    def d(self) -> Any:
        return self.cfg.delivery

    def now(self) -> float:
        return self.clock.now()

    def adapter(self, p: Participant) -> Adapter:
        return self.adapter_for(p.harness, getattr(p, "host", ""))

    def adapter_for(self, harness: str, host: str = "") -> Adapter:
        """The adapter of a ``harness`` session on ``host`` ('' for this machine). A Codex
        session on another host has its own pull-only adapter (DESIGN.md §27.7): the
        Codex push paths all talk to this machine's app-server."""
        if harness == "codex" and host:
            return self.adapters.get("codex@remote") or self.adapters["unknown"]
        return self.adapters.get(harness) or self.adapters["unknown"]

    def token(self, b: Batch) -> str:
        return envelope.batch_token(self.key, b.id, b.membership_id)

    def parked_reason(self, membership_id: int) -> str | None:
        return self.parked.get(membership_id)

    # the humans (DESIGN.md §32): the owner, and on a hosted broker everyone the owner added
    def _humans(self) -> list[str]:
        return [self.cfg.human_name] + [p.name for p in self.store.people()]

    def _whose(self) -> str:
        humans = self._humans()
        return f"{humans[0]}'s" if len(humans) == 1 else "people's"

    def _a_human(self) -> str:
        humans = self._humans()
        return humans[0] if len(humans) == 1 else "a person"

    def _snap(self, room_ids: Iterable[int]) -> list[Action]:
        return [Snapshot(r) for r in sorted(set(room_ids))]

    def _fit(self, items: list[Item], room: Room, m: Membership, *, peer_inline: bool, max_chars: int,
             item_limit: int | None = envelope.ITEM_LIMIT, shrink: bool = True) -> envelope.Fit:
        return envelope.fit_batch(items, room=room.name, recipient=m.screen_name,
                                  human_name=self.cfg.human_name, peer_inline=peer_inline,
                                  max_chars=max_chars, item_limit=item_limit, shrink=shrink)

    def _render(self, b: Batch, fit: envelope.Fit, room: Room, m: Membership, *, peer_inline: bool,
                item_limit: int | None = envelope.ITEM_LIMIT, more: bool = False) -> str:
        return envelope.render_batch(fit.items, room=room.name, recipient=m.screen_name,
                                     human_name=self.cfg.human_name, token=self.token(b),
                                     peer_inline=peer_inline, more=more, item_limit=item_limit,
                                     limits=fit.limits)

    def _rooms_of(self, participant_id: int) -> list[int]:
        return [m.room_id for m in self.store.participant_memberships(participant_id)]

    def _event(self, kind: str, *, room_id: int | None = None, membership_id: int | None = None,
               participant_id: int | None = None, **data: Any) -> None:
        self.store.add_event(kind, room_id=room_id, membership_id=membership_id,
                             participant_id=participant_id, data=data)

    def _ctx(self, m: Membership) -> tuple[Participant, Room] | None:
        p = self.store.get_participant(m.participant_id)
        room = self.store.room_by_id(m.room_id)
        if p is None or room is None or not p.active:
            return None
        return p, room

    # ============================================================== evaluate
    def evaluate(self, membership_id: int) -> list[Action]:
        """Decide whether anything goes out to one member now (§8.2)."""
        if self._deferred is not None:
            self._deferred.add(membership_id)
            return []
        out = self._evaluate(membership_id)
        self._track_parked(membership_id)
        return out

    def _track_parked(self, membership_id: int) -> None:
        """Keep when a member's parked spell began (the watchdog's parked escalation), and
        write a ``parked``/``unparked`` event at each edge (``switchboard report``, §12.6)."""
        if membership_id in self.parked:
            if membership_id not in self.parked_since:
                self.parked_since[membership_id] = self.now()
                m = self.store.get_membership(membership_id)
                if m is not None:
                    self._parked_at[membership_id] = (m.room_id, m.participant_id)
                    self._event("parked", room_id=m.room_id, membership_id=membership_id,
                                participant_id=m.participant_id, reason=self.parked[membership_id])
        else:
            since = self.parked_since.pop(membership_id, None)
            self.parked_escalated.discard(membership_id)
            where = self._parked_at.pop(membership_id, None)
            if since is not None and where is not None:
                self._event("unparked", room_id=where[0], membership_id=membership_id, participant_id=where[1],
                            seconds=round(self.now() - since, 1))

    def _unpark(self, membership_id: int) -> None:
        self.parked.pop(membership_id, None)
        self._track_parked(membership_id)

    def _evaluate(self, membership_id: int) -> list[Action]:
        m = self.store.get_membership(membership_id)
        if m is None or not m.active:
            self.parked.pop(membership_id, None)
            return []
        ctx = self._ctx(m)
        if ctx is None:
            return []
        p, _ = ctx
        room = self.store.refill_budget(m.room_id)
        sink = self.sinks.open_for(p.id, m.id)
        eff = rules.effective_status(p.status, sink is not None)
        was = self.parked.pop(m.id, None)
        out: list[Action] = []

        def unparked() -> list[Action]:
            return [Snapshot(room.id)] if was is not None else []

        if room.paused or m.held or eff in ("waiting-approval", "offline"):
            return unparked()
        if self.store.inflight_offer(m.id):
            if was is not None:
                self.parked[m.id] = was  # unchanged until the offer settles
            return []
        pending = self.store.pending_items(m.id)
        if not pending:
            return unparked()
        adapter = self.adapter(p)
        caps = adapter.caps(p)
        rel = rules.releasable(
            pending,
            room=room,
            eff=eff,
            peer_batch_boundary=m.peer_batch_boundary,
            boundary_seq=p.boundary_seq,
            now=self.now(),
            quiet_s=self.d.quiet_s,
            max_hold_s=self.d.max_hold_s,
            batch_max_msgs=self.d.batch_max_msgs,
            max_chars=min(self.d.batch_max_chars, caps.ctx_max_chars),
        )
        if rel is None:
            unread = rules.unread_stubs(pending) if sink is not None and sink.kind == "wait" else []
            if unread:
                # A wait() takes peer messages announced as "not shown here" and never read:
                # a pull like read() (whole texts, not counted), so a wait loop that skipped
                # the read() can't sit on them until its timeout (the read-first rule, §24).
                ids = set(unread)
                pull = Release(items=tuple(i for i in pending if i.message_id in ids), kind="pull",
                               counted=False, reason="unread")
                return self._fill_sink(p, m, room, pull, sink, pending) + unparked()
            if rules.budget_blocked(pending, room=room, eff=eff,
                                    peer_batch_boundary=m.peer_batch_boundary,
                                    boundary_seq=p.boundary_seq):
                out += self._budget_exhausted(room)
            return out + unparked()
        route = adapter.route(p, rel, sink, self.now())
        if route.kind == "sink" and sink is not None:
            out += self._fill_sink(p, m, room, rel, sink, pending)
            out += unparked()
        elif route.kind == "push":
            if adapter.serial_push and self.store.offered_batch_exists(p.id, PULL_PATHS | HOOK_PATHS):
                # One frame in flight per session (§9.2): a second frame (another
                # room) would queue behind the turn the first one starts and could
                # straddle an approval prompt. It goes when the first settles.
                pass
            else:
                out += self._push(p, m, room, rel, route.path or "inbox")
            out += unparked()
        elif route.kind == "none":
            reason = route.reason or "no wake path"
            self.parked[m.id] = reason
            if was != reason:
                out.append(Snapshot(room.id))
        else:  # pull: the next hook / tool call claims it; defer: re-evaluated later
            out += unparked()
        return out

    def evaluate_participant(self, participant_id: int) -> list[Action]:
        out: list[Action] = []
        for m in self.store.participant_memberships(participant_id):
            out += self.evaluate(m.id)
        return out

    def evaluate_room(self, room_id: int) -> list[Action]:
        out: list[Action] = []
        for m in self.store.room_memberships(room_id):
            out += self.evaluate(m.id)
        return out

    # ------------------------------------------------------------ offer paths
    def _after_counted(self, room_id: int) -> list[Action]:
        room = self.store.room_by_id(room_id)
        if room is not None and room.budget_remaining <= 0:
            return self._budget_exhausted(room)
        return []

    def _budget_exhausted(self, room: Room) -> list[Action]:
        if not self.store.mark_budget_notice(room.id, room.budget_window_start):
            return []
        self._event("budget_exhausted", room_id=room.id, window=room.budget_window_start)
        return [
            Notice(room.id, "warn",
                   f"the wake budget for this hour is used up: agents now wake only for "
                   f"{self._whose()} messages. Raise it with /budget <n> in the web UI."),
            Snapshot(room.id),
        ]

    def _fill_sink(self, p: Participant, m: Membership, room: Room, rel: Release, sink: Sink,
                   pending: list[Item]) -> list[Action]:
        """A wait() call returns the release (plus any notified stubs: wait is a pull path)."""
        if sink.kind == "park":
            return self._fill_park(p, m, room, rel, sink)
        chosen = {i.message_id for i in rel.items}
        extra = [i for i in pending if i.notified_at is not None and i.message_id not in chosen]
        caps = self.adapter(p).caps(p)
        max_chars = min(self.d.batch_max_chars, caps.ctx_max_chars)
        items = rules.cap(list(rel.items) + rules.human_first(extra), self.d.batch_max_msgs, max_chars)
        # a wait() result is a tool result: texts are shown whole, never cut
        fit = self._fit(items, room, m, peer_inline=True, max_chars=max_chars, item_limit=None, shrink=False)
        items = list(fit.items)
        b = self.store.create_batch(
            m.id,
            path=sink.path,
            kind=rel.kind,
            wake_kind="wait_return" if rel.kind == "wake" else None,
            wake_reason=rel.reason,
            counted=rel.counted,
            items=envelope.inline_flags(items, True, fit.partial),
        )
        if rel.kind != "pull":  # announced items pulled back (§24) use no peer batch
            self._mark_peer(b, p, items)
        self.store.mark_posted(b.id)
        text = self._render(b, fit, room, m, peer_inline=True, item_limit=None)
        sink.batch_id = b.id
        tu = sink.meta.get("tool_use_id")
        if isinstance(tu, str):
            self.pull_tool_use[b.id] = tu  # Devin: only this call's PostToolUse confirms it
        out: list[Action] = [self._close_sink(sink, {"status": "messages", "text": text,
                                                     "batch_id": b.id, "count": len(items)},
                                              "filled")]
        self._event("offer", room_id=room.id, membership_id=m.id, participant_id=p.id,
                    batch_id=b.id, path=b.path, n=len(items), counted=rel.counted, ids=_ids(items))
        out += self._arm_pull(b, p)
        if rel.counted:
            out += self._after_counted(room.id)
        out.append(Snapshot(room.id))
        return out

    def _fill_park(self, p: Participant, m: Membership, room: Room, rel: Release, sink: Sink) -> list[Action]:
        """A parked Cursor stop hook returns a wake batch as its ``followup_message``
        (§9.4): the next user message, so peer text is a stub. Counted as a
        ``stop_cont`` wake; confirmed by the hook's ack plus the next hook."""
        caps = self.adapter(p).caps(p)
        inline = sink.path in caps.inline_paths
        fit = self._fit(list(rel.items), room, m, peer_inline=inline,
                        max_chars=min(self.d.batch_max_chars, caps.ctx_max_chars))
        items = list(fit.items)
        b = self.store.create_batch(
            m.id, path=sink.path, kind=rel.kind, wake_kind="stop_cont", wake_reason=rel.reason,
            counted=rel.counted, items=envelope.inline_flags(items, inline, fit.partial),
        )
        self._mark_peer(b, p, items)
        self.store.mark_posted(b.id)
        text = self._render(b, fit, room, m, peer_inline=inline)
        ack = self._continue(b, p, sink.meta.get("loop_count"))
        sink.batch_id = b.id
        out: list[Action] = [self._close_sink(sink, {"status": "messages", "text": text, "batch_id": b.id,
                                                     "ack": ack, "count": len(items)}, "filled")]
        self._event("offer", room_id=room.id, membership_id=m.id, participant_id=p.id,
                    batch_id=b.id, path=b.path, n=len(items), counted=rel.counted, ids=_ids(items))
        # the follow-up starts a turn: busy until a hook says otherwise
        self._set_status_quiet(p, "busy", "stop:followup")
        if rel.counted:
            out += self._after_counted(room.id)
        out += self._snap(self._rooms_of(p.id))
        return out

    def _mark_peer(self, b: Batch, p: Participant, items: Iterable[Item]) -> None:
        """A wake batch carrying peer chatter uses this turn boundary's one peer
        batch (§8.2) once it is *confirmed*: an offer that expires (a lost frame,
        a failed turn/start) must not leave the chatter waiting for a boundary
        that may never come (never lose a wake)."""
        if any(i.prio == 0 for i in items):
            self.peer_marks[b.id] = p.boundary_seq

    def _continue(self, b: Batch, p: Participant, loop_count: Any) -> str:
        """Arm a stop continuation: the hook must ack it, then the session's next hook confirms it."""
        ack = secrets.token_hex(16)
        self.hook_acks[b.id] = (ack, self.now() + self.d.hook_ack_s)
        lc = loop_count if isinstance(loop_count, int) and not isinstance(loop_count, bool) else None
        self.continues[b.id] = Cont(participant_id=p.id, loop_count=lc, created_at=self.now())
        return ack

    def _set_status_quiet(self, p: Participant, status: str, src: str) -> None:
        """A status change decided inside an evaluation (no re-evaluation from here)."""
        cur = self.store.get_participant(p.id)
        if cur is None or cur.status == status or cur.status in ("waiting-approval", "offline"):
            return
        before, after = self.store.set_status(p.id, status, src)
        self._event("status", participant_id=p.id, frm=before.status, to=after.status, src=src)

    def _push(self, p: Participant, m: Membership, room: Room, rel: Release, path: str) -> list[Action]:
        caps = self.adapter(p).caps(p)
        inline = path in caps.inline_paths
        fit = self._fit(list(rel.items), room, m, peer_inline=inline,
                        max_chars=min(self.d.batch_max_chars, caps.ctx_max_chars))
        items = list(fit.items)
        b = self.store.create_batch(
            m.id,
            path=path,
            kind=rel.kind,
            wake_kind="idle_wake" if rel.kind == "wake" else None,
            wake_reason=rel.reason,
            counted=rel.counted,
            items=envelope.inline_flags(items, inline, fit.partial),
        )
        self._mark_peer(b, p, items)
        text = self._render(b, fit, room, m, peer_inline=inline)
        self._event("offer", room_id=room.id, membership_id=m.id, participant_id=p.id,
                    batch_id=b.id, path=path, n=len(items), counted=rel.counted, ids=_ids(items))
        out: list[Action] = [Push(b.id, p.id, path, text, room=room.name,
                                  sender=items[0].sender_name if items else "")]
        if rel.counted:
            out += self._after_counted(room.id)
        out.append(Snapshot(room.id))
        return out

    def _arm_pull(self, b: Batch, p: Participant) -> list[Action]:
        """How a pull batch (wait/read/say) will be confirmed (§8.7, §9.6)."""
        if p.harness == "test":
            mode = self.ack_modes.get(p.id, "next_call")
            if mode == "immediate":
                return self.on_confirm(b.id, "test:immediate")
            if mode == "never":
                self.pull_deadlines[b.id] = self.now() + self.d.pull_ack_s
            return []
        if p.hooks_seen_at is not None:
            self.pull_deadlines[b.id] = self.now() + self.d.pull_ack_s
        # no hooks ever seen: the member's next switchboard call confirms it
        return []

    # ========================================================== confirmation
    def on_confirm(self, batch_id: int, evidence: str) -> list[Action]:
        self.hook_acks.pop(batch_id, None)
        self.pull_deadlines.pop(batch_id, None)
        self.pull_tool_use.pop(batch_id, None)
        cont = self.continues.pop(batch_id, None)
        peer_mark = self.peer_marks.pop(batch_id, None)
        b = self.store.confirm_batch(batch_id, evidence)
        if b is None:
            return []
        m = self.store.get_membership(b.membership_id)
        if m is None:
            return []  # pragma: no cover - membership rows are deleted only with a room without members (§28)
        if peer_mark is not None:
            self.store.mark_peer_batch(m.id, peer_mark)
        self._event("confirm", room_id=m.room_id, membership_id=m.id,
                    participant_id=m.participant_id, batch_id=b.id, evidence=evidence)
        extra: list[Action] = []
        if cont is not None and b.path == "stop_followup":
            p = self.store.get_participant(m.participant_id)
            if p is not None and p.unconfirmed_followups:
                self.store.update_participant(p.id, unconfirmed_followups=0)
                extra += self.refresh_tier(p.id)
        # a settled push frame may free another room's frame (one per session)
        again = (self.evaluate_participant(m.participant_id) if is_push_path(b.path)
                 else self.evaluate(m.id))
        return extra + again + [Snapshot(m.room_id)]

    def on_expire(self, batch_id: int, reason: str, *, state: str = "expired",
                  count_failure: bool = True) -> list[Action]:
        """An offer failed: its deliveries go back to pending. ``count_failure=False``
        (a push re-route: nothing reached the session) doesn't count toward the push-expiry
        warning, and a counted wake gives its room's budget unit back (the next offer
        spends it again), so re-routes never drain the budget."""
        self.hook_acks.pop(batch_id, None)
        self.pull_deadlines.pop(batch_id, None)
        self.pull_tool_use.pop(batch_id, None)
        self.peer_marks.pop(batch_id, None)
        cont = self.continues.pop(batch_id, None)
        cur = self.store.get_batch(batch_id)
        if cur is None or cur.state != "offered":
            if cont is not None and reason in TIMER_EXPIRIES:
                # cancelled meanwhile (a kick or leave of that room): the busy it set still goes
                return self._undo_stop_busy(cont.participant_id, cont.created_at, f"expired:{reason}")
            return []
        push = is_push_path(cur.path)
        b = self.store.expire_batch(batch_id, reason, state=state, push=push and count_failure,
                                    refund=push and not count_failure)
        if b is None:
            return []  # pragma: no cover - checked offered just above, in this same synchronous call
        m = self.store.get_membership(b.membership_id)
        if m is None:
            return []  # pragma: no cover - membership rows are deleted only with a room without members (§28)
        self._event("expire" if state == "expired" else "cancel", room_id=m.room_id,
                    membership_id=m.id, participant_id=m.participant_id, batch_id=b.id,
                    path=b.path, reason=reason)
        out: list[Action] = []
        if push and state == "expired" and count_failure:
            p = self.store.get_participant(m.participant_id)
            if p is not None:
                self.adapter(p).push_expired(p, b, reason, self.now())
                if p.push_expiries == PUSH_EXPIRY_WARN:
                    out.append(Notice(m.room_id, "warn",
                                      f"{m.screen_name}: deliveries not confirmed; check `switchboard status`"))
        if cont is not None and b.path == "stop_followup" and state == "expired" and self._missed(cont, reason):
            out += self._followup_expired(m, reason)
        if cont is not None and reason in TIMER_EXPIRIES:
            # the continuation set the member busy; with no hook since, no turn ran
            # (an expiry a hook caused leaves the status to that hook)
            out += self._undo_stop_busy(m.participant_id, cont.created_at, f"expired:{reason}")
        again = self.evaluate_participant(m.participant_id) if push else self.evaluate(m.id)
        return out + again + [Snapshot(m.room_id)]

    def _missed(self, cont: Cont, reason: str) -> bool:
        """Does this expiry count as a follow-up Cursor never ran? The human typing
        right after it was printed is a race, not evidence (§9.4); typing a while
        later, with no hook of the follow-up turn in between, is."""
        if reason != "human_prompt":
            return True
        return cont.acked_at is not None and self.now() - cont.acked_at >= FOLLOWUP_RACE_S

    def _undo_stop_busy(self, participant_id: int, since: float, src: str) -> list[Action]:
        """A stop continuation or re-arm set the member busy (a turn should start).
        If no hook of the session came after ``since``, no turn ran: back to idle,
        so the member shows parked instead of busy for good."""
        p = self.store.get_participant(participant_id)
        if p is None or not p.active or p.status != "busy" or p.status_src not in STOP_BUSY_SRCS:
            return []
        if p.hooks_seen_at is not None and p.hooks_seen_at > since:
            return []
        return self.set_status(p, "idle", src, bump=True)

    def _followup_expired(self, m: Membership, reason: str) -> list[Action]:
        """A Cursor follow-up that was never confirmed. After
        ``max_unconfirmed_followups`` in a row the member is degraded: no more
        parks until the human's next prompt in that session (§9.4)."""
        p = self.store.get_participant(m.participant_id)
        if p is None:
            return []  # pragma: no cover - participant rows are never deleted
        n = p.unconfirmed_followups + 1
        self.store.update_participant(p.id, unconfirmed_followups=n)
        limit = self.cfg.cursor.max_unconfirmed_followups
        out: list[Action] = []
        if limit and n == limit:
            self._event("tier", participant_id=p.id, what="degraded", reason=reason, n=n)
            out.append(Notice(m.room_id, "warn",
                              f"{m.screen_name}: {n} stop follow-ups in a row were not confirmed; no more"
                              " follow-ups until your next prompt in that Cursor session"))
        return out + self.refresh_tier(p.id)

    def refresh_tier(self, participant_id: int) -> list[Action]:
        """Re-derive a participant's tier from its adapter (binding, degraded, ...)."""
        p = self.store.get_participant(participant_id)
        if p is None or p.harness not in ("cursor", "devin"):
            return []
        tier, note = self.adapter(p).tier(p)
        if (tier, note) == (p.tier, p.tier_note):
            return []
        self.store.update_participant(p.id, tier=tier, tier_note=note)
        self._event("tier", participant_id=p.id, tier=tier, note=note or "")
        return self._snap(self._rooms_of(p.id))

    def expire_pull_batches(self, participant_id: int, reason: str, *,
                            before: float | None = None, paths: Iterable[str] = PULL_PATHS) -> list[Action]:
        """Unconfirmed wait/read/say answers of this participant go back to pending."""
        out: list[Action] = []
        paths = frozenset(paths)
        for m in self.store.participant_memberships(participant_id):
            for b in self.store.offered_batches(m.id, paths):
                if before is not None and b.created_at >= before:
                    continue
                out += self.on_expire(b.id, reason)
        return out

    def before_call(self, p: Participant) -> list[Action]:
        """An agent.* call is evidence that earlier pull answers reached the model
        when there is no hook to say so (test ``--ack next_call``; hook-less harnesses).
        It also confirms the session's acked stop continuations (``_call_confirms``)."""
        out = self._call_confirms(p)
        mode = self.ack_modes.get(p.id) if p.harness == "test" else None
        if p.harness == "test":
            if mode not in (None, "next_call"):
                return out
        elif p.hooks_seen_at is not None:
            return out
        for m in self.store.participant_memberships(p.id):
            for b in self.store.offered_batches(m.id, PULL_PATHS):
                out += self.on_confirm(b.id, "next_call")
        return out

    def _call_confirms(self, p: Participant) -> list[Action]:
        """A tool call from the session after a stop continuation was acked (printed
        by its hook) is the continuation's turn running: confirm it now, as the
        session's next hook would (§8.7). Without this a Cursor follow-up (no pre-tool
        hook) stays offered until the postToolUse *after* the agent's first call, so
        that call, say pass(), would come before its stubs were pending: never refused
        by the read-first rule, and a read() would find nothing (§24). A human prompt
        in between expires the continuation first (its hook comes before the turn)."""
        out: list[Action] = []
        now = self.now()
        for bid, c in list(self.continues.items()):
            if c.participant_id != p.id or c.acked_at is None:
                continue
            b = self.store.get_batch(bid)
            if b is None or b.state != "offered":
                continue
            self.store.set_batch_times(bid, turn_start_at=now, first_action_at=now)
            self._event("first_action", participant_id=p.id, batch_id=bid)
            out += self.on_confirm(bid, "agent_call")
        return out

    def check_token(self, participant_id: int, batch_id: int, mac: str) -> Batch | None:
        b = self.store.get_batch(batch_id)
        if b is None:
            return None
        m = self.store.get_membership(b.membership_id)
        if m is None or m.participant_id != participant_id:
            return None
        if not envelope.check_token(self.key, mac, b.id, m.id):
            return None
        return b

    def on_hook_ack(self, batch_id: int, ack: str) -> list[Action]:
        want = self.hook_acks.get(batch_id)
        if want is None or not isinstance(ack, str) or not hmac.compare_digest(want[0], ack):
            return []
        cont = self.continues.get(batch_id)
        if cont is not None:
            # a stop continuation was printed; the session's next hook confirms it (§8.7)
            self.hook_acks.pop(batch_id, None)
            cont.acked_at = self.now()
            return []
        return self.on_confirm(batch_id, "hook_ack")

    # ============================================================ rate limit
    def check_say(self, p: Participant, m: Membership, reply_to: Message | None) -> float | None:
        """The rate limit on say() (§8.4): None if the say may go ahead, else the
        seconds until it may (and a ``rate_limited`` event). Replying to the
        human, or to a message that @mentions the sender, is exempt, as is a
        member with priority items in context it hasn't answered yet. Nothing is
        queued: the say is refused and the agent may try again."""
        exempt = rules.rate_limit_exempt(
            reply_to_kind=reply_to.sender_kind if reply_to is not None else None,
            reply_to_mentions=reply_to.mentions if reply_to is not None else (),
            sender_name=m.screen_name,
            unhandled_priority=self.store.unhandled_priority(m.id),
        )
        retry = rules.check_rate_limit(p.last_say_at, self.now(), self.d.rate_limit_s, exempt)
        if retry is not None:
            self._event("rate_limited", room_id=m.room_id, membership_id=m.id, participant_id=p.id,
                        retry_after_s=retry)
        return retry

    def check_pass(self, p: Participant, m: Membership) -> list[int]:
        """The read-first rule (§24): [] if pass() may go ahead, else the ids of peer
        messages this member saw only as "call read()" stubs (and a ``pass_refused``
        event). A refusal is no answer: nothing is handled and the watchdog goes on.
        read() always shows every such item (they are pending), so it can't wedge."""
        ids = rules.unread_stubs(self.store.pending_items(m.id))
        if ids:
            self._event("pass_refused", room_id=m.room_id, membership_id=m.id, participant_id=p.id,
                        reason="read_first", n=len(ids), ids=ids[:20])
        return ids

    # ================================================================= pulls
    def pull(self, p: Participant, m: Membership, path: str, limit: int, *,
             before_id: int | None = None) -> tuple[str, int | None, int, bool, list[Action]]:
        """read()/say() answer: every pending item (notified stubs too), oldest first.

        Returns (text, batch_id, count, more, actions).
        """
        out: list[Action] = []
        if path == "read":
            # a newer read() takes back earlier unconfirmed answers: never skip
            out += self.expire_pull_batches(p.id, "newer_read")
        room = self.store.room_by_id(m.room_id)
        assert room is not None
        pending = self.store.pending_items(m.id)
        if before_id is not None:
            pending = [i for i in pending if i.message_id < before_id]
        limit = max(1, min(int(limit), MAX_READ))
        items, more = rules.pull_items(pending, limit)
        if not items:
            return envelope.batch_header(room.name, self.cfg.human_name, 0, 0, 0), None, 0, False, out
        # whole texts, up to pull_max_chars rendered; the rest is "more"
        fit = self._fit(items, room, m, peer_inline=True, max_chars=self.d.pull_max_chars,
                        item_limit=None, shrink=False)
        items, more = list(fit.items), more or fit.more
        b = self.store.create_batch(m.id, path=path, kind="pull",
                                    items=envelope.inline_flags(items, True, fit.partial))
        self.store.mark_posted(b.id)
        text = self._render(b, fit, room, m, peer_inline=True, item_limit=None, more=more)
        self._event("offer", room_id=room.id, membership_id=m.id, participant_id=p.id,
                    batch_id=b.id, path=path, n=len(items), counted=False, ids=_ids(items))
        out += self._arm_pull(b, p)
        out.append(Snapshot(room.id))
        return text, b.id, len(items), more, out

    # ================================================================= sinks
    def open_wait(self, p: Participant, m: Membership, wait_id: str, timeout_s: float,
                  conn_id: int | None = None) -> tuple[Sink, list[Action]]:
        """A wait() call: supersede older sinks, expire unconfirmed pulls, open, evaluate."""
        out: list[Action] = []
        for old in self.sinks.for_participant(p.id):
            out.append(self._close_sink(old, {"status": "superseded",
                                              "text": "[switchboard] this wait() was replaced by a newer wait() call."},
                                        "superseded"))
        out += self.expire_pull_batches(p.id, "superseded")
        now = self.now()
        sink = self.sinks.open(participant_id=p.id, membership_id=m.id, room_id=m.room_id,
                               kind="wait", path="wait", wait_id=wait_id, opened_at=now,
                               deadline=now + float(timeout_s), conn_id=conn_id)
        pre = self.wait_calls.pop(p.id, None)
        if pre is not None and now - pre[1] <= WAIT_PRE_FRESH_S:
            sink.meta["tool_use_id"] = pre[0]  # the PreToolUse of this very wait() call (Devin)
        # Each new wait() is a turn boundary for peer batches: an agent that
        # loops wait() inside one prompt (Claude when idle, Devin's wait loop)
        # would otherwise get one chatter batch per prompt and then only
        # timeouts. Wakes stay bounded by the budget and the loop guard.
        if p.harness == "test" or p.hooks_seen_at is None:
            # status is inferred from sinks
            self.store.set_status(p.id, "idle" if p.harness == "test" else p.status, "sink",
                                  bump_boundary=True)
        else:
            self.store.bump_boundary(p.id)
        out += self.evaluate(m.id)
        out.append(Snapshot(m.room_id))
        return sink, out

    def open_park(self, p: Participant, ev: HookEvent, secs: float,
                  conn_id: int | None = None) -> tuple[Sink, list[Action]]:
        """A Cursor stop hook parks (§9.4): one live park per conversation (an
        older one resolves with no continuation), serving every room of the
        session. The engine fills it with the first wake batch that is due."""
        out: list[Action] = []
        for old in self.sinks.parks_for(p.id):
            out.append(self._close_sink(old, None, "superseded"))
        ms = self.store.participant_memberships(p.id)
        now = self.now()
        m = ms[0]
        sink = self.sinks.open(participant_id=p.id, membership_id=m.id, room_id=m.room_id, kind="park",
                               path="stop_followup", wait_id=f"park-{secrets.token_hex(4)}", opened_at=now,
                               deadline=now + float(secs), conn_id=conn_id,
                               meta={"loop_count": ev.loop_count})
        self._event("park", participant_id=p.id, secs=round(float(secs), 1), loop_count=ev.loop_count)
        out += self.evaluate_participant(p.id)
        return sink, out + self._snap(m2.room_id for m2 in ms)

    def release_parks(self, participant_id: int, reason: str) -> list[Action]:
        """Open parks of this session resolve with no continuation (``{}``)."""
        return [self._close_sink(s, None, reason) for s in self.sinks.parks_for(participant_id)]

    def _close_sink(self, sink: Sink, result: dict[str, Any] | None, reason: str) -> Action:
        s = self.sinks.close(sink.id, result, reason)
        if s is not None:
            p = self.store.get_participant(s.participant_id)
            if p is not None and p.harness == "test" and p.status == "idle" and p.active:
                self.store.set_status(p.id, "busy", "sink")
        return ResolveSink(sink.id, result or {"status": "cancelled"})

    def sink_timeout(self, sink_id: int) -> list[Action]:
        s = self.sinks.get(sink_id)
        if s is None or not s.open:
            return []
        room = self.store.room_by_id(s.room_id)
        name = room.name if room else "?"
        if room is not None and room.paused:
            res = {"status": "paused", "text": paused_text(name)}
        else:
            secs = int(round(s.deadline - s.opened_at))
            text = f"[switchboard] {name}: no new messages within {secs} s."
            # an open wait() takes unread stubs (``_evaluate``), except while a hold, a pause, an
            # approval prompt or another offer keeps it from filling: then say so (§24)
            unread = rules.unread_stubs(self.store.pending_items(s.membership_id))
            if unread:
                text += envelope.unread_note(name, len(unread))
            res = {"status": "timeout", "text": text}
        return [self._close_sink(s, res, res["status"]), Snapshot(s.room_id)]

    def unwait(self, p: Participant, wait_id: str) -> list[Action]:
        """The harness cancelled a wait(): close it, and take back anything it was given."""
        s = self.sinks.find_wait(p.id, wait_id)
        out: list[Action] = []
        if s is None:
            return out
        if s.open:
            out.append(self._close_sink(s, {"status": "cancelled"}, "unwait"))
        if s.batch_id is not None:
            out += self.on_expire(s.batch_id, "unwait")
        out += self.evaluate(s.membership_id)
        out.append(Snapshot(s.room_id))
        return out

    def close_conn_sinks(self, conn_id: int) -> list[Action]:
        out: list[Action] = []
        for s in self.sinks.for_conn(conn_id):
            out.append(self._close_sink(s, {"status": "cancelled"}, "disconnect"))
            out.append(Snapshot(s.room_id))
        return out

    # ============================================================== messages
    def on_message(self, msg_id: int) -> list[Action]:
        """After a chat message commits: the loop guard, then every recipient."""
        msg = self.store.get_message(msg_id)
        if msg is None or msg.kind != "chat":
            return []
        out: list[Action] = []
        room = self.store.room_by_id(msg.room_id)
        if room is None:
            return []  # pragma: no cover - a room is deleted only without members (§28)
        if msg.sender_kind == "agent" and rules.hop_tripped(room):
            out += self.pause_room(room.id, "loop guard", event="loop_guard",
                                   notice=f"loop guard: {room.hop_count} agent messages in a row"
                                          f" with no message from {self._a_human()}. {room.name}"
                                          " is paused; /resume in the web UI to continue."
                                          f" /hops <n> changes the limit (now {room.hop_limit}).")
        out += self.evaluate_room(room.id)
        out.append(Snapshot(room.id))
        return out

    # ============================================================== commands
    def pause_room(self, room_id: int, reason: str, *, event: str = "pause",
                   notice: str | None = None) -> list[Action]:
        self.store.set_paused(room_id, True, reason)
        out: list[Action] = []
        if event != "pause":
            self._event(event, room_id=room_id, reason=reason)
        if notice:
            out.append(Notice(room_id, "warn", notice))
        return out + self._pause_actions(room_id)

    def _pause_actions(self, room_id: int) -> list[Action]:
        """/pause (or the loop guard): open waits return 'paused', unposted offers are cancelled."""
        out: list[Action] = []
        room = self.store.room_by_id(room_id)
        name = room.name if room else "?"
        for s in self.sinks.for_room(room_id):
            out.append(self._close_sink(s, {"status": "paused", "text": paused_text(name)}, "paused"))
        for m in self.store.room_memberships(room_id):
            # a Cursor stop park ends too, unless another room of that session is still live
            if self.sinks.parks_for(m.participant_id) and all(
                    (r := self.store.room_by_id(x.room_id)) is None or r.paused
                    for x in self.store.participant_memberships(m.participant_id)):
                out += self.release_parks(m.participant_id, "paused")
            for b in self.store.offered_batches(m.id):
                if b.posted_at is None:
                    out += self.on_expire(b.id, "pause", state="cancelled")
            self._unpark(m.id)
        out.append(Snapshot(room_id))
        return out

    def on_command(self, room_id: int, name: str, membership_id: int | None = None) -> list[Action]:
        if name == "pause":
            return self._pause_actions(room_id)
        if name in ("resume", "budget"):
            return self.evaluate_room(room_id) + [Snapshot(room_id)]
        if name == "release" and membership_id is not None:
            return self.evaluate(membership_id) + [Snapshot(room_id)]
        if name == "hold" and membership_id is not None:
            self._unpark(membership_id)
            return [Snapshot(room_id)]
        return [Snapshot(room_id)]

    def on_membership_ended(self, membership_id: int, reason: str) -> list[Action]:
        """Leave, kick or session end: open waits get told, nothing more is offered."""
        m = self.store.get_membership(membership_id)
        out: list[Action] = []
        status = "kicked" if reason == "kick" else "left"
        text = ("[switchboard] you were removed from the room by your user." if reason == "kick"
                else "[switchboard] you are no longer in this room.")
        if reason == "closed":
            # /close (§28.3): the room is already renamed #name~closed-<id>; name it as people saw it
            room = self.store.room_by_id(m.room_id) if m is not None else None
            status = "closed"
            text = (f"[switchboard] {display_room(room.name) if room is not None else 'this room'}"
                    f" was closed by {self.cfg.human_name}; you are no longer in it.")
        for s in self.sinks.for_membership(membership_id):
            out.append(self._close_sink(s, {"status": status, "text": text}, reason))
        if m is not None and not self.store.participant_memberships(m.participant_id):
            out += self.release_parks(m.participant_id, reason)  # no room left to follow up for
        elif m is not None and not self._unpaused_memberships(m.participant_id):
            out += self.release_parks(m.participant_id, "paused")  # the rooms left are all paused
        self._unpark(membership_id)
        for b in self.store.offered_batches(membership_id):  # pragma: no cover - callers end it in the store first
            self.hook_acks.pop(b.id, None)
            self.pull_deadlines.pop(b.id, None)
        for bid in list(self.peer_marks):  # the store cancelled this member's offers
            cur = self.store.get_batch(bid)
            if cur is None or cur.state != "offered":
                self.peer_marks.pop(bid, None)
        if m is not None:
            out.append(Snapshot(m.room_id))
        return out

    # ================================================================ status
    def set_status(self, p: Participant, status: str, src: str, *, bump: bool = False) -> list[Action]:
        if p.status == status and not bump:
            return []
        before, after = self.store.set_status(p.id, status, src, bump_boundary=bump)
        if before.status != after.status:
            self._event("status", participant_id=p.id, frm=before.status, to=after.status, src=src)
        out: list[Action] = []
        if status == "offline":
            out += self.expire_push_batches(p.id, "offline")
        out += self.evaluate_participant(p.id)
        return out + self._snap(self._rooms_of(p.id))

    def expire_push_batches(self, participant_id: int, reason: str) -> list[Action]:
        """Unconfirmed push offers (Claude inbox, ...) of this participant go back to pending."""
        out: list[Action] = []
        for m in self.store.participant_memberships(participant_id):
            for b in self.store.offered_batches(m.id):
                if is_push_path(b.path):
                    out += self.on_expire(b.id, reason)
        return out

    # ================================================================= hooks
    def claim_for_hook(self, p: Participant, ev: HookEvent) -> tuple[HookOut | None, list[Action]]:
        """A hook event from the verified participant: confirmations, status, then
        (only where §7.3 allows it) priority context for the model.

        Every evaluation the event triggers (a confirmation, an expiry, the
        status change) runs once at the end, with the status this event set and
        after its own context was claimed (§9.1 "updates status first"). Without
        that, the UserPromptSubmit that confirms an idle-wake frame would
        evaluate the member as still idle and push a second frame into the turn
        that frame just started, instead of giving the rest as hook context."""
        if self._deferred is not None:  # pragma: no cover - hooks are not re-entrant
            return self._claim_for_hook(p, ev)
        self._deferred = set()
        try:
            hook_out, out = self._claim_for_hook(p, ev)
        finally:
            deferred, self._deferred = self._deferred, None
        for mid in sorted(deferred):
            out += self.evaluate(mid)
        return hook_out, out

    def _claim_for_hook(self, p: Participant, ev: HookEvent) -> tuple[HookOut | None, list[Action]]:
        now = self.now()
        E = ev.ev
        out: list[Action] = []
        adapter = self.adapter(p)
        was_idle = p.status in ("idle", "starting")
        upd: dict[str, Any] = {"hooks_seen_at": now, "last_seen": now}
        if ev.sid and p.harness in ("claude", "devin", "test"):
            upd["session_id"] = ev.sid[:128]
        if ev.permission_mode and p.harness in ("claude", "codex"):
            upd["approval_mode"] = approval_mode_of(ev.permission_mode)
        # Codex: input steered into a running turn (a turn/steer, or a turn/start
        # merged into a busy turn) fires UserPromptSubmit with that turn's own id.
        # It is mid-turn input, not a new turn (M4 live).
        mid_turn = (E == "UserPromptSubmit" and p.harness == "codex" and bool(ev.gen)
                    and (ev.gen or "")[:128] == p.gen and not was_idle)
        gen = (ev.gen or "")[:128] or None
        if E == "UserPromptSubmit" and not mid_turn:
            upd.update(gen=gen, gen_tainted=int(gen is not None and gen in self.tainted_gens.get(p.id, {})),
                       rearms_in_gen=0)
            limit = self.cfg.cursor.max_unconfirmed_followups
            if p.harness == "cursor" and p.unconfirmed_followups and (
                    not limit or p.unconfirmed_followups >= limit):
                # the human's prompt ends a degraded spell (§9.4); below the limit the
                # count stands: only a confirmed follow-up breaks a run of misses
                upd["unconfirmed_followups"] = 0
        if E == "PreToolUse" and ev.subagent_bg and p.harness == "devin":
            self._taint(p.id, gen or p.gen)
            if gen is None or gen == p.gen:
                upd["gen_tainted"] = 1
        if E == "Stop" and ev.loop_count is not None:
            upd["last_loop_count"] = ev.loop_count
        mode_changed = "approval_mode" in upd and upd["approval_mode"] != p.approval_mode
        p = self.store.update_participant(p.id, **upd)
        self.rearm_busy.pop(p.id, None)  # a hook came: the re-arm (if any) was acted on
        adapter.on_hook(p, ev)
        if "unconfirmed_followups" in upd:
            out += self.refresh_tier(p.id)
        # Devin: a hook of a prompt that started a background subagent may be the
        # subagent's (F§6 5.2): no context, no continue, no re-deliver for it.
        tainted = p.harness == "devin" and (bool(p.gen_tainted) or (
            gen is not None and gen in self.tainted_gens.get(p.id, {})))
        stale_sub = tainted and gen is not None and gen != p.gen
        if not stale_sub:
            # A parked stop hook of this session ends at any hook of it: a newer
            # stop parks anew, anything else means the conversation moved on (§9.4).
            out += self.release_parks(p.id, "superseded" if E == "Stop" else f"hook:{E}")
            # Devin: an interrupt sends no cancel, so the wait() the agent left behind
            # would swallow the next message; any other hook of the session ends it
            # (not a hook that may be a subagent's: the main agent's wait is live).
            if adapter.closes_waits(p, ev) and not tainted:
                out += self._close_orphan_waits(p, ev)
            if adapter.is_wait_pre(ev):
                self.wait_calls[p.id] = ((ev.tool_use_id or "")[:128], ev.t if ev.t else now)

        # 1. confirmations carried by this event
        confirmed: set[int] = set()
        woke: int | None = None  # a push batch whose frame started this turn
        if E in CONFIRMING_EVENTS and ev.ok is not False:
            for bid, mac in ev.tokens:
                b = self.check_token(p.id, bid, mac)
                if b is None or b.state != "offered" or b.path in CONTINUE_PATHS:
                    continue  # continuations are confirmed by the next hook (below)
                if b.path in PULL_PATHS and not adapter.pull_confirms(b.path, self.pull_tool_use.get(b.id), ev):
                    continue
                confirmed.add(b.id)
                if (E == "UserPromptSubmit" and is_push_path(b.path)
                        and (b.wake_kind == "idle_wake" or was_idle)):
                    # turn start = the confirming UserPromptSubmit (§12.5); a
                    # bypass mid-task frame lands inside a running turn: not one
                    self.store.set_batch_times(b.id, turn_start_at=ev.t if ev.t else now)
                    woke = b.id
                elif b.wake_kind == "wait_return" and E == "PostToolUse" and not stale_sub:
                    # a wait() answer is in context now (§12.5 "in context")
                    self.store.set_batch_times(b.id, turn_start_at=ev.t if ev.t else now)
                    self.first_action[p.id] = b.id
                out += self.on_confirm(b.id, f"hook:{E}")
        if stale_sub:
            # A background subagent of an earlier prompt (its hooks carry the prompt id
            # it started in, F§6 5.2): it confirms only what it read itself. It never
            # settles the main agent's continuations, ends its turn or waits, changes
            # its status, or gets context or a Stop continue (which would continue it).
            return None, out
        out += self._settle_continues(p, ev)
        if E == "PreToolUse":
            bid = self.first_action.pop(p.id, None)
            if bid is not None:
                # the agent's first action after a wake (Devin's latency mark, §12.5)
                self.store.set_batch_times(bid, first_action_at=ev.t if ev.t else now)
                self._event("first_action", participant_id=p.id, batch_id=bid)
        elif E in TURN_BOUNDARY_EVENTS and not mid_turn:
            # the turn ended (or a new one began) with no tool call since the wake:
            # it had no first action (a later PreToolUse, e.g. the re-armed wait(), isn't it)
            self.first_action.pop(p.id, None)
        if E == "UserPromptSubmit" and not mid_turn:
            if woke is not None:
                self._event("turn_start", participant_id=p.id, src="hook", batch_id=woke)
            else:
                self._event("turn_start", participant_id=p.id, src="hook")
        # 2. a turn ended without the PostToolUse that would confirm a pull answer
        if E in TURN_BOUNDARY_EVENTS and not mid_turn:
            out += self.expire_pull_batches(p.id, f"hook:{E}", before=ev.t)
        # 3. re-deliver once: priority items the turn saw but didn't answer (§8.5)
        if E == "Stop" and not tainted and adapter.redeliver_on_stop(p, ev):
            out += self._redeliver(p)

        # 4. status (hooks never touch waiting-approval, §7.4)
        new, bump = None, False
        if E in ("SessionStart",):
            new = "idle"
        elif E in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure"):
            new = "busy"
        elif E in ("Stop", "Interrupt"):
            if not tainted:
                new, bump = "idle", p.status != "idle"
        elif E == "SessionEnd":
            if not (p.harness == "claude" and ev.reason == "clear"):
                new = "offline"
        p = self.store.get_participant(p.id) or p
        if new is not None and (p.status != "waiting-approval" or new == "offline"):
            out += self.set_status(p, new, f"hook:{E}", bump=bump)
            p = self.store.get_participant(p.id) or p
        elif mode_changed:
            out += self._snap(self._rooms_of(p.id))

        # 5. output, only where the harness table allows it
        hook_out: HookOut | None = None
        if tainted:
            pass  # Devin: this hook may be a subagent's (see above)
        elif E in adapter.context_events(p) and p.status != "waiting-approval":
            if E == "SessionStart":
                if ev.source in ("clear", "compact"):
                    hook_out = self._reminder(p)
            else:
                hook_out, acts = self._claim_context(p, "hook_ups" if E == "UserPromptSubmit" else "hook_ctx")
                out += acts
        elif E == "Stop" and p.status != "waiting-approval":
            secs = adapter.park_s(p, ev)
            if secs is not None and self._unpaused_memberships(p.id):
                # Cursor: the hook long-polls; the evaluation at the end of this
                # event may fill the park at once (§9.4)
                sink, acts = self.open_park(p, ev, secs)
                out += acts
                hook_out = HookOut("park", "", sink_id=sink.id)
            elif adapter.stop_continues(p):
                hook_out, acts = self._stop_continue(p, ev)
                out += acts
        return hook_out, out

    def _unpaused_memberships(self, participant_id: int) -> list[Membership]:
        """Memberships of this session whose room is not paused. A Cursor stop
        hook parks only for these: a park serves every room of the session, and
        /pause ends one only when all of them are paused (§8.5), so a stop that
        arrives then gets no park either (no continuation until /resume)."""
        out = []
        for m in self.store.participant_memberships(participant_id):
            room = self.store.room_by_id(m.room_id)
            if room is not None and not room.paused:
                out.append(m)
        return out

    def _taint(self, participant_id: int, gen: str | None) -> None:
        """Remember a Devin prompt that started a background subagent (newest last, capped)."""
        if not gen:
            return
        gens = self.tainted_gens.setdefault(participant_id, {})
        gens.pop(gen, None)
        gens[gen] = self.now()
        while len(gens) > TAINTED_GENS_MAX:
            gens.pop(next(iter(gens)))

    def _close_orphan_waits(self, p: Participant, ev: HookEvent) -> list[Action]:
        """wait() calls of this session opened before this hook started are orphans
        (Devin sends no cancel on an interrupt): close them, and take back an
        answer one was already given (§9.5). The next wait() works as usual."""
        out: list[Action] = []
        t = ev.t if ev.t is not None else self.now()
        for s in self.sinks.for_participant(p.id):
            if s.kind == "wait" and s.opened_at < t:
                out.append(self._close_sink(s, {"status": "superseded",
                                                "text": "[switchboard] this wait() ended: your session moved on."},
                                            "orphaned"))
                out.append(Snapshot(s.room_id))
        out += self.expire_pull_batches(p.id, f"hook:{ev.ev}", before=t, paths=("wait",))
        return out

    def _settle_continues(self, p: Participant, ev: HookEvent) -> list[Action]:
        """This session's unconfirmed stop continuations, judged by this hook (§8.7)."""
        out: list[Action] = []
        adapter = self.adapter(p)
        for bid, c in list(self.continues.items()):
            if c.participant_id != p.id:
                continue
            v = adapter.continue_verdict(p, c.loop_count, c.acked_at is not None, ev)
            if v == "confirm":
                t = ev.t if ev.t else self.now()
                self.store.set_batch_times(bid, turn_start_at=t)  # §12.5: "first hook"
                if ev.ev == "PreToolUse":
                    self.store.set_batch_times(bid, first_action_at=t)
                    self._event("first_action", participant_id=p.id, batch_id=bid)
                else:
                    self.first_action[p.id] = bid
                out += self.on_confirm(bid, f"hook:{ev.ev}")
            elif v:
                out += self.on_expire(bid, v)
        return out

    def _stop_continue(self, p: Participant, ev: HookEvent) -> tuple[HookOut | None, list[Action]]:
        """Devin's Stop (§9.5): a wake release now as ``decision: block`` (peer text
        stubbed: the reason arrives as a user message), else the "call wait()"
        re-arm while the budget and ``rearm_max_per_prompt`` allow, else nothing."""
        adapter = self.adapter(p)
        caps = adapter.caps(p)
        best: tuple[int, Membership, Room, Release] | None = None
        ms = self.store.participant_memberships(p.id)
        for m in ms:
            room = self.store.refill_budget(m.room_id)
            if room.paused or m.held or self.store.inflight_offer(m.id):
                continue
            rel = rules.releasable(
                self.store.pending_items(m.id), room=room, eff="idle",
                peer_batch_boundary=m.peer_batch_boundary, boundary_seq=p.boundary_seq,
                now=self.now(), quiet_s=self.d.quiet_s, max_hold_s=self.d.max_hold_s,
                batch_max_msgs=self.d.batch_max_msgs,
                max_chars=min(self.d.batch_max_chars, caps.ctx_max_chars),
            )
            if rel is None or rel.kind != "wake":
                continue
            top = max(i.prio for i in rel.items)
            if best is None or top > best[0]:
                best = (top, m, room, rel)
        out: list[Action] = []
        if best is not None:
            _, m, room, rel = best
            inline = "stop_block" in caps.inline_paths
            fit = self._fit(list(rel.items), room, m, peer_inline=inline,
                            max_chars=min(self.d.batch_max_chars, caps.ctx_max_chars))
            items = list(fit.items)
            b = self.store.create_batch(
                m.id, path="stop_block", kind="wake", wake_kind="stop_cont", wake_reason=rel.reason,
                counted=rel.counted, items=envelope.inline_flags(items, inline, fit.partial),
            )
            self._mark_peer(b, p, items)
            self.store.mark_posted(b.id)
            ack = self._continue(b, p, None)
            text = self._render(b, fit, room, m, peer_inline=inline)
            self._event("offer", room_id=room.id, membership_id=m.id, participant_id=p.id,
                        batch_id=b.id, path=b.path, n=len(items), counted=rel.counted, ids=_ids(items))
            out += self.set_status(p, "busy", "stop:block")
            if rel.counted:
                out += self._after_counted(room.id)
            return HookOut("continue", text, b.id, ack), out + [Snapshot(room.id)]
        dv = self.cfg.devin
        if not dv.rearm or p.rearms_in_gen >= dv.rearm_max_per_prompt:
            return None, out
        # a per-session hourly cap on top of the per-prompt one: re-arms spend the
        # room's shared budget, and the per-prompt count resets at every prompt
        now = self.now()
        recent = [t for t in self.rearm_log.get(p.id, []) if now - t < REARM_WINDOW_S]
        self.rearm_log[p.id] = recent
        if len(recent) >= dv.rearm_max_per_hour:
            return None, out
        for m in ms:
            room = self.store.refill_budget(m.room_id)
            if room.paused or m.held or room.budget_remaining <= 0:
                continue
            self.store.spend_budget(room.id)
            recent.append(now)
            self.store.update_participant(p.id, rearms_in_gen=p.rearms_in_gen + 1)
            self._event("rearm", room_id=room.id, membership_id=m.id, participant_id=p.id,
                        n=p.rearms_in_gen + 1)
            out += self.set_status(p, "busy", "stop:rearm")
            self.rearm_busy[p.id] = now
            out += self._after_counted(room.id)
            text = devin_rearm_text(room.name, caps.wait_cap_s)
            return HookOut("continue", text), out + [Snapshot(room.id)]
        return None, out

    def _redeliver(self, p: Participant) -> list[Action]:
        """Stop: each in-context priority item of this member that got no say()
        or pass() goes back to pending once, for the next idle wake (again=yes)."""
        out: list[Action] = []
        for m in self.store.participant_memberships(p.id):
            ids = rules.redeliver_ids(self.store.in_context_items(m.id))
            n = self.store.requeue(m.id, ids, redeliver=True)
            if n:
                self._event("requeue", room_id=m.room_id, membership_id=m.id, participant_id=p.id,
                            reason="redeliver", n=n, ids=ids[:20])
                out.append(Snapshot(m.room_id))
        return out

    def _reminder(self, p: Participant) -> HookOut | None:
        pairs = []
        for m in self.store.participant_memberships(p.id):
            room = self.store.room_by_id(m.room_id)
            if room is not None:
                pairs.append((room.name, m.screen_name))
        if not pairs:
            return None
        return HookOut("context", envelope.render_reminder(pairs, self._humans()))

    def _claim_context(self, p: Participant, path: str) -> tuple[HookOut | None, list[Action]]:
        """Mid-task priority items for the model, as hook context (one room per hook call)."""
        adapter = self.adapter(p)
        caps = adapter.caps(p)
        best: tuple[int, Membership, Room, Release] | None = None
        for m in self.store.participant_memberships(p.id):
            room = self.store.refill_budget(m.room_id)
            if room.paused or m.held or self.store.inflight_offer(m.id):
                continue
            rel = rules.releasable(
                self.store.pending_items(m.id), room=room, eff="busy",
                peer_batch_boundary=m.peer_batch_boundary, boundary_seq=p.boundary_seq,
                now=self.now(), quiet_s=self.d.quiet_s, max_hold_s=self.d.max_hold_s,
                batch_max_msgs=self.d.batch_max_msgs,
                max_chars=min(self.d.batch_max_chars, caps.ctx_max_chars),
            )
            if rel is None:
                continue
            top = max(i.prio for i in rel.items)
            if best is None or top > best[0]:
                best = (top, m, room, rel)
        if best is None:
            return None, []
        _, m, room, rel = best
        inline = path in caps.inline_paths
        # fitted to the hook's printable limit: nothing is cut after this, and
        # an item whose text had to be cut stays pending for read() (§8.6)
        fit = self._fit(list(rel.items), room, m, peer_inline=inline,
                        max_chars=min(self.d.batch_max_chars, caps.ctx_max_chars))
        items = list(fit.items)
        b = self.store.create_batch(m.id, path=path, kind="priority",
                                    items=envelope.inline_flags(items, inline, fit.partial))
        self.store.mark_posted(b.id)
        ack = secrets.token_hex(16)
        self.hook_acks[b.id] = (ack, self.now() + self.d.hook_ack_s)
        text = self._render(b, fit, room, m, peer_inline=inline)
        self._event("offer", room_id=room.id, membership_id=m.id, participant_id=p.id,
                    batch_id=b.id, path=path, n=len(items), counted=False, ids=_ids(items))
        return HookOut("context", text, b.id, ack), [Snapshot(room.id)]

    def _push_expiry(self, b: Batch, now: float) -> str | None:
        m = self.store.get_membership(b.membership_id)
        p = self.store.get_participant(m.participant_id) if m is not None else None
        if m is None or p is None:
            return None  # pragma: no cover - membership rows are deleted only with a room without members (§28), participant rows never
        return self.adapter(p).expire_due(p, b, now)

    # ================================================================== tick
    def tick(self, now: float | None = None) -> list[Action]:
        """Timers: hook-ack and pull expiries, the offer backstop, wait deadlines,
        and quiet-period / max-hold releases."""
        now = self.now() if now is None else now
        out: list[Action] = []
        for bid, (_ack, deadline) in list(self.hook_acks.items()):
            if now >= deadline:
                out += self.on_expire(bid, "no_ack")
        for bid, deadline in list(self.pull_deadlines.items()):
            if now >= deadline:
                out += self.on_expire(bid, "no_confirm")
        for bid, c in list(self.continues.items()):
            # printed (acked), but the session never sent another hook
            if c.acked_at is not None:
                cp = self.store.get_participant(c.participant_id)
                window = self.adapter(cp).confirm_window_s() if cp is not None else 0.0
                if now - c.acked_at >= window:
                    out += self.on_expire(bid, "no_hook")
        for pid, t in list(self.rearm_busy.items()):
            # a Devin re-arm with no hook of the session since: it never ran
            rp = self.store.get_participant(pid)
            if rp is None or now - t >= self.adapter(rp).confirm_window_s():
                self.rearm_busy.pop(pid, None)
                out += self._undo_stop_busy(pid, t, "rearm:no_hook")
        for b in self.store.offered_batches():
            if now - b.created_at >= self.d.offer_backstop_s:
                out += self.on_expire(b.id, "backstop")
            elif is_push_path(b.path):
                reason = self._push_expiry(b, now)
                if reason:
                    out += self.on_expire(b.id, reason)
        for s in self.sinks.open_sinks():
            if now >= s.deadline:
                out += self.sink_timeout(s.id)
        out += self.watchdog(now)
        for mid in self.store.memberships_with_pending():
            out += self.evaluate(mid)
        return out

    # ============================================================== watchdog
    def watchdog(self, now: float | None = None) -> list[Action]:
        """Unanswered @mentions (§8.5, run every tick).

        - An @mention in context (or notified as a stub) for ``watchdog_s`` with
          no say()/pass() from the member since, while the member is effectively
          idle, goes back to pending as a *reminder* (a counted wake like any
          other, ``reminder=yes`` and a reminder header), ``reminders += 1``.
        - After ``watchdog_max`` reminders the human gets a ``watchdog_escalate``
          warn notice; the item stays readable with read() but never wakes again.
        - A mention still pending because the member is *parked* (nothing can
          reach it) for ``watchdog_s`` escalates once per parked spell.
        - A member that isn't idle (busy in a turn, waiting on an approval
          prompt, offline) can't be reminded; once an @mention has gone
          unanswered for as long as reminding and escalating would have taken,
          ``(watchdog_max + 1) * watchdog_s``, the human is told (a notice only,
          once per item; the reminders still follow once the member is idle).
        Paused rooms and held members are left alone."""
        d = self.d
        if d.watchdog_s <= 0:
            return []
        now = self.now() if now is None else now
        wmax = max(0, min(int(d.watchdog_max), WATCHDOG_DONE - 1))
        out: list[Action] = []
        by_member: dict[int, list[Item]] = {}
        for it in self.store.watch_items(now - d.watchdog_s):
            by_member.setdefault(it.membership_id, []).append(it)
        # forget stalled notices for items no longer watched (answered, reminded, left)
        self.stalled_told &= {(it.membership_id, it.message_id) for items in by_member.values() for it in items}
        for mid, items in by_member.items():
            m = self.store.get_membership(mid)
            ctx = self._ctx(m) if m is not None and m.active else None
            if m is None or ctx is None:
                continue
            p, room = ctx
            if room.paused or m.held:
                continue
            sink = self.sinks.open_for(p.id, m.id)
            eff = rules.effective_status(p.status, sink is not None)
            idle = rules.is_idle(eff)
            if not idle:
                out += self._watchdog_stalled(m, room, eff, items, now, wmax)
                continue
            answered = self.store.last_answer_at(m.id)
            verdicts: dict[str, list[int]] = {}
            for it in items:
                v = rules.watchdog_verdict(it, now=now, watchdog_s=d.watchdog_s, watchdog_max=wmax,
                                           answered_at=answered, idle=idle)
                if v is not None:
                    verdicts.setdefault(v, []).append(it.message_id)
            if not verdicts:
                continue  # pragma: no cover - watch_items picked only due items (float rounding at the edge aside)
            if verdicts.get("done"):
                self.store.watchdog_done(m.id, verdicts["done"], keep_count=False)
            ids = verdicts.get("remind", [])
            if ids and self.store.watchdog_requeue(m.id, ids):
                self._event("watchdog_remind", room_id=room.id, membership_id=m.id, participant_id=p.id,
                            n=len(ids), ids=ids[:20])
            ids = verdicts.get("escalate", [])
            if ids and self.store.watchdog_done(m.id, ids, keep_count=True):
                self._event("watchdog_escalate", room_id=room.id, membership_id=m.id, participant_id=p.id,
                            why="unanswered", n=len(ids), ids=ids[:20])
                out.append(Notice(room.id, "warn",
                                  f"watchdog: {m.screen_name} hasn't answered @mention {_id_list(ids)}"
                                  f" after {wmax} reminder{'s' if wmax != 1 else ''}. It can still read()"
                                  f" it, but won't be woken for it again."))
            out += self.evaluate(m.id)
            out.append(Snapshot(room.id))
        for mid, reason in list(self.parked.items()):
            since = self.parked_since.get(mid)
            if mid in self.parked_escalated or since is None or now - since < d.watchdog_s:
                continue
            m = self.store.get_membership(mid)
            room = self.store.room_by_id(m.room_id) if m is not None else None
            if m is None or room is None or not m.active or room.paused or m.held:
                continue
            due = rules.parked_escalation(self.store.pending_items(mid), parked_since=since, now=now,
                                          watchdog_s=d.watchdog_s)
            if not due:
                continue
            self.parked_escalated.add(mid)
            ids = [i.message_id for i in due]
            self._event("watchdog_escalate", room_id=room.id, membership_id=m.id,
                        participant_id=m.participant_id, why="parked", n=len(ids), ids=ids[:20])
            out.append(Notice(room.id, "warn",
                              f"watchdog: {m.screen_name} is parked — needs a poke ({reason})."
                              f" Waiting for it: @mention {_id_list(ids)}."))
        return out

    def _watchdog_stalled(self, m: Membership, room: Room, eff: str, items: list[Item], now: float,
                          wmax: int) -> list[Action]:
        """@mentions overdue on a member that isn't idle: tell the human once per item."""
        d = self.d
        due = [i for i in rules.stalled(items, now=now, watchdog_s=d.watchdog_s, watchdog_max=wmax)
               if (m.id, i.message_id) not in self.stalled_told]
        if not due:
            return []
        # looked at once: a say()/pass() since it arrived answers it (settled once the member is idle)
        self.stalled_told.update((m.id, i.message_id) for i in due)
        due = rules.stalled(due, now=now, watchdog_s=d.watchdog_s, watchdog_max=wmax,
                            answered_at=self.store.last_answer_at(m.id))
        if not due:
            return []
        ids = [i.message_id for i in due]
        self._event("watchdog_escalate", room_id=room.id, membership_id=m.id, participant_id=m.participant_id,
                    why="not_idle", status=eff, n=len(ids), ids=ids[:20])
        what = {"busy": "busy in a turn", "waiting-approval": "waiting on an approval prompt"}.get(eff, eff)
        later = " It will be reminded once it is idle." if wmax > 0 else ""
        return [Notice(room.id, "warn",
                       f"watchdog: {m.screen_name} hasn't answered @mention {_id_list(ids)} for"
                       f" {_duration((wmax + 1) * d.watchdog_s)}; it is {what}.{later}")]


BYPASS_MODES = frozenset({"bypassPermissions"})
# Recorded or documented modes that still ask before running tools. Anything
# else (e.g. an unrecorded Codex never-policy value, or a future mode) is
# 'unknown', shown as "?" = treat like ⚠ (fail closed, §6.3).
PROMPTING_MODES = frozenset({"default", "acceptEdits", "plan"})


def is_push_path(path: str) -> bool:
    """A batch pushed by an adapter's transport (Claude inbox; Codex in M4)."""
    return path not in PULL_PATHS and path not in HOOK_PATHS


def approval_mode_of(permission_mode: str) -> str:
    if permission_mode in BYPASS_MODES:
        return "bypass"
    if permission_mode in PROMPTING_MODES:
        return "prompting"
    return "unknown"


def _duration(secs: float) -> str:
    return f"{int(secs)} s" if secs < 120 else f"{int(secs // 60)} min"


def _ids(items: Iterable[Item]) -> list[int]:
    """The message ids a batch carries, for its ``offer`` event (the report's per-message latency)."""
    return [i.message_id for i in items]


def _id_list(ids: list[int], n: int = 5) -> str:
    shown = ", ".join(f"#{i}" for i in ids[:n])
    return shown + (f" and {len(ids) - n} more" if len(ids) > n else "")


def paused_text(room: str) -> str:
    return (f"[switchboard] {room} is paused by your user. End your turn now; don't call"
            " wait() again until your user resumes the room.")
