"""Machines that dial in (issue #41 part 2, DESIGN.md §31.7): ``switchboard remote join``, the
dialer, ``/link/pair`` and the ``/link`` WebSocket, approval, one live link per key, Remove.

A test-mode broker runs in this process behind ``http://localhost:<port>``, claimed with a
software passkey; the machine is a second temp home whose ``remote join`` and dialer run as
real processes over plain ``ws://`` (``fakes.fake_dialin.DialIn``). Its agents are scripted
MCP clients (``FakeAgent``) on the machine's own socket.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from conftest import FakeClock
from fakes.fake_agent import FakeAgent
from fakes.fake_dialin import DialIn
from fakes.fake_link import make_pi_home, wait_for
from switchboard.broker.passkeys import CLAIM_GRACE_S
from switchboard.remote import linkkey
from switchboard.remote.dialer import EXIT_FINAL, read_state


@pytest.fixture
def d() -> Any:
    with DialIn().start() as x:
        x.b.on_loop(lambda: x.b.state.service.create_room("#build"))
        yield x


def notices(d: DialIn, room: str = "#build") -> list[str]:
    msgs = d.b.call("room.history", {"room": room, "limit": 200})["messages"]
    return [m["text"] for m in msgs if m["kind"] == "notice"]


def events(d: DialIn, what: str) -> list[dict[str, Any]]:
    evs = d.b.on_loop(lambda: d.b.state.store.recent_events(kinds=["machine"], limit=200))
    return [e.data for e in evs if e.data.get("what") == what]


def link_ws(d: DialIn, *, origin: str | None = None) -> Any:
    """A raw /link WebSocket, as the dialer (or something else) opens it."""
    return connect(f"ws://localhost:{d.b.port}/link", origin=origin, open_timeout=5, close_timeout=2,  # type: ignore[arg-type]
                   compression=None)


def recv(ws: Any) -> dict[str, Any]:
    return json.loads(ws.recv(timeout=10))


# ------------------------------------------------------------------ the path
async def test_pair_wait_approve_talk_and_remove(d: DialIn) -> None:
    r = d.join(d.code())
    assert r.returncode == 0, r.stderr
    fp = r.stdout.split("This machine's key: ", 1)[1].split()[0]
    assert "Check the web UI shows the same before you approve it." in r.stdout
    assert f"switchboard install claude --home {d.machine}" in r.stdout or "switchboard install" in r.stdout
    conf = (d.machine / "satellite.toml").read_text()
    assert 'transport = "wss"' in conf and f'broker_url = "{d.origin}"' in conf
    m = d.machine_info()
    assert m is not None and m["state"] == "pending" and m["key_fp"] == fp and not m["dialed_in"]
    assert m["facts"]["version"] and m["facts"]["arch"]
    assert [e["key"] for e in events(d, "paired")] == [fp]

    # it dials in and waits in the handshake: no session, so no socket for its agents
    d.start_dialer()
    d.wait_machine(lambda m: m["dialed_in"], what="dialed in")
    wait_for(lambda: (read_state(d.machine_paths) or {}).get("state") == "pending", what="the dialer says pending")
    assert not d.sock.exists()
    assert "work-laptop" not in d.b.state.remotes.machines

    # approved: the link comes up, then the socket
    d.approve()
    d.wait_machine(lambda m: m["state"] == "up", what="up")
    wait_for(d.sock.exists, what="the machine's socket")
    assert (read_state(d.machine_paths) or {}).get("state") == "up"
    async with FakeAgent(d.machine, "bench") as a:
        j = await a.join("#build", "bench")
        assert j["ok"], j
        assert (await a.say("#build", "hello from the machine"))["ok"]
    hist = d.b.call("room.history", {"room": "#build"})["messages"]
    said = [(x["from"], x.get("host"), x["text"]) for x in hist if x["kind"] == "chat"]
    assert said == [("bench", "work-laptop", "hello from the machine")]
    st = d.cli("status")
    assert st.returncode == 0 and "switchboard dialer for work-laptop: up" in st.stdout and d.origin in st.stdout
    assert d.cli("say", "#build", "x").returncode != 0  # human verbs belong to the web UI
    m = d.machine_info()
    assert m is not None and m["approved"] and m["approved_via"] == "web" and m["last_seen"]

    # removed: the member is ended at once, the dialer is refused and stops for good
    p = d.dialers[0]
    assert d.remove() == {"name": "work-laptop", "ended": 1}
    assert p.wait(15) == EXIT_FINAL
    assert "the broker's owner removed this machine" in d.dialer_output()
    assert (read_state(d.machine_paths) or {}).get("reason") == "removed"
    assert not d.sock.exists()
    leaves = [x["text"] for x in d.b.call("room.history", {"room": "#build"})["messages"] if x["kind"] == "leave"]
    assert leaves == ["left (work-laptop removed)"]
    assert d.machine_info() is None and d.b.state.store.machine("work-laptop").key == b""


async def test_the_link_drops_and_comes_back(d: DialIn) -> None:
    d.linked()
    wait_for(d.sock.exists, what="the socket")
    # last seen: kept in memory at every frame (a ping every 2 s), saved once a minute and at a disconnect
    store = d.b.state.store
    writes: list[float] = []
    real = store.machine_seen
    store.machine_seen = lambda name, t: (writes.append(t), real(name, t))[1]  # type: ignore[method-assign]
    first = d.machine_info()["last_seen"]
    await asyncio.sleep(4.5)
    assert d.machine_info()["last_seen"] > first and writes == []
    async with FakeAgent(d.machine, "bench") as a:
        await a.join("#build", "bench")
        link = d.b.state.remotes.machines["work-laptop"]
        # the broker side goes away (a restart, a network drop): the dialer redials, the member comes back
        d.b.on_loop(lambda: link._abort(link.attempt, __import__("switchboard.broker.remote", fromlist=["x"])
                                         .LinkClosed("down", "eof")))
        d.wait_machine(lambda m: m["state"] != "up", what="down")
        d.wait_machine(lambda m: m["state"] == "up", what="up again", timeout=30)
        wait_for(lambda: next(x for x in d.b.call("room.who", {"room": "#build"})["members"]
                              if x["name"] == "bench")["status"] != "offline", timeout=15, what="bench back")
        assert (await a.say("#build", "still here"))["ok"]
    assert len([t for t in notices(d) if "link down" in t]) == 1
    assert len(writes) in (1, 2)  # the drop saved it (both ends of the bridge may), never each ping


def test_pending_waits_longer_than_a_welcome_without_redialing(d: DialIn) -> None:
    """The 10 s a satellite session waits for its welcome never runs while pending: the dialer
    waits in the handshake, on one connection, with no socket for its agents."""
    d.paired()
    d.start_dialer()
    d.wait_machine(lambda m: m["dialed_in"], what="dialed in")
    time.sleep(11.5)
    m = d.machine_info()
    assert m is not None and m["dialed_in"] and m["state"] == "pending"
    assert not d.sock.exists()
    assert d.dialer_output().count("dialer: connecting") <= 1 and "no welcome" not in d.dialer_output()
    logs = (d.machine / "logs" / "dialer.log").read_text()
    assert logs.count("dialer: connecting") == 1 and "dialer: pending" in logs
    d.approve()
    d.wait_machine(lambda m: m["state"] == "up", what="up at once")


def test_a_second_connection_with_the_same_key_replaces_the_first(d: DialIn) -> None:
    d.linked()
    copy = make_pi_home(satellite=False)
    try:
        shutil.copy2(d.machine / "satellite.toml", copy / "satellite.toml")
        (copy / "link").mkdir(mode=0o700)
        shutil.copy2(d.machine / "link" / linkkey.MACHINE_KEY, copy / "link" / linkkey.MACHINE_KEY)
        d.b.on_loop(lambda: d.b.state.service.post_notice(d.b.state.store.get_room("#build"), "marker"))
        first = d.dialers[0]
        d.start_dialer(copy)
        wait_for(lambda: (read_state(d.machine_paths) or {}).get("reason") == "replaced", timeout=20,
                 what="the first dialer replaced")
        d.wait_machine(lambda m: m["state"] == "up", what="up on the copy")
        assert first.poll() is None  # it redials later (a minute), it doesn't stop
        assert (read_state(d.machine_paths) or {}).get("retry_in_s") == 60.0
        link = d.b.state.remotes.machines["work-laptop"]
        assert link.attempt is not None
    finally:
        for p in d.dialers[1:]:
            p.send_signal(signal.SIGTERM)
            p.wait(10)
        shutil.rmtree(copy, ignore_errors=True)


def test_a_changed_broker_key_stops_the_dialer(d: DialIn) -> None:
    d.paired()
    conf = d.machine / "satellite.toml"
    other = linkkey.b64u(linkkey.pub_raw(linkkey.make_key(d.machine / "link" / "other")))
    text = conf.read_text()
    conf.write_text("\n".join(f'broker_key = "{other}"' if ln.startswith("broker_key") else ln
                              for ln in text.splitlines()) + "\n")
    p = d.start_dialer()
    assert p.wait(20) == EXIT_FINAL
    assert "the broker's key is not the one pinned" in d.dialer_output()
    st = read_state(d.machine_paths) or {}
    assert st.get("state") == "stopped" and st.get("reason") == "broker_key"
    assert d.machine_info()["dialed_in"] is False


def test_an_unknown_machine_stops_for_good(d: DialIn) -> None:
    """A machine the broker holds no key for (removed while its dialer was off)."""
    d.paired()
    d.remove()
    p = d.start_dialer()
    assert p.wait(20) == EXIT_FINAL
    assert (read_state(d.machine_paths) or {}).get("reason") == "unknown"
    assert events(d, "unknown_key")


# ------------------------------------------------------------- /link itself
def handshake_until_challenge(d: DialIn, ws: Any, name: str, key: Any, nm: bytes) -> dict[str, Any]:
    ws.send(json.dumps({"t": "auth", "v": 1, "name": name, "key": linkkey.b64u(linkkey.pub_raw(key)),
                        "nm": linkkey.b64u(nm)}))
    return recv(ws)


def test_link_refuses_browsers_unknown_keys_and_bad_proofs(d: DialIn) -> None:
    d.paired()
    key = linkkey.load_key(d.machine / "link" / linkkey.MACHINE_KEY)
    # a browser (anything with an Origin) never gets a WebSocket here
    with pytest.raises(InvalidStatus):
        link_ws(d, origin=d.origin)
    # the wrong Host: refused by the guard
    with pytest.raises(InvalidStatus):
        connect(f"ws://127.0.0.1:{d.b.port}/link", open_timeout=5)
    # an unknown key for the name, and an unknown name
    stranger = linkkey.make_key(d.machine / "link" / "stranger")
    for name, k in (("work-laptop", stranger), ("other-box", key)):
        with link_ws(d) as ws:
            f = handshake_until_challenge(d, ws, name, k, os.urandom(32))
            assert f["t"] == "refuse" and f["why"] == "unknown"
    # the right key, a proof over the wrong host, a replayed proof, the nonces swapped
    for how in ("host", "swapped", "replay"):
        with link_ws(d) as ws:
            nm = os.urandom(32)
            ch = handshake_until_challenge(d, ws, "work-laptop", key, nm)
            assert ch["t"] == "challenge"
            nb = linkkey.unb64u(ch["nb"], 32)
            assert linkkey.verify(linkkey.unb64u(ch["bkey"], 32), ch["sig"], "broker", d.host, nm, nb)
            if how == "host":
                sig = linkkey.sign(key, "machine", "sb.example.com", nb, nm)
            elif how == "swapped":
                sig = linkkey.sign(key, "machine", d.host, nm, nb)
            else:
                sig = linkkey.sign(key, "machine", d.host, os.urandom(32), nm)  # another challenge's
            ws.send(json.dumps({"t": "proof", "sig": sig}))
            f = recv(ws)
            assert f["t"] == "refuse" and f["why"] == "proof", how
    # frames over 4 KiB before the handshake is done, and no auth in time
    with link_ws(d) as ws:
        ws.send("x" * 5000)
        assert recv(ws)["why"] == "protocol"
    with link_ws(d) as ws:
        ws.send("[]")
        assert recv(ws)["why"] == "protocol"
    assert events(d, "unknown_key") and events(d, "bad_proof")
    # a good proof: pending
    with link_ws(d) as ws:
        nm = os.urandom(32)
        ch = handshake_until_challenge(d, ws, "work-laptop", key, nm)
        ws.send(json.dumps({"t": "proof", "sig": linkkey.sign(key, "machine", d.host,
                                                               linkkey.unb64u(ch["nb"], 32), nm)}))
        assert recv(ws) == {"t": "pending"}
        ws.send('{"t":"hello"}')  # nothing is sent while pending
        assert recv(ws)["why"] == "protocol"


def test_link_answers_nothing_to_silence(d: DialIn) -> None:
    with link_ws(d) as ws:
        t0 = time.monotonic()
        f = recv(ws)
        assert f["t"] == "refuse" and f["why"] == "protocol" and 4.0 < time.monotonic() - t0 < 9.0


# ------------------------------------------------------------ /link/pair
def pair(d: DialIn, body: dict[str, Any], **headers: str) -> httpx.Response:
    return httpx.post(f"http://127.0.0.1:{d.b.port}/link/pair", json=body, headers={"Host": d.host, **headers})


def test_the_pairing_route(d: DialIn) -> None:
    key = linkkey.b64u(os.urandom(32))
    # a browser is refused, whatever it sends
    r = pair(d, {"code": "x", "key": key}, Origin=d.origin, **{"X-Switchboard": "1"})
    assert r.status_code == 403
    # a bad code: refused, counted once a minute
    for i in range(5):
        assert pair(d, {"code": f"AAAA-AAAA-AAA{i}", "key": key}).status_code == 403
    assert events(d, "bad_code") == [{"what": "bad_code", "count": 1}]
    code = d.code()
    assert pair(d, {"code": code, "key": "short"}).status_code == 400
    facts = {"hostname": "box‮", "os": 5, "harnesses": ["claude", "evil", "test"]}
    r = pair(d, {"code": code.lower().replace("-", " "), "key": key, "facts": facts})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["name"] == "work-laptop" and got["fingerprint"] == linkkey.fingerprint(linkkey.unb64u(key, 32))
    assert got["link_url"] == f"ws://{d.host}/link" and len(linkkey.unb64u(got["broker_key"], 32)) == 32
    m = d.machine_info()
    assert m is not None and m["facts"] == {"hostname": "box", "harnesses": ["claude"]}
    # the same code again: "already used", loudly, and a warning in the web UI
    r = pair(d, {"code": code, "key": linkkey.b64u(os.urandom(32))})
    assert r.status_code == 409 and r.json()["error"] == "used"
    # a pending or approved name gets no new code; a removed one does, and pairs a new key
    r = d.api("POST", "/api/machines/pair", {"name": "work-laptop"})
    assert r.status_code == 409 and "remove it first" in r.json()["message"]
    d.remove()
    code = d.code()
    key2 = linkkey.b64u(os.urandom(32))
    assert pair(d, {"code": code, "key": key2}).status_code == 200
    assert d.machine_info()["key_fp"] == linkkey.fingerprint(linkkey.unb64u(key2, 32))
    # names that aren't names
    for bad, status in (("Work Laptop", 400), ("", 400), (None, 400)):
        assert d.api("POST", "/api/machines/pair", {"name": bad}).status_code == status


def test_a_code_expires_after_ten_minutes() -> None:
    clock = FakeClock()
    with DialIn(clock=clock).start() as d:
        code = d.code()
        clock.advance(600)
        assert pair(d, {"code": code, "key": linkkey.b64u(os.urandom(32))}).status_code == 403


def test_pairing_and_approving_need_a_fresh_passkey_check() -> None:
    clock = FakeClock()
    with DialIn(clock=clock).start() as d:
        d.paired()
        clock.advance(CLAIM_GRACE_S)
        for path, body in (("/api/machines/pair", {"name": "box-2"}), ("/api/machines/work-laptop/approve", {})):
            r = d.api("POST", path, body)
            assert r.status_code == 403 and r.json()["error"] == "reauth", path
        # removing only takes access away: no check
        from fakes.fake_owner import sign_in

        assert d.owner is not None and d.auth is not None
        assert sign_in(d.owner, d.auth, counter=1).status_code == 200  # the check, from this browser
        assert d.api("POST", "/api/machines/work-laptop/approve").status_code == 200
        # no session at all
        anon = httpx.Client(base_url=f"http://127.0.0.1:{d.b.port}", headers={"Host": d.host})
        assert anon.get("/api/machines").status_code == 401
        assert anon.post("/api/machines/pair", json={}, headers={"Origin": d.origin,
                                                                 "X-Switchboard": "1"}).status_code == 401


def test_the_desktop_takes_no_machines(broker: Any, web: httpx.Client) -> None:
    h = broker.write_headers()
    assert broker.state.machines is None
    assert httpx.post(f"{broker.base}/link/pair", json={"code": "x", "key": "y"}).status_code == 404
    assert web.get("/api/machines").json() == {"hosted": False, "machines": []}
    assert web.post("/api/machines/pair", json={"name": "box"}, headers=h).status_code == 404
    with pytest.raises(InvalidStatus):
        connect(f"ws://switchboard.localhost:{broker.port}/link", open_timeout=5)


# ------------------------------------------------------------ remote join
def test_join_refuses_a_home_with_a_broker_and_a_used_code(d: DialIn, tmp_path: Path) -> None:
    code = d.code()
    desk = make_pi_home(satellite=False)
    try:
        (desk / "switchboard.db").write_bytes(b"")
        r = d.cli("remote", "join", d.origin, code, "--test-mode", "--no-start", home=desk)
        assert r.returncode == 1 and "runs a switchboard broker" in r.stderr and "--home" in r.stderr
    finally:
        shutil.rmtree(desk, ignore_errors=True)
    assert d.join(code).returncode == 0
    other = make_pi_home(satellite=False)
    try:
        r = d.cli("remote", "join", d.origin, code, "--test-mode", "--no-start", home=other)
        assert r.returncode == 1
        assert "This code was already used by another machine. Don't approve the pending machine" in " ".join(
            r.stdout.split())
        assert "This machine's key" not in r.stdout  # nothing to compare: it paired nothing
        assert not (other / "satellite.toml").exists() and not (other / "link" / linkkey.MACHINE_KEY).exists()
        r = d.cli("remote", "join", d.origin, "NOT-A-CODE", "--test-mode", "--no-start", home=other)
        assert r.returncode == 1 and "a pairing code looks like" in r.stderr
        r = d.cli("remote", "join", "http://evil.example.com", code, "--test-mode", "--no-start", home=other)
        assert r.returncode == 1 and "plain http://" in r.stderr
    finally:
        shutil.rmtree(other, ignore_errors=True)


def test_leave_stops_the_dialer_and_forgets_the_pairing(d: DialIn) -> None:
    d.linked()
    r = d.cli("remote", "remove", "work-laptop", "--yes")
    assert r.returncode == 0, r.stderr
    assert "dialer stopped" in r.stdout
    assert not (d.machine / "satellite.toml").exists() and not (d.machine / "link" / linkkey.MACHINE_KEY).exists()
    assert d.dialers[0].wait(10) == 0
    d.wait_machine(lambda m: m["state"] != "up", what="down")


def test_the_web_ui_lists_live_codes_and_cancels_one(d: DialIn) -> None:
    code = d.code()
    got = d.api("GET", "/api/machines").json()
    assert [c["name"] for c in got["codes"]] == ["work-laptop"] and got["codes"][0]["expires_in_s"] > 590
    # cancelling needs a session and no passkey check (it only takes access away)
    assert httpx.post(f"{d.origin}/api/machines/work-laptop/cancel", json={}, headers={"Origin": d.origin,
                      "X-Switchboard": "1"}).status_code == 401
    assert d.api("POST", "/api/machines/work-laptop/cancel").json() == {"name": "work-laptop", "cancelled": True}
    assert d.api("GET", "/api/machines").json()["codes"] == []
    assert pair(d, {"code": code, "key": linkkey.b64u(os.urandom(32))}).status_code == 403
    assert d.api("POST", "/api/machines/Bad Name/cancel").status_code == 400
    assert events(d, "code_cancelled") == [{"what": "code_cancelled", "name": "work-laptop"}]


def test_a_machine_refused_at_each_dial_says_why(d: DialIn) -> None:
    """A test-mode machine and a broker that no longer is: each dial is refused, and the web UI
    shows why and what to do (not "offline, it redials by itself")."""
    d.linked()
    link = d.b.state.remotes.machines["work-laptop"]

    def off() -> None:
        d.b.state.test_mode = False
        link._abort(link.attempt, __import__("switchboard.broker.remote", fromlist=["x"]).LinkClosed("down", "eof"))

    d.b.on_loop(off)
    m = d.wait_machine(lambda m: m["reason"] == "test_mode", what="refused")
    assert m["state"] == "down" and m["hint"] == "the machine runs in test mode and this broker doesn't"

