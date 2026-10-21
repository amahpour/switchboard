"""What is left of the install machinery's own helpers (DESIGN.md §9.7): masking of secret-looking values,
the hook command's shape (old installs wrote it, and ``uninstall`` recognises it), the editable-install
check that ``switchboard update`` uses, and the atomic write that ``uninstall`` makes."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from switchboard.install import common
from switchboard.install.common import InstallError, hook_command

PY = "/opt/yk/venv/bin/python"
HOME = "/opt/yk/home"


def test_mask() -> None:
    m = common.mask(
        {"env": {"A": "1"}, "apiKey": "x", "url": "http://x", "nested": [{"token": "t"}], "ok": "v"}
    )
    assert m["apiKey"] == "***" and m["url"] == "***" and m["nested"][0]["token"] == "***" and m["ok"] == "v"
    assert m["env"] == "***"  # a whole object or list under a secret-looking key is masked


def test_hook_command_shape_and_refusals() -> None:
    c = hook_command(PY, HOME, "abcdef012345", "claude", "Stop")
    assert c.startswith("/bin/sh -c '") and c.endswith("; exit 0'")
    assert 'exec "$P" -I -S "$H" --home "/opt/yk/home" --harness claude --event Stop' in c
    assert hook_command(PY, HOME, "abcdef012345", "claude", "Stop") == c  # byte-stable
    for bad in ("/opt/it's/home", '/opt/"q"/home', "/opt/$HOME", "relative/home", "/opt/a`b`"):
        with pytest.raises(InstallError):
            hook_command(PY, bad, "abcdef012345", "claude", "Stop")


def test_this_dev_venv_is_detected_as_editable() -> None:
    assert common.editable_install() is True  # uv sync installs the project in editable mode


def test_atomic_write_does_not_follow_a_planted_temp_symlink(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{}")
    os.chmod(target, 0o640)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    for name in (f".settings.json.switchboard-{os.getpid()}.tmp", ".settings.json.switchboard-.tmp"):
        (tmp_path / name).symlink_to(victim)
    common.atomic_write(target, '{"a": 1}\n')
    assert victim.read_text() == "keep me" and target.read_text() == '{"a": 1}\n'
    assert not target.is_symlink() and stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not [
        p
        for p in tmp_path.iterdir()
        if p.name.startswith(".settings.json.switchboard-") and not p.is_symlink()
    ]
