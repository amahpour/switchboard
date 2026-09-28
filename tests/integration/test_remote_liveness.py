"""A remote member's liveness comes from its own host (DESIGN.md §27.5.6): the satellite's
``alive`` frames. With the PID shift, any probe of this machine for a remote pid would
find no process and end the member at once."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any

import pytest

from fakes.fake_agent import FakeAgent
from fakes.fake_cli import FakeCli
from fakes.fake_link import FakeLink, wait_for


@pytest.fixture
def link() -> Any:
    lk = FakeLink(kind="inproc")
    try:
        lk.start()
        yield lk
    finally:
        lk.close()


def leaves(link: FakeLink) -> list[str]:
    return [f"{m['from']} {m['text']}" for m in link.messages() if m["kind"] == "leave"]


def test_pi_agent_exit_ends_session_within_3s(link: FakeLink) -> None:
    dv = FakeCli(None, "devin", home=link.pi)
    try:
        assert dv.tool("join", room="#fpga", screen_name="devin-pi")["ok"]
        time.sleep(1.2)  # a watch naming it has been answered
        t0 = time.monotonic()
        os.kill(dv.pid, signal.SIGKILL)
        wait_for(lambda: "devin-pi left (session ended)" in leaves(link), timeout=6, what="the session ended")
        took = time.monotonic() - t0
        assert took <= 3.0, took
        assert link.members() == []
    finally:
        dv.close()


def test_agent_dead_during_outage_ended_on_relink(link: FakeLink) -> None:
    dv = FakeCli(None, "devin", home=link.pi)
    try:
        assert dv.tool("join", room="#fpga", screen_name="devin-pi")["ok"]
        link.call("remote.disable", {"name": link.name})
        link.wait_state("disabled", reason="disabled")
        os.kill(dv.pid, signal.SIGKILL)
        time.sleep(4.5)  # two liveness ticks with the link down: nothing can tell, nothing is ended
        assert [m["name"] for m in link.members()] == ["devin-pi"]
        assert leaves(link) == []
        link.call("remote.enable", {"name": link.name}, timeout=30)
        wait_for(lambda: "devin-pi left (session ended)" in leaves(link), timeout=6, what="ended on relink")
    finally:
        dv.close()


async def test_remote_member_survives_5s_of_liveness_checks(link: FakeLink) -> None:
    async with FakeAgent(link.pi, "bench") as a:
        await a.join("#fpga", "bench")
        p = link.broker.on_loop(lambda: link.broker.state.store.joined_participants()[0])
        # its agent (this pytest process) as the satellite reports it: a pid no process here has
        assert p.agent_pid == os.getpid() + 1_000_000_000 and p.host == "fpga-pi"
        await asyncio.sleep(5.0)
        assert [m["name"] for m in link.members()] == ["bench"] and leaves(link) == []
        view = link.broker.state.hosts.view("fpga-pi")
        assert view.alive(p.agent_pid, p.agent_start) is True
        assert link.broker.state.hosts.local.alive(p.agent_pid, p.agent_start) is False  # what a desktop probe says
        assert (await a.who("#fpga"))["ok"]
