"""The engine asks a session's adapter what its hooks mean, not its harness name (#169,
DESIGN.md §9.1): which hooks set the session id and the approval mode, which SessionEnd
takes it offline, which hook starts a background subagent, how a Stop re-arms, when missed
stop follow-ups degrade a session, whose tier follows from its row, which prompt is input
into the running turn, and which harness needs another adapter on another host. A harness added with
its own adapter needs no edit to the engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.adapters import REMOTE_CODEX, PullAdapter
from switchboard.adapters.codex import steered
from switchboard.adapters.cursor import CursorAdapter
from switchboard.config import Config
from switchboard.models import HookEvent, Notice, Participant


class OtherAdapter(PullAdapter):
    """Claude's answers, on an adapter the engine's old lists never named: it stands in
    for the unknown harness (the schema takes only the harnesses it knows)."""

    reports_session_id = True
    reports_approval_mode = True

    def ends_session(self, ev: HookEvent) -> bool:
        return ev.reason != "clear"


class ListeningAdapter(PullAdapter):
    """Devin's answers, on the unknown harness: subagents that taint their prompt, and a
    Stop that re-arms once per prompt with its own text."""

    def starts_subagent(self, ev: HookEvent) -> bool:
        return ev.ev == "PreToolUse" and ev.subagent_bg

    def stop_continues(self, p: Participant) -> bool:
        return True

    def rearm_caps(self) -> tuple[int, int]:
        return 1, 5

    def rearm_text(self, room: str, wait_s: int) -> str:
        return f"listen in {room}"


class SteeredAdapter(PullAdapter):
    """Codex's answers, on the unknown harness: steered input, and push paths that reach
    only this machine."""

    local_only = True

    def is_mid_turn(self, p: Participant, ev: HookEvent, was_idle: bool) -> bool:
        return steered(p, ev, was_idle)


class FollowupAdapter(CursorAdapter):
    """Cursor's stop follow-ups, on the unknown harness, degraded after one miss."""

    def followup_limit(self) -> int:
        return 1

    def degraded_text(self, name: str, n: int) -> str:
        return f"{name} missed {n}"


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)


@pytest.fixture
def other(w: World) -> World:
    w.engine.adapters["unknown"] = OtherAdapter("unknown", w.cfg)
    return w


@pytest.fixture
def listening(w: World) -> World:
    w.engine.adapters["unknown"] = ListeningAdapter("unknown", w.cfg)
    return w


def test_an_adapter_that_reports_them_gets_its_session_id_and_approval_mode(other: World) -> None:
    """Fails if the engine keeps its own list of harnesses that report them: "unknown" is
    on no list, so its session id and approval mode would be dropped."""
    w = other
    p, _m = w.agent("bot", harness="unknown", status="idle", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="g1", sid="sess-2", permission_mode="bypassPermissions")
    assert w.p(p).session_id == "sess-2"
    assert w.p(p).approval_mode == "bypass"


def test_an_adapter_that_keeps_the_session_at_a_clear_keeps_it_online(other: World) -> None:
    w = other
    p, _m = w.agent("bot", harness="unknown", status="busy", hooks=True)
    w.hook(p, "Stop")
    w.hook(p, "SessionEnd", reason="clear")
    assert w.p(p).status == "idle"
    w.hook(p, "SessionEnd", reason="logout")
    assert w.p(p).status == "offline"


def test_the_default_adapter_takes_neither_and_any_session_end_ends_it(w: World) -> None:
    """An unknown harness: its hooks' claims about a session id or an approval mode are
    not taken (it fails closed, ``unknown``), and a SessionEnd ends it whatever the reason."""
    p, _m = w.agent("bot", harness="unknown", status="busy", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="g1", sid="sess-2", permission_mode="bypassPermissions")
    assert w.p(p).session_id == "sid-bot"
    assert w.p(p).approval_mode == "unknown"
    w.hook(p, "SessionEnd", reason="clear")
    assert w.p(p).status == "offline"


def test_an_adapter_whose_hook_starts_a_subagent_taints_that_prompt(listening: World) -> None:
    """Fails if the engine taints only a session it knows by name as Devin's: the prompt
    would stay clean, and a Stop that may be the subagent's would get a continue."""
    w = listening
    p, _m = w.agent("bot", harness="unknown", status="idle", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="g1")
    w.hook(p, "PreToolUse", gen="g1", tool="run_subagent", subagent_bg=True)
    assert w.p(p).gen_tainted
    assert w.hook(p, "Stop", gen="g1") is None


def test_a_stop_re_arms_with_the_adapters_text_and_cap(listening: World) -> None:
    """Fails if the re-arm's limits and text come from Devin's config and module rather
    than the session's adapter: the text would be Devin's, and a second re-arm would go."""
    w = listening
    p, _m = w.agent("bot", harness="unknown", status="idle", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="g1")
    out = w.hook(p, "Stop", gen="g1")
    assert out is not None and out.kind == "continue" and out.text == "listen in #build"
    w.hook(p, "PostToolUse", gen="g1", ok=True)
    assert w.hook(p, "Stop", gen="g1") is None  # one per prompt


def test_missed_follow_ups_degrade_by_the_adapters_limit_and_text(tmp_path: Path, clock: FakeClock) -> None:
    """Fails if the engine takes the limit and the notice from Cursor's config and text, or
    re-derives the tier and ends the degraded spell only for a session named Cursor's."""
    w = World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))
    w.engine.adapters["unknown"] = FollowupAdapter(w.cfg)
    p, _m = w.agent("bot", harness="unknown", status="busy", hooks=True)
    p = w.store.update_participant(p.id, session_key="cursor:conv-bot", bind_state="bound")
    w.actions += w.engine.refresh_tier(p.id)
    assert w.p(p).tier_note == "provisional"
    w.human("try")
    out = w.hook(p, "Stop", status="completed", loop_count=0, max_wait_s=630.0)
    sink = w.engine.sinks.get(out.sink_id)
    assert sink is not None and not sink.open
    w.engine.on_hook_ack(sink.result["batch_id"], sink.result["ack"])
    w.take()
    w.hook(p, "Stop", status="completed", loop_count=0, max_wait_s=630.0)  # the follow-up never ran
    assert w.p(p).unconfirmed_followups == 1
    assert any(isinstance(a, Notice) and a.text == "bot missed 1" for a in w.take())
    assert w.p(p).tier_note == "provisional, degraded"
    w.hook(p, "UserPromptSubmit", gen="g2")  # the human's next prompt ends the spell
    assert w.p(p).unconfirmed_followups == 0 and w.p(p).tier_note == "provisional"


def test_input_into_the_running_turn_is_not_a_new_turn(w: World, clock: FakeClock) -> None:
    """Fails if only a session named Codex's can have mid-turn input: the prompt would end
    the turn and take back the pull answer the agent is reading."""
    w.engine.adapters["unknown"] = SteeredAdapter("unknown", w.cfg)
    p, m = w.agent("bot", harness="unknown", status="idle", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="t1")
    w.human("x")
    _t, bid, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    clock.advance(1)
    w.hook(p, "UserPromptSubmit", gen="t1")  # steered into turn t1
    assert w.store.get_batch(bid).state == "offered"
    w.hook(p, "UserPromptSubmit", gen="t2")  # a new turn
    assert w.store.get_batch(bid).state == "expired"


def test_a_local_only_harness_on_another_host_gets_its_remote_adapter(w: World) -> None:
    """Fails if only Codex is looked up as ``<harness>@remote``."""
    local, remote = SteeredAdapter("unknown", w.cfg), PullAdapter("unknown", w.cfg)
    w.engine.adapters["unknown"] = local
    w.engine.adapters["unknown@remote"] = remote
    assert w.engine.adapter_for("unknown", "") is local
    assert w.engine.adapter_for("unknown", "pi") is remote
    del w.engine.adapters["unknown@remote"]  # none registered: the unknown harness's, not this one
    assert w.engine.adapter_for("unknown", "pi") is w.engine.adapters["unknown"] is local
    assert w.engine.adapter_for("claude", "pi") is w.engine.adapters["claude"]


def test_each_harness_answers_as_the_engine_did_by_name(w: World) -> None:
    """The move changes nothing: the harnesses the engine named before #169 answer yes."""
    ads = w.engine.adapters
    clear = HookEvent(harness="claude", event="SessionEnd", t=0.0, reason="clear")
    assert {h for h, a in ads.items() if a.reports_session_id} == {"claude", "devin", "test"}
    assert {h for h, a in ads.items() if a.reports_approval_mode} == {"claude", "codex", REMOTE_CODEX}
    assert {h for h, a in ads.items() if not a.ends_session(clear)} == {"claude"}
    sub = HookEvent(harness="devin", event="PreToolUse", t=0.0, subagent_bg=True)
    assert {h for h, a in ads.items() if a.starts_subagent(sub)} == {"devin"}
    assert {h for h, a in ads.items() if a.rearm_caps() != (0, 0)} == {"devin"}
    dv = w.cfg.devin
    assert ads["devin"].rearm_caps() == (dv.rearm_max_per_prompt, dv.rearm_max_per_hour)
    assert {h for h, a in ads.items() if a.derives_tier} == {"cursor", "devin"}
    assert {h for h, a in ads.items() if a.followup_limit()} == {"cursor"}
    assert ads["cursor"].followup_limit() == w.cfg.cursor.max_unconfirmed_followups
    assert {h for h, a in ads.items() if a.local_only} == {"codex"}
    assert w.engine.adapter_for("codex", "pi") is ads[REMOTE_CODEX]
