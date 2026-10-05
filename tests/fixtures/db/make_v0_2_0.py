"""Write ``v0_2_0.sql``: a schema-v1 database made by switchboard 0.2.0, dumped as SQL.

The v1 -> v2 migration (DESIGN.md §27.6) is tested on this dump
(``tests/unit/test_db_migrate_v2.py``). The rows come from 0.2.0's own store and
delivery engine (rooms, participants of every harness, memberships, messages
of every kind, batches and deliveries in every state, events, a web session):
no broker, no processes, a fixed clock, made-up pids and ids, nobody's data.

It must run against the 0.2.0 code (schema version 1), not this checkout's:

    git worktree add --detach <tmp>/sb-020 v0.2.0
    cd <tmp>/sb-020 && uv sync
    uv run python <this repo>/tests/fixtures/db/make_v0_2_0.py <this repo>/tests/fixtures/db/v0_2_0.sql

It imports only the installed ``switchboard`` package, never this repo's tests.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

from switchboard import db
from switchboard.adapters import build_adapters
from switchboard.config import Config
from switchboard.delivery.engine import Engine
from switchboard.delivery.sinks import SinkRegistry
from switchboard.models import HookEvent
from switchboard.store import Store

T0 = 1_790_000_000.0


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, s: float) -> float:
        self.t += s
        return self.t


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def build(path: Path) -> dict[str, int]:
    if db.SCHEMA_VERSION != 1:
        raise SystemExit(f"run this with switchboard 0.2.0 (schema 1), not schema {db.SCHEMA_VERSION}")
    clock = Clock(T0)
    cfg = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
    store = Store(db.open_db(path), clock)
    engine = Engine(store, clock, cfg, build_adapters(cfg), SinkRegistry(), key=b"k" * 32, test_mode=True)
    d = cfg.delivery
    build_room = store.create_room("#build", "alice", d.budget_per_hour, d.hop_limit)
    review = store.create_room("#review", "alice", d.budget_per_hour, d.hop_limit)
    clock.advance(1.0)

    def part(harness: str, key: str, pid: int, **kw):
        base = dict(
            agent_pid=pid,
            agent_start=T0 - 600.0,
            mcp_pid=pid + 7,
            mcp_start=T0 - 599.5,
            status="idle",
            status_at=clock.now(),
            status_src="join",
            tier="mcp-only",
        )
        base.update(kw)
        return store.upsert_participant(harness, key, **base)

    def join(room, p, name: str):
        m = store.create_membership(room.id, p.id, name, h("cred-" + name))
        store.add_event(
            "join",
            room_id=room.id,
            membership_id=m.id,
            participant_id=p.id,
            data={"harness": p.harness, "tier": p.tier},
        )
        store.insert_message(
            room.id,
            sender_name=name,
            sender_kind="agent",
            via="mcp",
            kind="join",
            sender_membership_id=m.id,
            sender_harness=p.harness,
            text=f"joined ({p.harness}, {p.tier})",
        )
        clock.advance(0.5)
        return m

    claude = part(
        "claude",
        f"claude:41001@{T0 - 600.0:.2f}",
        41001,
        tier="claude:hook",
        claude_socket="/tmp/yk-inbox-41001.sock",
        approval_mode="prompting",
        session_id="00000000-0000-4000-8000-00000000c1a0",
        hooks_seen_at=clock.now(),
    )
    codex = part(
        "codex",
        "codex:00000000-0000-4000-8000-0000000c0de1",
        42001,
        thread_proof=1,
        session_id="00000000-0000-4000-8000-0000000c0de1",
        approval_mode="bypass",
    )
    cursor = part(
        "cursor",
        "cursor:conv-00000000-0001",
        43001,
        bind_state="bound",
        session_id="conv-00000000-0001",
        tier="cursor:stop-park",
    )
    devin = part("devin", f"devin:44001@{T0 - 600.0:.2f}", 44001, tier="devin:wait-loop")
    bot = part("test", "test:bot-a", 45001, status="busy")
    gone = part("unknown", f"unknown:46001@{T0 - 600.0:.2f}", 46001)
    engine.ack_modes[bot.id] = "next_call"

    mc = join(build_room, claude, "vivado")
    mx = join(build_room, codex, "codex-1")
    mu = join(build_room, cursor, "cursor-1")
    md = join(review, devin, "devin-1")
    mb = join(build_room, bot, "bot-a")
    mg = join(build_room, gone, "helper")
    store.web_session_create(h("web-session-1"), 7 * 24 * 3600.0)

    # the human asks, agents answer; the engine offers, confirms and expires batches
    msg1 = store.insert_message(
        build_room.id,
        sender_name="alice",
        sender_kind="human",
        via="web",
        text="@vivado build blinky and hand it to @bot-a",
        mentions=("vivado", "bot-a"),
    )
    engine.on_message(msg1.id)
    clock.advance(1.0)
    p_bot = store.get_participant(bot.id)
    _text, bid, _n, _more, _acts = engine.pull(p_bot, mb, "read", 20)  # a pull batch, offered
    clock.advance(0.5)
    engine.before_call(store.get_participant(bot.id))  # the next call confirms it
    store.mark_handled(mb.id)
    msg2 = store.insert_message(
        build_room.id,
        sender_name="bot-a",
        sender_kind="agent",
        via="mcp",
        sender_membership_id=mb.id,
        sender_harness="test",
        text="artifact: blinky/top.bit size:1024",
        reply_to=msg1.id,
        mentions=("vivado",),
    )
    engine.on_message(msg2.id)
    clock.advance(1.0)
    # a Claude hook: busy, then PostToolUse context
    ev = HookEvent(harness="claude", event="UserPromptSubmit", sid=claude.session_id, t=clock.now())
    engine.claim_for_hook(store.get_participant(claude.id), ev)
    clock.advance(0.5)
    ev = HookEvent(
        harness="claude", event="PostToolUse", sid=claude.session_id, tool="Bash", ok=True, t=clock.now()
    )
    engine.claim_for_hook(store.get_participant(claude.id), ev)
    clock.advance(1.0)
    msg3 = store.insert_message(
        build_room.id,
        sender_name="vivado",
        sender_kind="agent",
        via="mcp",
        sender_membership_id=mc.id,
        sender_harness="claude",
        text="built; @bot-a please flash it",
        mentions=("bot-a",),
    )
    engine.on_message(msg3.id)
    clock.advance(1.0)
    # a Devin wait() in #review that a human message answers
    store.insert_message(
        review.id,
        sender_name="alice",
        sender_kind="human",
        via="cli",
        text="devin-1: review the UART test when you can",
    )
    engine.pull(store.get_participant(devin.id), md, "wait", 20)
    clock.advance(2.0)
    # an offer that expires, a notice, a command, a leave and an ended session
    _t, bid2, _n, _m, _a = engine.pull(store.get_participant(codex.id), mx, "read", 20)
    if bid2 is not None:
        store.expire_batch(bid2, "disconnect")
    store.insert_message(
        build_room.id,
        sender_name="switchboard",
        sender_kind="system",
        via="system",
        kind="notice",
        text="#build: hop limit set to 6",
    )
    store.add_event("hops", room_id=build_room.id, data={"limit": 6})
    store.add_event("model", participant_id=claude.id, data={"model": "claude-test-model"})
    store.add_event(
        "pass",
        room_id=build_room.id,
        membership_id=mu.id,
        participant_id=cursor.id,
        data={"handled": 0, "note_len": 0},
    )
    clock.advance(1.0)
    ended = store.end_participant(gone.id, "session_end")
    for m in ended:
        engine.on_membership_ended(m.id, "session_end")
        store.insert_message(
            m.room_id,
            sender_name=m.screen_name,
            sender_kind="agent",
            via="system",
            kind="leave",
            sender_membership_id=m.id,
            sender_harness="unknown",
            text="left (session ended)",
        )
    store.set_status(codex.id, "offline", "mcp:bye")
    store.insert_message(
        build_room.id, sender_name="alice", sender_kind="human", via="web", text="thanks all"
    )
    engine.on_message(store.last_message_id(build_room.id))
    store.set_held(mu.id, True)
    store.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {t: store.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.TABLES}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(argv[1])
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "switchboard.db"
        counts = build(path)
        con = db.connect(path)
        sql = "\n".join(con.iterdump()) + "\n"
        con.close()
    out.write_text(sql, encoding="utf-8")
    print(f"wrote {out.name}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    empty = [k for k, v in counts.items() if not v]
    if empty:
        print("empty tables: " + ", ".join(empty), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
