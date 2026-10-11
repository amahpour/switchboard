"""``delivery/engine.py`` names no harness (#169; CLAUDE.md, "The shape to keep": each
harness's quirks in its own adapter). The engine asks a session's adapter (DESIGN.md §9.1),
so a harness is added or changed in its adapter alone. A new guard: it only tightens.

It fails on a harness name in a string outside a docstring, a comparison with a ``harness``
attribute, or an import from one harness's adapter module.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import switchboard.delivery.engine as engine_mod

HARNESSES = ("claude", "codex", "cursor", "devin", "test")
PREFIXES = tuple(f"{h}{sep}" for h in HARNESSES for sep in ":@")
NAMED = re.compile(r"\b(claude|codex|cursor|devin)\b", re.IGNORECASE)
HARNESS_MODULES = {"claude", "codex", "codex_rpc", "cursor", "devin", "remote_codex", "testagent"}


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def _harness_attr(n: ast.AST) -> bool:
    return isinstance(n, ast.Attribute) and n.attr == "harness"


def findings(src: str) -> list[str]:
    tree = ast.parse(src)
    docs = _docstrings(tree)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            v = n.value
            if v.lower() in HARNESSES or v.lower().startswith(PREFIXES) or NAMED.search(v):
                out.append(f"line {n.lineno}: the string {v!r}")
        elif isinstance(n, ast.Compare) and any(_harness_attr(x) for x in [n.left, *n.comparators]):
            out.append(f"line {n.lineno}: a comparison with .harness")
        elif isinstance(n, ast.ImportFrom) and (n.module or "").startswith("switchboard.adapters."):
            if (n.module or "").rsplit(".", 1)[1] in HARNESS_MODULES:
                out.append(f"line {n.lineno}: an import from {n.module}")
    return out


def test_the_engine_names_no_harness() -> None:
    src = Path(engine_mod.__file__).read_text(encoding="utf-8")
    assert findings(src) == [], "move the harness's rule into its adapter (adapters/base.py, §9.1)"


def test_the_scan_sees_each_kind_of_naming() -> None:
    """The scan itself: each way the engine used to name a harness is caught, a docstring
    is not, and asking the adapter is not."""
    src = '''
"""The engine (Cursor's parks, Devin's re-arm): docstrings may say so."""
from switchboard.adapters.cursor import FOLLOWUP_RACE_S
from switchboard.adapters.base import Adapter

def f(p, ev):
    if p.harness in ("claude", "devin"):
        pass
    if p.harness == x:
        pass
    note = "no more follow-ups in that Cursor session"
    ok = f"{p.harness}:immediate"
    return adapter_for(p.harness, "")
'''
    got = findings(src)
    assert len(got) == 6  # two comparisons
    kinds = {g.split(":", 1)[1].strip() for g in got}
    assert kinds == {
        "an import from switchboard.adapters.cursor",
        "a comparison with .harness",
        "the string 'claude'",
        "the string 'devin'",
        "the string 'no more follow-ups in that Cursor session'",
    }
