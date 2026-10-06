"""Check the sdist and the wheel before PyPI gets them (issue #233; CONTRIBUTING.md, "Releases").

    uv build --out-dir dist && python3 .github/scripts/check_dist.py dist

- exactly one wheel and one sdist, of `switchboard-chat` at the version in pyproject.toml and
  switchboard/__init__.py, and the wheel's metadata saying the same;
- the wheel carries what a running broker reads from its own package: the web UI's files and
  the hook script, which a harness runs as a copy of that file;
- the sdist carries only what builds the wheel (src/switchboard, pyproject.toml) and the README,
  LICENSE and CHANGELOG: not the screenshots, the docs or the tests;
- the wheel installs into a fresh environment, and its `switchboard --version` says the version.

CI runs it on every pull request (the lint job) and release.yml before an upload. Standard
library only, plus `uv` on PATH for the fresh environment.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WHEEL_NEEDS = (
    "switchboard/__init__.py",
    "switchboard/cli.py",
    "switchboard/hook/switchboard_hook.py",
    "switchboard/web/static/index.html",
    "switchboard/web/static/app.js",
    "switchboard/web/static/md.js",
    "switchboard/web/static/style.css",
)
SDIST_MAY = (
    "src/switchboard/",
    "pyproject.toml",
    "PKG-INFO",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    ".gitignore",
)
SDIST_MAX_BYTES = 4_000_000  # it was 11 MB with docs/media and the tests in it


def fail(msg: str) -> None:
    raise SystemExit(f"check_dist: {msg}")


def expected() -> tuple[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    m = re.search(r'(?m)^__version__ = "([^"]+)"', (ROOT / "src/switchboard/__init__.py").read_text())
    if not m or m[1] != project["version"]:
        fail(f"pyproject.toml says {project['version']}, switchboard/__init__.py {m[1] if m else 'nothing'}")
    return project["name"], project["version"]


def check(dist: Path) -> None:
    name, version = expected()
    stem = f"{name.replace('-', '_')}-{version}"
    wheels, sdists = sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))
    if [w.name for w in wheels] != [f"{stem}-py3-none-any.whl"] or [s.name for s in sdists] != [
        f"{stem}.tar.gz"
    ]:
        fail(
            f"expected {stem}-py3-none-any.whl and {stem}.tar.gz,"
            f" found {[p.name for p in (*wheels, *sdists)]}"
        )

    with zipfile.ZipFile(wheels[0]) as z:
        names = set(z.namelist())
        missing = [n for n in WHEEL_NEEDS if n not in names]
        if missing:
            fail(f"the wheel lacks {missing}")
        meta = z.read(f"{stem}.dist-info/METADATA").decode()
        entry = z.read(f"{stem}.dist-info/entry_points.txt").decode()
    if f"\nName: {name}\n" not in meta or f"\nVersion: {version}\n" not in meta:
        fail(f"the wheel's METADATA isn't {name} {version}")
    if "switchboard = switchboard.cli:main" not in entry:
        fail("the wheel has no `switchboard` command")

    if sdists[0].stat().st_size > SDIST_MAX_BYTES:
        fail(f"the sdist is {sdists[0].stat().st_size} bytes, over {SDIST_MAX_BYTES}")
    with tarfile.open(sdists[0]) as t:
        extra = [
            m.name
            for m in t.getmembers()
            if m.isfile() and not m.name.removeprefix(f"{stem}/").startswith(SDIST_MAY)
        ]
    if extra:
        fail(f"the sdist carries more than it needs: {extra[:10]}")

    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        subprocess.run(["uv", "venv", "-q", "--python", "3.13", str(venv)], check=True)
        subprocess.run(
            ["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), str(wheels[0])],
            check=True,
        )
        out = subprocess.run(
            [str(venv / "bin" / "switchboard"), "--version"], check=True, capture_output=True, text=True
        ).stdout.strip()
    if not out.startswith(f"switchboard {version}"):
        fail(f"the installed wheel's `switchboard --version` says {out!r}")
    print(f"check_dist: {wheels[0].name} and {sdists[0].name} ok ({out})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    check(Path(sys.argv[1]))
