"""The joinly voice patch is one pair of files in four places that must not drift (DESIGN.md §52.5, #713).

``deploy/joinly/`` holds them with joinly's licence and a NOTICE. Each plugin ships its own copy (a hosted
relay mounts it from the plugin with no download), and the self-test, DEPLOY.md and the plugins'
``fixes.pins`` name their sha256, because a mount makes them run inside the bot.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILES = ("server.py", "kokoro.py")
FOLDERS = (
    ROOT / "deploy" / "joinly",
    ROOT / "plugins" / "claude" / "meeting" / "joinly",
    ROOT / "plugins" / "codex" / "meeting" / "joinly",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", FILES)
def test_the_plugins_copies_are_the_deploy_files_byte_for_byte(name: str) -> None:
    """A copy that differs is code the bot would run that no one reviewed as the patch. Fails when one of the
    three is edited without the others."""
    assert len({sha(folder / name) for folder in FOLDERS}) == 1


@pytest.mark.parametrize("name", FILES)
def test_each_file_carries_joinlys_licence_and_says_it_was_changed(name: str) -> None:
    """MIT requires the notice with copies. Fails if a file loses its header, or the NOTICE stops naming
    joinly's release and the files."""
    head = (FOLDERS[0] / name).read_text().split("\n\n", 1)[0]
    assert "MIT License" in head and "Dominik Brockmann" in head and "changed by switchboard" in head
    notice = (FOLDERS[0] / "NOTICE").read_text()
    assert "v0.5.4" in notice and name in notice and "MIT" in notice


@pytest.mark.parametrize("name", FILES)
def test_each_file_is_valid_python(name: str) -> None:
    ast.parse((FOLDERS[0] / name).read_text())


def test_the_pins_the_self_test_and_deploy_md_name_are_the_files_hashes() -> None:
    """One hash per file everywhere: ``fixes.pins`` of both plugins (``plugin:joinly/<name>``), the self-test
    that mounts them in the broker's own bot, and DEPLOY.md, where an operator checks them. Fails if the files
    change and one of them is not updated."""
    selftest = (ROOT / "tests" / "live" / "test_meeting_selftest.py").read_text()
    deploy = (ROOT / "docs" / "DEPLOY.md").read_text()
    for name in FILES:
        digest = sha(FOLDERS[0] / name)
        for plugin in ("claude", "codex"):
            pins = (ROOT / "plugins" / plugin / "meeting" / "fixes.pins").read_text().splitlines()
            assert f"{name} {digest} plugin:joinly/{name}" in pins, (plugin, name)
        assert digest in selftest, name
        assert digest in deploy, name


def test_the_downloads_docs_name_are_files_of_this_folder() -> None:
    """DEPLOY.md and the Kubernetes patch download ``deploy/joinly/<name>`` at a release tag. Fails if a file
    is named that this folder does not hold."""
    deploy = (ROOT / "docs" / "DEPLOY.md").read_text()
    for name in FILES:
        assert f"/deploy/joinly/{name}" in deploy
        assert (FOLDERS[0] / name).is_file()
