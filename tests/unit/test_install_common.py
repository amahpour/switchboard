"""What is left of the install machinery's own helpers (DESIGN.md §9.7): masking of secret-looking values,
the editable-install check that ``switchboard update`` uses, and the atomic write that ``uninstall``
makes."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from switchboard.install import common


def test_mask() -> None:
    m = common.mask(
        {"env": {"A": "1"}, "apiKey": "x", "url": "http://x", "nested": [{"token": "t"}], "ok": "v"}
    )
    assert m["apiKey"] == "***" and m["url"] == "***" and m["nested"][0]["token"] == "***" and m["ok"] == "v"
    assert m["env"] == "***"  # a whole object or list under a secret-looking key is masked


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
