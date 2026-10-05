"""The web UI renders with textContent only, and has no inline JS or CSS (DESIGN.md §5.5, §12.1)."""

from __future__ import annotations

import hashlib
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
    assert {"index.html", "login.html", "setup.html", "app.js", "md.js", "diagram.js", "webauthn.js", "login.js",
            "setup.js", "style.css", "favicon.svg", "favicon-32.png", "apple-touch-icon.png"} <= names


ICON_LINKS = ('<link rel="icon" href="/static/favicon-32.png" sizes="32x32">',
              '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">',
              '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">')


def test_both_pages_link_the_tab_icons_and_the_icon_is_the_sidebar_mark() -> None:
    svg = (STATIC / "favicon.svg").read_text()
    for page in ("index.html", "login.html", "setup.html"):
        text = (STATIC / page).read_text()
        assert all(link in text for link in ICON_LINKS), page
        # the tab icon is the sidebar's own glyph on its tile: every shape of the brand tile's
        # <svg> appears in favicon.svg as it is (so the two can't drift apart)
        tile = re.search(r'<span class="brand-tile"[^>]*>\s*<svg[^>]*>(.*?)</svg>', text, re.S)
        assert tile, page
        shapes = re.findall(r"<(?:circle|path)[^>]*/>", tile.group(1))
        assert shapes and all(s in svg for s in shapes), (page, shapes)


def test_the_svg_icon_is_inert() -> None:
    """Opened directly, /static/favicon.svg is a document on this origin: no script, no event
    handler, no link or embedded document, no reference outside the file."""
    text = (STATIC / "favicon.svg").read_text()
    for pat in (r"<script", r"\son\w+\s*=", r"href", r"<foreignObject", r"<use\b", r"url\(", r"javascript:",
                r"<style", r"<image"):
        assert not re.search(pat, text, re.IGNORECASE), pat


def test_the_png_icons_have_their_sizes() -> None:
    for name, size, colour_type in (("favicon-32.png", 32, 6), ("apple-touch-icon.png", 180, 2)):
        b = (STATIC / name).read_bytes()
        assert b.startswith(b"\x89PNG\r\n\x1a\n"), name
        assert b[16:24] == size.to_bytes(4, "big") * 2, name
        assert b[25] == colour_type, name  # RGBA (transparent corners) / RGB (iOS shows alpha as black)


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


def test_browser_confirm_alert_and_prompt_are_not_used() -> None:
    for p in files("js"):
        assert not re.search(r"\bwindow\.(?:confirm|alert|prompt)\s*\(", p.read_text()), p.name


def test_closed_rooms_ui() -> None:
    # §28: tabs are pruned (by name and by id), the Closed panel lists and reopens closed rooms,
    # and /close asks before it runs
    js = (STATIC / "app.js").read_text()
    assert "state.rooms.delete(" in js
    assert "/api/closed-rooms" in js
    guard = re.search(r"=== '/close' &&\s*!await confirmDialog\('Close '", js)
    assert guard, "the shared dialog must guard /close"
    assert "return false;" in js[guard.end() : guard.end() + 400]  # declining gives the text back
    html = (STATIC / "index.html").read_text()
    assert 'id="closed-panel"' in html and 'id="closed-rooms"' in html
    assert 'id="closed-body"' in html and 'id="empty-title"' in html


# Markdown (issue #19): message text is untrusted, so there is exactly one place in all static JS
# that sets a link target, md.js's mdLink(), and it only runs after safeUrl() accepted an absolute
# http(s) URL. No script may create an element that fetches or runs something (but the one script
# below that loads Mermaid), or reach for another HTML/URL sink.
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
        assert not re.search(r"createElement\(\s*['\"](img|iframe|object|embed|link|style|base|form|meta|svg)['\"]", t, re.I), n
        assert not re.search(r"\.(srcdoc|srcset|action|formAction|innerText)\s*=(?!=)", t), n
        assert "setAttributeNS" not in t and "DOMParser" not in t and "createContextualFragment" not in t, n
        for ns in re.findall(r"createElementNS\(\s*['\"]([^'\"]+)", t):
            assert ns == "http://www.w3.org/2000/svg", n


# Mermaid diagrams (issue #57, DESIGN.md §33): the one script any static JS loads is the vendored
# Mermaid, from diagram.js, when someone first asks for a diagram. Exactly one script element and
# one src in all static JS, both in diagram.js, for one constant same-origin path. The file there
# is the release that was vetted, unmodified, and nothing else under static/ escapes this lint.
MERMAID = "vendor/mermaid/mermaid.min.js"
MERMAID_SHA256 = "581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8"  # 11.17.2's dist file
MERMAID_LICENSE_SHA256 = "ec9fb67dcb25eccc416ed56e1aab819222c805a2a4bfe4cb19e7556bf2ffde80"


def test_the_one_script_load_is_the_vendored_mermaid() -> None:
    js = {p.name: p.read_text() for p in files("js")}
    made = [n for n, t in js.items() for _ in re.finditer(r"createElement\(\s*['\"]script['\"]", t, re.I)]
    assert made == ["diagram.js"], made
    srcs = [(n, m.group(0)) for n, t in js.items() for m in re.finditer(r"\w*\.src\s*=(?!=)[^;\n]*", t)]
    assert srcs == [("diagram.js", "s.src = SRC")], srcs
    d = js["diagram.js"]
    assert d.count("const SRC = ") == 1 and f"const SRC = '/static/{MERMAID}';" in d
    # strict, no HTML labels, and a diagram can't loosen either (or restyle itself) from its own config
    assert "securityLevel: 'strict'" in d and "htmlLabels: false" in d and "secure: SECURE" in d
    secure = d[d.index("const SECURE = ["):]
    secure = secure[: secure.index("];")]
    for key in ("securityLevel", "htmlLabels", "flowchart", "journey", "theme", "themeVariables", "themeCSS",
                "fontFamily", "dompurifyConfig"):
        assert f"'{key}'" in secure, key


def test_the_vendored_mermaid_is_the_vetted_release() -> None:
    nested = sorted(p.relative_to(STATIC).as_posix() for p in STATIC.rglob("*") if p.is_file() and p.parent != STATIC)
    assert nested == ["vendor/mermaid/LICENSE", MERMAID], nested
    assert hashlib.sha256((STATIC / MERMAID).read_bytes()).hexdigest() == MERMAID_SHA256
    lic = STATIC / "vendor" / "mermaid" / "LICENSE"
    assert hashlib.sha256(lic.read_bytes()).hexdigest() == MERMAID_LICENSE_SHA256


def test_markdown_loads_before_the_app() -> None:
    html = (STATIC / "index.html").read_text()
    assert html.index("/static/md.js") < html.index("/static/app.js")
    assert html.index("/static/diagram.js") < html.index("/static/app.js")
    assert "/static/md.js" not in (STATIC / "login.html").read_text()
    for page in ("login.html", "setup.html"):
        assert "diagram.js" not in (STATIC / page).read_text() and "mermaid" not in (STATIC / page).read_text()


# Passkeys (issue #41, DESIGN.md §31): the sign-in and claim pages load exactly two same-origin
# scripts, the shared WebAuthn helper first; the app loads it too. No page holds a token, and
# the helper is the one place that talks to navigator.credentials.
def test_the_passkey_pages_load_only_their_scripts() -> None:
    for page, own in (("login.html", "login.js"), ("setup.html", "setup.js")):
        html = (STATIC / page).read_text()
        scripts = re.findall(r'<script src="([^"]+)"', html)
        assert scripts == ["/static/webauthn.js", f"/static/{own}"], page
        assert "app.js" not in html and "md.js" not in html
    html = (STATIC / "index.html").read_text()
    assert html.index("/static/webauthn.js") < html.index("/static/app.js")
    js = {p.name: p.read_text() for p in files("js")}
    assert [n for n, t in js.items() if "navigator.credentials" in t] == ["webauthn.js"]
    assert "'X-Switchboard'" in js["webauthn.js"] and "credentials: 'same-origin'" in js["webauthn.js"]
    # the claim page reads the token after # and removes it from the address bar at once
    assert "location.hash" in js["setup.js"] and "history.replaceState(null, '', location.pathname)" in js["setup.js"]
    assert "setup#t=" not in "".join(js.values())
