"""paths: layout, socket path fallback, private dirs, hook copies (DESIGN.md §2)."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

import pytest

from switchboard import paths as P
from switchboard.hook.switchboard_hook import sock_path


def test_layout(tmp_home: Path) -> None:
    p = P.Paths.from_home(tmp_home)
    assert p.home == Path(os.path.realpath(tmp_home))
    assert p.db == p.home / "switchboard.db"
    assert p.config == p.home / "config.toml"
    assert p.sock == p.home / "run" / "broker.sock"
    assert p.pidfile.parent == p.lockfile.parent == p.run_dir
    assert p.log == p.home / "logs" / "broker.log"
    assert p.hook_copy("abc") == p.home / "hooks" / "switchboard_hook-abc.py"
    assert p.test_marker.exists()


def test_default_home_uses_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SWITCHBOARD_HOME", raising=False)
    assert P.default_home() == os.path.join(os.path.expanduser("~"), ".switchboard")
    monkeypatch.setenv("SWITCHBOARD_HOME", str(tmp_path))
    assert P.Paths.from_home(None).home == Path(os.path.realpath(tmp_path))


def test_sock_path_short_and_long() -> None:
    assert sock_path("/tmp/yk-short") == os.path.realpath("/tmp/yk-short") + "/run/broker.sock"
    long_home = "/tmp/" + "x" * 120
    s = sock_path(long_home)
    digest = hashlib.sha256(os.path.realpath(long_home).encode()).hexdigest()[:12]
    assert s == f"/tmp/switchboard-{os.getuid()}/{digest}.sock"
    assert len(s.encode()) <= 100
    # the same home spelled via a symlinked prefix maps to the same socket
    # (macOS: /tmp -> /private/tmp; elsewhere a symlink of our own)
    if os.path.realpath("/private/tmp") == os.path.realpath("/tmp"):
        assert sock_path("/tmp/" + "x" * 120) == sock_path("/private/tmp/" + "x" * 120)
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        os.symlink("/tmp", os.path.join(d, "t"))
        assert sock_path("/tmp/" + "x" * 120) == sock_path(os.path.join(d, "t", "x" * 120))


def test_ensure_private_dir(tmp_path: Path) -> None:
    d = tmp_path / "priv"
    P.ensure_private_dir(d)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    P.ensure_private_dir(d)  # idempotent


def test_ensure_private_dir_rejects_open_mode(tmp_path: Path) -> None:
    d = tmp_path / "open"
    d.mkdir()
    d.chmod(0o755)
    with pytest.raises(P.UnsafePathError, match="chmod 700"):
        P.ensure_private_dir(d)


def test_ensure_private_dir_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(P.UnsafePathError, match="symlink"):
        P.ensure_private_dir(link)


def test_ensure_private_dir_rejects_foreign_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "theirs"
    d.mkdir(mode=0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(P.UnsafePathError, match="owned by"):
        P.ensure_private_dir(d)


def test_paths_ensure_creates_socket_dir_for_long_homes(tmp_path: Path) -> None:
    home = tmp_path / ("h" * 90)
    p = P.Paths.from_home(home)
    assert p.sock.parent != p.run_dir
    p.ensure()
    for d in (p.home, p.run_dir, p.logs_dir, p.hooks_dir, p.sock.parent):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_hook_copy_is_content_addressed_and_read_only(tmp_home: Path) -> None:
    p = P.Paths.from_home(tmp_home)
    p.ensure()
    target = P.write_hook_copy(p)
    data = P.hook_source().read_bytes()
    assert target.name == f"switchboard_hook-{hashlib.sha256(data).hexdigest()[:12]}.py"
    assert target.read_bytes() == data
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o444
    assert P.check_hook_copies(p) == []
    assert P.write_hook_copy(p) == target  # idempotent
    # tampering is detected, and the next start replaces the copy
    target.chmod(0o644)
    target.write_bytes(data + b"\n# evil\n")
    assert P.check_hook_copies(p) == [target.name]
    P.write_hook_copy(p)
    assert target.read_bytes() == data and P.check_hook_copies(p) == []


def test_is_under_system_tmp(tmp_path: Path) -> None:
    assert P.is_under_system_tmp("/tmp/yk-abc")
    assert P.is_under_system_tmp("/private/tmp/yk-abc")
    assert not P.is_under_system_tmp("/tmp")
    assert not P.is_under_system_tmp("/usr/local")
