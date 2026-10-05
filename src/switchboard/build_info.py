"""The running broker's build revision (DESIGN.md §1.1, issue #94)."""

from __future__ import annotations

import os
import re
import subprocess
from functools import cache
from pathlib import Path

REVISION = re.compile(r"[0-9a-f]{40}")


def read_commit(package_dir: Path) -> str | None:
    """Use the image's declared build commit, or Git only for an editable source tree."""
    built = os.environ.get("SWITCHBOARD_BUILD_COMMIT", "")
    if built:
        return built if REVISION.fullmatch(built) else None
    package_dir = package_dir.resolve()
    if package_dir.parent.name != "src":
        return None
    root = package_dir.parent.parent
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and REVISION.fullmatch(revision) else None


@cache
def commit() -> str | None:
    """The revision is constant for one running process; avoid Git on every status request."""
    return read_commit(Path(__file__).resolve().parent)


def version_text(version: str, revision: str | None) -> str:
    return f"switchboard {version}" + (f" ({revision[:7]})" if revision else "")
