"""One source for ``wait()``'s cap (#547, DESIGN.md §6.1, §9.1).

The tool a harness calls clamps ``timeout_s`` before the broker's adapter does. The tool's cap used to be
a table of its own (``WAIT_CAPS``), so ``[claude] wait_cap_s = 1800`` could lower the cap but never raise
it: the tool still sent 110. Now the tool asks the caller's adapter, the same ``caps().wait_cap_s`` the
broker clamps with, so each of these is that adapter's answer."""

from __future__ import annotations

import contextvars
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeClock

from switchboard.adapters import adapter_for, build_adapters
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
        ("claude", 110),
        ("codex", 280),
        ("cursor", 55),
        ("devin", 100),  # 600 in the config, held to 100 over HTTPS (§49.7)
        ("opencode", 50),
        ("test", 50),
        ("unknown", 50),
        (None, 50),
    ],
)
def test_the_defaults_are_the_caps_they_always_were(harness: str | None, cap: int) -> None:
    """No config at all: each harness waits as long as before the tool shared its adapter's cap. (The tool
    used to say 600 for Devin, and the broker cut that to 100, so the wait it got was 100 either way.)"""
    assert tool_cap(harness, Config()) == cap


def test_a_config_cap_raises_what_the_tool_lets_through_and_lowers_it() -> None:
    """``wait_cap_s`` in config.toml sets the tool's cap in both directions, for every harness that has the
    key. Fails if the tool keeps a table of its own: Claude's would stay 110 under 1800."""
    cfg = from_dict(
        {
            "claude": {"wait_cap_s": 1800},
            "codex": {"wait_cap_s": 900},
            "cursor": {"wait_cap_s": 300},
            "opencode": {"wait_cap_s": 400},
            "devin": {"wait_cap_s": 600},
        }
    )
    assert [tool_cap(h, cfg) for h in ("claude", "codex", "cursor", "opencode")] == [1800, 900, 300, 400]
    # Devin's HTTP client repeats a call after 120 s: its adapter holds the cap to 100 whatever is set
    assert tool_cap("devin", cfg) == 100
    # the scripted test agent and an unknown harness have no config key
    assert tool_cap("test", cfg) == tool_cap("unknown", cfg) == 50
    low = from_dict({"claude": {"wait_cap_s": 30}, "codex": {"wait_cap_s": 20}})
    assert (tool_cap("claude", low), tool_cap("codex", low)) == (30, 20)
