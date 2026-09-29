"""The web UI renders with textContent only, and has no inline JS or CSS (DESIGN.md §5.5, §12.1)."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "switchboard" / "web" / "static"

BANNED_JS = [
    r"\.innerHTML\b",
    r"\.outerHTML\b",
    r"insertAdjacentHTML",
    r"document\.write",
    r"\beval\s*\(",
    r"new\s+Function\b",
    r"setTimeout\s*\(\s*['\"]",
    r"setInterval\s*\(\s*['\"]",
    r"\.style\b",
    r"setAttribute\s*\(\s*['\"](style|on\w+|href|src)['\"]",
    r"javascript:",
    r"localStorage|sessionStorage",
]
BANNED_HTML = [
    r"<script(?![^>]*\bsrc=)[^>]*>",  # inline scripts
    r"\sstyle\s*=",
    r"<style\b",
    r"\son\w+\s*=",  # inline event handlers
    r"javascript:",
    r"switchboard_session|login\?t=",  # never a token in a page
]


def files(ext: str) -> list[Path]:
    return sorted(STATIC.glob(f"*.{ext}"))


def test_static_files_exist() -> None:
    names = {p.name for p in STATIC.iterdir()}
    assert {"index.html", "login.html", "app.js", "md.js", "style.css"} <= names


def test_js_has_no_html_injection_sinks() -> None:
    for p in files("js"):
        text = p.read_text()
        for pat in BANNED_JS:
            assert not re.search(pat, text), f"{p.name}: {pat}"


def test_html_has_no_inline_code_or_style() -> None:
    for p in files("html"):
        text = p.read_text()
        for pat in BANNED_HTML:
            assert not re.search(pat, text, re.I), f"{p.name}: {pat}"
        for src in re.findall(r"""(?:src|href)=["']([^"']+)["']""", text):
            assert src.startswith("/") and not src.startswith("//"), f"{p.name}: external resource {src}"


def test_css_loads_nothing_external() -> None:
    for p in files("css"):
        text = p.read_text()
        assert "@import" not in text and "url(" not in text


def test_writes_carry_the_csrf_header() -> None:
    js = (STATIC / "app.js").read_text()
    assert "'X-Switchboard'" in js and "credentials: 'same-origin'" in js


def test_catchup_hint_for_commands_written_mid_message() -> None:
    js = (STATIC / "app.js").read_text()
    assert "CATCHUP_HINT" in js and "/catchup <agent> on <member>" in js
    assert r"(^|\s)\/(catchup|review)\b" in js  # only a mention inside plain text triggers it


def test_closed_rooms_ui() -> None:
    # §28: tabs are pruned (by name and by id), the Closed panel lists and reopens closed rooms,
    # and /close asks before it runs
    js = (STATIC / "app.js").read_text()
    assert "state.rooms.delete(" in js
    assert "/api/closed-rooms" in js
    guard = re.search(r"=== '/close' &&\s*!window\.confirm\('Close '", js)
    assert guard, "a window.confirm must guard /close"
    assert "return false;" in js[guard.end() : guard.end() + 400]  # declining gives the text back
    html = (STATIC / "index.html").read_text()
    assert 'id="closed-panel"' in html and 'id="closed-rooms"' in html
    assert 'id="closed-body"' in html and 'id="empty-title"' in html


# Markdown (issue #19): message text is untrusted, so there is exactly one place in all static JS
# that sets a link target, md.js's mdLink(), and it only runs after safeUrl() accepted an absolute
# http(s) URL. No script may create an element that fetches or runs something, or reach for
# another HTML/URL sink.
def test_one_vetted_link_path() -> None:
    js = {p.name: p.read_text() for p in files("js")}
    sets = [(n, m.start()) for n, t in js.items() for m in re.finditer(r"\.href\s*=(?!=)", t)]
    assert [n for n, _ in sets] == ["md.js"], sets  # exactly one assignment, in md.js
    md = js["md.js"]
    fn = md[md.index("function mdLink("):]
    fn = fn[: fn.index("\n  }\n") if "\n  }\n" in fn else 1200]
    assert "a.href = v.href;" in fn and "a.rel = 'noopener noreferrer nofollow';" in fn
    assert "u.protocol === 'http:' || u.protocol === 'https:'" in md
    assert "new URL(" in md
    for n, t in js.items():
        assert not re.search(r"createElement\(\s*['\"](img|iframe|script|object|embed|link|style|base|form|meta|svg)['\"]", t, re.I), n
        assert not re.search(r"\.(src|srcdoc|srcset|action|formAction|innerText)\s*=(?!=)", t), n
        assert "setAttributeNS" not in t and "DOMParser" not in t and "createContextualFragment" not in t, n
        for ns in re.findall(r"createElementNS\(\s*['\"]([^'\"]+)", t):
            assert ns == "http://www.w3.org/2000/svg", n


def test_markdown_loads_before_the_app() -> None:
    html = (STATIC / "index.html").read_text()
    assert html.index("/static/md.js") < html.index("/static/app.js")
    assert "/static/md.js" not in (STATIC / "login.html").read_text()
