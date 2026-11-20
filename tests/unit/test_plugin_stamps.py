"""Every plugin under ``plugins/`` carries the package's version, and the release stamps it (#887, §49.13).

The server compares a session's plugin with its own release (``plugin_versions.PINNED``), so a plugin whose
version no release moves reports a number that is wrong the day after the next release. A release PR skips
the suite, so what keeps this true is that ``release.py`` stamps each plugin's manifest and this test finds
a plugin folder it doesn't know.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from switchboard import __version__

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
import release as rel  # noqa: E402

PLUGIN_DIRS = sorted(p.name for p in (ROOT / "plugins").iterdir() if p.is_dir())


def test_every_plugin_folder_is_one_the_release_stamps() -> None:
    """A new folder under ``plugins/`` has to be added to ``PLUGIN_MANIFESTS`` or the release leaves its
    version behind. Fails if a plugin folder is not stamped, or a stamped one is gone."""
    assert PLUGIN_DIRS, "no plugins found"
    assert sorted(Path(k).name for k in rel.PLUGIN_MANIFESTS) == PLUGIN_DIRS


@pytest.mark.parametrize("folder", PLUGIN_DIRS)
def test_each_plugins_stamped_version_is_the_packages(folder: str) -> None:
    """The manifest the release stamps says the package's own version (a release PR moves them together), and
    the plugin's code reads that file. Fails if a plugin's version is left at an old number."""
    manifest = ROOT / rel.PLUGIN_MANIFESTS[f"plugins/{folder}"]
    assert manifest.is_file(), manifest
    assert json.loads(manifest.read_text())["version"] == __version__, folder
    assert rel.PLUGIN_VERSION_RE.search(manifest.read_text()), folder  # a form the stamp can move


READS = {
    "cursor": ("lib/hook.py", ".cursor-plugin"),
    "devin": ("lib/hook.py", "plugin.json"),
    "opencode": ("listener.mjs", "package.json"),
}


@pytest.mark.parametrize("folder", sorted(READS))
def test_the_plugins_that_send_the_header_read_the_stamped_file(folder: str) -> None:
    """Cursor's and Devin's hooks and OpenCode's listener send the header from the manifest the release
    stamps, not from a constant a release would not move. Fails if the code names another source or has no
    header."""
    code, source = READS[folder]
    text = (ROOT / "plugins" / folder / code).read_text()
    assert re.search(r"X-Switchboard-Plugin-Version|VERSION_HEADER", text)
    assert source in text
