"""One source for ``wait()``'s cap (#547, DESIGN.md §6.1, §9.1), held to what each client allows (#559).

The tool a harness calls clamps ``timeout_s`` before the broker's adapter does. The tool's cap used to be
a table of its own (``WAIT_CAPS``), so ``[claude] wait_cap_s = 1800`` could lower the cap but never raise
it: the tool still sent 110. Now the tool asks the caller's adapter, the same ``caps().wait_cap_s`` the
broker clamps with, so each of these is that adapter's answer. That answer is the config value, never more
than the harness's own client lets a call run (``CLIENT_WAIT_LIMIT_S``): past it the client has given up
on the call and the message that came meanwhile is late."""

from __future__ import annotations

import contextvars
import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeClock

from switchboard.adapters import adapter_for, build_adapters
from switchboard.adapters.base import (
    CLIENT_WAIT_LIMIT_S,
    UNKNOWN_WAIT_LIMIT_S,
    client_wait_limit,
    waits_over_client_limit,
)
from switchboard.broker.agent_http import Caller, HttpTools
from switchboard.config import Config, from_dict


def tool_cap(harness: str | None, cfg: Config) -> int:
    """What ``HttpTools.wait_cap()`` answers for a call from ``harness`` (None: a call with no caller), on a
    broker whose adapters are built from ``cfg``. The tools reach them through ``engine.adapter_for``."""
    adapters = build_adapters(cfg)
    engine = SimpleNamespace(adapter_for=lambda h: adapter_for(adapters, h))
    tools = HttpTools(SimpleNamespace(clock=FakeClock(), engine=engine))  # type: ignore[arg-type]

    def ask() -> int:
        nothing: Any = None  # the caller's token and connection play no part in the cap
        if harness is not None:
            tools._caller.set(Caller(token=nothing, harness=harness, thread=None, conn=nothing))
        return tools.wait_cap()

    return contextvars.copy_context().run(ask)  # the caller is set for this call only


@pytest.mark.parametrize(
    "harness,cap",
    [
        ("claude", 600),
        ("codex", 280),
        ("cursor", 55),
        ("devin", 100),  # 600 in the config, held to 100 over HTTPS (§49.7)
        ("opencode", 50),
        ("test", 50),
        ("unknown", 50),
        (None, 50),
    ],
)
def test_the_defaults_are_the_measured_harness_caps(harness: str | None, cap: int) -> None:
    """No config at all: each harness uses its measured default, with Claude raised by #558. (The tool
    used to say 600 for Devin, and the broker cut that to 100, so the wait it got was 100 either way.)"""
    assert tool_cap(harness, Config()) == cap


def test_a_config_cap_sets_the_tools_cap_up_to_what_each_client_allows() -> None:
    """``wait_cap_s`` in config.toml sets the tool's cap, in both directions, as far as the harness's own
    client lets a call run and no further. Fails if the tool keeps a table of its own (Claude's would stay
    110 under 1800 even with room to spare), or if a value past a client's limit gets through: Claude Code
    is held to the measured 600 s, Cursor cancels at 60 and Devin repeats a call at 120."""
    cfg = from_dict(
        {
            "claude": {"wait_cap_s": 1800},
            "codex": {"wait_cap_s": 900},
            "cursor": {"wait_cap_s": 300},
            "opencode": {"wait_cap_s": 400},
            "devin": {"wait_cap_s": 600},
        }
    )
    assert [tool_cap(h, cfg) for h in ("claude", "codex", "cursor", "opencode", "devin")] == [
        600,  # measured with the plugin timeout and background-result confirmation (#558)
        900,  # Codex's limit is its tool_timeout_sec, which the setup raises to a day
        55,  # Cursor cancels a call at 60 s
        50,  # OpenCode was not measured
        100,  # Devin's HTTP client repeats a call after 120 s (§49.7)
    ]
    # the scripted test agent and an unknown harness have no config key
    assert tool_cap("test", cfg) == tool_cap("unknown", cfg) == 50
    # lowering is free, for every harness
    low = from_dict({"claude": {"wait_cap_s": 30}, "codex": {"wait_cap_s": 20}, "cursor": {"wait_cap_s": 10}})
    assert [tool_cap(h, low) for h in ("claude", "codex", "cursor")] == [30, 20, 10]


def test_codex_can_be_raised_to_a_day_but_not_past_its_tool_timeout() -> None:
    """The setup sets Codex's ``tool_timeout_sec`` to 86400 (USAGE), and a ``wait()`` is held less than the
    10 s its Stop hook's wait keeps under it. Fails if a config value past that gets through, or if Codex
    can't be raised to a long wait at all (#560's point)."""
    cfg = from_dict({"codex": {"wait_cap_s": 1200}})
    assert tool_cap("codex", cfg) == 1200
    huge = from_dict({"codex": {"wait_cap_s": 10**9}})
    assert tool_cap("codex", huge) == CLIENT_WAIT_LIMIT_S["codex"] == 86390


def test_an_unknown_harness_gets_the_lowest_cap() -> None:
    """A harness the table doesn't name fails closed: the lowest limit of them all, whatever a config says.
    Fails if an unrecognized harness gets a longer wait than the shortest-lived client."""
    assert UNKNOWN_WAIT_LIMIT_S == min(CLIENT_WAIT_LIMIT_S.values()) == 50
    assert client_wait_limit("a-harness-nobody-wrote") == UNKNOWN_WAIT_LIMIT_S
    assert client_wait_limit("") == UNKNOWN_WAIT_LIMIT_S
    loud = from_dict({"claude": {"wait_cap_s": 1800}, "codex": {"wait_cap_s": 1800}})
    assert tool_cap("a-harness-nobody-wrote", loud) == tool_cap(None, loud) == 50


def test_every_harness_with_a_cap_setting_has_a_client_limit() -> None:
    """A harness added to config.toml with a ``wait_cap_s`` key must get a limit of its own, chosen on
    purpose: without an entry it would be held to the lowest cap, which is safe and surprising. Fails when
    a section gains the key and the table doesn't."""
    cfg = Config()
    sections = {f.name for f in dataclasses.fields(cfg) if hasattr(getattr(cfg, f.name), "wait_cap_s")}
    assert sections == set(CLIENT_WAIT_LIMIT_S) - {"test"}, sections
    # a default config says nothing at start: no default is over its own limit (Devin's 600 is left out)
    assert waits_over_client_limit(cfg) == []


def test_the_start_up_report_names_caps_that_are_held_down() -> None:
    """The broker says at start which settings are above what their client allows (the clamp is otherwise
    silent: the person sees only waits that end early). Fails if one is missed, a setting within its limit
    is named, or Devin's documented 600 (held to 100 whatever is set) is reported on every start."""
    cfg = from_dict(
        {
            "claude": {"wait_cap_s": 1800},
            "codex": {"wait_cap_s": 900},  # within a day
            "cursor": {"wait_cap_s": 300},
            "opencode": {"wait_cap_s": 40},  # lowered
            "devin": {"wait_cap_s": 600},  # its default: not news (§49.7)
        }
    )
    assert waits_over_client_limit(cfg) == [("claude", 1800, 600), ("cursor", 300, 55)]
