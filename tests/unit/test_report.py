"""``switchboard report`` (DESIGN.md §12.5, §12.6): latency per path and label, first
delivery only, turns/wakes/continuations, posts vs passes, rules, stalls, the
model per participant, and no text, paths or emails in the output."""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import World
from test_claude_adapter import FakeConn, ad, claude, pushes, reg, tok
from test_devin_adapter import WAIT_TOOL, devin, open_wait, tokens

from switchboard import report
from switchboard.cli import main
from switchboard.models import Message


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)


def build(w: World, **kw: Any) -> dict[str, Any]:
    return report.build(w.store.con, "#build", now=w.clock.now(), **kw)


def row(rows: list[dict[str, Any]], **match: Any) -> dict[str, Any]:
    got = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    assert len(got) == 1, (match, rows)
    return got[0]


def quiet_msg(
    w: World,
    text: str,
    *,
    sender: str = "alice",
    kind: str = "human",
    mentions: tuple[str, ...] = (),
    membership: Any = None,
) -> Message:
    """A message with its delivery rows but no engine action (the test offers it by hand)."""
    extra: dict[str, Any] = {}
    if membership is not None:
        extra = {"sender_membership_id": membership.id, "sender_harness": "codex"}
    return w.store.insert_message(
        w.room.id,
        sender_name=sender,
        sender_kind=kind,
        via="web" if kind == "human" else "mcp",
        text=text,
        mentions=mentions,
        **extra,
    )


def offer(
    w: World,
    m: Any,
    msgs: list[Message],
    *,
    path: str,
    kind: str,
    wake_kind: str | None = None,
    reason: str | None = None,
    counted: bool = False,
) -> int:
    ids = [x.id for x in msgs]
    b = w.store.create_batch(
        m.id,
        path=path,
        kind=kind,
        items=[(i, True) for i in ids],
        wake_kind=wake_kind,
        wake_reason=reason,
        counted=counted,
    )
    w.store.add_event(
        "offer",
        room_id=w.room.id,
        membership_id=m.id,
        participant_id=m.participant_id,
        data={"batch_id": b.id, "path": path, "n": len(ids), "counted": counted, "ids": ids},
    )
    return b.id


# ------------------------------------------------------------------ helpers
def test_percentiles_follow_the_design() -> None:
    assert report.pctl([], 50) is None
    assert report.pctl([0.4], 95) == 0.4
    xs = [0.05, 0.07, 0.06, 0.2]
    assert report.pctl(xs, 50) == statistics.quantiles(xs, n=100, method="inclusive")[49]
    assert report.pctl(xs, 95) == pytest.approx(statistics.quantiles(xs, n=100, method="inclusive")[94])
    assert report.fmt_s(None) == "n/a" and report.fmt_s(0.0591) == "59 ms" and report.fmt_s(1.314) == "1.31 s"
    assert report.fmt_s(300) == "5.0 min"


def test_window_arguments() -> None:
    now = 1_790_000_000.0
    assert report.parse_window(None, None, now) is None
    assert report.parse_window(None, "2h", now) == now - 7200
    assert report.parse_window(None, "90m", now) == now - 5400
    assert report.parse_window("2026-09-25T14:00:00Z", None, now) == 1_790_344_800.0
    for bad in (("x", None), (None, "2 weeks"), ("2026-09-25", "1h")):
        with pytest.raises(report.ReportError):
            report.parse_window(*bad, now)


def test_scrubs_paths_and_emails() -> None:
    assert report._safe("queue guard: /Users/someone/.codex/x.sock gone") == "queue guard: <path> gone"
    assert report._safe("see ~/work and me@example.com") == "see <path> and <email>"
    assert report._safe(3) == 3


# --------------------------------------------------------- latency by path
def test_claude_inbox_idle_wakes_report_turn_start(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w)
    for i, d in enumerate((0.05, 0.07, 0.06)):
        reg(w, w.p(p), "idle")
        w.human(f"ping {i}")
        [push] = pushes(w)
        w.store.mark_posted(push.batch_id)
        clock.advance(d)
        w.hook(p, "UserPromptSubmit", gen=f"g{i}", tokens=(tok(push.text),), permission_mode="default")
        w.store.mark_handled(m.id)  # it answered: no re-delivery
        clock.advance(2.0)
        w.hook(p, "Stop", gen=f"g{i}")
        clock.advance(5.0)
    rep = build(w)
    r = row(rep["latency"]["detail"], harness="claude", path="inbox", label="turn start")
    assert r["tier"] == "claude:inbox" and r["reason"] == "human" and r["n"] == 3
    assert r["p50_ms"] == 60.0 and r["max_ms"] == 70.0
    assert row(rep["latency"]["by_harness"], harness="claude", label="turn start")["n"] == 3
    assert row(rep["latency"]["by_tier"], tier="claude:inbox", label="turn start")["p50_ms"] == 60.0
    [a] = rep["agents"]
    assert a["turns"] == 3 and a["wakes"]["confirmed"] == 3 and a["wakes"]["offered"] == 3
    assert a["wakes"]["budget_counted"] == 3 and a["wakes"]["by_kind"] == {"idle_wake": 3}
    assert rep["latency"]["first_delivery_paths"] == {"inbox": 3}


def test_devin_wait_loop_reports_in_context_and_first_action(w: World, clock: FakeClock) -> None:
    p, m = devin(w)
    w.store.update_participant(p.id, tier="devin:wait-loop")
    for i, (ctx, act) in enumerate(((0.04, 1.3), (0.06, 1.1))):
        open_wait(w, p, m, tuid=f"call_{i}", wid=f"w{i}")
        msg = w.human(f"ping {i}")
        bid = w.store.con.execute(
            "SELECT batch_id FROM deliveries WHERE message_id=? AND membership_id=?", (msg.id, m.id)
        ).fetchone()[0]
        clock.advance(ctx)
        w.hook(p, "PostToolUse", tool=WAIT_TOOL, tool_use_id=f"call_{i}", ok=True, tokens=tokens(bid, m))
        clock.advance(act)
        w.hook(p, "PreToolUse", tool="mcp__switchboard__say", tool_use_id=f"call_s{i}")
        w.store.mark_handled(m.id)
        clock.advance(3.0)
    rep = build(w)
    ctx = row(rep["latency"]["detail"], harness="devin", path="wait", label="in context")
    act = row(rep["latency"]["detail"], harness="devin", path="wait", label="first action")
    assert ctx["tier"] == "devin:wait-loop"
    assert ctx["n"] == 2 and ctx["p50_ms"] == 50.0
    assert act["n"] == 2 and act["p50_ms"] == pytest.approx(1250.0, abs=0.2)
    # the main measure for the wait path is the first action (M5's gate)
    assert row(rep["latency"]["by_reason"], reason="human", label="first action")["n"] == 2
    [a] = rep["agents"]
    assert a["wakes"]["by_kind"] == {"wait_return": 2} and a["turns"] == 0  # one long prompt: no new turns


def test_each_path_gets_its_label_and_tier(w: World, clock: FakeClock) -> None:
    px, mx = w.agent("codex-1", harness="codex", status="idle", hooks=True)
    w.store.update_participant(px.id, tier="codex:daemon")
    pu, mu = w.agent("cursor-1", harness="cursor", status="idle", hooks=True)
    w.store.add_event(
        "join",
        room_id=w.room.id,
        membership_id=mu.id,
        participant_id=pu.id,
        data={"harness": "cursor", "tier": "cursor:stop-park"},
    )
    a = quiet_msg(w, "task")
    t0 = a.ts
    clock.advance(0.04)
    b1 = offer(
        w, mx, [a], path="turn_start", kind="wake", wake_kind="idle_wake", reason="human", counted=True
    )
    w.store.set_batch_times(b1, turn_start_at=t0 + 0.045)
    w.store.confirm_batch(b1, "rpc:turn/start")
    b2 = offer(
        w, mu, [a], path="stop_followup", kind="wake", wake_kind="stop_cont", reason="human", counted=True
    )
    clock.advance(1.0)
    w.store.set_batch_times(b2, turn_start_at=clock.now())
    w.store.confirm_batch(b2, "hook:postToolUse")
    # mid-task: a steer confirmed 6 s later; a peer @mention via hook context
    s = quiet_msg(w, "also a test please")
    clock.advance(6.0)
    b3 = offer(w, mx, [s], path="steer", kind="priority", reason="human")
    w.store.confirm_batch(b3, "hook:UserPromptSubmit")
    peer = quiet_msg(
        w, "@cursor-1 look", sender="codex-1", kind="agent", mentions=("cursor-1",), membership=mx
    )
    clock.advance(2.5)
    b4 = offer(w, mu, [peer], path="hook_ctx", kind="priority", reason="mention")
    w.store.confirm_batch(b4, "hook_ack")
    # an explicit read(): pulled
    chat = quiet_msg(w, "fyi", sender="codex-1", kind="agent", membership=mx)
    clock.advance(9.0)
    b5 = offer(w, mu, [chat], path="read", kind="pull")
    w.store.confirm_batch(b5, "hook:postToolUse")
    rep = build(w)
    d = rep["latency"]["detail"]
    assert row(d, path="turn_start", label="turn start") | {} == {
        "harness": "codex",
        "tier": "codex:daemon",
        "path": "turn_start",
        "reason": "human",
        "label": "turn start",
        "n": 1,
        "p50_ms": 45.0,
        "p95_ms": 45.0,
        "max_ms": 45.0,
    }
    assert row(d, path="stop_followup")["label"] == "first hook"
    assert row(d, path="stop_followup")["tier"] == "cursor:stop-park"
    assert row(d, path="steer")["label"] == "in context" and row(d, path="steer")["p50_ms"] == 6000.0
    ctx = row(d, path="hook_ctx")
    assert (ctx["reason"], ctx["label"], ctx["tier"], ctx["p50_ms"]) == (
        "mention",
        "in context",
        "cursor:stop-park",
        2500.0,
    )
    pulled = row(d, path="read")
    assert (pulled["reason"], pulled["label"], pulled["p50_ms"]) == ("chatter", "pulled", 9000.0)
    assert {r["tier"] for r in rep["latency"]["by_tier"]} == {"codex:daemon", "cursor:stop-park"}


def test_a_remote_codex_wake_reports_its_own_tier(w: World, clock: FakeClock) -> None:
    """Issue #63: a remote Codex thread's wake has the ``turn_start`` path of a local one,
    but it went through that machine's app-server, not this one's daemon."""
    px, mx = w.agent("cx-pi", harness="codex", status="idle", hooks=True, host="fpga-pi")
    w.store.update_participant(px.id, tier="codex:link")
    a = quiet_msg(w, "task")
    clock.advance(0.2)
    b = offer(w, mx, [a], path="turn_start", kind="wake", wake_kind="idle_wake", reason="human", counted=True)
    w.store.set_batch_times(b, turn_start_at=a.ts + 0.25)
    w.store.confirm_batch(b, "link:turn/start")
    r = row(build(w)["latency"]["detail"], path="turn_start", label="turn start")
    assert (r["tier"], r["p50_ms"]) == ("codex:link", 250.0)


def test_only_the_first_confirmed_batch_counts_per_recipient(w: World, clock: FakeClock) -> None:
    px, mx = w.agent("codex-1", harness="codex", status="idle", hooks=True)
    a = quiet_msg(w, "task")
    clock.advance(0.1)
    lost = offer(
        w, mx, [a], path="turn_start", kind="wake", wake_kind="idle_wake", reason="human", counted=True
    )
    w.store.expire_batch(lost, "send_error", push=True)
    clock.advance(1.0)
    b = offer(w, mx, [a], path="turn_start", kind="wake", wake_kind="idle_wake", reason="human", counted=True)
    w.store.set_batch_times(b, turn_start_at=clock.now() + 0.02)
    w.store.confirm_batch(b, "rpc:turn/start")
    # re-delivered once later: not a second sample
    w.store.requeue(mx.id, [a.id], redeliver=True)
    clock.advance(30.0)
    again = offer(
        w, mx, [a], path="turn_start", kind="wake", wake_kind="idle_wake", reason="human", counted=True
    )
    w.store.set_batch_times(again, turn_start_at=clock.now())
    w.store.confirm_batch(again, "rpc:turn/start")
    rep = build(w)
    r = row(rep["latency"]["detail"], path="turn_start")
    assert r["n"] == 1 and r["p50_ms"] == pytest.approx(1120.0, abs=0.1)
    [ag] = rep["agents"]
    assert ag["wakes"] == {
        "offered": 3,
        "confirmed": 2,
        "expired": 1,
        "cancelled": 0,
        "budget_counted": 3,
        "by_kind": {"idle_wake": 3},
        "reminders": 0,
    }
    assert rep["rules"]["expired"] == {}  # expire *events* are the engine's; none were written here


def test_held_deliveries_are_kept_out_of_the_main_tables(w: World, clock: FakeClock) -> None:
    """A message posted while the room is paused (or the member held, or its session on an approval
    prompt) waits for that: its latency is listed apart (DESIGN.md §21)."""
    px, mx = w.agent("codex-1", harness="codex", status="idle", hooks=True)
    pc, mc = w.agent("claude-1", harness="claude", status="idle", hooks=True)

    def wake(m: Any, msg: Message, after: float) -> None:
        b = offer(
            w,
            m,
            [msg],
            path="turn_start" if m is mx else "inbox",
            kind="wake",
            wake_kind="idle_wake",
            reason="human",
            counted=True,
        )
        w.store.set_batch_times(b, turn_start_at=msg.ts + after)
        w.store.confirm_batch(b, "hook:UserPromptSubmit")

    live = quiet_msg(w, "live")
    wake(mx, live, 0.05)
    wake(mc, live, 0.06)
    clock.advance(10)
    w.store.add_event("loop_guard", room_id=w.room.id, data={"reason": "loop guard"})
    clock.advance(5)
    paused = quiet_msg(w, "while paused")
    clock.advance(0.5)
    w.store.add_event("resume", room_id=w.room.id, data={"via": "web"})
    wake(mx, paused, 0.55)
    clock.advance(10)
    w.store.add_event("hold", room_id=w.room.id, data={"via": "web", "membership_id": mc.id})
    w.store.add_event("status", participant_id=px.id, data={"frm": "busy", "to": "waiting-approval"})
    held_msg = quiet_msg(w, "while held")
    clock.advance(20)
    w.store.add_event("release", room_id=w.room.id, data={"via": "web", "membership_id": mc.id})
    w.store.add_event("status", participant_id=px.id, data={"frm": "waiting-approval", "to": "idle"})
    wake(mc, held_msg, 20.1)
    wake(mx, held_msg, 20.2)
    rep = build(w)
    main_rows = rep["latency"]["detail"]
    assert row(main_rows, harness="codex")["n"] == 1 and row(main_rows, harness="codex")["p50_ms"] == 50.0
    assert row(main_rows, harness="claude")["n"] == 1 and row(main_rows, harness="claude")["p50_ms"] == 60.0
    held = rep["latency"]["held"]
    assert row(held, harness="codex")["n"] == 2 and row(held, harness="codex")["max_ms"] == 20200.0
    assert row(held, harness="claude")["n"] == 1 and row(held, harness="claude")["p50_ms"] == 20100.0
    assert rep["latency"]["room_paused_s"] == 5.5
    md = report.render_markdown(rep)
    assert "### Held by a pause, a /hold or an approval prompt" in md and "paused for 5.50 s" in md


def test_without_offer_ids_the_deliveries_last_batch_is_used(w: World, clock: FakeClock) -> None:
    px, mx = w.agent("codex-1", harness="codex", status="idle", hooks=True)
    a = quiet_msg(w, "task")
    b = w.store.create_batch(
        mx.id,
        path="turn_start",
        kind="wake",
        items=[(a.id, True)],
        wake_kind="idle_wake",
        wake_reason="human",
        counted=True,
    )
    clock.advance(0.3)
    w.store.set_batch_times(b.id, turn_start_at=clock.now())
    w.store.confirm_batch(b.id, "rpc:turn/start")
    assert row(build(w)["latency"]["detail"], path="turn_start")["p50_ms"] == 300.0


def test_no_samples_render_as_n_a(w: World) -> None:
    rep = build(w)
    assert rep["latency"]["detail"] == [] and rep["agents"] == []
    md = report.render_markdown(rep)
    assert "| (none) |  | 0 | n/a | n/a | n/a |" in md


# ------------------------------------------------------ agents, rules, stalls
def test_agents_rules_and_stalls(w: World, clock: FakeClock) -> None:
    p1, m1 = w.agent("claude-1", harness="claude", status="idle", hooks=True)
    p2, m2 = w.agent("devin-1", harness="devin", status="busy", hooks=True)
    w.store.add_event("model", participant_id=p1.id, data={"model": "claude-sonnet-4-6"})
    w.store.add_event("turn_start", participant_id=p1.id, data={"src": "hook"})
    w.store.add_event("turn_start", participant_id=p1.id, data={"src": "hook"})
    w.human("go")
    for text in ("plan", "done"):
        w.agent_says(m1, text)
    w.store.add_event(
        "pass", room_id=w.room.id, membership_id=m2.id, participant_id=p2.id, data={"handled": 1}
    )
    w.store.add_event("rate_limited", room_id=w.room.id, membership_id=m1.id, participant_id=p1.id, data={})
    w.store.add_event(
        "pass_refused",
        room_id=w.room.id,
        membership_id=m1.id,
        participant_id=p1.id,
        data={"reason": "read_first", "n": 1, "ids": [1]},
    )
    w.store.add_event("rearm", room_id=w.room.id, membership_id=m2.id, participant_id=p2.id, data={"n": 1})
    w.store.add_event("loop_guard", room_id=w.room.id, data={"reason": "loop guard"})
    w.store.add_event("resume", room_id=w.room.id, data={"via": "web"})
    w.store.add_event("watchdog_escalate", room_id=w.room.id, membership_id=m1.id, data={"why": "not_idle"})
    w.store.add_event(
        "expire",
        room_id=w.room.id,
        membership_id=m1.id,
        data={"batch_id": 9, "path": "inbox", "reason": "idle_no_token"},
    )
    w.store.add_event("requeue", room_id=w.room.id, membership_id=m1.id, data={"reason": "redeliver", "n": 1})
    # a 0.2.0 /review event and a /catchup event: one row counts both (§26)
    w.store.add_event(
        "review",
        room_id=w.room.id,
        data={"via": "web", "reviewer": m2.id, "author": m1.id, "harness": "claude", "message_id": 1},
    )
    w.store.add_event(
        "catchup",
        room_id=w.room.id,
        data={
            "via": "web",
            "agent": m2.id,
            "mode": "member",
            "subjects": [m1.id],
            "with_id": 1,
            "message_id": 2,
        },
    )
    # approval prompts from the status events: 75 s (a stall), 10 s, and one still open
    for secs, to in ((75.0, "idle"), (10.0, "busy"), (None, None)):
        w.store.add_event(
            "status",
            participant_id=p1.id,
            data={"frm": "busy", "to": "waiting-approval", "src": "claude:registry"},
        )
        if secs is None:
            clock.advance(3.0)
            break
        clock.advance(secs)
        w.store.add_event(
            "status",
            participant_id=p1.id,
            data={"frm": "waiting-approval", "to": to, "src": "claude:registry"},
        )
        clock.advance(1.0)
    w.store.add_event("pause", room_id=w.room.id, data={"via": "web"})  # the room's last activity
    w.store.update_participant(p1.id, tier_note="guard refused /Users/someone/.codex/x.sock")
    rep = build(w)
    a1 = row(rep["agents"], name="claude-1")
    a2 = row(rep["agents"], name="devin-1")
    assert a1["model"] == "claude-sonnet-4-6" and a2["model"] is None
    assert a1["turns"] == 2 and a1["posts"] == 2 and a1["passes"] == 0 and a1["rate_limited"] == 1
    assert a2["passes"] == 1 and a2["rearms"] == 1 and a2["continuations"] == 1
    assert a2["wakes"]["budget_counted"] >= 1
    assert a1["tier_note"] == "guard refused <path>"
    r = rep["rules"]
    assert r["loop_guard"] == 1 and r["resume"] == 1 and r["rate_limited"] == 1 and r["rearm"] == 1
    assert r["pass_refused"] == 1 and a1["passes"] == 0  # a refused pass is no pass
    assert r["watchdog_escalate"] == {"not_idle": 1} and r["redeliver"] == 1
    assert r["expired"] == {"inbox:idle_no_token": 1} and r["approval_holds"] == 3
    assert [s["seconds"] for s in rep["stalls"]] == [75.0]
    ongoing = [s for s in rep["approval_holds"] if s["ongoing"]]
    assert len(ongoing) == 1 and ongoing[0]["seconds"] == 3.0
    assert rep["traffic"] == {"human_messages": 1, "agent_messages": 2, "passes": 1, "members": 2}
    md = report.render_markdown(rep)
    assert "**stalled**" in md and "claude-sonnet-4-6" in md
    assert "| read first (pass refused: a stub not read yet) | 1 |" in md
    assert r["catchup"] == 2 and "review" not in r
    assert "| /catchup (catch-up requests posted, /review included) | 2 |" in md


def test_output_has_no_text_paths_or_emails(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w)
    reg(w, w.p(p), "idle")
    w.human("SECRET-TEXT-123 from me@example.com at /Users/someone/project")
    [push] = pushes(w)
    clock.advance(0.05)
    w.hook(p, "UserPromptSubmit", gen="g", tokens=(tok(push.text),), permission_mode="default")
    # a model event can only come from the hook's filter, but the report scrubs it too
    w.store.add_event("model", participant_id=p.id, data={"model": "first.last@example.com"})
    rep = build(w)
    blob = json.dumps(rep) + report.render_markdown(rep)
    assert "SECRET-TEXT" not in blob and "@example.com" not in blob and "/Users/" not in blob
    assert not re.search(r"claude:\d+@", blob)  # no session keys (pids)
    assert rep["agents"][0]["model"] == "<email>"


# ------------------------------------------------- the window and membership
def test_a_report_made_later_reads_the_same(w: World, clock: FakeClock) -> None:
    """The window ends at the room's last activity: a member's turns, approval prompts and Codex
    holds after it (or while it wasn't in the room) don't change the report (DESIGN.md §21)."""
    px, mx = w.agent("codex-1", harness="codex", status="idle", hooks=True)
    w.store.add_event("turn_start", participant_id=px.id, data={"src": "rpc"})
    a = quiet_msg(w, "task")
    clock.advance(0.05)
    b = offer(w, mx, [a], path="turn_start", kind="wake", wake_kind="idle_wake", reason="human", counted=True)
    w.store.set_batch_times(b, turn_start_at=clock.now())
    w.store.confirm_batch(b, "rpc:turn/start")
    w.store.add_event("turn_start", participant_id=px.id, data={"src": "rpc"})
    first = build(w)
    assert first["agents"][0]["turns"] == 2
    clock.advance(3600)
    for _ in range(5):
        w.store.add_event("turn_start", participant_id=px.id, data={"src": "rpc"})
    w.store.add_event("codex_hold", data={"why": "tui left", "threads": 1})
    w.store.add_event("status", participant_id=px.id, data={"frm": "busy", "to": "waiting-approval"})
    clock.advance(120)
    w.store.add_event("status", participant_id=px.id, data={"frm": "waiting-approval", "to": "idle"})
    later = build(w)
    assert later["window"] == first["window"] and later["agents"] == first["agents"]
    assert later["rules"] == first["rules"] and later["stalls"] == [] and later["approval_holds"] == []


def test_a_members_events_count_only_while_it_is_in_the_room(w: World, clock: FakeClock) -> None:
    pc, mc = w.agent("claude-1", harness="claude", status="idle", hooks=True)
    w.store.add_event("turn_start", participant_id=pc.id, data={"src": "hook"})
    clock.advance(10)
    pl, ml = w.agent("claude-2", harness="claude", status="idle", hooks=True)
    w.store.add_event("turn_start", participant_id=pl.id, data={"src": "hook"})  # before its join: no
    w.store.con.execute("UPDATE memberships SET joined_at=? WHERE id=?", (clock.now() + 1, ml.id))
    clock.advance(5)
    w.store.add_event("turn_start", participant_id=pl.id, data={"src": "hook"})
    w.human("go")
    rep = build(w)
    assert row(rep["agents"], name="claude-1")["turns"] == 1
    assert row(rep["agents"], name="claude-2")["turns"] == 1


# ------------------------------------------------------ main measure, pulls
def test_by_reason_uses_each_samples_main_measure(w: World, clock: FakeClock) -> None:
    """A bypass session's mid-task inbox frame is "in context"; a wait() answer with no tool call
    after it (every harness but Devin registers no PreToolUse) is "in context" too."""
    pc, mc = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    px, mx = w.agent("codex-1", harness="codex", status="busy", hooks=True)
    a = quiet_msg(w, "also this")
    clock.advance(0.4)
    b1 = offer(w, mc, [a], path="inbox", kind="priority", reason="human")
    w.store.confirm_batch(b1, "hook:UserPromptSubmit")
    b2 = offer(w, mx, [a], path="wait", kind="wake", wake_kind="wait_return", reason="human", counted=True)
    clock.advance(0.2)
    w.store.confirm_batch(b2, "wait")
    rep = build(w)
    d = rep["latency"]["detail"]
    assert row(d, harness="claude", path="inbox")["label"] == "in context"
    assert row(d, harness="codex", path="wait")["label"] == "in context"
    r = row(rep["latency"]["by_reason"], reason="human", label="in context")
    assert r["n"] == 2 and r["max_ms"] == 600.0


def test_pulls_are_never_held_by_a_pause(w: World, clock: FakeClock) -> None:
    px, mx = w.agent("codex-1", harness="codex", status="busy", hooks=True)
    pc, mc = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    w.store.add_event("pause", room_id=w.room.id, data={"via": "web"})
    chat = quiet_msg(w, "fyi", sender="claude-1", kind="agent", membership=mc)
    clock.advance(4.0)
    b = offer(w, mx, [chat], path="say", kind="pull")
    w.store.confirm_batch(b, "say")
    w.store.add_event("resume", room_id=w.room.id, data={"via": "web"})
    rep = build(w)
    assert row(rep["latency"]["detail"], path="say")["p50_ms"] == 4000.0 and rep["latency"]["held"] == []


# ------------------------------------------------------------ parked spells
def test_parked_spells_come_from_the_engines_events(w: World, clock: FakeClock) -> None:
    """A member with messages waiting and no way to wake it is parked ("needs a poke", §8.5): the
    engine writes an event at each edge and the report counts the spells and their time."""
    p, m, _c = claude(w, attached=False)
    w.human("anyone?")
    assert pushes(w) == [] and w.engine.parked_reason(m.id)
    clock.advance(30.0)
    ad(w).attach(p.mcp_pid, p.mcp_start, FakeConn())
    reg(w, w.p(p), "idle")
    w.actions += w.engine.evaluate_participant(p.id)
    assert w.engine.parked_reason(m.id) is None
    kinds = [e.kind for e in w.store.recent_events(room_id=w.room.id, kinds=["parked", "unparked"])]
    assert kinds == ["unparked", "parked"]
    rep = build(w)
    [a] = rep["agents"]
    assert a["parked"]["spells"] == 1 and a["parked"]["seconds"] == 30.0 and rep["rules"]["parked"] == 1
    assert "| parked (needs a poke) | 1 |" in report.render_markdown(rep)


def test_the_window_filters_older_traffic(w: World, clock: FakeClock) -> None:
    px, mx = w.agent("codex-1", harness="codex", status="idle", hooks=True)
    old = quiet_msg(w, "old")
    b = offer(
        w, mx, [old], path="turn_start", kind="wake", wake_kind="idle_wake", reason="human", counted=True
    )
    w.store.set_batch_times(b, turn_start_at=clock.now() + 0.1)
    w.store.confirm_batch(b, "rpc:turn/start")
    clock.advance(3600)
    cut = clock.now()
    new = quiet_msg(w, "new")
    b2 = offer(
        w, mx, [new], path="turn_start", kind="wake", wake_kind="idle_wake", reason="human", counted=True
    )
    w.store.set_batch_times(b2, turn_start_at=clock.now() + 0.2)
    w.store.confirm_batch(b2, "rpc:turn/start")
    assert row(build(w)["latency"]["detail"], path="turn_start")["n"] == 2
    r = row(build(w, since=cut)["latency"]["detail"], path="turn_start")
    assert r["n"] == 1 and r["p50_ms"] == 200.0


def test_the_report_connection_refuses_writes(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock)
    w.store.con.close()
    con = report.open_ro(tmp_path / "y.db")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM rooms")
    assert con.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 1


def test_a_corrupt_database_is_a_report_error(tmp_path: Path) -> None:
    """``open_ro`` wraps a low-level sqlite3 error (a file that fails to open as a database,
    #167's ``db.connect_query_only``) in ``ReportError``, so the CLI prints a plain message
    instead of a traceback."""
    path = tmp_path / "switchboard.db"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(report.ReportError, match="can't read the switchboard database"):
        report.open_ro(path)


def test_unknown_room_is_an_error(w: World) -> None:
    with pytest.raises(report.ReportError):
        report.build(w.store.con, "#nope")
    with pytest.raises(report.ReportError):
        report.build(w.store.con, "not a room!")


# ------------------------------------------------------------------ the CLI
def test_cli_writes_markdown_and_json_read_only(
    tmp_path: Path, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    w = World(home, clock)
    w.store.con.close()  # like a stopped broker: SQLite removes the -wal and -shm files
    (home / "y.db").rename(home / "switchboard.db")
    assert not (home / "switchboard.db-shm").exists()
    out = tmp_path / "r.md"
    assert main(["report", "--home", str(home), "--room", "#build", "--out", str(out)]) == 0
    assert out.read_text().startswith("# switchboard report: #build")
    assert main(["report", "--home", str(home), "--room", "build", "--json"]) == 0
    got = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert got["room"] == "#build"
    assert main(["report", "--home", str(home), "--room", "#other"]) == 1
    assert "no room #other" in capsys.readouterr().err
    assert (
        main(["report", "--home", str(home), "--room", "#build", "--out", str(tmp_path / "no" / "x.md")]) == 1
    )
    assert "can't write" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["report", "--home", str(empty), "--room", "#build"]) == 1
    assert not (empty / "switchboard.db").exists()  # never creates a database
    before = (home / "switchboard.db").read_bytes()
    assert main(["report", "--home", str(home), "--room", "#build"]) == 0
    assert (home / "switchboard.db").read_bytes() == before  # and never writes one
    assert main(["report", "--home", str(home), "--room", "#build", "--last", "2 weeks"]) == 1


# ---------------------------------------------------------- schema v1 and v2
def test_report_reads_v1_and_v2(tmp_path: Path) -> None:
    """``switchboard report`` reads a 0.2.0 (schema v1) database as it is, never
    migrating it, and a v2 one, where it names each participant's host (§27.6)."""
    from switchboard import db
    from switchboard.models import session_key

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_2_0.sql"
    path = tmp_path / "switchboard.db"
    raw = sqlite3.connect(path)
    raw.executescript(fixture.read_text(encoding="utf-8"))
    raw.close()
    before = path.read_bytes()
    con = report.open_ro(path)
    rep1 = report.build(con, "#build", now=1_790_000_100.0)
    md1 = report.render_markdown(rep1)
    con.close()
    assert db.schema_version(sqlite3.connect(path)) == 1  # read, never migrated
    assert path.read_bytes() == before and not list(tmp_path.glob("*.v1.bak*"))
    assert {a["name"] for a in rep1["agents"]} >= {"vivado", "bot-a"}
    assert (
        all(a["host"] == "" for a in rep1["agents"]) and "@" not in md1.split("## Agents")[1].split("\n\n")[1]
    )

    # the broker migrates it; then one of its participants is on a Pi
    c = db.open_db(path)
    pid = c.execute("SELECT participant_id FROM memberships WHERE screen_name='bot-a'").fetchone()[0]
    c.execute(
        "UPDATE participants SET host='fpga-pi', session_key=? WHERE id=?",
        (session_key("test", "fpga-pi", "bot-a"), pid),
    )
    c.close()
    con = report.open_ro(path)
    rep2 = report.build(con, "#build", now=1_790_000_100.0)
    md2 = report.render_markdown(rep2)
    con.close()
    hosts = {a["name"]: a["host"] for a in rep2["agents"]}
    assert hosts["bot-a"] == "fpga-pi" and hosts["vivado"] == ""
    assert "| bot-a@fpga-pi |" in md2 and "| vivado |" in md2
    # everything else reads the same as before the migration
    strip = [{k: v for k, v in a.items() if k != "host"} for a in rep2["agents"]]
    assert strip == [{k: v for k, v in a.items() if k != "host"} for a in rep1["agents"]]


# ------------------------------------------------------ closed rooms (#16)
def close_by_hand(w: World, room_id: int, *, by: str | None = "alice", event: bool = True) -> str:
    """What ``/close`` leaves in the database: members ended with reason ``closed``, a
    ``room_close`` event and the row renamed ``#x~closed-<id>`` (DESIGN.md §28.2)."""
    from switchboard.models import closed_room_name

    room = w.store.room_by_id(room_id)
    assert room is not None
    mids = [m.membership_id for m in w.store.members(room_id)]
    for mid in mids:
        w.store.end_membership(mid, "closed", keep_cred=True)
    closed = closed_room_name(room.name, room.id)
    if event:
        w.store.add_event(
            "room_close",
            room_id=room_id,
            data={
                "name": room.name,
                "closed_name": closed,
                "members": mids,
                **({"by": by} if by is not None else {}),
            },
        )
    w.store.rename_room(room_id, closed, expect=room.name)
    return closed


def test_a_closed_room_by_its_base_name(w: World, clock: FakeClock) -> None:
    w.agent("claude-1")
    w.human("hi")
    clock.advance(5)
    closed = close_by_hand(w, w.room.id)
    rep = build(w)
    assert rep["room"] == "#build"
    assert rep["closed"] == {"name": closed, "at": report.iso(clock.now()), "by": "alice"}
    assert row(rep["agents"], name="claude-1")["status_now"] == "left (closed)"
    md = report.render_markdown(rep)
    assert md.startswith("# switchboard report: #build (closed)\n")
    assert f" Closed {rep['closed']['at']} by alice (internal name `{closed}`)." in md
    # by its full name, too; an open room has no closed block
    assert report.build(w.store.con, closed, now=clock.now())["closed"]["name"] == closed
    w.store.create_room("#other", "alice", 10, 3)
    rep_open = report.build(w.store.con, "#other", now=clock.now())
    assert rep_open["closed"] is None
    md_open = report.render_markdown(rep_open)
    assert "(closed)" not in md_open and "internal name" not in md_open


def test_a_closed_room_without_its_close_event(w: World, clock: FakeClock) -> None:
    close_by_hand(w, w.room.id, event=False)
    rep = build(w)
    assert rep["closed"]["at"] is None and rep["closed"]["by"] is None
    assert " Closed n/a by n/a (internal name `#build~closed-" in report.render_markdown(rep)


def test_two_closed_rooms_with_one_name_are_ambiguous(w: World) -> None:
    first = close_by_hand(w, w.room.id)
    again = w.store.create_room("#build", "alice", 10, 3)
    second = close_by_hand(w, again.id)
    with pytest.raises(report.ReportError) as e:
        build(w)
    assert f"{second}, {first}" in str(e.value) and "full name" in str(e.value)
    assert report.build(w.store.con, first)["closed"]["name"] == first


def test_an_open_room_wins_over_a_closed_one(w: World, clock: FakeClock) -> None:
    old = close_by_hand(w, w.room.id)
    new = w.store.create_room("#build", "alice", 10, 3)
    rep = build(w)
    assert rep["room"] == "#build" and rep["closed"] is None
    assert report.build(w.store.con, old, now=clock.now())["closed"]["name"] == old
    assert w.store.get_room("#build") == new
    with pytest.raises(report.ReportError, match="no room #build~closed-99"):
        report.build(w.store.con, "#build~closed-99")


def test_cli_reports_a_closed_room(
    tmp_path: Path, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    w = World(home, clock)
    closed = close_by_hand(w, w.room.id)
    w.store.con.close()
    (home / "y.db").rename(home / "switchboard.db")
    assert main(["report", "--room", "#build", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "# switchboard report: #build (closed)" in out and closed in out
    assert main(["report", "--room", closed.upper(), "--home", str(home), "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["room"] == "#build" and got["closed"]["name"] == closed
