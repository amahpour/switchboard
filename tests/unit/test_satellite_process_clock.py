"""The satellite's view of its own machine, M8c review fixes (DESIGN.md §27.4.6, §27.4.8, §27.16):
start times that survive a clock step between two satellites, no ``ps`` on Linux, the replace
marker naming a real newer satellite, an absolute home, and requests no larger than local ones."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from switchboard import cli
from switchboard.broker import proc
from switchboard.broker.peer import Peer
from switchboard.broker.proc import ProcInfo
from switchboard.paths import Paths
from switchboard.remote import proto, satellite
from switchboard.remote.satellite import EXIT_REFUSED, LocalConn, Satellite, boot_time, replaced_by_other

LINUX = sys.platform.startswith("linux")


def run_dir(tmp_path: Path) -> Paths:
    paths = Paths.from_home(tmp_path)
    paths.run_dir.mkdir(parents=True, mode=0o700)
    return paths


def test_start_times_survive_a_clock_step_between_satellites(tmp_path: Path) -> None:
    """Every reconnect starts a new satellite. /proc/stat's btime moves when the clock is stepped
    (a Pi without an RTC syncing NTP, a WSL2 resume), so each satellite of one boot computes start
    times from the first one's btime: a watched process keeps its start time and stays alive, and
    a reconnecting MCP server stays the same (host, mcp_pid, mcp_start)."""
    paths = run_dir(tmp_path)
    assert boot_time(paths, "0b6f4a52-5e1d-4c7e-9d39-3f0c2d1e9a77", 1000.0) == 1000.0
    # an hour later the clock is stepped and the link reconnects: the new satellite reads 4600
    assert boot_time(paths, "0b6f4a52-5e1d-4c7e-9d39-3f0c2d1e9a77", 4600.0) == 1000.0
    assert (paths.run_dir / "boot_time").stat().st_mode & 0o777 == 0o600
    # a reboot: a new boot id, a new boot time
    assert boot_time(paths, "7d1c9e33-0a4b-4f2e-8c61-5b9d0e2f4a18", 4600.0) == 4600.0
    assert boot_time(paths, "7d1c9e33-0a4b-4f2e-8c61-5b9d0e2f4a18", 9999.0) == 4600.0
    # a file that doesn't parse is replaced, never trusted
    for junk in ("junk", "id nan", "7d1c9e33-0a4b-4f2e-8c61-5b9d0e2f4a18 -5", ""):
        (paths.run_dir / "boot_time").write_text(junk)
        assert boot_time(paths, "7d1c9e33-0a4b-4f2e-8c61-5b9d0e2f4a18", 5000.0) == 5000.0


@pytest.mark.skipif(not LINUX, reason="/proc start times: Linux")
def test_pinned_btime_is_what_start_times_come_from(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "_BTIME_PIN", None)
    base = proc.info(os.getpid())
    assert base is not None
    proc.pin_btime(proc._linux_btime() + 3600.0)  # restored by monkeypatch
    moved = proc.info(os.getpid())
    assert moved is not None and abs(moved.start - (base.start + 3600.0)) < 0.02


@pytest.mark.skipif(not LINUX, reason="the satellite pins its clock and drops ps on Linux")
def test_pin_linux_clock_pins_and_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "_BTIME_PIN", None)
    monkeypatch.setattr(proc, "_NO_SPAWN", False)
    paths = run_dir(tmp_path)
    assert satellite.pin_linux_clock(paths) == "pinned"
    assert proc._NO_SPAWN is True and proc._BTIME_PIN is not None
    bid, bt = (paths.run_dir / "boot_time").read_text().split()
    assert float(bt) == proc._BTIME_PIN
    assert bid == open("/proc/sys/kernel/random/boot_id").read().strip()


def test_no_spawn_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The satellite on Linux runs nothing (§27.4.8): what /proc can't tell reads as unknown
    ('' argv: verdict '?', which fails closed), never through ps."""
    monkeypatch.setattr(proc, "_NO_SPAWN", False)
    proc.set_no_spawn()

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("spawned a process")

    monkeypatch.setattr(proc.subprocess, "run", boom)
    assert proc._run_ps(["-o", "pid=", "-p", "1"]) == ""
    ghost = 2**22 + 4321  # above any real pid_max default: /proc has nothing
    assert proc.argv_many([ProcInfo(pid=ghost, ppid=0, start=1.0, uid=-1)]) == {ghost: ""}


def test_replace_marker_must_name_a_newer_satellite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``bye replaced`` blocks the link until the owner re-enables it, so the marker counts only
    when it names a live process with a satellite's argv that started after this one."""
    paths = run_dir(tmp_path)
    marker = paths.run_dir / "satellite.replace"
    me = (100, 200.0)
    is_sat = {"v": True}
    monkeypatch.setattr(satellite, "_is_satellite", lambda pid, start: is_sat["v"])
    assert replaced_by_other(paths, me) is False  # no marker
    satellite._write_pair(marker, 101, 300.0)
    assert replaced_by_other(paths, me) is True
    satellite._write_pair(marker, 101, 150.0)  # older than this satellite: not its replacement
    assert replaced_by_other(paths, me) is False
    satellite._write_pair(marker, 100, 200.0)  # itself
    assert replaced_by_other(paths, me) is False
    satellite._write_pair(marker, 101, 300.0)
    is_sat["v"] = False  # a live process, but not a satellite (any same-user process can write the file)
    assert replaced_by_other(paths, me) is False


def test_real_process_in_marker_is_not_a_satellite(tmp_path: Path) -> None:
    paths = run_dir(tmp_path)
    info = proc.info(os.getpid())
    assert info is not None
    satellite._write_pair(paths.run_dir / "satellite.replace", os.getpid(), info.start)
    assert replaced_by_other(paths, (1, info.start - 10.0)) is False  # pytest's argv isn't a satellite's


@pytest.mark.parametrize("home", ["", "relative/home", "."])
def test_satellite_needs_an_absolute_home(home: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert satellite.main(["--home", home, "--name", "fpga-pi"]) == EXIT_REFUSED
    assert "--home must be an absolute path" in capsys.readouterr().err


def test_satellite_cli_defaults_to_the_home_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    got: list[list[str]] = []
    monkeypatch.setattr(satellite, "main", lambda argv: got.append(argv) or 0)
    monkeypatch.setenv("SWITCHBOARD_HOME", str(tmp_path / "sbhome"))
    monkeypatch.chdir(tmp_path)
    args = cli.build_parser().parse_args(["satellite", "--name", "fpga-pi"])
    assert args.func(args) == 0
    assert got == [["--home", str(Paths.from_home(None).home), "--name", "fpga-pi"]]
    assert os.path.isabs(got[0][1]) and got[0][1] != str(tmp_path)
    args = cli.build_parser().parse_args(["satellite", "--name", "fpga-pi", "--test-mode"])
    assert args.func(args) == 2 and len(got) == 1
    assert "--test-mode needs an explicit --home" in capsys.readouterr().err


def test_request_larger_than_a_local_one_is_refused_on_the_pi(tmp_path: Path) -> None:
    """A 1 MiB line of short numbers grows when re-encoded (1e1 -> 10.0): the satellite refuses it
    rather than send the broker a request frame it would close the link over."""
    sat = Satellite(
        Paths.from_home(tmp_path), "fpga-pi", test_mode=True, sessions_dir=str(tmp_path), harden_state="none"
    )
    lc = LocalConn(1, Peer(pid=os.getpid(), uid=os.getuid(), start=0.0), writer=None)  # type: ignore[arg-type]
    nums = ",".join(["1e1"] * 250_000)
    line = f'{{"id":5,"method":"agent.say","params":{{"cred":"c","room":"#fpga","n":[{nums}]}}}}'.encode()
    assert len(line) < proto.MAX_LINE
    sat.on_local_line(lc, line)
    reply = json.loads(lc.q.get_nowait())
    assert reply == {"id": 5, "error": {"code": "bad_request", "message": "request too large"}}
    # an ordinary request goes out (no link here, so nothing is queued back to the client)
    sat.on_local_line(lc, b'{"id":6,"method":"agent.say","params":{"cred":"c","room":"#fpga","text":"hi"}}')
    assert lc.q.empty()
