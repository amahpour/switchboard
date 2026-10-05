"""The satellite in two parts (DESIGN.md §31.7): a session runs one link over the reader and
writer it is given and touches no fds, locks or signal handlers; two sessions in a row on one
satellite (a dialer's reconnect) don't clash; the socket exists only between a welcome and the
end of its session; and the start checks of the ssh path and of dialer mode."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
from pathlib import Path

import pytest
from fakes.fake_link import make_pi_home

from switchboard.paths import Paths
from switchboard.remote import proto
from switchboard.remote.config import write_satellite_conf
from switchboard.remote.satellite import Satellite, start_refusal

KEY = "A" * 43


def welcome() -> dict:
    return proto.welcome(
        version="0.0.0",
        link="0123456789abcdef",
        rooms=["#build"],
        harnesses=["claude"],
        limits={"max_conns": 64, "max_members": 8, "frame_rate": 300, "queue_lines": 5000},
    )


def fds() -> set[int]:
    return {int(n) for n in os.listdir("/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd")}


async def one_session(s: Satellite, *, refuse: str | None = None) -> tuple[str, list[dict], bool]:
    """A session over a socketpair, this side playing the broker: hello, welcome, a ping, then
    either a refuse or end of file. Returns why it ended, the frames it sent, and whether the
    socket existed while it was up."""
    a, b = socket.socketpair()
    s_reader, s_writer = await asyncio.open_unix_connection(sock=a, limit=proto.MAX_FRAME + 1)
    reader, writer = await asyncio.open_unix_connection(sock=b, limit=proto.MAX_FRAME + 1)
    task = asyncio.ensure_future(s.run_session(s_reader, s_writer))
    got = [proto.loads(await reader.readline())]
    assert got[0]["t"] == "hello" and not s.paths.sock.exists()  # nothing bound before the welcome
    writer.write(proto.encode(welcome()))
    writer.write(proto.encode(proto.ping(1)))
    await writer.drain()
    got.append(proto.loads(await reader.readline()))
    for _ in range(100):  # the bind follows the welcome, beside the frame loop that answered the ping
        if s.paths.sock.exists():
            break
        await asyncio.sleep(0.02)
    bound = s.paths.sock.exists()
    if refuse:
        writer.write(proto.encode(proto.refuse(refuse, "no")))
        await writer.drain()
    else:
        writer.close()
    why = await asyncio.wait_for(task, 10)
    while line := await reader.readline():
        got.append(proto.loads(line))
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return why, got, bound


def test_a_session_touches_no_fds_locks_or_signals_and_a_second_one_follows(tmp_path: Path) -> None:
    home = make_pi_home()
    try:
        s = Satellite(
            Paths.from_home(home),
            "fpga-pi",
            test_mode=True,
            sessions_dir=str(home / "claude-sessions"),
            harden_state="none",
            stdio="wss",
        )
        s.paths.ensure()
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}

        async def go() -> None:
            before = fds()
            why, got, bound = await one_session(s)
            assert why == "eof" and bound and got[1] == {"t": "pong", "n": 1}
            assert not s.paths.sock.exists()  # unlinked with its session
            why, got, bound = await one_session(s, refuse="removed")  # the dialer's next connection
            assert why == "removed" and bound and got[-1] == {"t": "bye", "why": "shutdown"}
            assert not s.paths.sock.exists()
            assert fds() == before
            loop = asyncio.get_running_loop()
            assert not loop.remove_signal_handler(signal.SIGTERM)  # none was added

        asyncio.run(go())
        assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
        assert not (s.paths.run_dir / "satellite.lock").exists() and not s.paths.lockfile.exists()
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)


def test_the_start_checks_of_both_paths(tmp_path: Path) -> None:
    home = make_pi_home()
    try:
        paths = Paths.from_home(home)
        ssh = {"SSH_CONNECTION": "192.0.2.10 5000 192.0.2.20 22"}
        r, w = os.pipe()
        try:
            # an ssh satellite home: the ssh path starts; dialer mode refuses it
            assert start_refusal(paths, "fpga-pi", test_mode=False, environ=ssh, fds=(r, w)) is None
            why = start_refusal(paths, "fpga-pi", test_mode=False, environ={}, dialer=True)
            assert "dialed over ssh" in (why or "")
            # a home that dials its broker: dialer mode starts without SSH_CONNECTION; the ssh path refuses it
            write_satellite_conf(
                paths,
                "fpga-pi",
                desktop="sb.example.com",
                broker_url="https://sb.example.com",
                broker_key=KEY,
            )
            assert start_refusal(paths, "fpga-pi", test_mode=False, environ={}, dialer=True) is None
            why = start_refusal(paths, "fpga-pi", test_mode=False, environ=ssh, fds=(r, w))
            assert why and "dials its broker" in why
            assert "names fpga-pi" in (
                start_refusal(paths, "other-pi", test_mode=False, environ={}, dialer=True) or ""
            )
            (home / ".switchboard-test").unlink()
            assert "marker" in (
                start_refusal(paths, "fpga-pi", test_mode=True, environ={}, dialer=True) or ""
            )
        finally:
            os.close(r)
            os.close(w)
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)


@pytest.mark.parametrize(
    "text,why",
    [
        ('name = "x-pi"\ntransport = "carrier-pigeon"\n', 'transport must be "ssh" or "wss"'),
        ('name = "x-pi"\ntransport = "wss"\n', "broker_url must be"),
        (
            'name = "x-pi"\ntransport = "wss"\nbroker_url = "https://sb.example.com/path"\n',
            "broker_url must be",
        ),
        (
            f'name = "x-pi"\ntransport = "wss"\nbroker_url = "https://sb.example.com"\nbroker_key = "{KEY[:-1]}"\n',
            "broker_key must be",
        ),
        (
            f'name = "x-pi"\nbroker_url = "https://sb.example.com"\nbroker_key = "{KEY}"\n',
            'only for transport = "wss"',
        ),
    ],
)
def test_a_bad_dialing_satellite_toml_is_refused(tmp_path: Path, text: str, why: str) -> None:
    from switchboard.remote.config import RemoteConfigError, read_satellite_conf

    (tmp_path / "satellite.toml").write_text(text)
    with pytest.raises(RemoteConfigError, match=why):
        read_satellite_conf(Paths.from_home(tmp_path))
