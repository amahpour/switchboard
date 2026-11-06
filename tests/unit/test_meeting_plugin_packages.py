"""The two isolated plugin packages ship the same meeting host (DESIGN.md §54, #623)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHARED = (
    "bin/meeting-host",
    "bin/meeting-host.cmd",
    "bin/windows/meeting-host.ps1",
    "meeting/compose.yaml",
    "meeting/fixes.pins",
)


@pytest.mark.parametrize("path", SHARED)
def test_both_installed_packages_have_the_same_host_and_compose(path: str) -> None:
    """git-subdir installs only one plugin root: cross-plugin symlinks would break. Keep these packaged
    files equal so a fix reaches both harnesses. Fails for a missing or divergent copy.
    """
    claude, codex = (ROOT / "plugins" / name / path for name in ("claude", "codex"))
    assert not codex.is_symlink() and codex.read_bytes() == claude.read_bytes()


def test_codex_registers_its_packaged_host_skill() -> None:
    """The host must be discoverable in an installed Codex package; a loose skill outside it is useless."""
    plugin = ROOT / "plugins" / "codex"
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
    assert manifest["skills"] == "./skills/"
    assert (plugin / "skills/host-meeting-bot/SKILL.md").is_file()
