"""Write ``v0_37_2.sql``: a schema-28 database made by switchboard 0.37.2, dumped as SQL.

The migrations that retire the links and the local path (DESIGN.md §49.8, #385: schema 29 and 30) are
tested on this dump (``tests/unit/test_db_migrate_v29.py``). 0.37.2 is the last release with both
ways for an agent to reach the broker, so the database has both:

- agents of the local path, one of each harness, with made-up process ids and a Claude inbox socket;
- an agent of an SSH remote and one of a machine that dials in, each on its own host, with the
  owner's consent for the remote and the machine's pairing in their tables;
- agents over HTTPS on two devices (no pid: ``mcp_pid`` 0), with an agent token each, one revoked;
- rooms (one private, one closed), people, a passkey, web sessions, messages, batches and deliveries
  in several states, claims, a pin, a review board, events and preferences.

The rows come from 0.37.2's own store and delivery engine, as ``make_v0_7_0.py``'s do from 0.7.0's. No
broker, no processes, a fixed clock, made-up pids, ids and keys, nobody's data.

It must run against the 0.37.2 code (schema version 28), not this checkout's:

    git worktree add --detach <tmp>/sb-0372 v0.37.2
    cd <tmp>/sb-0372 && uv sync
    uv run python <this repo>/tests/fixtures/db/make_v0_37_2.py <this repo>/tests/fixtures/db/v0_37_2.sql

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
from switchboard.models import HookEvent, closed_room_name
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
    if db.SCHEMA_VERSION != 28:
        raise SystemExit(f"run this with switchboard 0.37.2 (schema 28), not schema {db.SCHEMA_VERSION}")
    clock = Clock(T0)
    cfg = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
    store = Store(db.open_db(path), clock)
    engine = Engine(store, clock, cfg, build_adapters(cfg), SinkRegistry(), key=b"k" * 32, test_mode=True)
    d = cfg.delivery
    build_room = store.create_room("#build", "alice", d.budget_per_hour, d.hop_limit)
    bench_room = store.create_room("#bench", "alice", d.budget_per_hour, d.hop_limit)
    old_room = store.create_room("#old", "alice", d.budget_per_hour, d.hop_limit)
    secret = store.create_room("#secret", "alice", d.budget_per_hour, d.hop_limit, private_for=0)
    clock.advance(1.0)

    # people: the owner claimed the broker; bob was added (one-time password, then a passkey)
    store.claim_owner(bytes.fromhex("00112233445566778899aabbccddeeff"))
    store.passkey_add(
        b"cred-laptop-0001", b"cose-key-laptop", "MacBook Touch ID", "adce0002-35bc-c60a-648b-0b25f1f05503", 3
    )
    bob = store.person_add(
        "bob",
        bytes.fromhex("ffeeddccbbaa99887766554433221100"),
        h("one-time"),
        3600.0,
        first_name="Bob",
        last_name="Builder",
    )
    store.passkey_add(b"cred-bob-0001", b"cose-key-bob", "Bob's phone", None, 0, person_id=bob.id)
    store.room_person_add(secret.id, bob.id, 0)
    store.set_preferences(None, {"theme": "dark", "text_size": "large"})
    store.set_preferences(bob.id, {"hop_limit": 12})
    store.web_session_create(h("web-session-1"), 7 * 24 * 3600.0, "claim")
    store.web_session_create(h("web-session-2"), 7 * 24 * 3600.0, "passkey:Bob's phone", person_id=bob.id)
    # agent tokens: the owner's for the laptop, bob's for his desktop, an old revoked one
    laptop_token = store.agent_token_add(None, "laptop", h("token-laptop"))
    store.agent_token_add(bob.id, "desktop", h("token-desktop"))
    old_token = store.agent_token_add(None, "old-box", h("token-old-box"))
    assert laptop_token is not None and old_token is not None
    store.agent_token_revoke(old_token.id, None)

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

    def https(harness: str, thread: str, device: str, **kw):
        """A session over HTTPS: no process, so its MCP pid is 0 on its device's host."""
        base = dict(
            agent_pid=None,
            agent_start=None,
            mcp_pid=0,
            mcp_start=0.0,
            host=device,
            status="idle",
            status_at=clock.now(),
            status_src="hook",
            tier=f"{harness}:https",
            tier_note="over HTTPS: woken at stop",
            session_id=thread,
            approval_mode="prompting",
            hooks_seen_at=clock.now(),
        )
        base.update(kw)
        return store.upsert_participant(harness, f"{harness}@{device}:{thread}", **base)

    def join(room, p, name: str):
        m = store.create_membership(room.id, p.id, name, h("cred-" + name))
        store.add_event(
            "join",
            room_id=room.id,
            membership_id=m.id,
            participant_id=p.id,
            data={"harness": p.harness, "tier": p.tier},
        )
        where = f"{p.harness} on {p.host}" if p.host else p.harness
        store.insert_message(
            room.id,
            sender_name=name,
            sender_kind="agent",
            via="mcp",
            kind="join",
            sender_membership_id=m.id,
            sender_harness=p.harness,
            sender_host=p.host or None,
            text=f"joined ({where}, {p.tier})",
        )
        clock.advance(0.5)
        return m

    # the local path: one agent of each harness
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
        env_leak=1,
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
    # an agent of an SSH remote (its pids are that host's), with the owner's consent
    bench = part(
        "claude",
        f"claude@fpga-pi:1000041001@{T0 - 600.0:.2f}",
        1000041001,
        host="fpga-pi",
        tier="claude:inbox",
        claude_socket="/tmp/yk-inbox-remote.sock",
        approval_mode="prompting",
        session_id="00000000-0000-4000-8000-00000000be9c",
    )
    store.set_remote_enabled("fpga-pi", h("config-fpga-pi"), "cli")
    store.touch_remote_up("fpga-pi")
    # machines that dial in: approved, pending and removed (made-up keys), one with an agent
    store.machine_pair(
        "work-laptop",
        b"k" * 32,
        "SHA256:workLaptopFingerprintMadeUp000000000000000",
        {"os": "darwin", "version": "0.37.2", "harnesses": ["claude", "codex"]},
    )
    store.machine_approve("work-laptop", "web")
    store.machine_seen("work-laptop", clock.now())
    store.machine_pair(
        "lab-box", b"l" * 32, "SHA256:labBoxFingerprintMadeUp0000000000000000000", {"os": "linux"}
    )
    store.machine_pair("old-desk", b"o" * 32, "SHA256:oldDeskFingerprintMadeUp000000000000000000", {})
    store.machine_approve("old-desk", "cli")
    store.machine_remove("old-desk")
    dialed = part(
        "claude",
        f"claude@work-laptop:2000041001@{T0 - 600.0:.2f}",
        2000041001,
        host="work-laptop",
        tier="claude:inbox",
        claude_socket="/tmp/yk-inbox-laptop.sock",
        approval_mode="prompting",
        session_id="00000000-0000-4000-8000-0000001a9709",
    )
    # agents over HTTPS: a Codex thread and a Claude Code session on the laptop, Cursor on bob's desktop
    web_codex = https("codex", "00000000-0000-4000-8000-0000000c0de2", "laptop")
    web_claude = https("claude", "00000000-0000-4000-8000-00000000c1a1", "laptop")
    web_cursor = https("cursor", "conv-00000000-0002", "desktop", approval_mode="unknown")
    engine.ack_modes[bot.id] = "next_call"

    mc = join(build_room, claude, "vivado")
    mx = join(build_room, codex, "codex-1")
    mu = join(build_room, cursor, "cursor-1")
    md = join(bench_room, devin, "devin-1")
    mb = join(build_room, bot, "bot-a")
    join(build_room, gone, "helper")
    mr = join(bench_room, bench, "bench")
    ml = join(build_room, dialed, "laptop-1")
    mw = join(build_room, web_codex, "web-codex")
    join(build_room, web_claude, "web-claude")
    join(secret, web_cursor, "web-cursor")
    m_old = join(old_room, claude, "vivado")
    store.add_event("login", data={"what": "claim", "via": "web", "passkey": "MacBook Touch ID"})
    store.add_event("machine", data={"what": "approve", "name": "work-laptop", "via": "web"})
    store.add_event("remote", data={"what": "enable", "name": "fpga-pi", "via": "cli"})
    store.add_event("agent_tokens", data={"what": "add", "device": "laptop", "by": "alice"})

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
    engine.pull(p_bot, mb, "read", 20)  # a pull batch, offered
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
        text="built; @bot-a please flash it, @web-codex review it",
        mentions=("bot-a", "web-codex"),
    )
    engine.on_message(msg3.id)
    clock.advance(1.0)
    # an agent over HTTPS answers, and a hook of its own session reports
    msg4 = store.insert_message(
        build_room.id,
        sender_name="web-codex",
        sender_kind="agent",
        via="mcp",
        sender_membership_id=mw.id,
        sender_harness="codex",
        sender_host="laptop",
        text="reviewed: the constraints file is missing a pin",
        reply_to=msg3.id,
    )
    engine.on_message(msg4.id)
    ev = HookEvent(harness="codex", event="Stop", sid=web_codex.session_id, t=clock.now())
    engine.claim_for_hook(store.get_participant(web_codex.id), ev)
    clock.advance(1.0)
    # the SSH remote's agent and the one on the machine that dialed in report from their hosts
    msg5 = store.insert_message(
        bench_room.id,
        sender_name="bench",
        sender_kind="agent",
        via="mcp",
        sender_membership_id=mr.id,
        sender_harness="claude",
        sender_host="fpga-pi",
        text="flashed; UART shows PORT_OK 8080",
    )
    engine.on_message(msg5.id)
    clock.advance(1.0)
    msg6 = store.insert_message(
        build_room.id,
        sender_name="laptop-1",
        sender_kind="agent",
        via="mcp",
        sender_membership_id=ml.id,
        sender_harness="claude",
        sender_host="work-laptop",
        text="the UART log looks right to me",
        reply_to=msg4.id,
    )
    engine.on_message(msg6.id)
    store.pin_add(build_room.id, msg3.id, "alice")
    store.move_claim(build_room.id, mw.id, "reviewing top.bit", 600)
    store.move_claim(build_room.id, mc.id, "ticket-7", 600)
    board = store.open_review(build_room.id, "https://example.com/shop/pull/7", "abc1234", "vivado")
    finding = store.add_review_item(
        board["id"], "finding", "raised", title="a pin is missing", raised_by="web-codex", file="top.xdc"
    )
    question = store.add_review_item(
        board["id"],
        "question",
        "open",
        title="which bank?",
        raised_by="vivado",
        options=["A", "B"],
        recommend=0,
    )
    store.set_review_pick(question.id, "web-codex", 1)
    assert finding.n == 1
    store.insert_message(
        secret.id,
        sender_name="bob",
        sender_kind="human",
        via="web",
        text="between us",
        sender_person_id=bob.id,
    )
    clock.advance(1.0)
    # a Devin wait() in #bench that a human message answers
    store.insert_message(
        bench_room.id,
        sender_name="alice",
        sender_kind="human",
        via="cli",
        text="devin-1: review the UART test when you can",
    )
    engine.pull(store.get_participant(devin.id), md, "wait", 20)
    clock.advance(2.0)
    # an offer that expires, a notice, a command, a leave and an ended session
    _t, bid2, *_ = engine.pull(store.get_participant(codex.id), mx, "read", 20)
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
    # #old is closed: its name is free again and its history is kept
    store.end_membership(m_old.id, "closed", keep_cred=True)
    store.rename_room(old_room.id, closed_room_name("#old", old_room.id), expect="#old")
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
