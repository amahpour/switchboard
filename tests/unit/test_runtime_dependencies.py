"""Every package that switchboard's own code imports is installed by switchboard's runtime dependencies.

The tests run with the dev group installed, so a module the code imports that only the dev group brings
passes every test and still fails where switchboard is installed: the image (`uv sync --no-dev`) and
`uv tool install switchboard-chat`. That is how v1.9.0 shipped a talker that could never start: it imports
`httpx`, which was a dev dependency only, and `/meeting talker on` answered "the talker could not be set
up". This reads the imports from the source and the runtime dependencies from the package's metadata, so
it does not depend on what the dev environment happens to have installed.
"""

from __future__ import annotations

import ast
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from switchboard import DIST_NAME

SRC = Path(__file__).resolve().parents[2] / "src" / "switchboard"


def _runtime_closure() -> set[str]:
    """The distributions that installing switchboard pulls in: its dependencies and theirs, on this
    platform, with no extras."""
    seen: set[str] = set()
    todo = [DIST_NAME]
    while todo:
        name = canonicalize_name(todo.pop())
        if name in seen:
            continue
        seen.add(name)
        for line in metadata.requires(name) or []:
            req = Requirement(line)
            if req.marker is not None and not req.marker.evaluate({"extra": ""}):
                continue
            todo.append(req.name)
    return seen


def _imported_modules() -> dict[str, set[str]]:
    """Each top-level module that isn't the standard library or switchboard, and the files that import
    it. Imports inside a function count: the talker imports httpx there."""
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in sys.stdlib_module_names or top in ("__future__", "switchboard"):
                    continue
                found.setdefault(top, set()).add(str(path.relative_to(SRC.parent)))
    return found


def test_every_imported_package_is_a_runtime_dependency() -> None:
    closure = _runtime_closure()
    providers = metadata.packages_distributions()
    missing = {
        module: sorted(files)
        for module, files in _imported_modules().items()
        if not {canonicalize_name(d) for d in providers.get(module, [])} & closure
    }
    assert not missing, (
        f"imported by switchboard but not installed with it (add to [project] dependencies): {missing}"
    )


def test_the_check_sees_an_import_inside_a_function() -> None:
    """The talker's import is inside a method; a scan of module-level imports only would miss it."""
    assert "switchboard/meeting/talker.py" in _imported_modules().get("httpx", set())
