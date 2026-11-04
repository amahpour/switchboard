"""The documentation site (#586) is built from docs/ by Zensical (``zensical.toml``), so two rules keep the
Markdown and the site the same thing. A page under docs/ is listed in the site's nav or excluded on purpose,
so a new guide can't be left off the site. A relative link never leaves docs/: GitHub follows one to
``../SECURITY.md``, but the site has no such page, so it would be a dead link there. CI's strict build
catches a link to a missing page; these catch what it can't (an unlisted page, a link out to a file that
does exist in the repository) and run with the unit tests, without the docs dependency group."""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
CONFIG = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))["project"]
LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN = re.compile(r"(`[^`]*`)")


def excluded(rel: str) -> bool:
    patterns = [p.strip() for p in (CONFIG.get("exclude_docs") or "").splitlines() if p.strip()]
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def nav_pages(nav: list[Any]) -> list[str]:
    pages: list[str] = []
    for entry in nav:
        for value in entry.values() if isinstance(entry, dict) else [entry]:
            pages.extend(nav_pages(value) if isinstance(value, list) else [value])
    return pages


def site_pages() -> list[str]:
    pages = (p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*.md"))
    return sorted(rel for rel in pages if not excluded(rel))


def links(path: Path) -> list[tuple[int, str]]:
    """The inline Markdown links of a page, outside code blocks and code spans."""
    found, in_fence = [], False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for part in CODE_SPAN.split(line):
            if not part.startswith("`"):
                found.extend((n, m.group(1)) for m in LINK.finditer(part))
    return found


def test_every_page_under_docs_is_on_the_site_or_excluded() -> None:
    """A guide added under docs/ and not to ``nav`` would build, and be on no menu. Fails on such a page, and
    on a nav entry whose file is gone."""
    listed = nav_pages(CONFIG["nav"])
    assert sorted(set(listed)) == sorted(listed), "a page is listed twice in zensical.toml's nav"
    missing = [p for p in listed if not (DOCS / p).is_file()]
    assert missing == [], f"zensical.toml's nav lists pages that don't exist: {missing}"
    unlisted = [p for p in site_pages() if p not in listed]
    assert unlisted == [], f"add these pages to zensical.toml's nav, or to its exclude_docs: {unlisted}"


def test_no_relative_link_on_the_site_leaves_docs() -> None:
    """``[SECURITY.md](../SECURITY.md)`` works on GitHub and is a dead link on the site, which serves docs/
    only. Fails on such a link: link to the file on GitHub instead
    (``https://github.com/amahpour/switchboard/blob/main/SECURITY.md``)."""
    leaving = []
    for rel in site_pages():
        page = DOCS / rel
        for n, target in links(page):
            path = target.partition("#")[0]
            if not path or re.match(r"^[a-z][a-z0-9+.-]*:", target):
                continue  # an anchor on the page, or a URL
            if not (page.parent / path).resolve().is_relative_to(DOCS):
                leaving.append(f"docs/{rel}:{n}: {target}")
    on_github = "https://github.com/amahpour/switchboard/blob/main/..."
    assert leaving == [], f"link to these on GitHub ({on_github}): {leaving}"


def test_the_check_sees_a_link_that_leaves_docs(tmp_path: Path) -> None:
    """The link scan itself: a link out of docs/ is found, one inside a code block or a code span is not."""
    page = tmp_path / "page.md"
    page.write_text(
        "See [security](../SECURITY.md) and [usage](USAGE.md#x).\n"
        "`[not a link](../a.md)`\n```\n[nor this](../b.md)\n```\n",
        encoding="utf-8",
    )
    assert links(page) == [(1, "../SECURITY.md"), (1, "USAGE.md#x")]
