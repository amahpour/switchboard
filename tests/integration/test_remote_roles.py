"""Nothing human, room or sys crosses a link, under any peer policy (DESIGN.md §27.5.2)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
from typing import Any

import pytest

from conftest import sanitize_env
from fakes.fake_agent import FakeAgent
from fakes.fake_claude import FakeClaude
from fakes.fake_link import FakeLink, remotes_toml, wait_for
from switchboard.broker.rpc import REMOTE_FORBIDDEN
from switchboard.remote.satellite import ON_DESKTOP

FORBIDDEN = ["sys.stop", "sys.status", "sys.ping", "human.say", "human.command", "human.login_link",
             "human.logout_all", "room.create", "room.list", "room.who", "room.history", "room.tail",
             "remote.enable"]
PARAMS: dict[str, dict[str, Any]] = {
    "human.say": {"room": "#fpga", "text": "I am the human now"},
    "human.command": {"room": "#fpga", "text": "/pause"},
    "room.create": {"name": "#pwned"},
    "room.who": {"room": "#fpga"},
    "room.history": {"room": "#fpga"},
    "room.tail": {"room": "#fpga"},
    "remote.enable": {"name": "fpga-pi"},
}


@pytest.fixture(scope="module")
def link(tmp_path_factory: pytest.TempPathFactory) -> Any:
    # in-process: AllowAllHumans for every local peer, the most permissive policy there is
    mp = pytest.MonkeyPatch()
    sanitize_env(mp, tmp_path_factory)
    lk = FakeLink(kind="inproc")
    try:
        lk.start()
        yield lk
    finally:
        lk.close()
        mp.undo()


def raw(link: FakeLink, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """A request from a plain process on the remote host, on the satellite's socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(link.pi_paths.sock))
    try:
        s.sendall(json.dumps({"id": 7, "method": method, "params": params}).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            assert chunk, "the satellite closed the connection"
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])
    finally:
        s.close()


def injected(link: FakeLink, method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """The same request as a ``req`` frame on the live link, straight into the broker's frame
    handler: what a satellite that didn't refuse it (a forged one) would get."""
    rl = link.broker.state.remotes.links[link.name]
    a = rl.attempt
    got: list[dict[str, Any]] = []
    real = rl.send_frame

    def capture(att: Any, frame: dict[str, Any]) -> None:
        if frame.get("t") == "out" and frame.get("c") == 4242:
            got.append(frame["line"])
        real(att, frame)

    async def go() -> None:
        rl.send_frame = capture  # type: ignore[method-assign]
        try:
            await rl._on_frame(a, {"t": "open", "c": 4242})
            await rl._on_frame(a, {"t": "req", "c": 4242, "line": {"id": 9, "method": method, "params": params}})
            await asyncio.sleep(0.05)
            await rl._on_frame(a, {"t": "close", "c": 4242})
        finally:
            rl.send_frame = real  # type: ignore[method-assign]

    asyncio.run_coroutine_threadsafe(go(), link.broker.loop).result(10)
    return got


@pytest.mark.parametrize("method", FORBIDDEN)
def test_methods_forbidden_over_link(link: FakeLink, method: str) -> None:
    params = PARAMS.get(method, {})
    before = len(link.messages())
    # 1. the satellite refuses it on the remote host (sys.ping and sys.status it answers itself)
    r = raw(link, method, params)
    if method in ("sys.ping", "sys.status"):
        res = r["result"]
        assert res["role"] == "satellite" and res.get("port") is None
        assert "rooms" not in res or all(isinstance(x, str) for x in res["rooms"])  # the allowlist, no more
        assert "web_clients" not in res and "url" not in res
    else:
        assert r["error"]["code"] == "forbidden" and ON_DESKTOP in r["error"]["message"]
    # 2. the broker refuses it on its own, whatever the satellite does
    [reply] = injected(link, method, params)
    assert reply["error"]["code"] == "forbidden" and REMOTE_FORBIDDEN in reply["error"]["message"]
    # nothing happened: no message, the room isn't paused, the broker is up, the link too
    assert len(link.messages()) == before
    assert link.call("room.list")["rooms"][0]["settings"]["paused"] is False
    assert link.call("sys.ping")["pid"]
    assert link.status()["state"] == "up"


@pytest.mark.parametrize("verb", ["say", "cmd", "login", "logout", "stop", "create"])
def test_pi_cli_human_verbs_print_desktop_message(link: FakeLink, verb: str) -> None:
    args = {"say": ["say", "#fpga", "hi"], "cmd": ["cmd", "#fpga", "/pause"], "login": ["login"],
            "logout": ["logout", "--all"], "stop": ["stop"], "create": ["create", "#x"]}[verb]
    r = link.pi_cli(*args)
    assert r.returncode == 1 and "run this on the desktop (desk)" in r.stderr, (r.stdout, r.stderr)
    assert link.status()["state"] == "up"


def test_pi_cli_other_verbs_are_refused_by_the_satellite(link: FakeLink) -> None:
    r = link.pi_cli("rooms")
    assert r.returncode == 1 and "run this on the desktop" in r.stderr
    r = link.pi_cli("remote", "enable", "fpga-pi")
    assert r.returncode == 1 and "run this on the desktop" in r.stderr


async def test_room_allowlist_on_join() -> None:
    with FakeLink(rooms=("#fpga",), desk_rooms=("#fpga", "#secret"), kind="inproc") as link:
        async with FakeAgent(link.pi, "bench") as a:
            r = await a.join("#secret", "bench")
            assert r["ok"] is False and r["code"] == "forbidden" and "may join only #fpga" in r["error"]
            r = await a.join("#nonexistent", "bench")
            assert r["ok"] is False and r["code"] == "forbidden"  # no hint whether it exists
            assert (await a.join("#FPGA", "bench"))["ok"]
        assert link.members("#secret") == []


async def test_any_room_remote_joins_every_room() -> None:
    """rooms = ["*"] (the default): the link comes up (the welcome names the existing rooms)
    and the host's members may join any room, including one created after pairing."""
    with FakeLink(rooms=("*",), desk_rooms=("#fpga", "#secret"), kind="inproc") as link:
        assert link.status()["state"] == "up"
        async with FakeAgent(link.pi, "bench") as a:
            assert (await a.join("#secret", "bench"))["ok"]
            assert (await a.join("#fpga", "bench"))["ok"]


async def test_max_members_cap() -> None:
    with FakeLink(max_members=2, kind="inproc") as link:
        agents = [FakeAgent(link.pi, f"b{i}") for i in range(3)]
        try:
            for a in agents:
                await a.start()
            assert (await agents[0].join("#fpga", "b0"))["ok"]
            assert (await agents[1].join("#fpga", "b1"))["ok"]
            r = await agents[2].join("#fpga", "b2")
            assert r["ok"] is False and r["code"] == "conflict" and "max_members" in r["error"]
            assert any("a join from fpga-pi was refused" in t for t in link.notices())
            # a member already in counts once, in any room; one leaving frees its place
            assert (await agents[1].leave("#fpga"))["ok"]
            assert (await agents[2].join("#fpga", "b2"))["ok"]
        finally:
            for a in agents:
                await a.close()
        wait_for(lambda: link.status()["state"] == "up", what="link up")


def leave_lines(link: FakeLink, room: str) -> list[str]:
    return [f"{m['from']} {m['text']}" for m in link.messages(room) if m["kind"] == "leave"]


async def test_narrowed_rooms_revoke_at_once() -> None:
    """The rooms allowlist is a boundary, not a join-time gate: a room removed from a remote's
    entry ends that host's memberships there at once, and a re-enable doesn't bring them back."""
    with FakeLink(rooms=("#fpga", "#secret"), kind="inproc") as link:
        async with FakeAgent(link.pi, "bench") as a:
            assert (await a.join("#fpga", "bench"))["ok"]
            assert (await a.join("#secret", "bench"))["ok"]
            # belt and braces: every call checks the live entry, not only join
            rl = link.broker.state.remotes.links[link.name]
            full = rl.entry
            link.broker.on_loop(lambda: setattr(rl, "entry", dataclasses.replace(full, rooms=("#fpga",))))
            try:
                r = await a.say("#secret", "only #fpga now")
                assert r["ok"] is False and r["code"] == "forbidden" and "no longer use #secret" in r["error"], r
                assert (await a.say("#fpga", "fine here"))["ok"]
            finally:
                link.broker.on_loop(lambda: setattr(rl, "entry", full))
            # the owner narrows remotes.toml: the membership ends at once, before any enable
            link.write_remotes(remotes_toml(link.name, link.pi, ("#fpga",)))
            wait_for(lambda: not link.members("#secret"), timeout=10, what="bench out of #secret")
            assert "bench left (#secret is no longer allowed for fpga-pi)" in leave_lines(link, "#secret")
            link.wait_state("disabled", reason="config_changed")
            assert link.call("remote.enable", {"name": link.name}, timeout=30)["state"] == "up"
            wait_for(lambda: any(m["name"] == "bench" and m["status"] != "offline" for m in link.members("#fpga")),
                     timeout=15, what="bench back in #fpga")
            r = await a.say("#secret", "still here?")
            assert r["ok"] is False, r
            assert (await a.read("#secret"))["ok"] is False
            r = await a.join("#secret", "bench")
            assert r["ok"] is False and r["code"] == "forbidden"
            assert (await a.say("#fpga", "and here I stay"))["ok"]
            assert link.members("#secret") == []


def test_narrowed_harnesses_end_sessions_at_once() -> None:
    """A harness removed from a remote's entry ends that host's sessions of it at once; its
    MCP server, now 'unknown' there, can't use the old credential after a re-enable."""
    tid = "019a0000-0000-7000-8000-0000000c0de1"
    with FakeLink(kind="inproc") as link:
        cx = FakeClaude(None, as_harness="codex", home=link.pi)
        try:
            r = cx.tool("join", meta={"threadId": tid}, room="#fpga", screen_name="cx-pi")
            assert r["ok"], r
            assert next(m for m in link.members() if m["name"] == "cx-pi")["harness"] == "codex"
            link.write_remotes(remotes_toml(link.name, link.pi, link.rooms, harnesses=["claude"]))
            wait_for(lambda: not any(m["name"] == "cx-pi" for m in link.members()), timeout=10, what="cx-pi ended")
            assert "cx-pi left (codex is no longer allowed on fpga-pi)" in leave_lines(link, "#fpga")
            assert link.call("remote.enable", {"name": link.name}, timeout=30)["state"] == "up"
            r = cx.tool("say", meta={"threadId": tid}, room="#fpga", text="still codex?")
            assert r["ok"] is False, r
        finally:
            cx.close()
