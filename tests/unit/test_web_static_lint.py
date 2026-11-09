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
        if n == "whiteboard.js":
            # #671: one fixed opaque frame, approved by the maintainer; no other dynamic frame.
            assert t.count("document.createElement('iframe')") == 1
            assert "frame.setAttribute('sandbox', 'allow-scripts');" in t
            assert "frame.setAttribute('credentialless', '');" in t
            assert "frame.src = '/board';" in t and "allow-same-origin" not in t
            t = t.replace("document.createElement('iframe')", "fixedBoardFrame()")
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
    assert srcs == [("diagram.js", "s.src = SRC"), ("whiteboard.js", "frame.src = '/board'")], srcs
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
        [
            "agents/NOTICE",
            *AGENT_ICONS,
            "calls/NOTICE",
            *CALL_LOGOS,
            "vendor/mermaid/LICENSE",
            MERMAID,
            *BOARD_ASSETS,
        ]
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


# #671, DESIGN §56.8: the approved sandbox build. Every byte, including fonts and notices, is pinned.
BOARD_ASSETS = {
    "vendor/board/Assistant-Bold-ZDZZ6JHA.woff2": (
        "e0cdd332ce039770e93ec44336b6db65f931940f42b0342aad35a06e7f991d29"
    ),
    "vendor/board/Assistant-Medium-DZ25RZU3.woff2": (
        "81a9f280ce6a790f20ca8cd521183caa430f1f7fdc0752c36ef1cc6dc828ac57"
    ),
    "vendor/board/Assistant-Regular-PLF2XOGW.woff2": (
        "76945f09225aae65bdbd204cb4ac40d4caa4ff67150e5074bab1a04d1b778fc3"
    ),
    "vendor/board/Assistant-SemiBold-CZ5MX6FK.woff2": (
        "bba47a7d1b185a4a2e0d265ec82aeac5dbc2dc9cb0f482a2ff57cbdfe4e5045d"
    ),
    "vendor/board/NOTICE": ("33d9eeb9aad442432c882d6de7c2019b61bbed36b132423af85847b5edf172b7"),
    "vendor/board/board.css": ("03c329f17085043b2602f5f121484d8f7944fd3c4d87d77d0c428043583a733b"),
    "vendor/board/board.js": ("363f7f58f882c7376a31de56f6780bac0f823a79e00e39c8a12cb27a34f88055"),
    "vendor/board/board.js.LEGAL.txt": ("7b826dafefdf12e44d1d201e45d1b30de5f5ca8889629bfe6622da9441711e7e"),
    "vendor/board/chunks/abnfDiagram-VCTEODGH-YG5Z4CC2.js": (
        "5b7abdb9a3edc9db2a61621fbacac73c37610782b32da6a2c17babad56d5df01"
    ),
    "vendor/board/chunks/ar-SA-G6X2FPQ2-RHGBW7MY.js": (
        "53bfecf0bc26055ef459358421488342cd5809697b608ea1d4c7ac8fd4a1348c"
    ),
    "vendor/board/chunks/architecture-7GRP2DOG-4PHRC2N4.js": (
        "758b676eaab3cf61c3ec01c6cdcc233d16d25e68e4faf044f0b4059b1d60422a"
    ),
    "vendor/board/chunks/architectureDiagram-5GKGNRK7-CVE2UKMS.js": (
        "f039458da889f89c16ab9e4fd68fd3b450d53c8aee1257895287ca6d290c0183"
    ),
    "vendor/board/chunks/az-AZ-76LH7QW2-P34JJ2SR.js": (
        "eec8c61526fb282644cf762ebce035b8a7f75dc2428a5de02a74613359577645"
    ),
    "vendor/board/chunks/bg-BG-XCXSNQG7-ERM6WHQM.js": (
        "d66f1ae9c4be4f3b35be938dfa195edd2755c573b302d129584ec7947a6ad37d"
    ),
    "vendor/board/chunks/blockDiagram-I7D4REHJ-4P5RWNPH.js": (
        "1f64d47d34dfd3f2e9bcb11cb312711b100ff083af78aba02e0dcd9dba7626af"
    ),
    "vendor/board/chunks/bn-BD-2XOGV67Q-O3JVPYCL.js": (
        "4302a2e4d1d9f0d2563be9fcbb3d42c85d8e9c16e007534c6615518a8935f667"
    ),
    "vendor/board/chunks/c4Diagram-7LVT6UL2-SB7IBK7M.js": (
        "2dd6308166bea10dfce7f050b0959da12e1aa9a0a03a335bec9ff80584f04214"
    ),
    "vendor/board/chunks/ca-ES-6MX7JW3Y-R7FEXG6G.js": (
        "d56827bec6d44d49bd1222095971f2e3f8d1620592714f01a1917b83fb594e8c"
    ),
    "vendor/board/chunks/chunk-22JFAUNZ.js": (
        "f8bf9112ea3eeac19e2b54f8cfee50cf30fa94bd9d48d679cf3af5501494b232"
    ),
    "vendor/board/chunks/chunk-2GUBPQ3I.js": (
        "c895be02702fd30244ff081f7ab9089e92f9f3235d136943a1e2faad6cdc935e"
    ),
    "vendor/board/chunks/chunk-2PVHA67V.js": (
        "012b24744791fb1a6ba41575338634d6ea3ac0793484f351d90419e572b6a6ae"
    ),
    "vendor/board/chunks/chunk-3JCBMXLA.js": (
        "2c35c2594a30c713a96f001dca6746dfb41e9bf242ecc7fc1857fb06b075d3c0"
    ),
    "vendor/board/chunks/chunk-4G4Y6B5G.js": (
        "60a6eafe5fc11d1ab4eb5ea202ff17a94bbbb14cd90d393ec0fde8e8309a445c"
    ),
    "vendor/board/chunks/chunk-53I323WB.js": (
        "8dd743e96f95fd9b14100675c1a8639a950424605eaae0796b99d0e60ba0fbd3"
    ),
    "vendor/board/chunks/chunk-53I323WB.js.LEGAL.txt": (
        "4245c420f535f6b11e3f590606105d70565d76e4ba670d1f1497c1f39c57800a"
    ),
    "vendor/board/chunks/chunk-6AUQRJOP.js": (
        "768f6699e9f632ddce47e6bee24d6a59acec46e6945a02a919d4c8e45e211f46"
    ),
    "vendor/board/chunks/chunk-6LGVZAFW.js": (
        "c9a5329a8bae4899f07908c3db00edb36f778ba9370dc52da470bf94756adaab"
    ),
    "vendor/board/chunks/chunk-7B4TXBCD.js": (
        "ff41fd8a4e03315adb298a692853439c9b20e1e7ea4d85e89b70ccf7e3b17465"
    ),
    "vendor/board/chunks/chunk-7B4TXBCD.js.LEGAL.txt": (
        "d7775cb7e11ff052896456388b072b6c234c4ecf13254e902d1fd0a0a5d7288f"
    ),
    "vendor/board/chunks/chunk-7FUVYZS7.js": (
        "e146324332f3483251669e2f12c3fddd385fdd6f51daca3387b78a3b6da73318"
    ),
    "vendor/board/chunks/chunk-A7CGNYZU.js": (
        "1542d1135fd4b756f4a15b20ea5b29e02e37ef4d6617975bfcd5833bb1c8863f"
    ),
    "vendor/board/chunks/chunk-AEWNGHQA.js": (
        "daaff89f24158fca8e602bdca57ec0d9faea201e4e0429206da8ed9b23685dd0"
    ),
    "vendor/board/chunks/chunk-AZAV2LH6.js": (
        "1eb0cda756e4578a7330452455980afcb41aee876347f14672ae47942152db3b"
    ),
    "vendor/board/chunks/chunk-B6LZSJN7.js": (
        "c8a9421d0e257ab9b6c2087d83effaa680531654c25c7410741f6d1ae8d380da"
    ),
    "vendor/board/chunks/chunk-BJ5UEYGP.js": (
        "941973185036840f4aa773e518507f9a8535fc49e249ff01e453ddf27b8b5cdf"
    ),
    "vendor/board/chunks/chunk-BPLYPLL3.js": (
        "9e4e7719cbf6211d5c7e8626bf4f6ae1743aa9e61ecdd5ee6f6396470df635bc"
    ),
    "vendor/board/chunks/chunk-BPLYPLL3.js.LEGAL.txt": (
        "9aca14ef8d4b303c6666aa8d58ef291346f64af381a0aa1aecdbf238a06f2931"
    ),
    "vendor/board/chunks/chunk-CU2LWWM4.js": (
        "224a3d60d7d2ed959d8c9f1e77097995cb1983cd47fd0b94f56c613ff4df2a5d"
    ),
    "vendor/board/chunks/chunk-CWOSGGWM.js": (
        "2de90a9a09a45bbe056306e73de09162107212987f40cf219fb554527d69bb15"
    ),
    "vendor/board/chunks/chunk-DNRPPGA3.js": (
        "0e4bc3cc9550b8ed467a88cc6fef9fd62f88b850d4be9cbd4e7133a049b16f46"
    ),
    "vendor/board/chunks/chunk-EJXFWEXN.js": (
        "aaa1cf5f41ce81cde9485fa95c6982a3aea4cd09b2f8bccb35ab7c5b4c2f3a57"
    ),
    "vendor/board/chunks/chunk-EP233B4I.js": (
        "751f5e4bb8faef7ae9f07fef13e70304ced8558e53b7ff090e22aa70b4c7f653"
    ),
    "vendor/board/chunks/chunk-ER2XNAB2.js": (
        "c702f1388f09743478a0c67e31d44ec8bf50e3cd75ca8b2de1a06edf7541446f"
    ),
    "vendor/board/chunks/chunk-ESZPWZLX.js": (
        "6117aac22a3bc113ebec2e3cba776ebf696461c90b657b9f0016d303aa9a08c4"
    ),
    "vendor/board/chunks/chunk-F7JNQFTV.js": (
        "56ad2bd9bf005fb7f0e3db3a6957c7c63a4d61b45f7690d2fd70932c84072bd6"
    ),
    "vendor/board/chunks/chunk-FEM5XCYT.js": (
        "c5414befcb620e5f5e8d6ce56109514886227309b6388e4f71df3e38c004f59d"
    ),
    "vendor/board/chunks/chunk-FIYD7FGS.js": (
        "553b981946728ab9be3be55eeaffec1e33d0de1a08a05c813c571e0bba92df5b"
    ),
    "vendor/board/chunks/chunk-FNNBNI6R.js": (
        "279c18af75ea9e0cb12ac180a7e1ce93844d91304cff9dcc74dda259fa3e1d76"
    ),
    "vendor/board/chunks/chunk-FNNBNI6R.js.LEGAL.txt": (
        "62544d6446f960ae3f37318b41d8e85566921a7777ae492da7ab72d45ca1af31"
    ),
    "vendor/board/chunks/chunk-FPQQ6FPT.js": (
        "5eed0452f35fb849468f62dab6badd0398e8c57ed54d8b7d103068943950e3e7"
    ),
    "vendor/board/chunks/chunk-HND5NSDJ.js": (
        "fe5e24252868ff71bd3c229af9f795f4b5110f9db260c57cda816a28074e9046"
    ),
    "vendor/board/chunks/chunk-HWGYIVYH.js": (
        "7b6c62fae7b2e807bd655dc8abc737cea758532b0c9093f55df8cfb55a4510ca"
    ),
    "vendor/board/chunks/chunk-II2VAMMO.js": (
        "a2f57d58c2b9210246341efd58674bffc3d67694bcece23db843133974ed78a6"
    ),
    "vendor/board/chunks/chunk-IO7KF67M.js": (
        "5ac08c0e54095eeb9b7b4aa8dcca092fa78b910977b7ba23d7ae247379cfbdcd"
    ),
    "vendor/board/chunks/chunk-K7VJ6PCG.js": (
        "07bfec8fe3a16104403110acbfbdd6db9d5252deb2398ecca2e00322edfa167d"
    ),
    "vendor/board/chunks/chunk-LZXEGDTS.js": (
        "0f13cb61b3ed7da16d3f2898f5be60f626ec8f616a51e69df0b5ce3fa521b8e2"
    ),
    "vendor/board/chunks/chunk-MHJOTOFG.js": (
        "98493c79e7b44af7574873770b563b282a90a30b858c1d2cf4a65a0f7d91a7cc"
    ),
    "vendor/board/chunks/chunk-MWQVXMXE.js": (
        "64c3cdb96170d5d87c37f0a41f4da1fcabe2c95c2c43f5be536aefb6024ed354"
    ),
    "vendor/board/chunks/chunk-MWQVXMXE.js.LEGAL.txt": (
        "e307472273e1ef372ca969af6d0a32f575f172970adc3b89dfad3600fc632107"
    ),
    "vendor/board/chunks/chunk-O7YFOXTE.js": (
        "9d6a3fbe6595bf0d6aa9f1e4f67caf1032ce34821bab6f901fab67984c121bbc"
    ),
    "vendor/board/chunks/chunk-OB3WUVTR.js": (
        "bd40ea70c257759981adfbb875d3e46c4514526205134cb296cd87aaf6d739eb"
    ),
    "vendor/board/chunks/chunk-PBMPNUCO.js": (
        "812e63e7c40364b8a4f69cb9fdd9540051e7668676ce30b8b420e20d914aa350"
    ),
    "vendor/board/chunks/chunk-PBMPNUCO.js.LEGAL.txt": (
        "9d6729d5b5efcd1497bc4ea02c55ae358dd16e192613e741a47418db53e0c61e"
    ),
    "vendor/board/chunks/chunk-QDEQVKGJ.js": (
        "6f53eb7490adecdb51a48d5d25ea6eaf63b70ddb4746c3c7affda323835067a7"
    ),
    "vendor/board/chunks/chunk-QMEGLWWV.js": (
        "9af43ac8382310b081d9339b22a8189b65000fde2e20c8b9adc95317d82ba0f6"
    ),
    "vendor/board/chunks/chunk-QU2TTTXF.js": (
        "e4c8668149658a2c84664a37581f49a88b6cf756f3fb031e1d6ee88174043498"
    ),
    "vendor/board/chunks/chunk-RVLQKDDY.js": (
        "ee3a6d9cd855095d5df03244bcb6a58620bdf68183f9df9c6e0005150b1b9d18"
    ),
    "vendor/board/chunks/chunk-RXA4X4BB.js": (
        "b56d1737d04f8037d02db16049167c4d1c04c60e22d8366cb02005e655c3053e"
    ),
    "vendor/board/chunks/chunk-S6K3BDIY.js": (
        "cb3a31993f1730fa9093260ff21f84a9d8d3ee1390f54fb61eed42259af968dd"
    ),
    "vendor/board/chunks/chunk-SG36ODWK.js": (
        "8b28c39d8242eee129c9403d1d36dde75e7c540c7720249ab40cb86ad58969f1"
    ),
    "vendor/board/chunks/chunk-SLE4MROL.js": (
        "d46dc7ae7c2467b9d71968c5f62e2465fac5aeda8c1f538d5ea91665fbeba1b6"
    ),
    "vendor/board/chunks/chunk-TRNEPHQU.js": (
        "c561f6ab3ad910bc718e398c0f80858b02dccae4cf5cef23144b47c6b9b4b896"
    ),
    "vendor/board/chunks/chunk-UVDIT6BP.js": (
        "877135e8c1db4a50db3a4d65e7f7de238b47864c9dfba6c2cf2f887ee57bf834"
    ),
    "vendor/board/chunks/chunk-VBDRXDQL.js": (
        "b0fe7cb2902e7dfd7b80065067915b8af6e86915712ff47f309beb56e1e99644"
    ),
    "vendor/board/chunks/chunk-WEFFAZ3C.js": (
        "4d338e0b29558d9e43ffa8079b48a8428051f708fac354dfd267f86c72f074d5"
    ),
    "vendor/board/chunks/chunk-WGJTJAZS.js": (
        "ede2bb8930924062eff4bfef25e2a812247aa835f791c6b781ee66ceab895d5a"
    ),
    "vendor/board/chunks/chunk-WTGRISO7.js": (
        "d3b888e8b3543aa5dd500952dc67c48970872f6a1f59b1ecbd80df42f1617d72"
    ),
    "vendor/board/chunks/chunk-Y7PGVHT2.js": (
        "e043d6e0e3b68d88cbc92e2c4fab6031e38718671aec128257fffd3a0abf3e85"
    ),
    "vendor/board/chunks/chunk-YGQMSKXN.js": (
        "12435161269d6292111f23f8a801297047a7e8ec69fb7052e22fbfd54a5efd8f"
    ),
    "vendor/board/chunks/classDiagram-ZZMXUADV-K6JZSEDQ.js": (
        "bba057f12bdcad09e4f2392f15049e34b5821eff81e8bf7cf9959e0b9334229f"
    ),
    "vendor/board/chunks/classDiagram-v2-VYDZK3BY-4FVVOLUG.js": (
        "bba057f12bdcad09e4f2392f15049e34b5821eff81e8bf7cf9959e0b9334229f"
    ),
    "vendor/board/chunks/cose-bilkent-JH36ORCC-64WVY7U4.js": (
        "7fa96acbe84f9ab601ff32c5eefeca4d12649baebeb333c7404a598273654cbb"
    ),
    "vendor/board/chunks/cs-CZ-2BRQDIVT-LSE76WTX.js": (
        "bce086b6dd62ae56888610fb11d9889076d5b7013b49c0c278a0654a8f8a5e04"
    ),
    "vendor/board/chunks/cynefin-OW5HDTMX-7CUAK3RC.js": (
        "3379fcbfea14fcf5814f4839267869a77a13cff4e2853727b035e072dacaf5d1"
    ),
    "vendor/board/chunks/cynefinDiagram-5FMLGOSQ-YRBJYMQZ.js": (
        "433ad359458038a6e1d0fc3e01e03bfa5f3ffa89160935a3feff3a2dad2f28a6"
    ),
    "vendor/board/chunks/da-DK-5WZEPLOC-WRWWHETS.js": (
        "8609aca953f29a43fef0dd4eb27aae04290515801a6d87db42ecb680927000d0"
    ),
    "vendor/board/chunks/dagre-GXQ25YYZ-GM3SNU75.js": (
        "7e1e282c12e43d99ec934b7909a395e66afbdcf3e4644c16f6edbd67b1c94e05"
    ),
    "vendor/board/chunks/de-DE-XR44H4JA-CIECYWNM.js": (
        "f857ca71084fafa181d7cec73c0d0296ba27f4ebc5a5aafd7be4ab45ce9bb505"
    ),
    "vendor/board/chunks/diagram-S7CK7UJ4-5D4G74G7.js": (
        "6235cc475f92e54c5a77537e5c735851778c026ef4eb3d4845d942bd0cd20361"
    ),
    "vendor/board/chunks/diagram-UQ7AKVKN-RKVCWHIQ.js": (
        "f3bfd731c97d7eb644c19e5422d61c5349a86541f483f74449b1196a042a22ab"
    ),
    "vendor/board/chunks/diagram-VSXAHHWV-CEHV2THH.js": (
        "f0c1d2cda5c23b8e223bea2735f9b623c40bc7f39b3d28d2ad339618f3cf1cf9"
    ),
    "vendor/board/chunks/diagram-VX7I27RA-OIVU5MOK.js": (
        "9e8505542b9e0ecf1d6dad4fcdabd6175eb64491c9e7492efd0b17f2ae9c57ed"
    ),
    "vendor/board/chunks/diagram-Z3DM3KII-ZLGYI3CL.js": (
        "4efeb63aaddd63d8c125d233b331014de07650ae230a3215e1786042e6a7f084"
    ),
    "vendor/board/chunks/directory-open-01563666-BCF2OQHL.js": (
        "a1dcd4ed8ae5083cbc201d276c66461b5956139ea0ec697a2c6ce4da396ef22f"
    ),
    "vendor/board/chunks/directory-open-4ed118d0-EJZEENFI.js": (
        "258599e3d97effbc55179ebf49d1bf046112fde932416c802ed7db4a2f665a7a"
    ),
    "vendor/board/chunks/dist-535VDP5Q.js": (
        "699ac48724f5e3342be0c8a8a362e6df3467b8a17da9d32a7e6fd0a771b422a5"
    ),
    "vendor/board/chunks/dist-535VDP5Q.js.LEGAL.txt": (
        "108b77c578b750eb21d34748148ce6a0c86e552be7e54ea96a6ffa103ccb9720"
    ),
    "vendor/board/chunks/ebnfDiagram-PWID7BFC-MMRD4WFB.js": (
        "5b8ffdd61c2de150861f5e33168d43ad09030a4fe4e5ca62abd47ab1eb2802f7"
    ),
    "vendor/board/chunks/el-GR-BZB4AONW-GLVWY4D6.js": (
        "6af54c96a4302af7e80c675d0e7c08f24f150051e95f472e0c0f61ba6c97da6e"
    ),
    "vendor/board/chunks/en-B4ZKOASM-DZ44YZKH.js": (
        "507d7d5e2f8b658f217b12d234b9e47140963c31c64b68acde54c7cf37990f8e"
    ),
    "vendor/board/chunks/erDiagram-RLTQ6QDP-6VI5DYJE.js": (
        "1ecef175b10a243cfcb294eee87aa988adec3999688fd9f5bd75bc96126c1bf0"
    ),
    "vendor/board/chunks/es-ES-U4NZUMDT-JQ64V7ZA.js": (
        "b4bae371c8f2536ca955537e76630d658f5d4ac27ddfa11b74740b4a2c6e17ef"
    ),
    "vendor/board/chunks/eu-ES-A7QVB2H4-QMQ2L3H6.js": (
        "e33ddc6542a39ed8b8527143be0126ffcac3a3d04b339fba8ea0c0d07871970c"
    ),
    "vendor/board/chunks/eventmodeling-NTZA5JFV-JCWGQAXK.js": (
        "0157f4eb50f0f4bdfe388d7db69c244e8e4bba908e1bc0f0a84840a19584677d"
    ),
    "vendor/board/chunks/fa-IR-HGAKTJCU-5XCKK3HU.js": (
        "35915be211c1193b35deb1938e9205c430f7418a0f85e4e876f3733430e4a723"
    ),
    "vendor/board/chunks/fi-FI-Z5N7JZ37-LTDIZ5BC.js": (
        "980031e6683a5f4d792354f4e796d19937a247a7299e31c55569053caf72e1e0"
    ),
    "vendor/board/chunks/file-open-002ab408-X2X74O5N.js": (
        "357cc568d08486c25b676d63ce6800a304cbd504757e181131f412bdc0d6f539"
    ),
    "vendor/board/chunks/file-open-7c801643-2KTMUCZB.js": (
        "f2ccbabc8d46f7a1af925c4df4bafbf30ade5a0221b680c8365c9c54c18e9925"
    ),
    "vendor/board/chunks/file-save-3189631c-57IVACNE.js": (
        "d957af1d5c34e85e5dbc5990153c010aca1ac9f3878bd784ad99f46d849bc218"
    ),
    "vendor/board/chunks/file-save-745eba88-7UR3GB4E.js": (
        "974f609a4b4a89fb18bd0985bf165a6fabae8835bac81dae33d12291f67ea69b"
    ),
    "vendor/board/chunks/flowDiagram-HODETNUW-2BIJVJI6.js": (
        "868ec4cbd102741629442806303008f83c0578ada155fa3de0148b54315e1ef9"
    ),
    "vendor/board/chunks/fr-FR-RHASNOE6-EVXS4JUS.js": (
        "a9ba21beb35902336766ee69e4bbaa79c0f3a2218e5bc3220e33a774f5d1b960"
    ),
    "vendor/board/chunks/ganttDiagram-EL5Y4UJY-KUZQWFSS.js": (
        "a7e6eafa84861f7e26c0df02495bd6cdbe505232765f81a5b0e71ff7fe1097d6"
    ),
    "vendor/board/chunks/gitGraph-4MIJSDKK-LZWZCKRI.js": (
        "8b6c2edd5939d82f2da3e3b59af8efa80a860f0e6143c4e195578768936bbbdf"
    ),
    "vendor/board/chunks/gitGraphDiagram-WWUBYQGX-33ABD4IP.js": (
        "971f0e580e173f417e81863c6f59755825b2b53a887ba98f71178e1c9b468c71"
    ),
    "vendor/board/chunks/gl-ES-HMX3MZ6V-2MAH5AQX.js": (
        "4209422fd7c2aae8e2f19bb8c8918155786af891963467a30483dcc9c2ea405d"
    ),
    "vendor/board/chunks/he-IL-6SHJWFNN-OZWISSYU.js": (
        "cfda0c3e1fabba54b6a24bf47af4ce4c3171130a6d61470d267d4b17939d5452"
    ),
    "vendor/board/chunks/hi-IN-IWLTKZ5I-B3UT6QLH.js": (
        "55813a98cfda8d8114fcb89c859201c572c2d60849afb23911a59f4213e8c3dd"
    ),
    "vendor/board/chunks/hu-HU-A5ZG7DT2-NAAIBGBX.js": (
        "f1874d5c025f6ebeb4ec6b8e9c2dd656f160157f78597c044d0c5534130778a4"
    ),
    "vendor/board/chunks/id-ID-SAP4L64H-UJ65FGUP.js": (
        "2a28899f2e4629503a9cb80e765c9c3cfc4b1d170d9ba6340b6694404da1fc16"
    ),
    "vendor/board/chunks/image-GAAHSSAO-5ZI4UCSW.js": (
        "25930bfd2773f8d49f23ff83862a7ff29cc389de9c9d2847ac6f5d9d816cb94e"
    ),
    "vendor/board/chunks/image-blob-reduce.esm-OS2VLHZ6.js": (
        "2ef12a35eb45b63bab38cb6b82ae40d2430c22a29cbf7d4a2befac13715ee5e2"
    ),
    "vendor/board/chunks/image-blob-reduce.esm-OS2VLHZ6.js.LEGAL.txt": (
        "784af932cf9057a7b45ac984633342b2b59b0028cb6506d0e114355fb3a4c8d4"
    ),
    "vendor/board/chunks/info-A6RAGUB7-ZKB2RQDR.js": (
        "f64ae1f02b3638c421fd677122cfef21f2daa680b71c2bda522ad324518e97fd"
    ),
    "vendor/board/chunks/infoDiagram-27XIBGKW-3TSPSEE7.js": (
        "572a55e56e242ecfd51ab50942cc0e5883f5da225100b9f769ffb0f62b91a6e7"
    ),
    "vendor/board/chunks/ishikawaDiagram-5VMMS53U-FL6SFJE3.js": (
        "bcc5e46725886742de483268750027a5807f3439c9cd7f6ffc044c4ae8574946"
    ),
    "vendor/board/chunks/it-IT-JPQ66NNP-BFLT72QG.js": (
        "64ca675fea2e6b3a6372d7d421525d5cb4309b2cc04f78fae906e6b4d0600c70"
    ),
    "vendor/board/chunks/ja-JP-DBVTYXUO-MD3SCTPA.js": (
        "1042555bf8f08524388362ea9bc6041bea925c8bdc9c08aae0c0aedd352b9ae2"
    ),
    "vendor/board/chunks/journeyDiagram-3NMN7TZE-OCZ3XXMF.js": (
        "0bcd2a1a447424c668f21d48485d67df86ddf0cc41fca70de00a166d9076dbee"
    ),
    "vendor/board/chunks/kaa-6HZHGXH3-VRNFV3GN.js": (
        "ff11d774178c3a135cdc5004414d690e24e28d585b0ddcce81d5bca5e48715cb"
    ),
    "vendor/board/chunks/kab-KAB-ZGHBKWFO-Q33DF4M3.js": (
        "b225d106b118758ff58592eab973752d2ca82987b88b27e5e1f8a6fdc162a5b6"
    ),
    "vendor/board/chunks/kanban-definition-UXKFOSKX-C5AIOOGO.js": (
        "b44a72bfad055d8632e08517a2f8dcc44e672f30ad9c5ebe2afec65b9c60567a"
    ),
    "vendor/board/chunks/katex-2P75RHWM.js": (
        "11caec1b39f7a8fb53b7f7f17ded4bc0dd0893d5d40d0714164524e58b78f0bb"
    ),
    "vendor/board/chunks/kk-KZ-P5N5QNE5-EM3X6S2C.js": (
        "9bf3048731d7c64728a999cb8c65491fc3125785014f95f8c61f34a466ce6efc"
    ),
    "vendor/board/chunks/km-KH-HSX4SM5Z-KQALNNGW.js": (
        "105272160a34e998fa4cd2cdbe1866c94f70b16738b7bb28de4422d71f4ed361"
    ),
    "vendor/board/chunks/ko-KR-MTYHY66A-7C7Y2WI7.js": (
        "d9311eef4832704c823c29d6a053cbe09d1d59c1c62a6d3413f3a82abcc8e2d2"
    ),
    "vendor/board/chunks/ku-TR-6OUDTVRD-HS6G4AM4.js": (
        "f401bffcf1dd6dfe2a2532f354a91eafc1d0bf02905f924424b654161bf88b4f"
    ),
    "vendor/board/chunks/lt-LT-XHIRWOB4-GRRGQYQW.js": (
        "a42b1bb28f69e8d77f11e7984a3f24dd4630f569540a532e0d3c0b2616661ded"
    ),
    "vendor/board/chunks/lv-LV-5QDEKY6T-3ZJNK7NG.js": (
        "20edda6101c03f5dac78b8ca5d7cdb9236b646ad75c6bfd3b10a7e066d10b76b"
    ),
    "vendor/board/chunks/mindmap-definition-YA3MSWOX-XUAKOGD4.js": (
        "0349422d4bb121cbb90420653ca19537bc1dae01ea2ad03ec4cba8a008831ef0"
    ),
    "vendor/board/chunks/mr-IN-CRQNXWMA-IEBQ4UZG.js": (
        "0bb034daed5cca3acffcfdb2961650c44f25d6845f169774d36887e760ce6acf"
    ),
    "vendor/board/chunks/my-MM-5M5IBNSE-AU3MND76.js": (
        "97838958d517c2eb29daf416af643eb85b74f9b15a72d02b6495a5818b26db59"
    ),
    "vendor/board/chunks/nb-NO-T6EIAALU-IXS5J2HE.js": (
        "cf4ffc1a8d6e9c3a6e229ee2b2082a9dccce7598fd9edad614de6c0e46a79e63"
    ),
    "vendor/board/chunks/nl-NL-IS3SIHDZ-A5ETTIQJ.js": (
        "84e6cb299952f7b23a80b1edba43969dc1607803f1128cba3ed8fb72f5e0746a"
    ),
    "vendor/board/chunks/nn-NO-6E72VCQL-BMUJGWES.js": (
        "1352e76385b881608774116eeaacd99146cd55af45fd8bdcd45a7319e29f0b69"
    ),
    "vendor/board/chunks/oc-FR-POXYY2M6-2A4EV6JX.js": (
        "9e1dc75f2efd9258913c938f1d83a068df1ee3e3b4e8c1334ad6109fbbf08674"
    ),
    "vendor/board/chunks/pa-IN-N4M65BXN-ZJLEEY3Q.js": (
        "5f2c625093cb3c9d3dc2ac653f2757c40707a6bb9c21dc114a76b5e9beefa1f0"
    ),
    "vendor/board/chunks/packet-AYTQ26CC-WMAUQQPC.js": (
        "f903ff72ef184cc8e8eaf00fcb1f10ef1a726468750491065468d302dcdec819"
    ),
    "vendor/board/chunks/pegDiagram-XKGWAZYB-PCGPVG2S.js": (
        "fbab1c03284673faa5d460a412d52263e63354d93536fa6f3c60d9e5427c0a25"
    ),
    "vendor/board/chunks/percentages-BXMCSKIN-YJSF5M2O.js": (
        "e10ec8a63d1747d594103ee30abc81da342ff2849b5350e902f492c6392c089b"
    ),
    "vendor/board/chunks/pica-IG5SJWFR.js": (
        "bee459e2db1755a8e81b0813de1fab071244554eff17858d036e007467bad23d"
    ),
    "vendor/board/chunks/pica-IG5SJWFR.js.LEGAL.txt": (
        "65f20b75bfa40cf26749e8adb218d4f3e5befd4be7d3d8af5b4ff8f1fe683fbd"
    ),
    "vendor/board/chunks/pie-WAS4IAKB-EURAM6BY.js": (
        "29ea0241b8ad1229cdf7fa71d7ca40a974230c2ecb3bee3aa5b9eb40627e27d7"
    ),
    "vendor/board/chunks/pieDiagram-E7YTZNPT-YMLFV4SC.js": (
        "5d16fedc91f46621d838842f8aef6e3ec4ed5526c9080076b5aa1125541e4091"
    ),
    "vendor/board/chunks/pl-PL-T2D74RX3-CJZLOTS7.js": (
        "1325318cfe7f59d42ec15c9e264a06e37f2b31c20326d10814560e1afd44d075"
    ),
    "vendor/board/chunks/pt-BR-5N22H2LF-7X7I6YSS.js": (
        "d37b934ecc0273cb1532cf6d8a5903a16fe3425463c4ee6e083089713870b0d3"
    ),
    "vendor/board/chunks/pt-PT-UZXXM6DQ-OXQH3Y7S.js": (
        "05e95ac360c2c8769df6aec0cc8b98bd059b6a554fa2e795cf2a4206d0cd5909"
    ),
    "vendor/board/chunks/quadrantDiagram-AXDQQJYC-OLSMWVIO.js": (
        "11c2f6be24477ef4fb6771d674a97dc479c4886e3372c67c1f15b1949ba9ab90"
    ),
    "vendor/board/chunks/radar-RG4KPBEZ-RUDTGGMQ.js": (
        "35f2d8d6ef2c08b6715f880746a70e15d5a30469923da3f18326ee0fd50947d6"
    ),
    "vendor/board/chunks/railroad-74A4TZTK-DNHW44U4.js": (
        "5dd6f2f168cf5a889d62ece010ed74573d06b9ec5305b7020c3d1e297a674f79"
    ),
    "vendor/board/chunks/railroad-abnf-HS5TGJTU-KNMFH6JC.js": (
        "f93fca4409bfde9b7d86b1da283e28180114adc38f67165eec26d4332c4e9290"
    ),
    "vendor/board/chunks/railroad-ebnf-LZEXJU2U-XY5NRPZH.js": (
        "7174a8afe55cf8e5f41f883bac8904d457fafa37c700c4b87adb7be9927484d1"
    ),
    "vendor/board/chunks/railroad-peg-WCYAUIDC-2U7WFDC7.js": (
        "9d14bc03e04d79e45a8ffb6df6effe50dacd7bb2c3c9bde69ffc44344706377d"
    ),
    "vendor/board/chunks/railroadDiagram-O6MQD6OU-JF7WLEIW.js": (
        "6172d264289760c4de7c687447dcc516eb0352d3b9d2fc73c6cd6baab3d7b9c4"
    ),
    "vendor/board/chunks/requirementDiagram-BXWQKSXE-FLPDFTL7.js": (
        "fc27687fbe34539198d8c419bc5e37b2b991bee87b3ece4f5ad1dd15ba5c3be3"
    ),
    "vendor/board/chunks/ro-RO-JPDTUUEW-RKAEWFA3.js": (
        "3135c049caa3ceb5ed676b89cb31545f9c7d8013e7c4b63e024bf6990e6562d1"
    ),
    "vendor/board/chunks/roundRect-BHUCBQ67.js": (
        "9be43f9137fc3bf967dd9450c578d270d94d31a8b3454e0faf9a99cb07f4711a"
    ),
    "vendor/board/chunks/ru-RU-B4JR7IUQ-EF4QHGPD.js": (
        "612b84928af6479ebe3c1d1d22218cf6b044144308753aec0e6581b96990aec8"
    ),
    "vendor/board/chunks/sankeyDiagram-P5KCCOFB-HRY4YOWP.js": (
        "6a0f1e1a3a5a5c571f0f1efdc5d1d73f83654a4d2c510729695343fb44528c38"
    ),
    "vendor/board/chunks/sequenceDiagram-WJ2MYXX4-57IWIVO5.js": (
        "c37bd3f0ff4beb95b82842c484cdb6bdcfbbeb2101c6d9573d76eecf1487d122"
    ),
    "vendor/board/chunks/si-LK-N5RQ5JYF-XPHSOL2R.js": (
        "4e3ac39b08d49faa73c17007cad28b406a6897ffda68ebee1d6c237bc69594d4"
    ),
    "vendor/board/chunks/sizeCapture-INFHLROL-HSBXB63C.js": (
        "cee02d47eb83511410364c47411b311e606040c38c1e414a7e995bbb38d9109c"
    ),
    "vendor/board/chunks/sk-SK-C5VTKIMK-VUDKQGHS.js": (
        "d53245abaa3e8807e6a64bf6f4a70bd751e735f32a5d2678f7b48bd32725803c"
    ),
    "vendor/board/chunks/sl-SI-NN7IZMDC-BPSX3LNX.js": (
        "e59903fa3f595a7a78eeb1c203c99ab8647ff83680389a4080f164d3e6277ce7"
    ),
    "vendor/board/chunks/stateDiagram-D77RDMKH-RQJEKTJF.js": (
        "7b59c5c988bc966d8fe0fbeea94fe9314a336c01e02cd0a0b18448b3b8153e03"
    ),
    "vendor/board/chunks/stateDiagram-v2-MP3YSRHH-VON3TNNY.js": (
        "3e5cfad2514b278994da1af7f093f18c3c153150a7d29d20eac6c58e73ab0866"
    ),
    "vendor/board/chunks/subset-shared.chunk-JKOXT53P.js": (
        "e2f7979fa57524d3da903c3a7135af7c39a878d97a56b550460c88d8b7909d58"
    ),
    "vendor/board/chunks/subset-worker.chunk-NFGOYKBR.js": (
        "b3be2833a07874e7c57b4756be11cc37c544d0490d7784c2a2e19933904f3337"
    ),
    "vendor/board/chunks/sv-SE-XGPEYMSR-34G4FIVR.js": (
        "c8961e36b4f45d2ffecd811e474ef48e61bc23d4a93a43006d0e39be7719587e"
    ),
    "vendor/board/chunks/swimlanes-42K2YHIH-KVADLOUQ.js": (
        "26bac561cde7987844c65c333bc5e265243b1326267d9cc1c226e32954754bee"
    ),
    "vendor/board/chunks/swimlanesDiagram-VR7AAH4N-OR7VP3LB.js": (
        "e82fea651945a9e1151ab9d5bb563be2f1565d88653454d1639c995d8ae28527"
    ),
    "vendor/board/chunks/ta-IN-2NMHFXQM-7YIW5ZIM.js": (
        "a688d27cf1dc8a53e563347216449055a7fbddcbfe78ace2754c7a6a01914b9f"
    ),
    "vendor/board/chunks/th-TH-HPSO5L25-DDDGNRHB.js": (
        "ba38a9952f605ccf64dcf132d908af91530ddc48c40b4a8d8a86a5cd879fe71e"
    ),
    "vendor/board/chunks/timeline-definition-24CTP7MA-W2Q2W2EB.js": (
        "f294ca964fa153fcced1ebe917883fdeb26a395b2f1edb2efd78fa16b0724c47"
    ),
    "vendor/board/chunks/tr-TR-DEFEU3FU-FNDQ44MR.js": (
        "fb96498f6d22398ddc3c8bc7d1e79ae5accc54d27c529409e24f31e413eb6a49"
    ),
    "vendor/board/chunks/treeView-Q6P3EWNA-GTR3TXKQ.js": (
        "f72a41cbf827ea07cb013088bbf6280ab442f1ca12c87a7e236f3ce7f0c20353"
    ),
    "vendor/board/chunks/treemap-WGGIJYW6-W5XCFKAD.js": (
        "198274949617f16c63e13aa9818930b8c47c98651844628a5637782f12907ee5"
    ),
    "vendor/board/chunks/uk-UA-QMV73CPH-Y2LL2BZX.js": (
        "88b2de600d9072c5ba666712a3c59699e528add1cbb710d857cfe08e9c6381ba"
    ),
    "vendor/board/chunks/vennDiagram-4TSXK5OY-NIVNM4ZW.js": (
        "7ae4121e253ae539266b37cce8f05baad1ef1afeaf55d1b70f42eefa9215a2ad"
    ),
    "vendor/board/chunks/vi-VN-M7AON7JQ-WHGDROW4.js": (
        "fcacb0bc43d58d66e27f57751cded66c7c1f57fce9ef2795cf704a2efa8d5099"
    ),
    "vendor/board/chunks/wardley-WFR3VGLG-DO5QLXJ3.js": (
        "92aa123f4ff67fb530035c9614da9db5b15eba6cb94982425e834ee33bd12a69"
    ),
    "vendor/board/chunks/wardleyDiagram-VM6X3IG4-G3UQOTIJ.js": (
        "fb44d53c9eb6d92b9e22c51b73de185757059a2f0a6713121f198c0f8e7e0f51"
    ),
    "vendor/board/chunks/xychartDiagram-S5SC5T6Z-MAWJVH7B.js": (
        "68f6c7a292bb30bef061df680cdbe80fa8f06d87800bff8063fdd5c32c3ae3db"
    ),
    "vendor/board/chunks/zh-CN-LNUGB5OW-S3RZKJ6R.js": (
        "25121d5a04b2376c0759f4a579300571b5231ee6c18b5ee1dbf9ad61b3ae05ae"
    ),
    "vendor/board/chunks/zh-HK-E62DVLB3-PE5WWFXX.js": (
        "d64fe74395a620864d32108141f70f44a9cc6333a6f29d9e5c747b3fe4c63d4d"
    ),
    "vendor/board/chunks/zh-TW-RAJ6MFWO-6D2LG3XM.js": (
        "6f9fa44f76ae28890f4a34ab35e1573e1eddb84864753f08b90258fac6548435"
    ),
    "vendor/board/fonts/Assistant/Assistant-Bold.woff2": (
        "e0cdd332ce039770e93ec44336b6db65f931940f42b0342aad35a06e7f991d29"
    ),
    "vendor/board/fonts/Assistant/Assistant-Medium.woff2": (
        "81a9f280ce6a790f20ca8cd521183caa430f1f7fdc0752c36ef1cc6dc828ac57"
    ),
    "vendor/board/fonts/Assistant/Assistant-Regular.woff2": (
        "76945f09225aae65bdbd204cb4ac40d4caa4ff67150e5074bab1a04d1b778fc3"
    ),
    "vendor/board/fonts/Assistant/Assistant-SemiBold.woff2": (
        "bba47a7d1b185a4a2e0d265ec82aeac5dbc2dc9cb0f482a2ff57cbdfe4e5045d"
    ),
    "vendor/board/fonts/Cascadia/CascadiaCode-Regular.woff2": (
        "5bce041e0fcce617e35efbc335b1372d27a84acb65fb464c65ff80ab3424a885"
    ),
    "vendor/board/fonts/ComicShanns/ComicShanns-Regular-279a7b317d12eb88de06167bd672b4b4.woff2": (
        "fa930fceb529a4b51ba3055d590302722c27704f6a6357495873950136b1e2ea"
    ),
    "vendor/board/fonts/ComicShanns/ComicShanns-Regular-6e066e8de2ac57ea9283adb9c24d7f0c.woff2": (
        "782f24081b810b8de9f04da055007ce4f1445c18abbf096c831f8888d65935c1"
    ),
    "vendor/board/fonts/ComicShanns/ComicShanns-Regular-dc6a8806fa96795d7b3be5026f989a17.woff2": (
        "61c376d32e1773588f6163599aed24fe0387b5a5cb622bd200455f20c2b36a87"
    ),
    "vendor/board/fonts/ComicShanns/ComicShanns-Regular-fcb0fc02dcbee4c9846b3e2508668039.woff2": (
        "ee641c2e35730c3e6e3826855f1b6cc3b343a017c3e216e4e4a2e47010c53ad1"
    ),
    "vendor/board/fonts/Excalifont/Excalifont-Regular-349fac6ca4700ffec595a7150a0d1e1d.woff2": (
        "a0637cff365c7344d2f7a15905a1f74dceaf56b5ac2ff913d150358bed6921f0"
    ),
    "vendor/board/fonts/Excalifont/Excalifont-Regular-3f2c5db56cc93c5a6873b1361d730c16.woff2": (
        "18ea9be1dd6b5a67b057f44eba8e34cb290f88308c272bcf35c79c1a220d0d7b"
    ),
    "vendor/board/fonts/Excalifont/Excalifont-Regular-41b173a47b57366892116a575a43e2b6.woff2": (
        "c96649a5f96e2faa9143ee94e5b590b5afb08092f8d67c67f09cef332d66e4a7"
    ),
    "vendor/board/fonts/Excalifont/Excalifont-Regular-623ccf21b21ef6b3a0d87738f77eb071.woff2": (
        "b004007be100261e58188cf8a0f7e850e997702f35d57110915995cd880e49f1"
    ),
    "vendor/board/fonts/Excalifont/Excalifont-Regular-a88b72a24fb54c9f94e3b5fdaa7481c9.woff2": (
        "e4423b318e11432aff2e6e865e300b7ca270f92321a3f56632268fede01c1b48"
    ),
    "vendor/board/fonts/Excalifont/Excalifont-Regular-b9dcf9d2e50a1eaf42fc664b50a3fd0d.woff2": (
        "b424d16da4398be3e9852857a54ff70e043c8e2ec5c4444dbf7f88d7469b9859"
    ),
    "vendor/board/fonts/Excalifont/Excalifont-Regular-be310b9bcd4f1a43f571c46df7809174.woff2": (
        "4fb8ad69be9aeac5664d99e5aa3a68a27ea40c98943b80387f7cd55c034fcc93"
    ),
    "vendor/board/fonts/Liberation/LiberationSans-Regular.woff2": (
        "006a2b28cbbeeaec937d1b367d0949f5f48ef4b2e7b4e1d8dc7a799d2d639be8"
    ),
    "vendor/board/fonts/Lilita/Lilita-Regular-i7dPIFZ9Zz-WBtRtedDbYE98RXi4EwSsbg.woff2": (
        "2b6568c7a0f16300bc6c8d292bd426c361fcdb40699c2cbfab2bd3e9e6993dea"
    ),
    "vendor/board/fonts/Lilita/Lilita-Regular-i7dPIFZ9Zz-WBtRtedDbYEF8RXi4EwQ.woff2": (
        "8d6cd0f298738a92ca9bf6e13f54a9191afd06ce04ea00ebbf24499c017191b7"
    ),
    "vendor/board/fonts/Nunito/Nunito-Regular-XRXI3I6Li01BKofiOc5wtlZ2di8HDIkhdTA3j6zbXWjgevT5.woff2": (
        "e512f2d7c9fe062b1acf8f6ea64d4bfd89a77d26728150977627e445c7b6e937"
    ),
    "vendor/board/fonts/Nunito/Nunito-Regular-XRXI3I6Li01BKofiOc5wtlZ2di8HDIkhdTQ3j6zbXWjgeg.woff2": (
        "6d1626aac658786e37e78e0adce3ffffddbd75abc923d72e45a7168bd80053da"
    ),
    "vendor/board/fonts/Nunito/Nunito-Regular-XRXI3I6Li01BKofiOc5wtlZ2di8HDIkhdTk3j6zbXWjgevT5.woff2": (
        "3e7f76fcc8798b8bba4d6bcc06bdb0d788f6e89706c1d6e64ac81d65ca683c14"
    ),
    "vendor/board/fonts/Nunito/Nunito-Regular-XRXI3I6Li01BKofiOc5wtlZ2di8HDIkhdTo3j6zbXWjgevT5.woff2": (
        "7928f47b21192cb6fdb2f46b71a474a3bd2fd608945c5ad7d7ed6bde8b0ae24c"
    ),
    "vendor/board/fonts/Nunito/Nunito-Regular-XRXI3I6Li01BKofiOc5wtlZ2di8HDIkhdTs3j6zbXWjgevT5.woff2": (
        "5117398298dd0dba3c95a406ff12fbeb88289fa4e913e3a5cf482cafe7f7071a"
    ),
    "vendor/board/fonts/Virgil/Virgil-Regular.woff2": (
        "2279cf7daf1c42a3a53b2c1437ad72f0050efb67ac263e0245f67612fd766714"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-019d66dcad46dc156b162d267f981c20.woff2": (
        "7404ad8086cf238269c6f6aa3ecb754dac903c7b28be88d1f683b9eab7c463e6"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-04b718e5623574919c8b0dea5f301444.woff2": (
        "cfdf67b97d5eee43f06d4fd6c8d8a1136d66e3391c3500341cef41bf39672559"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-069e77aac84590e2e991d0a0176d34f2.woff2": (
        "9a8e521a24e531cbeadfbd2750108c48aef9c0f13b0c0840e2cc9fae3257aed4"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-06c77b8c66e51ed6c63ccb502dd8b8af.woff2": (
        "cc2e553954218cc43f5f6f7cf54136d4d0a4317b88d670ae1940797545bdec61"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-08e0dc436ad0ad61ba5558db0674d762.woff2": (
        "101960c709c4a146a181f002808861249fe13ede28fc2d9cff24175715191abc"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-093b9ef39a46ceae95a1df18a0a3a326.woff2": (
        "819bbcac2468e1b44363442bca0a7a894883c32ce5cf9b28f707d94304a8a3ed"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-095c169f3314805276f603a362766abd.woff2": (
        "7f05badb0edb31118c69aa6f9bd20b13e737b5719f816617d342a50dc51c9506"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-09850c4077f3fffe707905872e0e2460.woff2": (
        "4cd090f380a82950208e5ff0b83b63f82d9a998881dfa12a8b8a8e6c7fd298bf"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-0986d134c05864f5025962eef9f994a0.woff2": (
        "76cdc801e75369e01fe0907f0e603a699dccf1fa0d880a717b8dceb0c8751806"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-0b5d723fdc4e249c140f0909e87d03b4.woff2": (
        "4a5ab634a6839e983344645d82d31d85a1786fd4e81d344f8544181158a86c45"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-0f626226ba1272e832aea87bafd9720e.woff2": (
        "c261de5643a0b6fdfeb3fa6c43548dc6270f19756532c2953a4c8f74a1bb670d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-0f7fb1e0d5015bb1371343153ecf7ce3.woff2": (
        "7d92022e9005636c778ebccb89ca8ab3a17c7d99e17201c422ce833e4d4e0e04"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-0fa55a080fcd0f9dc2e0b0058b793df8.woff2": (
        "a3531eb15218eca282196405dca5e502a0b0ce14d82824ee5c3104e241f12c56"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-0facdf1ea213ba40261022f5d5ed4493.woff2": (
        "4692019d3f011ff2b8c012ef4d5bcb9d649259bb3e68b8d5eded06fd5f6948ff"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-10a7ae9a371830a80c3d844acf1c02d7.woff2": (
        "343e81a0225cf0848a0c5c3df11589aba1386a8ea954fa549dd8749384e54c15"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-112c051027b2d766c19a519f6ee1f4f7.woff2": (
        "64d5521ca4b191e41422c2a1e775aae43a831a720a96c64b42d70a2229e1bec7"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-11c345711937f0ba4b8f7b6b919c8440.woff2": (
        "cb5cc5380aa885e6ea2d9502f15673ffe1e96d08a8e240cadc8a9a9e727a85d2"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-12b52b58eb3df36804b9a654ec9ee194.woff2": (
        "d10137e035185e36212a9ec70ea8c2b0b10f30b175f9543f57412a6f48abfddf"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-13ae07ed2e272d26d59bc0691cd7117a.woff2": (
        "4f0db89e7086b5eef3a83dc41d81ee3d46f9d7ae761e150e5689684e95ceb022"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-13d2887ec8ee73c43acdabc52a05af7b.woff2": (
        "b8a3f1ac2323db2a1ef228f021be3d13b1190ed7c1f4af6b0d50d0c4d454adbc"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-145aa02cdd91946e67dc934e1acffe75.woff2": (
        "96f696dd01f2534ceca68a6c9795f927c7c1dd276f7e97069cc7d76d85c9156c"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-15dc6d811c9cd078f9086a740d5a1038.woff2": (
        "b62e88759e83e6898a7c7588f8587d064aba185b9e6b9b79c15c22c7b195225f"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-173945821411c09f70c95f98d590e697.woff2": (
        "ef5693cd230dcc67a120cf627d59d9b66acab7b8f356f9a36c3a1e2edf479f0a"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-1b611157cd46bb184d4fa4dae2d6a2b8.woff2": (
        "5b79154058f247d013a6561ba070b044911b3206e872ce8a2476392245f9de27"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-1e6fd68f1f3902ce48ce8c69df385622.woff2": (
        "7a1db91b8de7dee60467eafa390af5aa6e8ad313e6b6fb62c31e7a03ea26f385"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-1ee544f0f1dac422545c505baa788992.woff2": (
        "4c478c0638b0ede25838805d6f99badcc57fbf022e44562c9194c0c75118f1c1"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-1fdc0c67ed57263a80fd108c1f6ccf24.woff2": (
        "cdc443c022e8300966f91ec8b3185953579979dd5bffbcc66b498fe6b71e6c39"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-203b0e569e3b14aac86a003dc3fa523e.woff2": (
        "ddfc3cd617e83e271190a01fcfeb8625cd5ff9c105ca362c8804b7f762e595b2"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-20cc1bbf50e7efb442756cb605672c1f.woff2": (
        "27dce6f37362f4ad1495da86e9aa9ec808237d41d69d3f2f13d54ccbfe504c83"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-20e7bf72fa05de9adf7dbcc7bf51dde6.woff2": (
        "d9742a018d17fea1da72556502ef20d02b5cb80ddd571e539b60ce26cd19d4df"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-21430ee05a1248901da8d0de08744d47.woff2": (
        "d7d7130ac4a5ce5cf5f0a01f408267133cde16ef2966dfb35a9fc5a37f762cf8"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-23686f7f29da6e8008c36dd3a80c83d6.woff2": (
        "ab60b69500468e81eb3a6796b8e23560699e2de1be596956bd1832256fae9d44"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-23ad2d71b280f00b1363b95b7bea94eb.woff2": (
        "e8dec71d8ede1b9b9f3d7325e1ea31432474a83095719bd2149852fef4794ed5"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-23f228f3999c01983860012330e4be08.woff2": (
        "20a7232989878afcad6de84b68872facaf96fb85c6827f0457d532c1cbf6bbcc"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-24476a126f129212beb33f66853ea151.woff2": (
        "c0c147e916a32a86306f2f9864982d87fdf41b6a0c3d1ed77c2aabd2ed772a94"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-24a21c1e4449222e8d1898d69ff3a404.woff2": (
        "e49f1063fddb7928f50c6490912df4bce1042795bd372ce0673cc0d87beec559"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-25b7f38e18f035f96cb5e547bd2bd08c.woff2": (
        "a6ec4baa25d737b49526c152b69a98e66373e3ab2a337ca1c7a895d7a6d8b45b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-29cec36cd205b211da97acabaa62f055.woff2": (
        "9ab470df6c456a0a376521f52fc8a857849ad15c640b6c8fae020fbdeb0cde0b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-2a26d20a23b00898ce82f09d2ee47c3f.woff2": (
        "d85471a49823e8757af9c1c7d920fbfb4ccdb9660e06f7b43ea2b2325b438d0f"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-2adbc89c11e65905393d3dfc468b9d5b.woff2": (
        "896a9145f32c6db6eb6ccdff9986859f08e349f505628dd9eeff2139def675cc"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-2b7441d46298788ac94e610ffcc709b6.woff2": (
        "d0493edd4b70aab6e7c4d78be1d5976d2fb0113c486f20bc2114d8d0f8256f5d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-2b77e8ebfb2367ab2662396a60e7d320.woff2": (
        "6cba567589067e182dd06054fa24e52ce5e8ac8d07278861125e3b6f6d2d2a31"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-2cf96d082d35ea3d8106851223ad0d16.woff2": (
        "714626f97e78b4ee72f935408a48508e9f9855399429ba23dc0c9ef0f4074368"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-2d43040e86ff03ba677f6f9c04cd0805.woff2": (
        "cb1ac8f87c3e4c2e3dd925480e2b6c659100acd59266e957065708c5c46d7385"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-2e33e8dc771ef5e1d9127d60a6b73679.woff2": (
        "13941783dc59754f2bcdcd3c54e7bb665e433fc1ffe1cafa0e273ddcd4f62dbe"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-33432927cd87d40cfe393c7482bf221f.woff2": (
        "9b2a6cb479d1b3d2966a5beb93ba63f30d6b05e83c2985b3f9aab447d802a4b5"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-353f33792a8f60dc69323ddf635a269e.woff2": (
        "aee4567e54a8428067774cbcc9ca8faf2d9bbb10d0a780a374c950d286e1d8d3"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-36925dfe329a45086cbb7fc5c20d45ac.woff2": (
        "542fd6f0fb2eabcd2cfd79b0cecafb376691aa74afcfbf718c8d26160d0cd9ab"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-3717077e38f98d89eae729b6c14e56dc.woff2": (
        "34718d3ab2e153ea66994f9b20475c76bcbe61c73dcef6b7d4096f0eec2eef6b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-3756e81d3e149cf6099163ee79944fec.woff2": (
        "9db5961935f12cad41726aa81c08c9783b6c71ecaaf7dfea1e4b8f5768defb4d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-395c35dd584b56b0789f58a0559beaf1.woff2": (
        "8d6bb509725a27cd4cde2daaf743be8ec74114ad3719add4c805cd1c2fdd0f13"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-3c9de2ae0ea4bc91a510942dfa4be8d2.woff2": (
        "9c4e535e741d6a13ec36bb2909058db1321919cb14561e661c62be8f0ffda7f9"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-3cc70dbb64df5b21f1326cc24dee2195.woff2": (
        "71a3287e8998e47016b5159ae03f636939516b4f5b251ee71f0c5a5212e4df6b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-3e1f8f654357353bf0e04ba5c34b5f7f.woff2": (
        "8c3887fce9435759019e1ecba3ee315164d0f8a598ce24b94c8bfecebbb724a4"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-3e63ed8162808a9e425ed80a8bc79114.woff2": (
        "cfafe77006c4bbd7ece2231a735f8c219a3faa6831d4d5733ab14bd91779157a"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-3eaa538115d76932653c21d8dc28f207.woff2": (
        "04a7b1244c272a46ca740937e1a3e790e8ff142d6fcf53934be0e4a167eacdc9"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4095eb84ef3874e2600247bee0b04026.woff2": (
        "6ab6ac356c313d98d26c9a23e9b1d5239330974b090d8aba8184ce99c24fcf16"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-41521fade99856108931b4768b1b2648.woff2": (
        "2c096e4a843896d2d735c8f19b8e2e8e1293202d2e0a41adfab16995e32ae07e"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-450da755d5bcb70906e1295e559b9602.woff2": (
        "e07fdcb886eb2f90a49b22e3d369eb1f480fdef7c509249d0437ba95af3aedc6"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-452225341522a7942f0f6aab1a5c91a3.woff2": (
        "a432715f6fed8499a420cb44acfe8f71e600afe0aa340ff2e63b741fa05f6fae"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4535823663ad81405188a528d8f2b1a2.woff2": (
        "f3c72f094659af2e6fbdbb0e5dba91089c4f5990d479dd9bbe5fa5ae4f95ab41"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4806e761d750087c2d734fc64596eaff.woff2": (
        "29aa32e6f5a6f5cee931aed56aaedb8d974b2913c71591c605ce5729842497ba"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4a0fdb40036e87b40aa08dd30584cb85.woff2": (
        "9be1f8a2fd341f5058d483304063535b6ac5be04294846c89a0a946f7fb33f9d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4a38cc3e9cf104e69ba246d37f8cf135.woff2": (
        "b3aebf243c2be02360a1452ba3b0090d223faf7a76fde5f7495bf25cedfda5a1"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4aca6a43e59aceee2166b0c7e4e85ef1.woff2": (
        "cdf1f8c3ff1154762aa390d8babe5ee12450d7ad5cdc3a0b8e58a66dcb75b8ea"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4bfaa8ffa64c5ee560aa2daba7c9cbd3.woff2": (
        "0405baa428ee4175650f5ea85196d2e2950c3749da32c902f94381ee69b3b72c"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4dc6d5f188d5c96d44815cd1e81aa885.woff2": (
        "a206f427ff843dbdeaddc1509a92d7b69faebc24871c3138d94baf9b1922b335"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4ddc14ed3eb0c3e46364317dfc0144a3.woff2": (
        "033809b0d9848a6161681c8e9c5e7ee2bf10e0b639feb800eba43f87dd32a5c2"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4ee10ae43505e2e0bc62656ced49c0fa.woff2": (
        "a70dd8aa91b85b3a92f4edec64307dbeba67a3e829300abf38bb9ca206c318c5"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-4f50e5136e136527280bc902c5817561.woff2": (
        "59f4770cf5df29b54036ca5f42e6eafdd70d2dd28acbdcea6e8275c30d2208f0"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-51502f1206be09c565f1547c406e9558.woff2": (
        "27f0dd9bee1139710abf7ab0f51c0e37df058bef7f2e41a5f8f919dc0ce54fa7"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-51a0e808bbc8361236ac521a119758a3.woff2": (
        "6f7d20f47fee620a9b6cfac10202ee7350a21667bacd5398084d6781841e9d4c"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-52a84a22fd1369bffeaf21da2d6158dc.woff2": (
        "bb00bece144c8b55b92445e0bcf013c64eb7250588ebcd8d33d4e972feceba1d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-5330a2119a716e4e7224ed108b085dac.woff2": (
        "c3d926f24f41fa042951f940854f7094b394929ba2b393c1ad0f69b8ac1e18d7"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-543fa46ace099a7099dad69123399400.woff2": (
        "dc52bcf986f21c5ecf4f95614edc3fb086008bc4589a606a5545d7be83c6aae7"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-544fc28abe2c5c30e62383fd4dac255f.woff2": (
        "1822913d0e7128a0d6b8affd019c551f06023368ef8608aa3a9718320d16011c"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-54acdfc2166ad7fcbd074f75fd4a56ba.woff2": (
        "7c5ee14e16e509b359baef1bc7aed848475cf6286bd449a56e9afaaa0b5d8c5d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-5572b3513ba8df57a3d5d7303ee6b11b.woff2": (
        "3faaa516fc74d539ca46940b392648c9e3cc4c031e9ad52242aa8b54f7d9e2f8"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-56467a5c8840c4d23a60b2f935114848.woff2": (
        "0535fd40943797bebaab03292f269eeb09b9844e28ee0a9b20bafcfe6d6f253c"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-56a32a7689abd0326e57c10c6c069bb4.woff2": (
        "0861a51a85640834a936afe816dc9262712a3b5fceb9d268c3ef013131b7f1c9"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-57862b464a55b18c7bf234ce22907d73.woff2": (
        "2ab06e5e9dc995faad6240b1b1f6edee7e8865ecfd24aa041dad0bf71aab94f9"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-583d166e56ba0de4b77eabb47ef67839.woff2": (
        "fbc085efd02787634c45076cfcd3f9a943919b1fd491953bd3199338d3655681"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-5882ffa04f32584d26109137e2da4352.woff2": (
        "0bca2dd7bf9c581d3b9aa2485571f919272790e346a37ead6522d169b9af2d4a"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-58fd02350d0bc52cf1ca3bb32ce9766e.woff2": (
        "3f843a854c82af7042965ae7f00b6f8698a05e87bbce715cbe087a3ec47899ff"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-5935a5775af3d5c6307ac667bd9ae74e.woff2": (
        "c54f2d93aa1b89e19903c4abc3008742613dcc52b45055a058ec13de99d3a91f"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-59e9ff77b0efaf684bc09274fb6908c9.woff2": (
        "57426b72fb5d6a406df24e2b60c9b3f8d09c095aade40ae55c23972e5b7d9e1c"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-5a1ce3117cfe90c48e8fb4a9a00f694d.woff2": (
        "164d468fbdee79c04b3898c7e62959de3ff43729c407ba60f0f8f27974352292"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-5a45d991244d4c7140217e1e5f5ca4f4.woff2": (
        "b7bc4c809953df9d0ed98cf11a9542ba959ed3c66a94d3090bb466395f945184"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-5a7fac4b8b23a6e4e5ba0c9bf1756c91.woff2": (
        "2802ab8038ff5266fe6303d79665f0b6cf370361aa67b22639e10e1f4634726b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-5b0ed6971aaab9c8ad563230bd5471a7.woff2": (
        "ec7e47923ed16e54d1a25bf22ad0638846be78be6e8f46009649d9cf7727e58b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-5d2898fbc097a7e24c6f38d80587621e.woff2": (
        "9473d9ba649a6a614fb877bc6051590cb19da98e54ef6104d25f31f1ff52ed55"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-603aefd23e350ba7eb124273e3c9bcf1.woff2": (
        "cdd0e13364c932bb3cb2c9ae58369716ce47c8d55cbabe86585bf742e710fb5e"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-60a3089806700d379f11827ee9843b6b.woff2": (
        "d964d94147e46e90fd06f0e271a19aacde808acfffb23e66b896be7fcb359544"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-60a41c7e1c68f22424e6d22df544bc82.woff2": (
        "0534b52a8dbb84d43b13773bba5cd15bb6d256aa4fb96c58a435ebe391904f66"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-642b26e2e5f5fb780b51b593dbc8c851.woff2": (
        "961bf60030e7c8b4f5395cd8b748c6979d967b7629e6c582d68e5c11b5d2dfc8"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-66493ba5a8367f2928812f446f47b56a.woff2": (
        "3b759c89aca9110eddc48220362b6e1113bf8e390f40ddeaccade9bec603028e"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-670ba603758d94268e8606f240a42e12.woff2": (
        "1c0d75c27b530030e64e77d99967230f71d4ea9db40ecdc2e10a6f1eaf1f150d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-671a2c20b1eb9e4ef8a192833940e319.woff2": (
        "3dd77c6fa69a4f80de93433e2868e457b7d04d68662d8d042e6348e7b6ffaf15"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-684d65f1793cac449dde5d59cb3c47fb.woff2": (
        "079d03e554628ed20a18018875b1bb5681ae892dd56604074969619ebd9f36f2"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-69c09cc5fa3e55c74fc4821f76909cc3.woff2": (
        "dde445e5fe69c7ab363f034df157c89750b421f31e52757f7a642be3c5644f2a"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-6ae5b42180ad70b971c91e7eefb8eba2.woff2": (
        "dc1eea8f0592ee505adb539566a9cebefa81ece39c3e70624afd06af0e323729"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-6e092f71c1e634059ada0e52abadce67.woff2": (
        "9b771d7ec93aab9efc47096f33c16c724d57a462b99d54735080759cc56e749d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-6f3256af8454371776bc46670d33cc65.woff2": (
        "4d1cd583547d080c6b94552d1473685c60e6a96340c74a659616e16cdde6bbae"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-6fe5c5973cc06f74b2387a631ea36b88.woff2": (
        "7142a02b00a992b9a57b75c05ddfaa28956c11f554fb9f1e3fe0333285dd6791"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-70c2eb8d64e71a42a834eb857ea9df51.woff2": (
        "89474028dcdfc03b27f45d5adc112d9d53a4d810c7e8f0ebf95f599b50ae84ed"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-70e811fd7994e61f408c923de6ddd078.woff2": (
        "e6818bb44f7d47d16cd8935ed9f5b29b8f10ac0e80043f85957b80501f85bcd9"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-7197d6fda6cba7c3874c53d6381ca239.woff2": (
        "66b77ead39e2479ee3bf4911920fb5cf39217c6b60e3cd84080c7fe429432430"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-72252d73220fa3cd856677888cee1635.woff2": (
        "1e34a49b238d61c6a723e79c2957f309687bcca89eeaebe99221883f302b80a2"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-72536a3d71b694a0d53dd90ddceae41e.woff2": (
        "4058683136a7bf8ebaadc59aaba8644044eff3b91776f6b0206954bf805a3132"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-726303e0774b4e678bff8c2deb6ca603.woff2": (
        "1ad344f682254b66d56187f673f817fe1ac8117d59cb26614796ebba9f2494aa"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-733171b4ffcd17ea1fe1c0ba627173bf.woff2": (
        "af32f1c4a193d9866655c866d4067b190b121fdc538b380cd45e99f172b4355b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-739bc1a567439c7cffcd1614644593d2.woff2": (
        "9bbdcb718fa1010f13c0a6058cca89d75774371e766eb1edf319ac25dc057f0b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-73e309718fd16cea44b4d54a33581811.woff2": (
        "6507fb8092efc309d9fdcc301fab6c90330b89931e7007fe7ed7fd1e722a738b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-7494dc504ae00ee9cd0505f990f88c5d.woff2": (
        "df7f6131e265282b00981c86f4c8d0fbae08c8fa3b3464909609abdafebe5d03"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-74e2263a91439c25b91d5132ce9f4d62.woff2": (
        "322645e0e2737aed64e43e45e97276930f67ce5aa1bfe845eb6090b27b0acc29"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-761d05e3cd968cf574166867998ef06a.woff2": (
        "5da4dd830dbcb73f6228469f49b12a1cc3db1edf00e5875e3f530ba62c907538"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-7718fe60986d8b42b1be9c5ace5ccf25.woff2": (
        "ed029431ebadd93a1ffae89347ced2b6835ed92703613d10f3dcd3d37c3ce19b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-774d4f764a1299da5d28ec2f2ffe0d69.woff2": (
        "6238cf7ea6f5e88177688f281f55c8d427f0110ca56a0786d8577ab748fb5f8e"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-79d494361ae093b69e74ee9dbe65bfd4.woff2": (
        "6f78f52b32b85a686bae2602b6f4bd9197e508140a87c23eb38ced0fa501808e"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-79f007c1c6d07557120982951ea67998.woff2": (
        "ecd5160dfe937d1e8271e2c7efe45569c2d3f53c94f5d7acaf600434c758a423"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-7a07ddc0f0c0f5f4a9bad6ee3dda66b5.woff2": (
        "e1443444d7865b4650e51da424a02e2b58a75ce1d3b5d125b9d4424c0cc5cff7"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-7ab2bed91166a9dca83a5ebfbe2a7f38.woff2": (
        "3d692c33792626f84eaf48e73fb2c9dfd237ffd3a5b8a882d0f1e4a7301949c7"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-7ccce86603f80a099ddb0cb21d4ae3e3.woff2": (
        "ba4c2f8e126245571624eb841623e3a13a394fba39fc215c60786e786d4e4f35"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-7e4bde7e9c7f84cd34d8a845e384c746.woff2": (
        "934ad4e8abe120c1dba1ff0427a04bfa431ecf61e8402d93965fdfc000198e66"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-7e929f262f30c8ee78bf398150b1a7cd.woff2": (
        "03de58036f102830f522035221d660524c01ff4ad4240d6273d6cb0aa8ccbccc"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-7eb9fffd1aa890d07d0f88cc82e6cfe4.woff2": (
        "ccf132ae51caa20dd14896b26c829b3e45f676d933bcf91f8068bf658a4f2860"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-7f855356ab893b0d2b9c1c83b8116f0e.woff2": (
        "1060f535a62ceaa15c3709cc71d5ba65aa7b25ca9f175b039b692a2f36427c3a"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-829615148e6357d826b9242eb7fbbd1e.woff2": (
        "4252a1dcca7da24ba065620e30429e9a2de3ee8b5ec7cbfb672c8a008779fefc"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-866fa7613df6b3fd272bcfd4530c0bb9.woff2": (
        "a4a710432761544ce376c443375cd3c30212a7eac28fe3e02c38fb6a51b30808"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-87599f94b6cc129d505b375798d0d751.woff2": (
        "3cb8e87a420ef7412eac2b1e7acd90557e3133f5926ab23b307cdd35d3293f7c"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-8c2f33cee3993174f7e87c28e4bf42ee.woff2": (
        "6b01fcbbd25e0588b009efee8aa02c56315c13b071e42f27b3a1b9ded83b9f4b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-8d3bcabb847b56243b16afe62adaaf21.woff2": (
        "5a27b08a7634da4e22ef40981ff0b4376a5c667fe6d315013c8283660bb3c175"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-8de5b863cb50dfefdd07cb11c774d579.woff2": (
        "8dc7c9aa8d7932d8a62b0d7e556cb7a94c1bb1728a84d50a7952d75b9f159ae0"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-8e9f97f01034820170065b2921b4fb5e.woff2": (
        "e053872045b6e69fa2b374386a30985985700925ea09831eb411299d61cc48a5"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-8f476c4c99813d57cbe6eca4727388ad.woff2": (
        "018d866b5eef3c096a75a56f53df14e4f3ed2b69fc546178b61095736a0cf92e"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-903bb6865f3452e2fda42e3a25547bc5.woff2": (
        "4f9d77b306f63f69dc23f187858c688f20590a582f4d614f8934b8fcb31cc01b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-91ddb2969bf2d31ba02ad82998d1314c.woff2": (
        "b1c7f6ba934a56bb9ef3c85602de346d594654798004ebfed86cb68fdec23a9f"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-938d90c10ff8c20386af7f242c05d6b0.woff2": (
        "3d4cb06a72e43dcee4114ca345cfbb93c3a037cc27164cad3ecd979b0affc62c"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-93fc8f28a33234bcadf1527cafabd502.woff2": (
        "950ff7a9d636ca4b7684833ff898ca7b4c3d21072c322dd6ca482825aaf6f06d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-95429962233afd82db1c27df1500a28c.woff2": (
        "63474c129283d98a4426f2af09ce89e7e0fc0a8bf897a5423c3230d513a2816d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-9544732d2e62d1a429674f8ee41b5d3a.woff2": (
        "b67f74ffa8e1b8ea3b4617c3fa4c68408ec54e34cc532ec71031adc8b15c3343"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-9592bfc861f07bcb8d75c196b370e548.woff2": (
        "aae1aaab500a71b28d1ec96380b49ccaf7550d06b57db71e75737d17715f5261"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-95bfd249da4902577b4b7d76ebdd0b44.woff2": (
        "ce77aebaeee894d03a85f1d9ccecdb3447ab27f77eb8e3f685c961464119c5ef"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-968cffdc8ee679da094e77ebf50f58ef.woff2": (
        "7af27211f5491341d85b051445a7ddbc9e28bec438b8124a18783708fef68667"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-97f7f48ce90c9429bf32ae51469db74d.woff2": (
        "b61251e62c3ecd8d556201f75e5d46e48f1e7619e67ecad2c36690e8d5a9ed4f"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-982b630266d87db93d2539affb1275c6.woff2": (
        "64ffc06d05e0eb263f161fc3ab45747bc09c916c92195450600576b0867a43cf"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-98f2ad84457de7f3740d9920b8fa8667.woff2": (
        "20e0422c76332e759d6daabb96bfa9ccdc02be80b17e9928e3873683ae5b9995"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-99a16ef6a64934d5781933dbd9c46b2e.woff2": (
        "8bdc8f9731f8730a61043729f403e06e6b9bcfed2fb65e71f7fc421ae51b37b7"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-9cfb2a77a4e45025105ad29a1748b90d.woff2": (
        "8a83a1c27c1dc42db2653f37bb39fb95e3bb8180e99ea372766366c995248cee"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-9d81066dd2b337c938df6e90380a00dc.woff2": (
        "3e7444c5704402da829c61e1cdeee82c1867939590d9b25290793c58de548867"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-9eb5a99df4e76ac3363453ac9ca288b1.woff2": (
        "c464198e0903b1025c20e23e6df719933adbcea5f5e3b68fa67bd388e66dc738"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-a004ddfcb26e67bd6e678c8ed19e25ce.woff2": (
        "429f417980fd11df51d1b7c6e2481e65e2f1a2bf3f16531b3d189556b22be20d"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-a0ca5df4258213d7fc9fce80f65ce760.woff2": (
        "4577ce8d2f2161ddcbb02f1154a9899bc55a77d3a715a3650261c56839153ee6"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-a1f916d6039285c4ffb900cd654e418f.woff2": (
        "355bbd7b9ccfe3501325f51a8b14fcbac0a784b72b5629f7e54d28ab00060ea9"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-a203b91dad570bf05a58c3c3ddb529bf.woff2": (
        "664f032ffd3d67ac08c988cb9df5ed9e14ef1bd29b599085aae5ce019535b8f4"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-a365e82ed54697a52f27adcea1315fe8.woff2": (
        "7431d7c929b66ddef73132026c17ba2bdd1c749a2733b12b15a2bd53da10cee9"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-a4c34be6d42152e64b0df90bc4607f64.woff2": (
        "85c4fb4d97312f0260a0db861ddcd8e1f14cc031db4c0fa8bfff098114eed1a1"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-a7accba310e821da5505f71c03b76bdb.woff2": (
        "a630539dbb8b53eeef26d3c14c65e8aa65056e6ee70ed55d02bad8c6e2cc34b1"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-aa0d470430e6391eca720c7cfa44446f.woff2": (
        "9e7d4e67d3026e623581bbb8ffb9767e5cc8ae4208ea1ad9589e1c877a396ae0"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-aa5c9ca6cf4fba00433b7aa3fa10671a.woff2": (
        "a612a4deee08c188d304a75acfaa1a5f606ec0e959ee36572585a21d63a05767"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-ac9ceb44437becc3e9c4dbfebab7fc2d.woff2": (
        "88cdb98b8b47719cb34de6d927db93102845979729309ed7c9d81c2eca267d18"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-b1220a3c61f85cc0408deedb4c5f57a2.woff2": (
        "0c5b4365bec5a439a325347b8e373ce2d733e69a31227d9be1a6bfb1a81f8339"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-b358f7a51ece39a3247942b1feabdb29.woff2": (
        "da427d935e5286ddb7bc67bbddb53f19f21b1b22a08eb18b81202d6a0d7104ff"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-b57aaedfd8ebdf3931f25119dc6a5eb2.woff2": (
        "489368f841681abe8c4a10d8c04eb1f3dd7ca0431866ee6d998738cb5d953e94"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-b5c1596551c256e0e9cf02028595b092.woff2": (
        "53ddc8aec903cc6923e7bea4290f8c22748d8a9d3fef9310c8db45144b8602f5"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-b6d128682ee29e471486354d486a1b90.woff2": (
        "6fec9175d81ae947476b6e3d23d8a952b3f6f2abf3a236a16d3b2030aec8ee2f"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-b6fd38ca30869792244804b04bc058da.woff2": (
        "729394216762fa8e54dcf7d8a6623812c68ee868fa423cb150eb730fa142d5fe"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-b7d203b051eff504ff59ddca7576b6a9.woff2": (
        "012348fc81bad86733c10d93edc49b31fa98d161162a7b9c7cbe2d02f184e684"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-b96d9226ce77ec94ceca043d712182e6.woff2": (
        "55f3829b501cf10b929c86d8703be025941489012ae3a237a0f7d4bde532e316"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-ba3de316d63c7e339987b16f41a0b879.woff2": (
        "204c98c6d957da9076afe9318003ced09bd3b44a4ce8365b555337fdd574a04b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-bafff7a14c27403dcc6cf1432e8ea836.woff2": (
        "16a0b6a3f5462fc87a5043bb89dfaeab6070f9b15bf7bb33d74c6f1f0e945d7e"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-bd77e3c7f9e0b072d96af37f73d1aa32.woff2": (
        "762f6b930564a70639a89c41e38b55e4fe93753fe30063aee0577ee6a57b14ed"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-be549ab72f0719d606a5c01e2c0219b6.woff2": (
        "51f88e333f2a5b2ed3b6670c7b7ff122d16e6b0a09d0297ae4dec70de721bcf3"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-c16ed9740b85badf16e86ea782a3062f.woff2": (
        "2650a650f03c1d5736cc0f1d774f778639d4df2eee805766ec69410108996344"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-c1f94158256bb1f3bf665b053d895af9.woff2": (
        "0d5a4da13c00111ae6e921de8a5d3e7e407773ceb415771997a9e08d490fc603"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-c40533fdf4cc57177b12803598af7e59.woff2": (
        "bf57ac6c74823d2193e4fd637e0fef9f3cf65867dc573bf287cab541eee2862e"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-c4a687ac4f0c2766eefc9f77ed99cddf.woff2": (
        "fbd978baf163d2cf637804b525e7df206e0657d217d3d4339aa204ec584460a0"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-c69f61a4ab18d0488c8d1fc12e7028e8.woff2": (
        "1d7f8daeee505d3f7830d0c0c74af7af68abc2a0b01b2e92147a85239b72249f"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-c8b71798409ccc126ee264a00aadcf21.woff2": (
        "5e78c653a12851d95e2754fd01052e65c952fa2cc603e6e40a7c1a104fe366e8"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-c99eda15fc26a2941579560f76c3a5cf.woff2": (
        "234ca0a3ee22640185863709a91ed8574ee10cdc97103c74913ef9e7aae8b399"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-cb17fc3db95f6d139afc9d31a8e93293.woff2": (
        "366d1d64220e84149380d851590fa86decac06ab04af7b5d6b84b1593fbc03a7"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-cbaaefaaf326668277aa24dfa93c4d28.woff2": (
        "d912c56af1eb36e3cf58ee3fd05a54cfaa2474d721bf6ebbeafde0163c10d6b3"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-cd145ce4a0ea18469358df53c207bc1b.woff2": (
        "3ef057732d62da9572e373f78d282b218e0f5cb8e003d275adaef15abd2d2cbf"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-cdbce89e82cc1ab53a2decbf5819278f.woff2": (
        "fc2ac910be82739b996e39063360efcccc5b82f4c99a048d8655885afc3ef933"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-ce4884f96f11589608b76b726a755803.woff2": (
        "ea1d4e3f3fa68c9193a72d5df249986350e892091e3edac9025baf7e1759cdee"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-cf2cc71752631e579e35b0e423bf2638.woff2": (
        "ce5a8058ed21535db620e89a942f8d4fe33968cbb4557c26c4521c536d5e8c66"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-cf6ff4e0f491ca0cf3038187a997b9b4.woff2": (
        "996fd1d3fe4f0783b30ffec0912ea858cadf71a077eef41bcbb6f488aa1853ed"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-cfb211578629b7e8153b37240de6a9d5.woff2": (
        "d07bdf1bc149ae52b05fff010bf5a5c97da67d7bd612cd7975939102446da207"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-d0cf73942fea1c74edbdf0b3011f4656.woff2": (
        "e802ce5eb6306d4272a9188ba65c41423894ccaa71e679d1b8f53df3acf7298f"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-d2666cbed13462c5dc36fa2f15c202ca.woff2": (
        "6d157af83341041b1110824b12bcad95a1267c6088aadb7cfebc1fee77c81a27"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-d3716376641d615e2995605b29bca7b6.woff2": (
        "9b9fa9f96d1ed9160912efab73c4883324225fe1096e6aa6c6f5a534fcac292b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-dac48066b5883d8b4551fc584f0c2a3e.woff2": (
        "50fd052c2e0b0ebf8f304eae64965486d1721da85fbbce115683055f05785bf0"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-dbea1af6dcd9860be40c3d18254338f5.woff2": (
        "4bb0a8345e803f659aca813aadca25703c16bfddf5d89b82ddcec2a50e338471"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-e11567fd2accf9957cd0d3c2be937d87.woff2": (
        "dc6a09381a42ce3aa802d029bc61ed01b4302f0303a1defa8234f130dafa5428"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-e2ead7ea7da0437f085f42ffc05f8d13.woff2": (
        "b4648f5ae511c1d0d4b2aa75a72a547e8436aaa5f1ca15c042e722a9fa8c77f4"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-e3fcf5180fd466c8915c4e8069491054.woff2": (
        "29a56eb3d183f71ddb753f000c8bd902d1b9a35ca08ff2d403c6af19934661fa"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-e480d9c614742d05f0e78f274f1e69e6.woff2": (
        "0ceef1123d1f46d7c9434f5d7f1d3896ba4d1f1b0fe0076383d920ea8da94ec5"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-e4bca6cfa53e499cae0a6be4894a90e9.woff2": (
        "3cc764666e9c2c6483c73c911b3823d74ee680bae209fb3ddee6b976c599f9f1"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-e51ef413167c6e14e0c0fdcc585f2fc9.woff2": (
        "931c27e602508ea6d712803d5647dabb164438140e309fd0be4f15917943f480"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-e5f453bb04da18eed01675eeebd88bf8.woff2": (
        "ab6c969a6b7c481d03f26f68e77eec932b55232608c9b86d710c48f5c200f259"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-e656f091b9dc4709722c9f4b84d3c797.woff2": (
        "b90cfb35e64c7f9f5b7e1fb48d0c396c44f757349e5669e64a25d98806d642fc"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-ec181b795ac1fb5a50d700b6e996d745.woff2": (
        "6b18e2adafaad34cf0544c53915d65af94806ab621f5de6a60f718ef60c9cb1e"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-ee8bae97908d5147b423f77ad0d3c1bb.woff2": (
        "6ccb9581b563074b1e101d2f5ed1efe5c1c53e2406f23139543a139f81a4ae5f"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-f0f13b5c60e0af5553bd359f5513be1b.woff2": (
        "75eac42c2d875a86029c0a684b728abb0dc8baa56e503aaaacb1c457f0a01aa6"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-f2b54d4e7be0eaefe1c2c56836fa5368.woff2": (
        "da1a8fde33ea6a56d70b5c87b1a4030c3fe663772effcbe45019fde0b336d3dc"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-f56414bf9bced67990def8660e306759.woff2": (
        "ffb6a7253b90f6f82b90619935ab41e063db09686921f6dace943f5ec183b16b"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-f5d079153c99a25b9be5b8583c4cc8a7.woff2": (
        "b8ec5b4516ddc269a99f306b9d63ff0bdecd37a800736cd0eab28f254291b15e"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-f6032fc06eb20480f096199713f70885.woff2": (
        "937b4d5222bb42c0e190a3f40a1aa9f984edcaa9e8d275e9d4bf1d88ef74754c"
    ),
    "vendor/board/fonts/Xiaolai/Xiaolai-Regular-f8ee5d36068a42b51d0e4a1116cfcec1.woff2": (
        "ae083037bfb691cf96205e109a287255f87a45a90848fe2a63cb0b3e7af3e904"
    ),
}


def test_board_assets_are_exactly_the_reviewed_bytes_with_notices() -> None:
    for name, digest in BOARD_ASSETS.items():
        assert hashlib.sha256((STATIC / name).read_bytes()).hexdigest() == digest, name
    notice = (STATIC / "vendor/board/NOTICE").read_text()
    assert "MIT License" in notice and "SIL OPEN FONT LICENSE" in notice
    assert "Excalidraw 0.18.1" in notice and "bytes" in notice
    for file in (STATIC / "vendor/board").rglob("*.js"):
        text = file.read_text()
        assert "https://esm.sh/" not in text
