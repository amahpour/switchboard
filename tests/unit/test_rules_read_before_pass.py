"""Read before pass (DESIGN.md §24). Live bug: on Codex's turn/start path a peer's
message arrived as a "call read()" stub, the batch line offered "or pass()", and
the model passed without ever reading it.

The stub stays (peer text never arrives with user authority), but:
- every stub path now says read() comes first, and never offers pass() instead;
- pass() is refused (``read_first``) while the member has a peer message it was
  only ever shown as a stub; read() (or say()'s unread, or a wait() fill)
  showing it inline lifts that. Human items and anything shown inline before
  never block, and nothing here can wedge wait(), a Devin Stop or the watchdog.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import World
from test_claude_adapter import claude
from test_codex_adapter import attach, codex
from test_cursor_adapter import cursor, park_result, stop
from test_devin_adapter import devin
from test_rules_release import item

from switchboard import envelope
from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import Push

W = 120.0  # watchdog_s


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0, watchdog_s=W))


def stubbed(it: Any, **kw: Any) -> Any:
    return dataclasses.replace(it, notified_at=kw.pop("notified", 1.0), **kw)


def pushes(w: World) -> list[Push]:
    return [a for a in w.take() if isinstance(a, Push)]


def hook_stub(
    w: World, p: Any, peer: Any, text: str = "@bot please look at parse_port", name: str = "bot"
) -> Any:
    """A peer @mention that reached ``p`` as a stub in PostToolUse hook context (acked)."""
    msg = w.agent_says(peer, text, mentions=(name,))
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and 'text=(not shown here; call read("#build"))' in out.text
    assert "parse_port" not in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    d = w.delivery(w.store.active_membership(w.room.id, p.id), msg)
    assert d["state"] == "pending" and d["notified_at"] is not None
    return msg


def blockers(w: World, p: Any, m: Any) -> list[int]:
    return w.engine.check_pass(w.p(p), w.m(m))


def read(w: World, p: Any, m: Any) -> tuple[str, int | None]:
    text, bid, _n, _more, acts = w.engine.pull(w.p(p), w.m(m), "read", 50)
    w.actions += acts
    return text, bid


def says_read_first(text: str, room: str = "#build") -> None:
    """A stub batch: read() first, and pass() never offered as the alternative to reading."""
    head = text.splitlines()[0]
    assert f'Call read("{room}") now' in head
    assert head.index(f'read("{room}")') < head.index("pass()")
    assert ", or pass(" not in text and 'or pass("' not in head
    assert envelope.PASS_ADVICE in head and envelope.PEER_WARNING in head
    foot = text.splitlines()[-1]
    assert foot.index(f'call read("{room}") first') < foot.index(f'pass("{room}")')


# ------------------------------------------------------------------ the rule itself (pure)
def test_only_peer_stubs_never_shown_inline_block() -> None:
    peer = stubbed(item(1, 1))
    assert rules.unread_stubs([peer]) == [1]
    assert rules.unread_stubs([stubbed(item(2, 0))]) == [2]  # a chatter stub too
    assert rules.unread_stubs([item(3, 1)]) == []  # pending, but never announced to it
    assert rules.unread_stubs([stubbed(item(4, 2))]) == []  # the human's (a cut text) never blocks
    assert rules.unread_stubs([stubbed(item(5, 1), in_context_at=2.0)]) == []  # shown inline before
    # offered (e.g. in the read() answer the agent holds) or in context: not unread
    assert rules.unread_stubs([dataclasses.replace(peer, state="offered")]) == []
    assert rules.unread_stubs([dataclasses.replace(peer, state="in_context")]) == []
    assert rules.unread_stubs([stubbed(item(9, 1)), stubbed(item(7, 0))]) == [7, 9]


# ------------------------------------------------------------------ engine: refused, then read
def test_a_hook_stub_blocks_pass_until_read_shows_it(w: World) -> None:
    p, m = w.agent("bot", status="busy", hooks=True)
    _pp, peer = w.agent("peer")
    msg = hook_stub(w, p, peer)
    assert blockers(w, p, m) == [msg.id]
    [ev] = w.store.recent_events(kinds=("pass_refused",))
    assert ev.membership_id == m.id and ev.data == {"reason": "read_first", "n": 1, "ids": [msg.id]}
    text, bid = read(w, p, m)
    assert "please look at parse_port" in text  # read() is a pull path: the full text
    # the answer is offered (its PostToolUse hasn't come yet): its text is in the agent's hands
    assert blockers(w, p, m) == []
    w.actions += w.engine.on_confirm(bid, "hook:PostToolUse")
    assert w.states(m)[msg.id] == "in_context" and blockers(w, p, m) == []
    assert w.store.count_events("pass_refused") == 1  # only refusals write it


def test_the_refusal_text_says_read_first(w: World) -> None:
    t = envelope.render_read_first("#build", [119, 120])
    assert t.startswith('[switchboard] pass("#build") refused:') and "not shown here" in t
    assert "ids 119, 120" in t and 'Call read("#build") now' in t
    assert t.index('read("#build")') < t.rindex("pass()")
    one = envelope.render_read_first("#build", [7])
    assert "1 message from a peer agent was" in one and "id 7" in one and "to see it;" in one
    many = envelope.render_read_first("#build", list(range(1, 15)))
    assert "and 4 more" in many


def test_an_expired_read_blocks_again(w: World) -> None:
    """A read() answer that never reached the model (no PostToolUse) goes back: unread again."""
    p, m = w.agent("bot", status="busy", hooks=True)
    _pp, peer = w.agent("peer")
    msg = hook_stub(w, p, peer)
    _text, bid = read(w, p, m)
    assert blockers(w, p, m) == []
    w.actions += w.engine.on_expire(bid, "no_hook")
    assert blockers(w, p, m) == [msg.id]


def test_say_unread_shows_it_and_lifts_the_rule(w: World) -> None:
    p, m = w.agent("bot", status="busy", hooks=True)
    _pp, peer = w.agent("peer")
    msg = hook_stub(w, p, peer)
    text, bid, n, _more, acts = w.engine.pull(w.p(p), w.m(m), "say", 50)  # the say() result's unread
    w.actions += acts
    assert n == 1 and "please look at parse_port" in text
    w.actions += w.engine.on_confirm(bid, "hook:PostToolUse")
    assert blockers(w, p, m) == []


def test_human_items_and_inline_peers_never_block(w: World) -> None:
    p, m = w.agent("bot")  # a test agent listening in wait(): a pull path, everything inline
    _pp, peer = w.agent("peer")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    w.agent_says(peer, "@bot inline please", mentions=("bot",))
    [res] = w.resolved(sink.id)
    assert "inline please" in res["text"]
    w.actions += w.engine.before_call(w.p(p))  # --ack next_call
    assert blockers(w, p, m) == []  # pass() works at once
    # a human message cut on a hook path is offered like a stub: it still never blocks
    ph, mh = w.agent("hooked", status="busy", hooks=True)
    w.human("@hooked " + "x" * 3000, mentions=("hooked",))
    out = w.hook(ph, "PostToolUse", ok=True)
    assert out is not None
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    [d] = [d for d in w.store.deliveries(mh.id)]
    assert d["state"] == "pending" and d["notified_at"] is not None  # partial: back to pending
    assert blockers(w, ph, mh) == []


def test_claude_inbox_is_inline_so_pass_works_at_once(w: World) -> None:
    p, m, _conn = claude(w, "claude-1")
    _pp, peer = w.agent("peer")
    msg = w.agent_says(peer, "@claude-1 what do you think?", mentions=("claude-1",))
    [push] = pushes(w)
    assert push.path == "inbox" and "what do you think?" in push.text
    assert "not shown here" not in push.text and 'call read("#build") first' not in push.text
    w.actions += w.engine.on_confirm(push.batch_id, "hook:UserPromptSubmit")
    assert w.states(m)[msg.id] == "in_context" and blockers(w, p, m) == []


def test_a_peer_text_cut_on_the_inbox_was_shown_inline_so_pass_works(w: World) -> None:
    """A cut text reached the model in part (inline, with "read() shows full"): it
    stays pending so read() shows the rest, but it never blocks pass()."""
    p, m, _conn = claude(w, "claude-1")
    _pp, peer = w.agent("peer")
    msg = w.agent_says(peer, "@claude-1 " + "y" * 3000, mentions=("claude-1",))
    [push] = pushes(w)
    assert "read() shows full" in push.text and "not shown here" not in push.text
    [(_mid, flag)] = w.store.con.execute(
        "SELECT message_id, offered_inline FROM deliveries WHERE membership_id=?", (m.id,)
    ).fetchall()
    assert flag == envelope.INLINE_CUT
    w.actions += w.engine.on_confirm(push.batch_id, "hook:UserPromptSubmit")
    d = w.delivery(m, msg)
    assert d["state"] == "pending" and d["notified_at"] is not None and d["in_context_at"] is not None
    assert blockers(w, p, m) == []  # it saw 1,500 characters of it: pass() works at once
    text, _bid = read(w, p, m)
    assert "y" * 3000 in text  # and read() still shows the rest


def test_codex_turn_start_stub_needs_a_read(w: World) -> None:
    p, m = codex(w)
    attach(w)
    _pp, peer = w.agent("peer")
    msg = w.agent_says(peer, "@codex-1 please review my diff", mentions=("codex-1",))
    [push] = pushes(w)
    assert push.path == "turn_start" and "please review my diff" not in push.text
    says_read_first(push.text)
    w.actions += w.engine.on_confirm(push.batch_id, "rpc:turn/start")
    assert blockers(w, p, m) == [msg.id]
    text, _bid = read(w, p, m)
    assert "please review my diff" in text and blockers(w, p, m) == []


def test_every_stub_path_says_read_first(w: World) -> None:
    got: dict[str, str] = {}
    ph, _mh, _c = claude(w, "claude-2", status="busy", attached=False, registry=None)  # hook context
    codex(w)
    attach(w)  # idle: turn_start
    pu, _mu = cursor(w, status="busy")
    park = stop(w, pu)  # parked stop hook: stop_followup
    pd, _md = devin(w)
    w.hook(pd, "UserPromptSubmit", gen="g1")
    _pp, peer = w.agent("peer")
    w.agent_says(
        peer,
        "@claude-2 @codex-1 @cursor-1 @devin-1 have a look",
        mentions=("claude-2", "codex-1", "cursor-1", "devin-1"),
    )
    for a in pushes(w):
        got[a.path] = a.text
    got["stop_followup"] = park_result(w, park.sink_id)["text"]
    got["hook_ctx"] = w.hook(ph, "PostToolUse", ok=True).text
    got["stop_block"] = w.hook(pd, "Stop", gen="g1").text
    assert set(got) == {"turn_start", "stop_followup", "hook_ctx", "stop_block"}
    for path, text in got.items():
        assert "have a look" not in text, path
        says_read_first(text)


def test_a_codex_steer_stub_says_read_first(w: World) -> None:
    codex(w, status="busy")
    attach(w, view="busy")
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "@codex-1 stop, wrong file", mentions=("codex-1",))
    [push] = pushes(w)
    assert push.path == "steer"
    says_read_first(push.text)


# ------------------------------------------------------------------ never wedged
def test_shown_before_then_stubbed_again_does_not_block(w: World) -> None:
    """Re-deliver once brings an unanswered item back; on a stub path it is a stub again,
    but the agent has seen its text: pass() works."""
    p, m = codex(w, status="busy")
    attach(w, view="busy")
    _pp, peer = w.agent("peer")
    msg = w.agent_says(peer, "@codex-1 check this", mentions=("codex-1",))
    [push] = pushes(w)
    w.actions += w.engine.on_confirm(push.batch_id, "hook:UserPromptSubmit")  # the steer landed
    _text, bid = read(w, p, m)
    w.actions += w.engine.on_confirm(bid, "hook:PostToolUse")
    assert w.states(m)[msg.id] == "in_context"
    w.hook(p, "Stop")  # the turn ended unanswered: re-deliver once
    attach(w, view="idle")
    w.actions += w.engine.evaluate(m.id)
    [again] = pushes(w)
    assert again.path == "turn_start" and "again=yes" in again.text and "not shown here" in again.text
    w.actions += w.engine.on_confirm(again.batch_id, "rpc:turn/start")
    assert w.delivery(m, msg)["notified_at"] is not None and blockers(w, p, m) == []


def test_devin_stop_block_stub_then_read(w: World) -> None:
    p, m = devin(w)
    w.hook(p, "UserPromptSubmit", gen="g1")
    _pp, peer = w.agent("peer")
    msg = w.agent_says(peer, "@devin-1 look at zebra_parser", mentions=("devin-1",))
    out = w.hook(p, "Stop", gen="g1")
    assert out is not None and out.kind == "continue" and "not shown here" in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    w.hook(p, "PreToolUse", tool="read", tool_use_id="t1", gen="g1")  # the next hook confirms it
    assert w.store.get_batch(out.batch_id).state == "confirmed"
    assert blockers(w, p, m) == [msg.id]
    text, _bid = read(w, p, m)
    assert "zebra_parser" in text and blockers(w, p, m) == []


def test_a_wait_returns_an_unread_stub_at_once(w: World) -> None:
    """A wait loop that skipped the read(): its wait() is a pull, so it gets the text
    at once (whole, not counted), and the rule is lifted once that is confirmed."""
    p, m = w.agent("bot", status="busy", hooks=True)
    _pp, peer = w.agent("peer")
    msg = hook_stub(w, p, peer)
    budget = w.store.room_by_id(w.room.id).budget_remaining
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 600)
    w.actions += acts
    [res] = w.resolved(sink.id)
    assert res["status"] == "messages" and "please look at parse_port" in res["text"]
    assert "not shown here" not in res["text"]
    b = w.store.get_batch(res["batch_id"])
    assert (b.path, b.kind, b.budget_counted) == ("wait", "pull", False)
    assert w.store.room_by_id(w.room.id).budget_remaining == budget
    assert blockers(w, p, m) == []  # offered: the answer is in its hands
    w.actions += w.engine.on_confirm(b.id, "hook:PostToolUse")
    assert w.states(m)[msg.id] == "in_context" and blockers(w, p, m) == []
    # nothing unread any more: the next wait() waits
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w2", 50)
    w.actions += acts
    assert sink.open
    w.actions += w.engine.sink_timeout(sink.id)
    assert w.resolved(sink.id)[0]["text"] == "[switchboard] #build: no new messages within 50 s."


def test_a_wait_timeout_points_at_unread_stubs_it_could_not_take(w: World) -> None:
    """While a /hold keeps the wait() from filling, its timeout still says what is unread."""
    p, m = w.agent("bot", status="busy", hooks=True)
    _pp, peer = w.agent("peer")
    hook_stub(w, p, peer)
    w.store.set_held(m.id, True)
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    assert sink.open
    w.actions += w.engine.sink_timeout(sink.id)
    [res] = w.resolved(sink.id)
    assert res["status"] == "timeout" and 'call read("#build")' in res["text"]
    assert "1 earlier message from a peer agent" in res["text"]


def test_a_refused_pass_is_no_answer_so_the_watchdog_reminds(w: World) -> None:
    p, m = w.agent("bot", status="busy", hooks=True)
    _pp, peer = w.agent("peer")
    msg = hook_stub(w, p, peer)
    assert blockers(w, p, m) == [msg.id]  # refused: nothing handled, no pass event
    assert w.store.count_events("pass") == 0
    w.hook(p, "Stop")  # idle, and it never read it
    w.clock.advance(W)
    w.actions += w.engine.tick()
    assert w.store.count_events("watchdog_remind") == 1
    assert w.delivery(w.store.active_membership(w.room.id, p.id), msg)["reminders"] == 1
    # the reminder is waiting for its wake; a wait() takes it inline
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 3600)
    w.actions += acts
    [res] = w.resolved(sink.id)
    assert "please look at parse_port" in res["text"] and "reminder=yes" in res["text"]  # inline
    w.actions += w.engine.on_confirm(res["batch_id"], "hook:PostToolUse")
    assert blockers(w, p, m) == []


def test_an_escalated_mention_it_had_read_does_not_block(w: World) -> None:
    p, m = w.agent("bot", status="busy", hooks=True)
    _pp, peer = w.agent("peer")
    msg = hook_stub(w, p, peer)
    _text, bid = read(w, p, m)
    w.actions += w.engine.on_confirm(bid, "hook:PostToolUse")
    w.store.watchdog_done(m.id, [msg.id], keep_count=True)  # escalated: a notified stub again
    d = w.delivery(m, msg)
    assert d["state"] == "pending" and d["notified_at"] is not None
    assert blockers(w, p, m) == []


# ------------------------------------------------ Cursor follow-ups (no pre-tool hook in Cursor)
def cursor_followup_stub(w: World) -> tuple[Any, Any, Any, int]:
    """A peer @mention sent to a parked Cursor stop as a follow-up (a stub: it arrives as
    the next user message), acked by the hook: printed, but no hook of its turn yet."""
    p, m = cursor(w, status="busy")
    _pp, peer = w.agent("peer")
    msg = w.agent_says(peer, "@cursor-1 look at zebra_parser", mentions=("cursor-1",))
    res = park_result(w, stop(w, p).sink_id)
    assert res is not None and res["status"] == "messages" and "zebra_parser" not in res["text"]
    says_read_first(res["text"])
    w.actions += w.engine.on_hook_ack(res["batch_id"], res["ack"])
    assert w.store.get_batch(res["batch_id"]).state == "offered"
    return p, m, msg, res["batch_id"]


def test_a_cursor_followup_is_confirmed_by_its_turns_first_call_so_pass_is_refused(w: World) -> None:
    """Cursor's follow-up is confirmed by the postToolUse *after* the turn's first tool call.
    If that call is pass(), the call itself is the evidence: the follow-up is confirmed
    first, its stub is pending, and pass() is refused until read() shows it."""
    p, m, msg, bid = cursor_followup_stub(w)
    w.clock.advance(2.0)
    w.actions += w.engine.before_call(w.p(p))  # the agent.pass call (AgentService._member)
    b = w.store.get_batch(bid)
    assert b.state == "confirmed" and b.evidence == "agent_call"
    assert b.turn_start_at == b.first_action_at == w.clock.now()
    assert w.p(p).unconfirmed_followups == 0
    assert blockers(w, p, m) == [msg.id]
    text, rbid = read(w, p, m)
    assert "zebra_parser" in text and blockers(w, p, m) == []
    w.actions += w.engine.on_confirm(rbid, "hook:PostToolUse")
    assert w.states(m)[msg.id] == "in_context" and blockers(w, p, m) == []
    w.hook(p, "postToolUse", ok=True)  # the later hook of that turn changes nothing
    assert w.store.get_batch(bid).evidence == "agent_call"


def test_a_cursor_followup_read_first_finds_the_stub(w: World) -> None:
    """The model follows the follow-up and calls read() first: it gets the text, not
    'no new messages' (the stub is pending once the call confirmed the follow-up)."""
    p, m, msg, _bid = cursor_followup_stub(w)
    w.actions += w.engine.before_call(w.p(p))  # the agent.read call
    text, _rbid = read(w, p, m)
    assert "zebra_parser" in text and "no new messages" not in text


def test_a_call_confirms_only_an_acked_followup_of_that_session(w: World) -> None:
    p, m = cursor(w, status="busy")
    q, _qm = cursor(w, "cursor-2", status="busy")
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "@cursor-1 hello", mentions=("cursor-1",))
    res = park_result(w, stop(w, p).sink_id)
    w.actions += w.engine.before_call(w.p(p))  # not acked (the hook may have died): unknown
    assert w.store.get_batch(res["batch_id"]).state == "offered"
    w.actions += w.engine.on_hook_ack(res["batch_id"], res["ack"])
    w.actions += w.engine.before_call(w.p(q))  # another session's call
    assert w.store.get_batch(res["batch_id"]).state == "offered"
    w.hook(p, "beforeSubmitPrompt")  # the human typed first: the follow-up expired
    assert w.store.get_batch(res["batch_id"]).state == "expired"
    w.actions += w.engine.before_call(w.p(p))
    assert w.store.get_batch(res["batch_id"]).state == "expired" and blockers(w, p, m) == []
