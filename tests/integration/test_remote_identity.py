"""Who is who on a remote host (DESIGN.md §27.5.3, §27.5.4): the satellite attests, the host
namespaces keys and credentials. The Exp B and Exp C regressions of the design (§27)."""

from __future__ import annotations

from typing import Any

import pytest

from fakes.fake_agent import FakeAgent
from fakes.fake_claude import FakeClaude
from fakes.fake_link import FakeLink, SatDriver, make_pi_home, wait_for
from switchboard.mcp.client import RpcError, Stream

TID = "019a0000-0000-7000-8000-0000000c0dex"


def spy_creds(link: FakeLink) -> list[str]:
    creds: list[str] = []
    agents = link.broker.state.agents
    orig = agents.join

    def spy(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        res = orig(conn, params)
        creds.append(res["cred"])
        return res

    agents.join = spy
    return creds


def member(link: FakeLink, name: str) -> dict[str, Any]:
    return next(m for m in link.members() if m["name"] == name)


def pi_agent(link: FakeLink, name: str = "tool") -> FakeClaude:
    """A stand-in agent CLI on the Pi that is no known harness (its MCP server's claim fails)."""
    return FakeClaude(None, as_harness=name, home=link.pi)


def test_two_pi_agents_are_two_participants() -> None:
    with FakeLink(kind="inproc") as link:
        a, b = pi_agent(link, "tool"), pi_agent(link, "tool")
        try:
            assert a.tool("join", room="#fpga", screen_name="one")["ok"]
            assert b.tool("join", room="#fpga", screen_name="two")["ok"]
            ms = {m["name"]: m for m in link.members()}
            assert set(ms) == {"one", "two"}
            assert all(m["harness"] == "unknown" and m["host"] == "fpga-pi" for m in ms.values())
            keys = link.broker.on_loop(lambda: sorted(p.session_key for p in
                                                      link.broker.state.store.joined_participants()))
            assert len(set(keys)) == 2 and all(k.startswith("unknown@fpga-pi:") for k in keys)
            assert any(k.startswith(f"unknown@fpga-pi:{a.pid + 1_000_000_000}@") for k in keys)
        finally:
            a.close()
            b.close()


def test_one_bye_leaves_the_other_online() -> None:
    with FakeLink(kind="inproc") as link:
        a, b = pi_agent(link, "tool"), pi_agent(link, "tool")
        try:
            assert a.tool("join", room="#fpga", screen_name="one")["ok"]
            assert b.tool("join", room="#fpga", screen_name="two")["ok"]
            a.close()  # its MCP server says bye
            wait_for(lambda: member(link, "one")["status"] == "offline", what="one offline")
            assert member(link, "two")["status"] != "offline"
            assert b.tool("say", room="#fpga", text="still here")["ok"]
        finally:
            b.close()


async def test_credential_from_another_pi_process_refused() -> None:
    with FakeLink(kind="inproc") as link:
        creds = spy_creds(link)
        async with FakeAgent(link.pi, "bench") as a:
            await a.join("#fpga", "bench")
            cred = creds[-1]
            # this pytest process on the Pi's socket: another process than the MCP server that joined
            with Stream(link.pi_paths.sock) as s:
                s.call("mcp.hello", {"harness": "test", "test_session": "thief"})
                with pytest.raises(RpcError) as e:
                    s.call("agent.say", {"cred": cred, "text": "I am bench"})
                assert e.value.code == "unauthorized" and "another process" in e.value.message
            # the same credential on the desktop's own socket: another host
            with Stream(link.desk_paths.sock) as s:
                s.call("mcp.hello", {"harness": "test", "test_session": "thief"})
                with pytest.raises(RpcError) as e:
                    s.call("agent.say", {"cred": cred, "text": "I am bench"})
                assert e.value.code == "unauthorized"
            assert (await a.who("#fpga"))["ok"]  # the real one still works
        assert not [m for m in link.messages() if "I am bench" in m["text"]]


async def test_test_harness_needs_test_mode_on_both_sides() -> None:
    # a satellite not in test mode refuses the test harness itself, before the broker sees it
    pi = make_pi_home()
    sat = SatDriver(pi, test_mode=False, env={"SSH_CONNECTION": "192.0.2.10 5000 192.0.2.20 22"})
    try:
        hello = sat.welcome()
        assert hello["test_mode"] is False
        wait_for(lambda: (pi / "run" / "broker.sock").exists(), what="the satellite's socket")
        with Stream(pi / "run" / "broker.sock") as s:
            with pytest.raises(RpcError) as e:
                s.call("mcp.hello", {"harness": "test", "test_session": "x"})
            assert e.value.code == "forbidden" and "test-mode" in e.value.message
            s.send("mcp.hello", {"harness": "unknown"})  # anything else is attested and relayed
            req = sat.recv_type("req")
        assert req["line"]["params"]["harness"] == "unknown" and req["facts"]["attest"]["harness"] == "unknown"
        # the refused test hello was never relayed: the first req frame is the second hello
        assert req["line"]["id"] == 2
    finally:
        sat.close()
        import shutil

        shutil.rmtree(pi, ignore_errors=True)
    # the broker refuses the test harness from a satellite that isn't in test mode
    with FakeLink(kind="inproc") as link:
        rl = link.broker.state.remotes.links[link.name]
        rl.sat_test_mode = False
        async with FakeAgent(link.pi, "bench") as a:
            r = await a.join("#fpga", "bench")
            assert r["ok"] is False and "test mode on both machines" in r["error"]
        rl.sat_test_mode = True
        # and a broker not in test mode blocks a test-mode satellite at the handshake (a production
        # broker never runs an exec link, so its handshake is driven here directly)
        from switchboard.broker.remote import Attempt, LinkClosed
        from switchboard.remote import proto

        hello = proto.hello(version="0.0.0", name=link.name, now=0.0, hook_state="ok", test_mode=True,
                            harden="none")
        link.broker.state.test_mode = False
        try:
            with pytest.raises(LinkClosed) as ei:
                rl._handshake(Attempt(n=99, link_id="0" * 16), hello)
            assert (ei.value.state, ei.value.reason) == ("blocked", "test_mode")
        finally:
            link.broker.state.test_mode = True


def test_codex_thread_cannot_cross_hosts() -> None:
    with FakeLink(kind="inproc") as link:
        local = FakeClaude(link.broker, as_harness="codex")
        remote = FakeClaude(None, as_harness="codex", home=link.pi)
        try:
            r = local.tool("join", meta={"threadId": TID}, room="#fpga", screen_name="cx-desk")
            assert r["ok"], r
            r = remote.tool("join", meta={"threadId": TID}, room="#fpga", screen_name="cx-pi")
            assert r["ok"] is False and r["code"] == "conflict" and "another machine" in r["error"]
            # the other way round: a thread joined from the Pi can't be joined here
            r = remote.tool("join", meta={"threadId": TID + "-2"}, room="#fpga", screen_name="cx-pi")
            assert r["ok"], r
            r = local.tool("join", meta={"threadId": TID + "-2"}, room="#fpga", screen_name="cx-desk2")
            assert r["ok"] is False and r["code"] == "conflict"
            m = member(link, "cx-pi")
            assert (m["harness"], m["tier"], m["tier_note"], m["host"]) == (
                "codex", "codex:hook", "remote Codex: pull only", "fpga-pi")
        finally:
            local.close()
            remote.close()


def test_harness_outside_allowlist_is_unknown() -> None:
    with FakeLink(kind="inproc", harnesses=["claude"]) as link:
        cx = FakeClaude(None, as_harness="codex", home=link.pi)
        try:
            r = cx.tool("join", meta={"threadId": TID}, room="#fpga", screen_name="cx-pi")
            assert r["ok"], r
            m = member(link, "cx-pi")
            assert (m["harness"], m["tier"], m["tier_note"]) == ("unknown", "mcp-only", "not allowed for fpga-pi")
        finally:
            cx.close()
