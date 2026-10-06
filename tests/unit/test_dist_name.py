"""The distribution's name, read where switchboard looks itself up (issue #233).

On PyPI switchboard is `switchboard-chat`, while the package and the command stay `switchboard`.
`install.common.editable_install` (DESIGN.md §12) finds this checkout's own distribution by that
name to refuse an editable install. A wrong name raises PackageNotFoundError there, which reads as
"not editable", so the guard would quietly switch itself off. A rename must keep these together.
"""

from __future__ import annotations

import importlib.metadata
import json
import tomllib
from pathlib import Path

from switchboard import DIST_NAME
from switchboard.install import common

ROOT = Path(__file__).resolve().parents[2]


def test_dist_name_is_the_projects_own_name() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == DIST_NAME


def test_the_editable_guard_finds_this_checkouts_distribution() -> None:
    """The dev environment (`uv sync`) installs this checkout editable: the guard must see that,
    which it can only through the distribution's real name."""
    dist = importlib.metadata.distribution(DIST_NAME)  # raises if the name is wrong
    info = json.loads(dist.read_text("direct_url.json") or "{}")
    assert (info.get("dir_info") or {}).get("editable") is True, info
    assert common.editable_install() is True
