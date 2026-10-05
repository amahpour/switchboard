"""The wheel ships the package, its static files and the hook, and nothing from tests/."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_wheel_contents(tmp_path: Path) -> None:
    pytest.importorskip("hatchling")
    # PEP 517 build_wheel straight from the dev venv: offline, no build isolation.
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, hatchling.build as b; print(b.build_wheel(sys.argv[1]))",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    [whl] = list(tmp_path.glob("switchboard-*.whl"))
    names = zipfile.ZipFile(whl).namelist()
    assert "switchboard/__init__.py" in names
    assert "switchboard/hook/switchboard_hook.py" in names
    for f in (
        "index.html",
        "login.html",
        "app.js",
        "md.js",
        "style.css",
        "favicon.svg",
        "favicon-32.png",
        "apple-touch-icon.png",
    ):
        assert f"switchboard/web/static/{f}" in names
    assert not [n for n in names if n.startswith("tests/") or "/tests/" in n or "conftest" in n]
    assert not [n for n in names if "codex_trust" in n or n.endswith(".db")]
    entry = next(n for n in names if n.endswith("entry_points.txt"))
    assert "switchboard = switchboard.cli:main" in zipfile.ZipFile(whl).read(entry).decode()
