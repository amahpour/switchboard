"""``switchboard report`` (DESIGN.md §12.5, §12.6): what happened in a room, from the database.

Built only from ``messages``, ``batches``, ``deliveries``, ``events``,
``memberships``, ``participants`` and ``rooms``, read through a read-only
SQLite connection (the broker may be running or stopped):

- **per-message latency**, send (``messages.ts``, broker clock) to the
  recipient, for the first batch that reached each recipient, labelled per path
  (§12.5): *turn start* (Claude inbox, Codex ``turn/start`` and ``codex queue``),
  *first hook* (a Cursor follow-up or a Devin Stop block), *in context* and
  *first action* (a Devin ``wait()`` answer), *in context* (mid-task paths) and
  *pulled* (``read()``/``say()`` answers); p50 and p95 with
  ``statistics.quantiles(n=100, method="inclusive")`` (n=1 shows the value,
  n=0 "n/a"), always with n, split by harness, tier, path and reason
  (human, mention, chatter; chatter includes the 3 s quiet period);
- per agent: turns (``turn_start`` events), wakes and continuations, posts vs
  passes, rate-limited says, what was still undelivered at the end, the model
  its hooks reported;
- rules that fired: loop guard, budget, rate limit, watchdog, re-deliver,
  expiries by reason, pauses, holds (``/hold`` and approval prompts), Devin
  re-arms, parked spells ("needs a poke"), Cursor parks;
- stalls: an approval prompt open longer than 60 s.

The window runs from the room's creation (or ``--since``/``--last``) to the
room's last activity, so a report made later reads the same; a participant's
own events (turns, approval prompts) count only inside it and while that
participant was a member of the room.

The output holds no message text, no session keys or pids, and no absolute
paths or email addresses (free-form strings pass through ``_safe``).
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STALL_S = 60.0

# what "reached the recipient" means per path (§12.5)
WAKE_TURN_PATHS = ("inbox", "turn_start", "queue")
CONTINUE_PATHS = ("stop_followup", "stop_block")
MID_TASK_PATHS = ("steer", "hook_ctx", "hook_ups")
PULL_PATHS = ("read", "say")
PATH_TIER = {
    "turn_start": "codex:daemon",
    "steer": "codex:daemon",
    "queue": "codex:queue",
    "stop_followup": "cursor:stop-park",
    "stop_block": "devin:wait-loop",
}
PRIO_REASON = {2: "human", 1: "mention", 0: "chatter"}
LABEL_ORDER = ("turn start", "first hook", "first action", "in context", "pulled")
REASON_ORDER = ("human", "mention", "chatter")

_PATH_RE = re.compile(r"(?:~|/(?:Users|home|private|tmp|var|opt|Volumes|root))/[^\s\"',;)]*")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LAST_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])$")


class ReportError(Exception):
    """A report that can't be built (no database, no such room, a bad window)."""


def _safe(v: Any) -> Any:
    """Free-form strings from the database, scrubbed of paths and email addresses."""
    if not isinstance(v, str):
        return v
    return _EMAIL_RE.sub("<email>", _PATH_RE.sub("<path>", v))


def pctl(xs: list[float], q: int) -> float | None:
    """The q-th percentile (§12.6): None for no samples, the value itself for one."""
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    return statistics.quantiles(xs, n=100, method="inclusive")[q - 1]


def fmt_s(v: float | None) -> str:
    if v is None:
        return "n/a"
    if abs(v) < 1.0:
        return f"{v * 1000:.0f} ms"
    if abs(v) < 120.0:
        return f"{v:.2f} s"
    return f"{v / 60:.1f} min"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_window(since: str | None, last: str | None, now: float) -> float | None:
    """``--since ISO`` (naive = local time) or ``--last 2h`` (s, m, h, d) as a timestamp."""
    if since and last:
        raise ReportError("give --since or --last, not both")
    if last:
        m = _LAST_RE.match(last.strip())
        if not m:
            raise ReportError("--last takes a number and a unit, e.g. 90m or 2h")
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return now - float(m.group(1)) * mult
    if since:
        try:
            dt = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ReportError("--since takes an ISO time, e.g. 2026-09-25T14:00") from None
        return dt.timestamp()
    return None


def open_ro(db_path: str | Path) -> sqlite3.Connection:
    """A connection that only reads and never creates the database.

    Not ``mode=ro``: after a clean broker stop SQLite has removed the WAL's
    ``-shm`` file, and a read-only connection can't recreate it (the open
    fails). ``mode=rw`` (the file must exist) with ``query_only`` can, and
    refuses every write."""
    p = Path(db_path)
    if not p.is_file():
        raise ReportError("no switchboard database in that home (has the broker ever run there?)")
    try:
        con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=rw", uri=True, timeout=5.0)
        con.execute("PRAGMA query_only = ON")
        con.execute("SELECT COUNT(*) FROM rooms").fetchone()
    except sqlite3.Error as e:
        raise ReportError(f"can't read the switchboard database: {e}") from None
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------- the model
@dataclass
class Member:
    membership_id: int
    participant_id: int
    name: str
    harness: str
    tier: str | None
    tier_note: str | None
    status: str
    approval_mode: str
    joined_at: float
    left_at: float | None
    left_reason: str | None
    host: str = ""  # '' for the broker's machine; a remote's name (schema v2)


def _data(raw: Any) -> dict[str, Any]:
    try:
        d = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


class _Db:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        con.row_factory = sqlite3.Row

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    def has_column(self, table: str, column: str) -> bool:
        """Schema v1 databases (0.1.0, 0.2.0) have no ``participants.host``; the report
        reads both and never migrates (it opens query-only)."""
        return any(r[1] == column for r in self.con.execute(f"PRAGMA table_info({table})").fetchall())


def _tier_timeline(db: _Db, pids: list[int]) -> dict[int, list[tuple[float, str]]]:
    """Per participant, (time, tier) from its join and tier events."""
    out: dict[int, list[tuple[float, str]]] = defaultdict(list)
    if not pids:
        return out
    marks = ",".join("?" * len(pids))
    for r in db.q(f"SELECT ts, participant_id, data FROM events WHERE kind IN ('join','tier')"
                  f" AND participant_id IN ({marks}) ORDER BY id", *pids):
        tier = _data(r["data"]).get("tier")
        if isinstance(tier, str) and tier:
            out[r["participant_id"]].append((r["ts"], tier))
    return out


def _tier_at(timeline: list[tuple[float, str]], t: float, fallback: str | None) -> str:
    cur = None
    for ts, tier in timeline:
        if ts <= t:
            cur = tier
        else:
            break
    return cur or fallback or "-"


def _metrics(path: str, kind: str, b: sqlite3.Row, ts: float) -> list[tuple[str, float, bool]]:
    """(label, seconds, main) for one message in its first confirmed batch (§12.5).

    ``main`` marks the sample's main measure (the "By reason" table): per sample,
    not per path, since a ``wait()`` answer has a first action only when a
    PreToolUse followed it (Devin), and an ``inbox`` frame starts a turn only when
    it woke the session (mid-task, in a bypass session, it is in context)."""
    out: list[tuple[str, float, bool]] = []
    if path in WAKE_TURN_PATHS and kind == "wake":
        if b["turn_start_at"] is not None:
            out.append(("turn start", b["turn_start_at"] - ts, True))
    elif path in CONTINUE_PATHS:
        if b["turn_start_at"] is not None:
            out.append(("first hook", b["turn_start_at"] - ts, True))
    elif path == "wait":
        act = b["first_action_at"]
        t_ctx = b["turn_start_at"] if b["turn_start_at"] is not None else b["confirmed_at"]
        if t_ctx is not None:
            out.append(("in context", t_ctx - ts, act is None))
        if act is not None:
            out.append(("first action", act - ts, True))
    elif path in MID_TASK_PATHS or path == "inbox":  # inbox while busy: a bypass session mid-task
        if b["confirmed_at"] is not None:
            out.append(("in context", b["confirmed_at"] - ts, True))
    elif path in PULL_PATHS:
        if b["confirmed_at"] is not None:
            out.append(("pulled", b["confirmed_at"] - ts, True))
    return out


def _stats(xs: list[float]) -> dict[str, Any]:
    return {"n": len(xs),
            "p50_ms": None if not xs else round(pctl(xs, 50) * 1000, 1),  # type: ignore[operator]
            "p95_ms": None if not xs else round(pctl(xs, 95) * 1000, 1),  # type: ignore[operator]
            "max_ms": None if not xs else round(max(xs) * 1000, 1)}


def build(con: sqlite3.Connection, room_name: str, *, since: float | None = None,
          now: float | None = None) -> dict[str, Any]:
    """The report for ``room_name`` as a JSON-able dict (``render_markdown`` prints it)."""
    from switchboard.models import InvalidName, normalize_room

    db = _Db(con)
    now = time.time() if now is None else now
    try:
        name = normalize_room(room_name)
    except InvalidName as e:
        raise ReportError(str(e)) from None
    rows = db.q("SELECT * FROM rooms WHERE name=?", name)
    if not rows:
        raise ReportError(f"no room {name}")
    room = rows[0]
    rid = room["id"]
    start = max(since, room["created_at"]) if since is not None else room["created_at"]

    # members of the room (a participant may have left and joined again: one row per membership)
    host_col = "p.host" if db.has_column("participants", "host") else "''"
    members = [Member(membership_id=r["mid"], participant_id=r["pid"], name=r["screen_name"], harness=r["harness"],
                      tier=r["tier"], tier_note=r["tier_note"], status=r["status"],
                      approval_mode=r["approval_mode"], joined_at=r["joined_at"], left_at=r["left_at"],
                      left_reason=r["left_reason"], host=r["host"] or "")
               for r in db.q("SELECT m.id AS mid, m.participant_id AS pid, m.screen_name, m.joined_at, m.left_at,"
                             " m.left_reason, p.harness, p.tier, p.tier_note, p.status, p.approval_mode,"
                             f" {host_col} AS host"
                             " FROM memberships m JOIN participants p ON p.id=m.participant_id"
                             " WHERE m.room_id=? AND (m.left_at IS NULL OR m.left_at>=?) ORDER BY m.id", rid, start)]
    by_mid = {m.membership_id: m for m in members}
    pids = sorted({m.participant_id for m in members})
    tiers = _tier_timeline(db, pids)

    msgs = {r["id"]: r for r in db.q("SELECT id, ts, sender_kind, sender_membership_id, kind FROM messages"
                                     " WHERE room_id=? AND ts>=?", rid, start)}
    prio = {(r["membership_id"], r["message_id"]): r["prio"]
            for r in db.q("SELECT d.membership_id, d.message_id, d.prio FROM deliveries d"
                          " JOIN memberships m ON m.id=d.membership_id WHERE m.room_id=?", rid)}

    # message ids per batch, from the offer events (M7); older databases: the deliveries' last batch
    offer_ids: dict[int, list[int]] = {}
    for r in db.q("SELECT data FROM events WHERE kind='offer' AND room_id=? AND ts>=?", rid, start):
        d = _data(r["data"])
        if isinstance(d.get("batch_id"), int) and isinstance(d.get("ids"), list):
            offer_ids[d["batch_id"]] = [i for i in d["ids"] if isinstance(i, int)]
    last_batch: dict[int, list[int]] = defaultdict(list)
    for r in db.q("SELECT d.batch_id, d.message_id FROM deliveries d JOIN memberships m ON m.id=d.membership_id"
                  " WHERE m.room_id=? AND d.batch_id IS NOT NULL", rid):
        last_batch[r["batch_id"]].append(r["message_id"])

    batches = db.q("SELECT b.* FROM batches b JOIN memberships m ON m.id=b.membership_id"
                   " WHERE m.room_id=? AND b.created_at>=? ORDER BY b.id", rid, start)

    # ---- events: the room's and its members' own, from the beginning (a pause or hold that
    # began before the window still holds); everything else is about other rooms
    cols = "SELECT id, ts, kind, room_id, membership_id, participant_id, data FROM events"
    room_all = db.q(f"{cols} WHERE room_id=? ORDER BY id", rid)
    part_all = (db.q(f"{cols} WHERE room_id IS NULL AND participant_id IN ({','.join('?' * len(pids))})"
                     " ORDER BY id", *pids) if pids else [])
    room_ev = [e for e in room_all if e["ts"] >= start]
    # the window ends at the room's last activity (its messages, its events, the batches that
    # reached its members), so a report made later reads the same
    batch_times = [b[c] for b in batches for c in ("confirmed_at", "turn_start_at", "first_action_at")
                   if b[c] is not None]
    end = max([start] + [r["ts"] for r in msgs.values()] + [e["ts"] for e in room_ev] + batch_times)
    # a participant's own events (turns, approval prompts, Cursor parks) count only inside the
    # window and while it was a member of this room: not its work elsewhere or afterwards
    spans_in_room: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for m in members:
        spans_in_room[m.participant_id].append((m.joined_at, m.left_at if m.left_at is not None else float("inf")))

    def in_room(pid: int | None, ts: float) -> bool:
        return pid is not None and start <= ts <= end and any(a <= ts <= b for a, b in spans_in_room.get(pid, []))

    part_ev = [e for e in part_all if in_room(e["participant_id"], e["ts"])]
    pauses = _room_pauses(room_all, now)
    holds = _member_holds(room_all, now)
    approvals = _approval_intervals([e for e in part_all if e["kind"] == "status"], now)

    def held(mem: Member, path: str, t0: float, t1: float) -> bool:
        """Was delivery of a message sent at t0 and reached at t1 held by a room pause, a /hold on
        this member or an approval prompt open in its session? (Its latency then includes the hold.)
        A pull (``read()``/``say()``) is never held: the agent asked for it, pause or not."""
        if path in PULL_PATHS:
            return False
        spans = pauses + holds.get(mem.membership_id, []) + approvals.get(mem.participant_id, [])
        return any(a < t1 and b > t0 for a, b in spans)

    # ---- per-message latency: the first confirmed batch that carried each message to each member
    seen: set[tuple[int, int]] = set()
    samples: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    main_samples: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    held_samples: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    first_by_path: Counter[str] = Counter()
    for b in sorted((b for b in batches if b["state"] == "confirmed"), key=lambda b: (b["confirmed_at"], b["id"])):
        mem = by_mid.get(b["membership_id"])
        if mem is None:
            continue
        ids = offer_ids.get(b["id"]) or last_batch.get(b["id"], [])
        tier = PATH_TIER.get(b["path"]) or ("claude:inbox" if b["path"] == "inbox" else
                                             _tier_at(tiers.get(mem.participant_id, []), b["created_at"], mem.tier))
        for mid_ in ids:
            key = (mem.membership_id, mid_)
            msg = msgs.get(mid_)
            if key in seen or msg is None:
                continue
            seen.add(key)
            reason = PRIO_REASON.get(prio.get(key, 0), "chatter")
            first_by_path[b["path"]] += 1
            for label, secs, main in _metrics(b["path"], b["kind"], b, msg["ts"]):
                key5 = (mem.harness, tier, b["path"], reason, label)
                if held(mem, b["path"], msg["ts"], msg["ts"] + secs):
                    held_samples[key5].append(max(0.0, secs))
                    continue
                samples[key5].append(max(0.0, secs))
                if main:
                    main_samples[key5].append(max(0.0, secs))

    order = {lab: i for i, lab in enumerate(LABEL_ORDER)}
    rord = {r: i for i, r in enumerate(REASON_ORDER)}

    def rollup(field: str, pick: Any, main_only: bool) -> list[dict[str, Any]]:
        agg: dict[tuple[str, str], list[float]] = defaultdict(list)
        for (h, tier, path, reason, label), xs in (main_samples if main_only else samples).items():
            agg[(pick(h, tier, reason), label)] += xs
        out = [{field: k, "label": lab, **_stats(xs)} for (k, lab), xs in agg.items()]
        out.sort(key=lambda r: (rord.get(r[field], 9) if field == "reason" else r[field],
                                order.get(r["label"], 9)))
        return out

    def flat(src: dict[tuple[str, str, str, str, str], list[float]]) -> list[dict[str, Any]]:
        out = [{"harness": h, "tier": tier, "path": path, "reason": reason, "label": label, **_stats(xs)}
               for (h, tier, path, reason, label), xs in src.items()]
        out.sort(key=lambda r: (r["harness"], r["tier"], r["path"], rord.get(r["reason"], 9),
                                order.get(r["label"], 9)))
        return out

    detail = flat(samples)
    held_detail = flat(held_samples)
    by_harness = rollup("harness", lambda h, t, rs: h, False)
    by_tier = rollup("tier", lambda h, t, rs: t, False)
    by_reason = rollup("reason", lambda h, t, rs: rs, True)

    # ---- per member
    posts = Counter(r["sender_membership_id"] for r in msgs.values()
                    if r["sender_kind"] == "agent" and r["kind"] == "chat")
    passes = Counter(e["membership_id"] for e in room_ev if e["kind"] == "pass")
    rate_limited = Counter(e["membership_id"] for e in room_ev if e["kind"] == "rate_limited")
    rearms = Counter(e["membership_id"] for e in room_ev if e["kind"] == "rearm")
    turns = Counter(e["participant_id"] for e in part_ev if e["kind"] == "turn_start")
    # the last model each session reported by the end of the window (one reported before the
    # window, e.g. at its join, still names it); scrubbed like any free-form string
    models: dict[int, str] = {}
    for e in part_all:
        if e["kind"] == "model" and e["ts"] <= end:
            mdl = _data(e["data"]).get("model")
            if isinstance(mdl, str):
                models[e["participant_id"]] = _safe(mdl)
    parked = _parked_spells([e for e in room_all if e["kind"] in ("parked", "unparked")], start, end)
    open_d = defaultdict(Counter)
    for r in db.q("SELECT d.membership_id, d.state, d.notified_at FROM deliveries d"
                  " JOIN memberships m ON m.id=d.membership_id WHERE m.room_id=? AND d.state IN ('pending','offered')",
                  rid):
        k = "offered" if r["state"] == "offered" else ("stubs" if r["notified_at"] is not None else "pending")
        open_d[r["membership_id"]][k] += 1

    agents = []
    for m in members:
        mb = [b for b in batches if b["membership_id"] == m.membership_id]
        wakes = [b for b in mb if b["kind"] == "wake"]
        agents.append({
            "name": m.name,
            "host": _safe(m.host) or "",
            "harness": m.harness,
            # the tier when the window ended (a teardown afterwards may have changed the row)
            "tier": _safe(_tier_at(tiers.get(m.participant_id, []), end, m.tier)),
            "tier_note": _safe(m.tier_note) if _tier_at(tiers.get(m.participant_id, []), end, m.tier) == m.tier
            else None,
            "model": models.get(m.participant_id),
            "approval_mode": m.approval_mode,
            "status_now": m.status if m.left_at is None else f"left ({_safe(m.left_reason) or '?'})",
            "turns": turns.get(m.participant_id, 0),
            "wakes": {
                "offered": len(wakes),
                "confirmed": sum(1 for b in wakes if b["state"] == "confirmed"),
                "expired": sum(1 for b in wakes if b["state"] == "expired"),
                "cancelled": sum(1 for b in wakes if b["state"] == "cancelled"),
                "budget_counted": sum(1 for b in wakes if b["budget_counted"]) + rearms.get(m.membership_id, 0),
                "by_kind": dict(sorted(Counter(b["wake_kind"] or "-" for b in wakes).items())),
                "reminders": sum(1 for b in wakes if b["wake_reason"] == "reminder"),
            },
            "continuations": sum(1 for b in mb if b["path"] in CONTINUE_PATHS) + rearms.get(m.membership_id, 0),
            "rearms": rearms.get(m.membership_id, 0),
            "mid_task": sum(1 for b in mb if b["kind"] == "priority"),
            "pulls": sum(1 for b in mb if b["kind"] == "pull"),
            "posts": posts.get(m.membership_id, 0),
            "passes": passes.get(m.membership_id, 0),
            "rate_limited": rate_limited.get(m.membership_id, 0),
            "parked": {"spells": len(parked.get(m.membership_id, [])),
                       "seconds": round(sum(x for x, _ in parked.get(m.membership_id, [])), 1),
                       "reasons": dict(sorted(Counter(r for _, r in parked.get(m.membership_id, [])).items()))},
            "undelivered_at_end": dict(open_d.get(m.membership_id, {})),
        })

    # ---- rules that fired
    kinds = Counter(e["kind"] for e in room_ev)
    esc = Counter(str(_data(e["data"]).get("why", "?")) for e in room_ev if e["kind"] == "watchdog_escalate")
    expire = Counter(f"{_data(e['data']).get('path', '?')}:{_safe(str(_data(e['data']).get('reason', '?')))}"
                     for e in room_ev if e["kind"] == "expire")
    cancel = Counter(str(_data(e["data"]).get("path", "?")) for e in room_ev if e["kind"] == "cancel")
    requeue = Counter(str(_data(e["data"]).get("reason", "?")) for e in room_ev if e["kind"] == "requeue")
    # approval prompts: paired over the whole history (a prompt that closed after the window still
    # has its length), kept when one opened inside the window while its session was in the room
    holds_approval, stalls = _approval_spells([e for e in part_all if e["kind"] == "status"], members, now,
                                              keep=in_room)
    # Codex holds name no participant or room: counted inside the window when a Codex agent was here
    codex_holds = (db.q("SELECT COUNT(*) FROM events WHERE kind='codex_hold' AND ts>=? AND ts<=?",
                        start, end)[0][0] if any(m.harness == "codex" for m in members) else 0)
    rules_ = {
        "loop_guard": kinds.get("loop_guard", 0),
        "budget_exhausted": kinds.get("budget_exhausted", 0),
        "rate_limited": kinds.get("rate_limited", 0),
        "pass_refused": kinds.get("pass_refused", 0),
        "watchdog_remind": kinds.get("watchdog_remind", 0),
        "watchdog_escalate": dict(sorted(esc.items())),
        "redeliver": sum(n for r, n in requeue.items() if r == "redeliver"),
        "expired": dict(sorted(expire.items())),
        "cancelled_by_pause": dict(sorted(cancel.items())),
        "pause": kinds.get("pause", 0),
        "resume": kinds.get("resume", 0),
        "budget_set": kinds.get("budget_set", 0),
        "hops_set": kinds.get("hop_limit_set", 0),
        "hold": kinds.get("hold", 0),
        "release": kinds.get("release", 0),
        "kick": kinds.get("kick", 0),
        # /catchup requests; 0.2.0's /review events count here too (§26)
        "catchup": kinds.get("catchup", 0) + kinds.get("review", 0),
        "approval_holds": len(holds_approval),
        "codex_holds": codex_holds,
        "rearm": kinds.get("rearm", 0),
        "parked": sum(len(v) for v in parked.values()),
        "cursor_parks": sum(1 for e in part_ev if e["kind"] == "park"),
        "degraded": sum(1 for e in part_ev if e["kind"] == "tier" and _data(e["data"]).get("what") == "degraded"),
    }

    humans = sum(1 for r in msgs.values() if r["sender_kind"] == "human" and r["kind"] == "chat")
    agent_msgs = sum(1 for r in msgs.values() if r["sender_kind"] == "agent" and r["kind"] == "chat")
    return {
        "room": room["name"],
        "window": {"start": iso(start), "end": iso(end), "minutes": round((end - start) / 60.0, 1)},
        "settings": {
            "budget_per_hour": room["budget_per_hour"],
            "budget_remaining_at_end": room["budget_remaining"],
            "hop_limit": room["hop_limit"],
            "paused_at_end": bool(room["paused"]),
            "paused_reason": _safe(room["paused_reason"]),
        },
        "traffic": {"human_messages": humans, "agent_messages": agent_msgs,
                    "passes": sum(passes.values()), "members": len(members)},
        "latency": {"by_harness": by_harness, "by_tier": by_tier, "by_reason": by_reason, "detail": detail,
                    "held": held_detail, "first_delivery_paths": dict(sorted(first_by_path.items())),
                    "room_paused_s": round(sum(max(0.0, min(b, end) - max(a, start)) for a, b in pauses), 1)},
        "agents": agents,
        "rules": rules_,
        "stalls": stalls,
        "approval_holds": holds_approval,
    }


def _room_pauses(room_ev: list[sqlite3.Row], now: float) -> list[tuple[float, float]]:
    """(start, end) of each spell the room was paused (``/pause`` or the loop guard, until ``/resume``)."""
    out: list[tuple[float, float]] = []
    since: float | None = None
    for e in room_ev:
        if e["kind"] in ("pause", "loop_guard") and since is None:
            since = e["ts"]
        elif e["kind"] == "resume" and since is not None:
            out.append((since, e["ts"]))
            since = None
    if since is not None:
        out.append((since, now))
    return out


def _member_holds(room_ev: list[sqlite3.Row], now: float) -> dict[int, list[tuple[float, float]]]:
    """(start, end) of each ``/hold`` per membership, until ``/release``."""
    out: dict[int, list[tuple[float, float]]] = defaultdict(list)
    since: dict[int, float] = {}
    for e in room_ev:
        mid = _data(e["data"]).get("membership_id")
        if not isinstance(mid, int):
            continue
        if e["kind"] == "hold" and mid not in since:
            since[mid] = e["ts"]
        elif e["kind"] == "release" and mid in since:
            out[mid].append((since.pop(mid), e["ts"]))
    for mid, t in since.items():
        out[mid].append((t, now))
    return out


def _approval_intervals(status_ev: list[sqlite3.Row], now: float) -> dict[int, list[tuple[float, float]]]:
    """(start, end) of each approval prompt per participant, from its status events."""
    out: dict[int, list[tuple[float, float]]] = defaultdict(list)
    since: dict[int, float] = {}
    for e in status_ev:
        d = _data(e["data"])
        pid = e["participant_id"]
        if d.get("to") == "waiting-approval" and pid not in since:
            since[pid] = e["ts"]
        elif d.get("frm") == "waiting-approval" and pid in since:
            out[pid].append((since.pop(pid), e["ts"]))
    for pid, t in since.items():
        out[pid].append((t, now))
    return out


def _approval_spells(status_ev: list[sqlite3.Row], members: list[Member], now: float,
                     keep: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Approval prompts from status events: (every spell, the stalls over 60 s). ``keep(pid, t0)``
    picks the spells to list (the window and the membership); a prompt still open runs to ``now``."""
    names: dict[int, str] = {}
    for m in members:
        names.setdefault(m.participant_id, m.name)
    open_at: dict[int, float] = {}
    spells: list[tuple[int, float, dict[str, Any]]] = []
    for e in status_ev:
        d = _data(e["data"])
        pid = e["participant_id"]
        if d.get("to") == "waiting-approval" and pid not in open_at:
            open_at[pid] = e["ts"]
        elif d.get("frm") == "waiting-approval" and pid in open_at:
            t0 = open_at.pop(pid)
            spells.append((pid, t0, {"name": names.get(pid, "?"), "at": iso(t0), "seconds": round(e["ts"] - t0, 1),
                                     "ended_by": _safe(str(d.get("to", "?"))), "ongoing": False}))
    for pid, t0 in open_at.items():
        spells.append((pid, t0, {"name": names.get(pid, "?"), "at": iso(t0), "seconds": round(now - t0, 1),
                                 "ended_by": None, "ongoing": True}))
    out = [sp for pid, t0, sp in sorted(spells, key=lambda x: x[1]) if keep is None or keep(pid, t0)]
    return out, [s for s in out if s["seconds"] > STALL_S]


def _parked_spells(park_ev: list[sqlite3.Row], start: float, end: float) -> dict[int, list[tuple[float, str]]]:
    """(seconds, reason) of each parked spell per membership (§8.5, "needs a poke") that began inside
    the window, from the engine's ``parked``/``unparked`` events; one still open runs to the end."""
    out: dict[int, list[tuple[float, str]]] = defaultdict(list)
    open_at: dict[int, tuple[float, str]] = {}
    for e in park_ev:
        mid = e["membership_id"]
        if mid is None:
            continue
        if e["kind"] == "parked" and mid not in open_at:
            open_at[mid] = (e["ts"], _safe(str(_data(e["data"]).get("reason") or "?")))
        elif e["kind"] == "unparked" and mid in open_at:
            t0, why = open_at.pop(mid)
            if start <= t0 <= end:
                out[mid].append((max(0.0, min(e["ts"], end) - t0), why))
    for mid, (t0, why) in open_at.items():
        if start <= t0 <= end:
            out[mid].append((max(0.0, end - t0), why))
    return out


# ---------------------------------------------------------------- markdown
def _row(cells: list[Any]) -> str:
    return "| " + " | ".join("" if c is None else str(c) for c in cells) + " |"


def _lat_cells(r: dict[str, Any]) -> list[str]:
    p50 = None if r["p50_ms"] is None else r["p50_ms"] / 1000
    p95 = None if r["p95_ms"] is None else r["p95_ms"] / 1000
    mx = None if r["max_ms"] is None else r["max_ms"] / 1000
    return [str(r["n"]), fmt_s(p50), fmt_s(p95), fmt_s(mx)]


def render_markdown(rep: dict[str, Any], *, title: str | None = None) -> str:
    w, s, t = rep["window"], rep["settings"], rep["traffic"]
    L: list[str] = []
    L.append(f"# {title or 'switchboard report: ' + rep['room']}")
    L.append("")
    L.append(f"Room `{rep['room']}`, {w['start']} to {w['end']} ({w['minutes']} min, UTC). "
             f"{t['members']} agent membership(s); {t['human_messages']} message(s) from the human, "
             f"{t['agent_messages']} from agents, {t['passes']} pass(es). Budget {s['budget_remaining_at_end']}"
             f"/{s['budget_per_hour']} left at the end, hop limit {s['hop_limit']}"
             + (" (loop guard off)" if s["hop_limit"] == 0 else "")
             + (f", paused at the end ({s['paused_reason']})." if s["paused_at_end"] else "."))
    L.append("")
    L.append("## Latency: message sent to the recipient")
    L.append("")
    L.append("Each message counts once per recipient, at the first batch that reached it (deliveries held by a"
             " pause, a /hold or an approval prompt are listed separately, below). T0 is the message's"
             " time on the broker clock. **turn start**: the new turn began (Claude: the UserPromptSubmit the inbox"
             " message started; Codex: `thread/status` went active after `turn/start`, or the UserPromptSubmit of a"
             " `codex queue` item). **first hook**: the first hook after a Cursor follow-up or a Devin Stop"
             " message. **in context**: a Devin `wait()` answer's PostToolUse, or mid-task context confirmed"
             " (hook ack, steer). **first action**: the tool call that followed a Devin `wait()` answer."
             " **pulled**: the agent's own `read()`/`say()` answer. Chatter includes the room's 3 s quiet period."
             " \"By reason\" uses each sample's main measure: turn start, first hook, first action (or in"
             " context when no tool call followed a `wait()` answer), in context for mid-task, pulled.")
    L.append("")
    L.append("### By harness")
    L.append("")
    L.append(_row(["harness", "measure", "n", "p50", "p95", "max"]))
    L.append(_row(["---"] * 6))
    for r in rep["latency"]["by_harness"]:
        L.append(_row([r["harness"], r["label"], *_lat_cells(r)]))
    if not rep["latency"]["by_harness"]:
        L.append(_row(["(none)", "", "0", "n/a", "n/a", "n/a"]))
    L.append("")
    L.append("### By tier")
    L.append("")
    L.append(_row(["tier", "measure", "n", "p50", "p95", "max"]))
    L.append(_row(["---"] * 6))
    for r in rep["latency"]["by_tier"]:
        L.append(_row([f"`{r['tier']}`", r["label"], *_lat_cells(r)]))
    L.append("")
    L.append("### By reason (each path's main measure)")
    L.append("")
    L.append(_row(["reason", "measure", "n", "p50", "p95", "max"]))
    L.append(_row(["---"] * 6))
    for r in rep["latency"]["by_reason"]:
        L.append(_row([r["reason"], r["label"], *_lat_cells(r)]))
    L.append("")
    L.append("### Detail")
    L.append("")
    L.append(_row(["harness", "tier", "path", "reason", "measure", "n", "p50", "p95", "max"]))
    L.append(_row(["---"] * 9))
    for r in rep["latency"]["detail"]:
        L.append(_row([r["harness"], f"`{r['tier']}`", f"`{r['path']}`", r["reason"], r["label"], *_lat_cells(r)]))
    L.append("")
    L.append("### Held by a pause, a /hold or an approval prompt")
    L.append("")
    held = rep["latency"]["held"]
    L.append("Deliveries that a room pause (`/pause` or the loop guard), a `/hold` or an approval prompt open in the"
             " recipient's session held up: their latency includes the hold, so they are kept out of the tables"
             " above. Pulls (`read()`/`say()` answers) are never held: the agent asked for them."
             f" The room was paused for {fmt_s(rep['latency']['room_paused_s'])} of the window.")
    L.append("")
    if held:
        L.append(_row(["harness", "tier", "path", "reason", "measure", "n", "p50", "p95", "max"]))
        L.append(_row(["---"] * 9))
        for r in held:
            L.append(_row([r["harness"], f"`{r['tier']}`", f"`{r['path']}`", r["reason"], r["label"],
                           *_lat_cells(r)]))
    else:
        L.append("None.")
    L.append("")
    L.append("## Agents")
    L.append("")
    L.append(_row(["agent", "harness", "model", "tier at end", "turns", "wakes (confirmed/offered)",
                   "continuations", "mid-task", "posts", "passes", "rate-limited", "parked", "undelivered at end"]))
    L.append(_row(["---"] * 13))
    for a in rep["agents"]:
        wk = a["wakes"]
        und = a["undelivered_at_end"]
        und_s = ", ".join(f"{k} {v}" for k, v in sorted(und.items())) or "0"
        tier = f"`{a['tier']}`" + (f" ({a['tier_note']})" if a["tier_note"] else "")
        pk = a["parked"]
        pk_s = f"{pk['spells']} ({fmt_s(pk['seconds'])})" if pk["spells"] else "0"
        who = f"{a['name']}@{a['host']}" if a.get("host") else a["name"]
        L.append(_row([who, a["harness"], a["model"] or "-", tier, a["turns"],
                       f"{wk['confirmed']}/{wk['offered']}", a["continuations"], a["mid_task"], a["posts"],
                       a["passes"], a["rate_limited"], pk_s, und_s]))
    L.append("")
    L.append("Turns are `turn_start` events (a UserPromptSubmit that began a turn: your own prompts and switchboard's"
             " wakes; a Devin agent in its `wait()` loop stays in one turn). Wakes are batches that could start a"
             " turn or return a `wait()` (counted against the budget, with Devin re-arms); continuations are"
             " Cursor follow-ups, Devin Stop messages and re-arms. Turns and approval prompts count only while"
             " the agent was a member of the room, inside the window. \"Parked\": spells with messages waiting"
             " and no way to wake the agent (\"needs a poke\"), and their total time. \"Undelivered\": pending"
             " (wake-eligible), stubs (already announced, `read()` only) or offered at the end.")
    L.append("")
    L.append("## Rules that fired")
    L.append("")
    r = rep["rules"]
    L.append(_row(["rule", "count", "detail"]))
    L.append(_row(["---"] * 3))
    L.append(_row(["loop guard (room paused after the hop limit)", r["loop_guard"], ""]))
    L.append(_row(["budget exhausted", r["budget_exhausted"], ""]))
    L.append(_row(["rate limit (say refused)", r["rate_limited"], ""]))
    L.append(_row(["read first (pass refused: a stub not read yet)", r.get("pass_refused", 0), ""]))
    L.append(_row(["watchdog reminders", r["watchdog_remind"], ""]))
    L.append(_row(["watchdog notices to the human", sum(r["watchdog_escalate"].values()),
                   ", ".join(f"{k} {v}" for k, v in r["watchdog_escalate"].items())]))
    L.append(_row(["re-deliver once (seen, not answered)", r["redeliver"], ""]))
    L.append(_row(["offers expired", sum(r["expired"].values()),
                   ", ".join(f"`{k}` {v}" for k, v in r["expired"].items())]))
    L.append(_row(["offers cancelled by a pause", sum(r["cancelled_by_pause"].values()),
                   ", ".join(f"`{k}` {v}" for k, v in r["cancelled_by_pause"].items())]))
    L.append(_row(["/pause", r["pause"], ""]))
    L.append(_row(["/resume", r["resume"], ""]))
    L.append(_row(["/budget n", r["budget_set"], ""]))
    L.append(_row(["/hops n (hop limit changed)", r.get("hops_set", 0), ""]))
    L.append(_row(["/hold, /release", f"{r['hold']}, {r['release']}", ""]))
    L.append(_row(["/kick", r["kick"], ""]))
    L.append(_row(["/catchup (catch-up requests posted, /review included)", r.get("catchup", 0), ""]))
    L.append(_row(["approval holds (a prompt was open)", r["approval_holds"], ""]))
    L.append(_row(["Codex holds (a TUI left the daemon)", r["codex_holds"], ""]))
    L.append(_row(["Devin re-arms", r["rearm"], ""]))
    L.append(_row(["parked (needs a poke)", r["parked"], ""]))
    L.append(_row(["Cursor parks", r["cursor_parks"], f"degraded {r['degraded']}"]))
    L.append("")
    L.append("## Stalls and approval prompts")
    L.append("")
    if not rep["approval_holds"]:
        L.append("No approval prompt was seen (Claude registry, Codex app-server).")
    else:
        L.append(f"A stall is an approval prompt open longer than {STALL_S:.0f} s; switchboard holds deliveries"
                 " while one is open, and nothing answers it but the human.")
        L.append("")
        L.append(_row(["agent", "opened (UTC)", "open for", "then", "stall"]))
        L.append(_row(["---"] * 5))
        for sp in rep["approval_holds"]:
            L.append(_row([sp["name"], sp["at"], fmt_s(sp["seconds"]), sp["ended_by"] or "still open",
                           "**stalled**" if sp["seconds"] > STALL_S else ""]))
    L.append("")
    return "\n".join(L)
