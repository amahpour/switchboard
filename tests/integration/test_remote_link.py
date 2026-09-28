"""The link itself (DESIGN.md §27.4.7, §27.5.8): consent, restarts, blocks and limits,
over the exec transport (a test-mode broker runs the satellite on this machine)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import threading
import time
from pathlib import Path

import pytest

from conftest import human_cli_denied_here
from fakes.fake_agent import FakeAgent
from fakes.fake_link import FakeLink, SatDriver, wait_for


def lines(link: FakeLink, kind: str) -> list[str]:
    return [f"{m['from']} {m['text']}" for m in link.messages() if m["kind"] == kind]


def test_needs_enable_then_link_up_notice() -> None:
    with FakeLink(enable=False, trust=True).start(wait_up=False) as link:
        st = link.wait_state("disabled", reason="not_enabled")
        assert not st["enabled"] and not os.path.exists(link.pi_paths.sock)
        assert "needs enable" in link.cli("remote", "status").stdout
        r = link.cli("remote", "enable", link.name)
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("link ok: satellite ") and "(proto 1)" in r.stdout, r.stdout
        st = link.wait_state("up")
        assert st["enabled"] and st["enabled_via"] == "cli"
        [notice] = [t for t in link.notices() if "link up" in t]
        assert notice.startswith(f"{link.name}: link up (enabled via cli by alice on ")
        assert "satellite " in notice and "rtt " in notice
        assert "fpga-pi: up " in link.cli("status").stdout
        # enabling a link that is up changes nothing
        assert link.cli("remote", "enable", link.name).returncode == 0
        assert len([t for t in link.notices() if "link up" in t]) == 1


@pytest.mark.skipif(not human_cli_denied_here(), reason="needs a pytest that the peer check refuses")
def test_enable_is_human_only_under_the_production_policy() -> None:
    with FakeLink(enable=False, trust=False).start(wait_up=False) as link:
        r = link.cli("remote", "enable", link.name)
        assert r.returncode != 0 and "forbidden" in r.stderr
        assert link.status()["state"] == "disabled"


def test_config_change_needs_reenable() -> None:
    with FakeLink(trust=True) as link:
        link.write_remotes(link.toml + "end_after_s = 3600\n")
        st = link.wait_state("disabled", reason="config_changed")
        assert not st["enabled"]
        assert any("remotes.toml changed" in t for t in link.notices())
        assert not os.path.exists(link.pi_paths.sock)  # the satellite is gone with its link
        assert "needs enable (config changed)" in link.cli("remote", "status").stdout
        assert link.cli("remote", "enable", link.name).returncode == 0
        link.wait_state("up")
        # a remotes.toml that doesn't parse stops nothing and arms nothing
        link.write_remotes("[remote.fpga-pi\n")
        time.sleep(1.5)
        st = link.call("remote.status")
        assert st["config_error"] and st["remotes"][0]["state"] == "up"


async def _member(link: FakeLink, name: str) -> dict:
    return next(m for m in link.members() if m["name"] == name)


async def test_satellite_killed_members_offline_then_back_with_same_creds() -> None:
    with FakeLink(trust=True) as link:
        async with FakeAgent(link.pi, "bench") as a:
            await a.join("#fpga", "bench")
            w = await a.who("#fpga")
            assert w["ok"] and "- bench harness=test host=fpga-pi " in w["text"]
            assert "  bench@fpga-pi  test  " in link.cli("who", "#fpga").stdout
            assert "members bench" in link.cli("remote", "status").stdout
            pid = link.satellite_pid()
            assert pid
            os.kill(pid, signal.SIGKILL)
            wait_for(lambda: link.status()["state"] != "up", what="link down")
            wait_for(lambda: next(m for m in link.members() if m["name"] == "bench")["status"] == "offline",
                     what="member offline")
            link.wait_state("up")
            wait_for(lambda: next(m for m in link.members() if m["name"] == "bench")["status"] != "offline",
                     what="member back")
            res = await a.say("#fpga", "after")  # the credential it had still works
            assert res["ok"] and res["posted_id"]
        joins, leaves = lines(link, "join"), lines(link, "leave")
        assert joins == ["bench joined (test on fpga-pi, mcp-only)"] and leaves == []
        assert not any("rejoined" in t for t in link.notices())
        assert any("link down" in t and "its members are offline" in t for t in link.notices())


async def test_broker_restart_remote_rows_offline_not_ended_then_back() -> None:
    with FakeLink(trust=True) as link:
        async with FakeAgent(link.pi, "bench") as a:
            await a.join("#fpga", "bench")
            link.restart_broker(wait_up=False)
            # the new broker: the remote row is offline, not ended (it can't probe another host)
            m = next(m for m in link.members() if m["name"] == "bench")
            assert m["host"] == "fpga-pi"
            link.wait_state("up")
            wait_for(lambda: next(m for m in link.members() if m["name"] == "bench")["status"] != "offline",
                     what="member back")
            res = await a.say("#fpga", "after the restart")
            assert res["ok"] and res["posted_id"]
        assert lines(link, "leave") == [] and len(lines(link, "join")) == 1


class AcceptAndClose:
    """A socket that accepts and closes at once (a half-dead satellite), counting connects."""

    def __init__(self, path: Path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.bind(str(path))
        os.chmod(path, 0o600)
        self.s.listen(64)
        self.s.settimeout(0.2)
        self.count = 0
        self.stop = False
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self) -> None:
        while not self.stop:
            try:
                c, _ = self.s.accept()
            except (TimeoutError, OSError):
                continue
            self.count += 1
            c.close()

    def close(self) -> None:
        self.stop = True
        self.t.join(2)
        self.s.close()


async def test_mcp_reconnects_bounded_while_link_down() -> None:
    with FakeLink(trust=True) as link:
        async with FakeAgent(link.pi, "bench") as a:
            await a.join("#fpga", "bench")
            assert link.cli("remote", "disable", link.name).returncode == 0
            wait_for(lambda: not os.path.exists(link.pi_paths.sock), what="the satellite's socket gone")
            fake = AcceptAndClose(link.pi_paths.sock)
            try:
                await asyncio.sleep(3.0)
                n = fake.count
                res = await a.say("#fpga", "anyone?")
            finally:
                fake.close()
            # the 2 s satellite-home cap and no reset without an answered hello: a few connects, not thousands
            assert 1 <= n <= 8, n
            assert res["ok"] is False and "the link to the switchboard broker on desk is down" in res["error"]


def test_bye_replaced_on_live_link_blocks_and_enable_clears() -> None:
    with FakeLink(trust=True) as link:
        old = link.satellite_pid()
        rogue = SatDriver(link.pi, pid_shift=0)
        try:
            hello = rogue.recv_type("hello")  # it took the home over from the broker's satellite
            assert hello["name"] == link.name
            st = link.wait_state("blocked", reason="replaced")
            assert st["state"] == "blocked"
            assert any("link blocked (replaced)" in t for t in link.notices())
            assert link.satellite_pid() != old
            # a block never retries by itself
            time.sleep(2.5)
            assert link.status()["state"] == "blocked"
        finally:
            rogue.close()
        r = link.cli("remote", "enable", link.name)
        assert r.returncode == 0, r.stdout + r.stderr
        link.wait_state("up")
        assert link.call("remote.status")["remotes"][0]["enabled"]


def test_replaced_from_abandoned_link_is_ignored() -> None:
    link = FakeLink(kind="inproc")
    try:
        link.start()
        mgr = link.broker.state.remotes
        rl = mgr.links[link.name]
        a1 = rl.attempt

        async def reconnect() -> None:
            await rl.restart()

        link.broker.on_loop(lambda: asyncio.ensure_future(reconnect()))
        wait_for(lambda: rl.attempt is not None and rl.attempt is not a1 and rl.state == "up", what="a new attempt")
        a2 = rl.attempt
        # the old child's "bye replaced" arrives late: dropped, never a block (§27.10 half-open race)
        fut = asyncio.run_coroutine_threadsafe(rl._on_frame(a1, {"t": "bye", "why": "replaced"}), link.broker.loop)
        fut.result(5)
        time.sleep(0.3)
        assert rl.state == "up" and rl.attempt is a2
        assert link.broker.on_loop(link.broker.state.store.remote_row, link.name).blocked_at is None
        # the same frame on the current attempt does block
        fut = asyncio.run_coroutine_threadsafe(rl._on_frame(a2, {"t": "bye", "why": "replaced"}), link.broker.loop)
        with pytest.raises(Exception):
            fut.result(5)
    finally:
        link.close()


async def test_frame_flood_closes_link_local_members_unaffected() -> None:
    with FakeLink(trust=True, desk_rooms=("#fpga",)) as link:
        async with FakeAgent(link.desk, "vivado") as local:
            await local.join("#fpga", "vivado")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(link.pi_paths.sock))
            line = b'{"id":1,"method":"agent.read","params":{"cred":"x"}}\n'
            try:
                # the broker closes the link after the 1501st frame and the satellite this client
                # with it, possibly while this send is still going: that is the behavior under test
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    s.sendall(line * 3000)
                wait_for(lambda: link.status()["state"] != "up", timeout=15, what="the link closed")
            finally:
                s.close()
            res = await local.say("#fpga", "still here")
            assert res["ok"] and res["posted_id"]
            assert any("more than 300 frames a second" in t for t in link.notices())
            link.wait_state("up", timeout=20)  # it reconnects with backoff


def test_proto_mismatch_blocked() -> None:
    with FakeLink(trust=True, env={"SWITCHBOARD_TEST_LINK_PROTO": "2"}).start(wait_up=False) as link:
        link.wait_state("blocked", reason="proto")
        [n] = [t for t in link.notices() if "link blocked (proto)" in t]
        assert "link protocol 2" in n and "install the same switchboard version" in n
        wait_for(lambda: link.satellite_pid() is None or not os.path.exists(link.pi_paths.sock),
                 what="no satellite socket")


async def test_end_after_s_ends_unreachable_members() -> None:
    with FakeLink(trust=True, end_after_s=2) as link:
        async with FakeAgent(link.pi, "bench") as a:
            await a.join("#fpga", "bench")
            assert link.cli("remote", "disable", link.name).returncode == 0
            wait_for(lambda: not link.members(), timeout=10, what="the member ended")
            assert "bench left (fpga-pi unreachable)" in lines(link, "leave")
            assert any("link disabled by alice" in t for t in link.notices())


async def test_removed_remote_ends_its_members_at_once() -> None:
    with FakeLink(trust=True) as link:
        async with FakeAgent(link.pi, "bench") as a:
            await a.join("#fpga", "bench")
            link.write_remotes("")
            wait_for(lambda: not link.members(), timeout=10, what="the member ended")
            assert "bench left (fpga-pi unreachable)" in lines(link, "leave")
            assert link.call("remote.status")["remotes"] == []


def test_concurrent_enables_share_one_attempt() -> None:
    """Two enables at once (two terminals, or the CLI and the web UI) never end each other's
    attempt: both answer up, one link-up notice, no false link-down."""
    link = FakeLink(kind="inproc", enable=False)
    try:
        link.start(wait_up=False)
        link.wait_state("disabled", reason="not_enabled")
        mgr = link.broker.state.remotes
        rl = mgr.links[link.name]

        async def both() -> list[dict]:
            return list(await asyncio.gather(mgr.enable(link.name, via="cli"), mgr.enable(link.name, via="cli")))

        res = asyncio.run_coroutine_threadsafe(both(), link.broker.loop).result(40)
        assert [r["state"] for r in res] == ["up", "up"], res
        link.wait_state("up")
        assert link.broker.on_loop(lambda: rl.attempt is not None and rl.attempt.up_at is not None)
        time.sleep(1.5)
        notices = link.notices()
        assert len([t for t in notices if "link up" in t]) == 1, notices
        assert not any("link down" in t for t in notices), notices
        assert link.status()["state"] == "up"
    finally:
        link.close()


def test_edit_then_enable_at_once_is_one_clean_attempt() -> None:
    """An edit followed at once by an enable: whichever of the enable and the manager's 1 s
    tick reads the edit first, the other sees no change; the enable's attempt is never
    ended under it (no 'exit -15'), and each edit is told once."""
    link = FakeLink(kind="inproc")
    try:
        link.start()
        for i in range(3):
            link.write_remotes(link.toml + f"end_after_s = {3600 + i}\n")
            time.sleep(0.45 * i)  # before, around and after the manager's tick
            r = link.call("remote.enable", {"name": link.name}, timeout=40)
            assert r["state"] == "up", r
            time.sleep(1.2)
            assert link.status()["state"] == "up"
        notices = link.notices()
        assert len([t for t in notices if "remotes.toml changed" in t]) == 3, notices
        assert not any("link down" in t for t in notices), notices
    finally:
        link.close()


def test_key_file_edit_needs_reenable() -> None:
    """``config_hash`` covers the link key and the pinned host key: an edit of either stops
    the link within a second, and no reconnect dials it before a new enable (§27.5.8)."""
    link = FakeLink(kind="inproc")
    try:
        link.start()
        rl = link.broker.state.remotes.links[link.name]
        d = link.desk / "remotes" / link.name
        d.mkdir(parents=True, mode=0o700)
        (d / "known_hosts").write_text("switchboard-fpga-pi ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOTHERKEY\n")
        # the dial gate reads the files itself, before the manager's tick has seen the edit
        assert link.broker.on_loop(rl.may_dial) is False
        st = link.wait_state("disabled", reason="config_changed", timeout=10)
        assert not st["enabled"]
        wait_for(lambda: not os.path.exists(link.pi_paths.sock), what="the satellite gone")
        time.sleep(2.5)  # no backoff retry dials it
        assert link.status()["state"] == "disabled" and not os.path.exists(link.pi_paths.sock)
        assert any("remotes.toml changed (its entry or key files)" in t for t in link.notices())
        assert link.call("remote.enable", {"name": link.name}, timeout=30)["state"] == "up"
    finally:
        link.close()


def test_repeated_failure_is_noticed_once() -> None:
    """A failure that repeats at every retry is told once until the link is next up."""
    from switchboard.broker.remote import LinkClosed

    link = FakeLink(kind="inproc")
    try:
        link.start()
        rl = link.broker.state.remotes.links[link.name]

        def fail_twice() -> None:
            rl.state = "down"  # as between two retries
            rl._closed(LinkClosed("down", "malformed", "fpga-pi: a malformed hello from the satellite (x)"))
            rl._closed(LinkClosed("down", "malformed", "fpga-pi: a malformed hello from the satellite (x)"))
            rl.state, rl.reason = "up", ""

        link.broker.on_loop(fail_twice)
        assert len([t for t in link.notices() if "malformed hello" in t]) == 1
    finally:
        link.close()


def test_broker_closed_remote_conn_is_cleaned_up() -> None:
    """``RemoteConn.close()`` (the broker ends one remote connection): the satellite is told,
    the member goes offline as on the local path, and its slot is free."""
    link = FakeLink(kind="inproc")
    try:
        link.start()
        rl = link.broker.state.remotes.links[link.name]
        agents = link.broker.state.agents
        seen: list[int] = []
        sent: list[dict] = []

        async def go() -> None:
            a = rl.attempt
            real_closed, real_send = agents.conn_closed, rl.send_frame
            agents.conn_closed = lambda c: (seen.append(c.c), real_closed(c))  # type: ignore[method-assign]
            rl.send_frame = lambda att, f: (sent.append(f), real_send(att, f))  # type: ignore[method-assign]
            try:
                await rl._on_frame(a, {"t": "open", "c": 4243})
                conn = a.conns[4243]
                conn.close()
                assert 4243 not in a.conns and conn.closed
                conn.close()  # idempotent
            finally:
                agents.conn_closed, rl.send_frame = real_closed, real_send  # type: ignore[method-assign]

        asyncio.run_coroutine_threadsafe(go(), link.broker.loop).result(10)
        assert seen == [4243]
        assert {"t": "close", "c": 4243} in sent
        assert link.status()["state"] == "up"
    finally:
        link.close()
