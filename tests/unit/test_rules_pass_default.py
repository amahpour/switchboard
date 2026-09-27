"""pass() is the default (DESIGN.md §8.6): every woken agent
is told that pass() is a good default and that it should speak only when it adds
something new. The advice is part of every batch header, on every path, and of
the join() result."""

from __future__ import annotations

import dataclasses
import random
from pathlib import Path

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
from switchboard.models import Push

ADVICE = envelope.PASS_ADVICE


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def test_the_advice_says_what_the_rule_says() -> None:
    assert ADVICE == "pass() is a good default; speak only if you add something new."


def texts_on_every_path(w: World) -> dict[str, str]:
    out: dict[str, str] = {}
    pc, mc, _conn = claude(w, "claude-1")  # idle: inbox
    ph, mh, _c2 = claude(w, "claude-2", status="busy", attached=False, registry=None)  # hook context
    px, mx = codex(w)
    attach(w)
    pt, mt = w.agent("bot")
    st, acts = w.engine.open_wait(w.p(pt), w.m(mt), "w1", 3600)
    w.actions += acts
    pu, mu = cursor(w, status="busy")
    park = stop(w, pu)
    w.human("hello everyone")
    for a in w.take():
        if isinstance(a, Push):
            out[a.path] = a.text
    out["wait"] = w.engine.sinks.get(st.id).result["text"]
    out["stop_followup"] = park_result(w, park.sink_id)["text"]
    out["hook_ctx"] = w.hook(ph, "PostToolUse", ok=True).text
    return out


def test_every_path_carries_the_advice(w: World) -> None:
    got = texts_on_every_path(w)
    assert {"inbox", "turn_start", "wait", "stop_followup", "hook_ctx"} <= set(got)
    for path, text in got.items():
        assert text.startswith("[switchboard]"), path
        assert ADVICE in text.splitlines()[0], path  # in the header, before any message


def test_devin_stop_block_and_a_steer_carry_it(w: World) -> None:
    pd, md = devin(w)
    w.hook(pd, "UserPromptSubmit", gen="g1")
    w.human("for devin")
    out = w.hook(pd, "Stop", gen="g1")
    assert out is not None and out.kind == "continue" and ADVICE in out.text.splitlines()[0]
    px, mx = codex(w, status="busy")
    attach(w, view="busy")
    w.human("steer this")
    [push] = [a for a in w.take() if isinstance(a, Push)]
    assert push.path == "steer" and ADVICE in push.text.splitlines()[0]


def test_stub_only_reminder_and_again_headers_carry_it(w: World) -> None:
    stub = envelope.render_batch([item(1, 1)], room="#build", recipient="bot", human_name="alice",
                                 token="yk:b1.00000000", peer_inline=False)
    assert 'Call read("#build")' in stub and ADVICE in stub.splitlines()[0]
    again = dataclasses.replace(item(2, 2), redelivered=True)
    rem = dataclasses.replace(item(3, 1), reminders=1)
    for it in (again, rem):
        text = envelope.render_batch([it], room="#build", recipient="bot", human_name="alice",
                                     token="yk:b1.00000000", peer_inline=True)
        assert ADVICE in text.splitlines()[0]


def test_every_rendered_batch_has_it_in_its_header() -> None:
    rng = random.Random(1234)
    for _ in range(300):
        n = rng.randint(1, 8)
        items = []
        for i in range(n):
            it = item(i + 1, rng.choice([0, 1, 2]), text=rng.choice(["x", "<b>", "/pause", "yk:b1.aa"]))
            it = dataclasses.replace(it, redelivered=rng.random() < 0.2, reminders=rng.choice([0, 0, 1, 2]))
            items.append(it)
        text = envelope.render_batch(items, room="#build", recipient="bot", human_name="alice",
                                     token="yk:b9.12345678", peer_inline=rng.random() < 0.5,
                                     more=rng.random() < 0.3)
        head = text.splitlines()[0]
        assert head.startswith("[switchboard]") and head.endswith(ADVICE)


def test_the_join_result_carries_it() -> None:
    text = envelope.render_join(room="#build", screen_name="bot", human_name="alice", others=[],
                                catchup=[], nonce="0" * 16, guidance="x", test_mode=False)
    assert ADVICE in text
