"""Grok Build over HTTPS (DESIGN.md §49.15, #851): the adapter, the client name, the hook table and the
plugin's static shape, from the payloads Grok Build 1.0.89 really sent (``tests/fixtures/payloads/grok/``).

Each event is read as the broker reads it: the plugin's script sends Grok's ``hook_event_name`` and a few
fields to ``/mcp/event``, ``agent_http.hook_params`` cuts them, and ``agents.parse_hook_event`` makes the
``HookEvent`` the adapter is asked about. The process side (the script itself against a broker) is in
``tests/integration/test_agents_http_grok.py``."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from switchboard.adapters import HTTPS_GROK, build_adapters
from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, client_wait_limit
from switchboard.adapters.https import GROK_NO_SESSION, GROK_TIER, HttpsGrokAdapter
from switchboard.broker import agent_http
from switchboard.broker import hook_replies as hr
from switchboard.broker.agents import HOOK_EVENTS, parse_hook_event
from switchboard.config import Config
from switchboard.models import Participant, Release

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "payloads" / "grok"
PLUGIN = ROOT / "plugins" / "grok"
CFG = Config(human_name="alice")
WAKE = Release(items=(), kind="wake", counted=True, reason="human")
PRIO = Release(items=(), kind="priority", counted=False, reason="human")


def payload(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text())


def as_broker_reads(name: str, **extra: Any) -> Any:
    """A recorded payload as the broker reads it: the fields the plugin's script sends (by Grok's
    ``hook_event_name``, mapped through ``GROK_EVENTS``), through ``hook_params`` and ``parse_hook_event``."""
    p = payload(name)
    event = agent_http.GROK_EVENTS[p["hook_event_name"]]
    aborted = p["hook_event_name"] in ("StopCancelled", "StopFailure")
    fields = {
        "session_id": p["session_id"],
        "prompt_id": p.get("promptId"),
        "permission_mode": p.get("permission_mode"),
        "reason": p.get("reason"),
        "stop_hook_active": p.get("stop_hook_active"),
        "notification_type": p.get("notification_type"),
        "tool_name": p.get("tool_name"),
        "tool_use_id": p.get("tool_use_id"),
        **extra,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    params = agent_http.hook_params(event, fields, 100.0)
    if aborted:
        params["aborted"] = True  # as ``CommandHook`` marks Grok's two aborted ends
    return parse_hook_event("grok", event, params)


def part(**kw: Any) -> Participant:
    base: dict[str, Any] = {f.name: None for f in dataclasses.fields(Participant)}
    base.update(
        id=7,
        harness="grok",
        session_key="grok@laptop:00000000-0000-4000-8000-0000000a1001",
        session_id="00000000-0000-4000-8000-0000000a1001",
        host="laptop",
        status="idle",
        approval_mode="prompting",
        boundary_seq=0,
        gen_tainted=False,
        rearms_in_gen=0,
        created_at=1.0,
    )
    base.update(kw)
    return Participant(**base)


def test_the_client_name_grok_sends_is_grok_and_nothing_like_it_is() -> None:
    """Grok names its MCP client ``grok-shell-<server>`` (recorded in https_mcp.jsonl, both handshakes). Fails
    if the broker refuses Grok's tools as ``unsupported`` (the spike's finding), or a bare or look-alike name
    is taken for Grok."""
    names = set()
    for line in (FIX / "https_mcp.jsonl").read_text().splitlines():
        body = json.loads(line)["body"]
        params = body.get("params") or {}
        meta = params.get("_meta") or {}
        info = params.get("clientInfo") or meta.get("io.modelcontextprotocol/clientInfo")
        if info:
            names.add(info["name"])
    assert names == {"grok-shell-sb"}
    assert agent_http.harness_of("grok-shell-sb") == "grok"
    assert agent_http.harness_of("grok-shell-switchboard") == "grok"
    for other in ("grok-shell-", "grok-shell", "grok", "xgrok-shell-sb", "Grok-shell-sb", ""):
        assert agent_http.harness_of(other) is None, other
    assert agent_http.harness_of("codex-mcp-client") == "codex"  # the exact names are unchanged
    assert "Grok Build" in agent_http.UNSUPPORTED


def test_grok_has_its_own_adapter_tier_and_session_key() -> None:
    """A Grok session is keyed by the id its MCP entry sends (``X-Switchboard-Session``) on the token's
    device; a call that names none is refused with how to fix it. Fails if Grok gets another harness's rules.
    """
    a = build_adapters(CFG)[HTTPS_GROK]
    assert isinstance(a, HttpsGrokAdapter) and a.harness == "grok"
    sid = payload("SessionStart.json")["session_id"]
    assert a.session_key("laptop", sid) == f"grok@laptop:{sid}"
    with pytest.raises(ValueError, match="X-Switchboard-Session"):
        a.session_key("laptop", None)
    assert "{{session_id}}" in GROK_NO_SESSION
    tier, note = a.tier(part())
    assert tier == GROK_TIER == "grok:https"
    assert note is not None and "1 hour" in note and "7 wakes" in note and "type in Grok" in note


def test_a_grok_session_is_woken_at_its_held_stop_and_hears_priority_after_a_tool() -> None:
    """Idle: a wake goes to the held Stop hook (the park). Busy: a priority message is pulled by the next tool
    hook. Nothing held: nothing, with why, which names Grok's limits. The plugin asks for 3630 s and is held 5
    s less; a Stop that asks for nothing (a cancelled turn's report) is never held."""
    a = HttpsGrokAdapter(CFG)
    p = part()
    sink = type("S", (), {"path": "stop_followup", "id": 9})()
    assert a.route(p, WAKE, sink, 0.0).kind == "sink"
    assert a.route(p, PRIO, None, 0.0).kind == "pull"
    r = a.route(p, WAKE, None, 0.0)
    assert r.kind == "none" and "7 wakes" in (r.reason or "") and "type in Grok" in (r.reason or "")
    assert a.park_s(p, as_broker_reads("Stop.json", wait_s=3630)) == 3625.0
    assert a.park_s(p, as_broker_reads("Stop.json")) is None
    assert a.park_s(p, as_broker_reads("StopCancelled_permission.json")) is None
    join = a.join_guidance(p, "#build")
    assert "at most 7 times" in join and "1 hour" in join


def test_only_a_permission_prompt_notification_is_an_approval() -> None:
    """Grok's open approval prompt is a ``Notification`` of type ``permission_prompt`` (recorded): it is the
    only event that shows the session waiting on its person (§7.4, §49.9). Fails if a turn's end, a tool or
    another notification type holds the session as waiting, or the prompt is missed."""
    a = HttpsGrokAdapter(CFG)
    assert a.asks_approval(as_broker_reads("Notification_permission_prompt.json"))
    other = as_broker_reads("Notification_permission_prompt.json", notification_type="idle_prompt")
    assert not a.asks_approval(other)
    others = ("Stop.json", "StopCancelled_permission.json", "PostToolUse_mcp.json", "UserPromptSubmit.json")
    for name in others:
        assert not a.asks_approval(as_broker_reads(name)), name


def test_a_cancelled_turn_is_a_stop_that_is_never_held_and_an_old_one_is_dropped() -> None:
    """StopCancelled and StopFailure end a turn: they are Stops to the broker (idle), never held, and marked
    ``aborted``. Grok reports a cancelled turn off its command loop, so it can come after the next turn's
    UserPromptSubmit: a turn's end naming another prompt than the session's current one is an old turn's and
    is dropped (``stale_hook``), never read as a subagent's (``other_thread``), whose path ends an approval
    hold (§11). Fails if a late cancel is the session's, or is routed through ``other_thread``."""
    assert agent_http.GROK_EVENTS["StopCancelled"] == agent_http.GROK_EVENTS["StopFailure"] == "Stop"
    a = HttpsGrokAdapter(CFG)
    cancelled = as_broker_reads("StopCancelled_permission.json")
    assert cancelled.ev == "Stop" and cancelled.aborted
    assert cancelled.gen == payload("StopCancelled_permission.json")["promptId"]
    assert not as_broker_reads("Stop.json").aborted
    for ev in (cancelled, as_broker_reads("Stop.json"), as_broker_reads("Stop_continuation.json")):
        assert a.stale_hook(part(gen="a-newer-prompt"), ev)
        assert not a.other_thread(part(gen="a-newer-prompt"), ev)
        assert not a.stale_hook(part(gen=ev.gen), ev)
        assert not a.stale_hook(part(gen=None), ev)  # no turn known yet: nothing to compare
        # F1: while an approval is open, only an end naming the current prompt is the session's
        held = "waiting-approval"
        assert not a.stale_hook(part(gen=ev.gen, status=held), ev)
        assert a.stale_hook(part(gen="a-newer-prompt", status=held), ev)
        assert a.stale_hook(part(gen=None, status=held), ev)  # joined mid-turn: it could be an older turn's
        assert a.stale_hook(part(gen=ev.gen, status=held), dataclasses.replace(ev, gen=None))
    tool = as_broker_reads("PostToolUse_mcp.json")
    assert tool.gen is None and not a.stale_hook(part(gen="p"), tool)  # Grok's tool hooks carry no prompt
    assert not a.stale_hook(part(gen=None, status="waiting-approval"), tool)  # only a turn's end is dropped
    assert not a.other_thread(part(gen="p"), tool)


def test_the_turn_that_joined_is_anchored_by_its_completed_stop() -> None:
    """The prompt that joined came before the session was a member, so its turn's id is learned at the turn's
    completed Stop (reason ``end_turn``), not at a cancelled one."""
    a = HttpsGrokAdapter(CFG)
    assert a.anchors_turn(part(), as_broker_reads("Stop.json"))
    assert a.anchors_turn(part(), as_broker_reads("Stop_continuation.json"))
    assert not a.anchors_turn(part(), as_broker_reads("StopCancelled_permission.json"))
    assert not a.anchors_turn(part(), as_broker_reads("PostToolUse_bash.json"))
    # an approval prompt is the current turn's: one that carries its prompt id names it
    assert a.anchors_turn(part(), as_broker_reads("Notification_permission_prompt.json", prompt_id="p-1"))


def test_a_follow_up_is_confirmed_by_the_turn_it_continued_and_lost_to_a_typed_prompt() -> None:
    """A Stop block continues the turn with no UserPromptSubmit (measured), so the continued turn's next tool
    hook or Stop confirms it; a UserPromptSubmit first is the person's prompt, sent while the hook waited
    (which ends the hook), and the follow-up goes back to pending. Fails if a message the model never saw is
    marked delivered."""
    a = HttpsGrokAdapter(CFG)
    p = part()
    assert a.continue_verdict(p, True, as_broker_reads("Stop_continuation.json")) == "confirm"
    assert a.continue_verdict(p, True, as_broker_reads("PostToolUse_bash.json")) == "confirm"
    assert a.continue_verdict(p, False, as_broker_reads("PostToolUse_bash.json")) is None
    assert a.continue_verdict(p, True, as_broker_reads("UserPromptSubmit.json")) == "human_prompt"
    assert a.continue_verdict(p, False, as_broker_reads("UserPromptSubmit.json")) == "human_prompt"
    assert a.continue_verdict(p, True, as_broker_reads("SessionEnd.json")) == "session_end"


def test_a_follow_up_whose_turn_was_cancelled_or_failed_first_goes_back_to_pending() -> None:
    """A Stop block continues the turn; if the next thing the session reports is that turn's StopCancelled
    (the person pressed Esc, or declined a prompt) or StopFailure (the model's request failed), the model
    never worked on the follow-up, so it goes back to pending (``turn_aborted``) for the next held Stop.
    Before #928's
    review both were read as a plain Stop, which confirmed it: a message nobody saw, marked delivered."""
    a = HttpsGrokAdapter(CFG)
    p = part()
    cancelled = as_broker_reads("StopCancelled_permission.json")
    assert a.continue_verdict(p, True, cancelled) == "turn_aborted"
    assert a.continue_verdict(p, False, cancelled) == "turn_aborted"
    assert a.continue_verdict(p, True, as_broker_reads("Stop_continuation.json")) == "confirm"


def test_the_hook_table_answers_grok_only_after_a_tool_and_at_stop() -> None:
    """Only a tool hook's ``additionalContext`` reaches Grok's model, and a Stop's ``decision: block`` reason
    (both measured with a stand-in model); SessionStart's and UserPromptSubmit's output never does, so they
    are never answered. Fails if any other event, or any other shape, can be printed for Grok."""
    ctx = {"kind": "context", "text": "hello"}
    cont = {"kind": "continue", "text": "wake"}
    for event in ("PostToolUse", "PostToolUseFailure"):
        assert hr.reply_for("grok", event, ctx) == {
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": "hello"}
        }
        assert hr.reply_for("grok", event, cont) is None
    assert hr.reply_for("grok", "Stop", cont) == {"decision": "block", "reason": "wake"}
    assert hr.reply_for("grok", "Stop", ctx) is None
    for event in ("SessionStart", "UserPromptSubmit", "Notification", "SessionEnd", "PreToolUse"):
        assert hr.reply_for("grok", event, ctx) is None and hr.reply_for("grok", event, cont) is None
    assert HOOK_CONTEXT_EVENTS["grok"] == frozenset(hr.CONTEXT_EVENTS["grok"])
    assert hr.reply_for("grok", "PostToolUse", {"kind": "context", "text": "y" * 8001}) is None


def test_the_broker_takes_only_the_events_grok_s_plugin_reports() -> None:
    """What ``/mcp/event`` accepts from Grok is a fixed table by Grok's own names; the engine's events for
    Grok are exactly what it maps to. No PreToolUse and no permission event: the plugin registers neither, and
    nothing Grok sends can be answered with a decision."""
    assert set(agent_http.GROK_EVENTS) == {
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "PostToolUseFailure",
        "Notification",
        "Stop",
        "StopCancelled",
        "StopFailure",
        "SessionEnd",
    }
    assert HOOK_EVENTS["grok"] == frozenset(agent_http.GROK_EVENTS.values())
    for p in sorted(FIX.glob("*.json")):
        name = json.loads(p.read_text())["hook_event_name"]
        if name in ("PreToolUse", "PermissionDenied", "SubagentStart"):
            assert name not in agent_http.GROK_EVENTS, p.name


def test_the_plugin_registers_what_the_script_reports_and_waits_within_grok_s_limits() -> None:
    """The plugin's hooks.json registers one command per event the script reports, none for PreToolUse or a
    permission event, and gives the Stop hook longer than the script waits; the script stops listening under
    Grok's 8 blocks a turn. Its MCP entry reads the token from the environment and sends the session id."""
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(hooks) == set(agent_http.GROK_EVENTS)
    assert "PreToolUse" not in hooks and not [e for e in hooks if "permission" in e.lower()]
    for event, groups in hooks.items():
        [group] = groups
        [hook] = group["hooks"]
        assert hook["type"] == "command" and hook["command"] == '"${GROK_PLUGIN_ROOT}/bin/hook"', event
        assert hook["timeout"] == (3660 if event == "Stop" else 5), event
    script = (PLUGIN / "lib" / "hook.py").read_text()
    assert "WAIT_S = 3630" in script and "MAX_WAKES = 7" in script
    mcp = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]
    assert set(mcp) == {"switchboard"}
    assert mcp["switchboard"]["headers"] == {
        "Authorization": "Bearer ${SWITCHBOARD_AGENT_TOKEN}",
        "X-Switchboard-Session": "{{session_id}}",
    }
    assert mcp["switchboard"]["url"] == "${SWITCHBOARD_URL}/mcp"
    manifest = json.loads((PLUGIN / "plugin.json").read_text())
    assert manifest["name"] == "switchboard" and manifest["version"]


def test_grok_s_wait_cap_and_context_size() -> None:
    """``[grok]`` in config.toml: a 60 s wait() by default (a Stop listener wakes it), never past 600 s, and
    8000 characters of hook context, which the hook table prints whole."""
    a = HttpsGrokAdapter(CFG)
    assert a.caps().wait_cap_s == 60 and a.caps().ctx_max_chars == 8000 == hr.CONTEXT_MAX["grok"]
    assert client_wait_limit("grok") == 600
    big = HttpsGrokAdapter(Config(human_name="alice", grok=dataclasses.replace(CFG.grok, wait_cap_s=5000)))
    assert big.caps().wait_cap_s == 600
