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
    assert {
        "index.html",
        "login.html",
        "setup.html",
        "oauth.html",
        "app.js",
        "md.js",
        "diagram.js",
        "webauthn.js",
        "login.js",
        "setup.js",
        "oauth.js",
        "style.css",
        "favicon.svg",
        "favicon-32.png",
        "apple-touch-icon.png",
    } <= names


ICON_LINKS = (
    '<link rel="icon" href="/static/favicon-32.png" sizes="32x32">',
    '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">',
    '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">',
)


def test_both_pages_link_the_tab_icons_and_the_icon_is_the_sidebar_mark() -> None:
    svg = (STATIC / "favicon.svg").read_text()
    for page in ("index.html", "login.html", "setup.html", "oauth.html"):
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
    for pat in (
        r"<script",
        r"\son\w+\s*=",
        r"href",
        r"<foreignObject",
        r"<use\b",
        r"url\(",
        r"javascript:",
        r"<style",
        r"<image",
    ):
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


# The agents' icons (#62, DESIGN.md §29.9): the vendors' artwork, unaltered, and the one thing CSS
# may load. Each is pinned by its hash; a new or changed icon is a reviewed change to this list.
AGENT_ICONS = {
    "agents/codex-light.png": "c721c4bf79af5c233916d71091156c2fb259bb1429283cf296fbcf4826886f8d",
    "agents/codex-dark.png": "9938d1c9d54afd6de6c8302e9237a3bc4c39d4faa8939ce3bfc9a213ecf3f20b",
    "agents/cursor-light.png": "1ea13e52299f52568335d08fb39b248660992bf66a0c0fe452ee382b008452d8",
    "agents/cursor-dark.png": "ff6890b460641ae05d8e58691c3b4e4438ef7da473df6c34ab3f50334be8c9b3",
    # Cognition's own avatar files from its brand kit (devin.ai/brand), byte for byte (#441)
    "agents/devin-light.png": "3afb7f4c0e2c233a6e3a1956e4c9606a0ac3f774972bc7085f17d1b48dca6ee2",
    "agents/devin-dark.png": "b604b099bcdb8d09455368e67eb7ae74962871eac87524eddc3b30e3a31b4e2d",
}
# The call platforms' logos (#623, DESIGN.md §29.9): Zoom's, Google's and Microsoft's artwork, unaltered,
# which the maintainer chose to ship (2026-11-05). Pinned by hash the same way, and CSS may load exactly these
# too.
CALL_LOGOS = {
    "calls/zoom.png": "d4c91dcf188a12e1426aa9cb3aade313c12189fc171d8f3e2b17299075ab2ac9",
    "calls/meet.png": "2cbdf72a1413bf855574f4419bb9f3b07b4fa10d80ac7c23c15c6ada9f1574ce",
    # rendered at 512 x 512 from Microsoft's teams_48x1.svg (sha256 6ec0e1af...), the artwork unchanged
    "calls/teams.png": "ab89ed8883e1fa1625f14d3ed52db6dc701ac2b1954dd94356d832a10a2c6029",
}
PINNED_IMAGES = {**AGENT_ICONS, **CALL_LOGOS}
AGENT_ICON_URL = re.compile(r"url\('/static/((?:agents|calls)/[a-z-]+\.png)'\)")


def test_css_loads_nothing_external() -> None:
    """No @import, and no url() but an agent icon's or a call logo's: one of the pinned files, by its exact
    path."""
    for p in files("css"):
        text = p.read_text()
        assert "@import" not in text
        icons = AGENT_ICON_URL.findall(text)
        assert set(icons) <= set(PINNED_IMAGES), icons
        assert "url(" not in AGENT_ICON_URL.sub("", text), "a url() that isn't an agent icon or a call logo"


def test_css_allows_only_the_pinned_images() -> None:
    """The exception is that narrow: any other url(), even one below /static/agents/ or /static/calls/,
    still fails."""
    for bad in (
        "url('/static/agents/other.png')",
        "url(/static/agents/codex-light.png)",
        "url('https://example.com/agents/codex-light.png')",
        "url('/static/favicon-32.png')",
        "url('/static/agents/../app.js')",
        "url('/static/calls/other.png')",
        "url(/static/calls/zoom.png)",
        "url('https://example.com/calls/zoom.png')",
        "url('/static/calls/../app.js')",
        "url('/static/calls/zoom.png/../../app.js')",
    ):
        icons = AGENT_ICON_URL.findall(bad)
        assert not set(icons) <= set(PINNED_IMAGES) or "url(" in AGENT_ICON_URL.sub("", bad), bad


def test_the_agent_icons_are_the_pinned_files() -> None:
    """Each icon is a square PNG of at least 120 px (the Inspector's 40 px avatar on a 3x screen),
    with transparency, and the file that was reviewed; its NOTICE names the source and owner."""
    for name, sha in AGENT_ICONS.items():
        b = (STATIC / name).read_bytes()
        assert b.startswith(b"\x89PNG\r\n\x1a\n"), name
        w, h = int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
        assert w == h >= 120 and b[25] == 6, (name, w, h, b[25])  # square, RGBA
        assert hashlib.sha256(b).hexdigest() == sha, name
    notice = (STATIC / "agents" / "NOTICE").read_text()
    for name in AGENT_ICONS:
        assert name.removeprefix("agents/") in notice, name
    assert "not affiliated" in notice and "Cognition" in notice
    for source in ("openai.com/brand", "cursor.com/brand", "devin.ai/brand"):
        assert source in notice, source


def test_the_call_logos_are_the_pinned_files_with_a_notice_that_claims_no_permission() -> None:
    """#623: Zoom's, Google's and Microsoft's logos are shipped as the owners made them: a square PNG of at
    least 120 px, the file that was reviewed (by hash), and each one is what style.css puts on its tile.
    The NOTICE beside them names each file, its owner, its source and its hash, says switchboard is not
    affiliated with the owners and that the logos are theirs and not under the MIT license, says an owner can
    have its logo removed, quotes the maintainer's choice, and does not say that an owner's terms permit it
    (Zoom's say otherwise). Fails if a file changes, one is added without a pin, the CSS points at a file
    that is not there, or the NOTICE claims a permission."""
    for name, sha in CALL_LOGOS.items():
        b = (STATIC / name).read_bytes()
        assert b.startswith(b"\x89PNG\r\n\x1a\n"), name
        w, h = int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
        assert w == h >= 120, (name, w, h)  # square; Zoom's has no transparency, Meet's and Teams' do
        assert hashlib.sha256(b).hexdigest() == sha, name
    on_disk = sorted(p.name for p in (STATIC / "calls").iterdir())
    assert on_disk == sorted(["NOTICE", *(n.removeprefix("calls/") for n in CALL_LOGOS)]), on_disk
    css = (STATIC / "style.css").read_text()
    for name in CALL_LOGOS:
        assert css.count(f"url('/static/{name}')") == 1, name  # each logo is on one tile, in one rule
    notice = (STATIC / "calls" / "NOTICE").read_text()
    for name, sha in CALL_LOGOS.items():
        assert name.removeprefix("calls/") in notice and sha in notice, name
    for owner in ("Zoom", "Google", "Microsoft"):
        assert owner in notice, owner
    for source in (
        "brand.zoom.com/document/9",
        "media.ffycdn.net/us/zoom-video-communications/bsYy1D8LURh18tD1iLzH.png",
        "fonts.gstatic.com/s/i/productlogos/meet_2020q4/v1/web-512dp/logo_meet_2020q4_color_1x_web_512dp.png",
        "about.google/brand-resource-center/logos-list",
        "res-1.cdn.office.net/files/fabric-cdn-prod_20230815.002/assets/brand-icons/product/svg/teams_48x1.svg",
        "6ec0e1afd7359a71915430bab4a49554bcfd1f1b881db5108c807ed08f1a0afb",  # the Teams SVG it came from
    ):
        assert source in notice, source
    assert "rendered" in notice and "512 x 512" in notice and "Chromium" in notice
    flat = " ".join(notice.split())
    assert "not affiliated with, sponsored by or endorsed by Zoom, Google or Microsoft" in flat
    assert "trademarks and artwork, not covered by switchboard's MIT license" in flat
    assert "used only to show which platform a call is on" in flat
    assert "If an owner asks, its logo is removed and switchboard's own drawing comes back" in flat
    assert "The maintainer chose to ship them" in flat and "Use all three logos including teams" in flat
    assert "does not say that any owner's terms permit this use" in flat
    assert "written trademark license" in flat  # what Zoom's brand page says, which the NOTICE does not hide
    for claim in ("permitted", "licensed to", "with permission", "under license", "used under"):
        assert claim not in flat, claim


def test_the_one_stylesheet_the_page_applies_by_script_is_the_brokers_own() -> None:
    """#498, DESIGN.md §29.11: the uploaded avatars' stylesheet is the one piece of CSS app.js applies from
    script, through a constructable sheet, and its text is only what ``/api/agent-icons.css`` answered:
    no other file touches the CSSOM, nothing is written into a rule by hand, and the page still links
    that stylesheet (after style.css, so it wins at equal weight). A tightening of the lint above, not an
    exception to it: ``.style``, inline styles and creating <link> or <img> stay banned."""
    js = {p.name: p.read_text() for p in files("js")}
    for name, text in js.items():
        if name != "app.js":
            assert not re.search(r"CSSStyleSheet|adoptedStyleSheets|insertRule|replaceSync", text), name
    app = js["app.js"]
    assert app.count("replaceSync(") == 1 and app.count("new CSSStyleSheet(") == 1
    assert not re.search(r"insertRule|addRule|\.cssText|document\.styleSheets", app)
    refresh = app[app.index("async function refreshAgentIcons()") :]
    refresh = refresh[: refresh.index("\n  }\n")]
    assert "fetch('/api/agent-icons.css'" in refresh and "css = await r.text();" in refresh
    assert "iconSheet.replaceSync(css);" in refresh
    html = (STATIC / "index.html").read_text()
    link = '<link rel="stylesheet" href="/api/agent-icons.css" id="agent-icons-css">'
    assert html.count(link) == 1 and html.index('href="/static/style.css"') < html.index(link)
    for page in ("login.html", "setup.html", "oauth.html"):
        assert "agent-icons" not in (STATIC / page).read_text(), page  # only the app shows avatars


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


# Markdown (issue #19, #544): message text is untrusted, so there are exactly two places in all
# static JS that set a link target, both in md.js. mdLink() only runs after safeUrl() accepted an
# absolute http(s) URL. fileLink() sets only `/api/files/` plus 32 hex digits. No script may create
# an element that fetches or runs something (but the one script below that loads Mermaid), or
# reach for another HTML/URL sink.
def test_one_vetted_link_path() -> None:
    js = {p.name: p.read_text() for p in files("js")}
    sets = [(n, m.start()) for n, t in js.items() for m in re.finditer(r"\.href\s*=(?!=)", t)]
    assert [n for n, _ in sets] == ["md.js", "md.js"], sets  # the two assignments, both in md.js
    md = js["md.js"]
    fn = md[md.index("function mdLink(") :]
    fn = fn[: fn.index("\n  }\n") if "\n  }\n" in fn else 1200]
    assert "a.href = v.href;" in fn and "a.rel = 'noopener noreferrer nofollow';" in fn
    file_fn = md[md.index("function fileLink(") :]
    file_fn = file_fn[: file_fn.index("\n  }\n")]
    assert "a.href = '/api/files/' + id;" in file_fn
    assert "u.protocol === 'http:' || u.protocol === 'https:'" in md
    assert "new URL(" in md
    for n, t in js.items():
        assert not re.search(
            r"createElement\(\s*['\"](img|iframe|object|embed|link|style|base|form|meta|svg)['\"]", t, re.I
        ), n
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
    secure = d[d.index("const SECURE = [") :]
    secure = secure[: secure.index("];")]
    for key in (
        "securityLevel",
        "htmlLabels",
        "flowchart",
        "journey",
        "theme",
        "themeVariables",
        "themeCSS",
        "fontFamily",
        "dompurifyConfig",
    ):
        assert f"'{key}'" in secure, key


def test_the_vendored_mermaid_is_the_vetted_release() -> None:
    nested = sorted(
        p.relative_to(STATIC).as_posix() for p in STATIC.rglob("*") if p.is_file() and p.parent != STATIC
    )
    assert nested == sorted(
        ["agents/NOTICE", *AGENT_ICONS, "calls/NOTICE", *CALL_LOGOS, "vendor/mermaid/LICENSE", MERMAID]
    ), nested
    assert hashlib.sha256((STATIC / MERMAID).read_bytes()).hexdigest() == MERMAID_SHA256
    lic = STATIC / "vendor" / "mermaid" / "LICENSE"
    assert hashlib.sha256(lic.read_bytes()).hexdigest() == MERMAID_LICENSE_SHA256


def test_markdown_loads_before_the_app() -> None:
    html = (STATIC / "index.html").read_text()
    assert html.index("/static/md.js") < html.index("/static/app.js")
    assert html.index("/static/diagram.js") < html.index("/static/app.js")
    assert "/static/md.js" not in (STATIC / "login.html").read_text()
    for page in ("login.html", "setup.html"):
        assert (
            "diagram.js" not in (STATIC / page).read_text() and "mermaid" not in (STATIC / page).read_text()
        )


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
    assert (
        "location.hash" in js["setup.js"]
        and "history.replaceState(null, '', location.pathname)" in js["setup.js"]
    )
    assert "setup#t=" not in "".join(js.values())
