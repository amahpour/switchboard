"""The web UI's Markdown renderer (src/switchboard/web/static/md.js), run for real in node against
a fake DOM (tests/web_md_harness.js).

Message text comes from agents that read untrusted input, so beyond "each construct renders"
these tests pin the security contract: raw HTML is text, images never render, only absolute
http(s) URLs become links and never to this switchboard page, the real URL is shown, and no
element except a.md-link ever gets an href. The performance cases feed pathological input and
require every render to finish quickly (the parser is meant to be linear). Skipped where node is
missing, except under SWITCHBOARD_REQUIRE_NODE=1 (CI), where that fails."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import node_for_tests

HARNESS = Path(__file__).resolve().parents[1] / "web_md_harness.js"
NODE, pytestmark = node_for_tests()  # a skip without node; a failure under SWITCHBOARD_REQUIRE_NODE=1

# Built at runtime so this file never holds the literal pseudo-scheme.
JS = "java" + "script:"

ALLOWED_TAGS = {
    "p",
    "br",
    "div",
    "span",
    "strong",
    "em",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "blockquote",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "a",
    "button",
}
TITLE_SCHEME = "Not a link: only http(s) URLs are followed"
TITLE_LOCAL = "Not a link: links to this switchboard page are never followed"
TITLE_CREDS = "Not a link: a URL with a user name or password can hide its real host"


# ------------------------------------------------------------------ helpers


def harness(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assert NODE is not None
    r = subprocess.run(
        [NODE, str(HARNESS)], input=json.dumps(cases), capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    out: list[dict[str, Any]] = json.loads(r.stdout)
    return out


def render(text: str, mentions: tuple[str, ...] = (), local_host: str = "") -> dict[str, Any]:
    [tree] = harness([{"text": text, "mentions": list(mentions), "localHost": local_host}])
    assert_safe(tree)
    return tree


def walk(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for c in node.get("children", []):
        yield from walk(c)


def elements(tree: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in walk(tree) if n["tag"] not in ("#text", "#fragment")]


def classes(node: dict[str, Any]) -> list[str]:
    return str(node.get("cls", "")).split()


def by_cls(tree: dict[str, Any], cls: str) -> list[dict[str, Any]]:
    return [n for n in elements(tree) if cls in classes(n)]


def by_tag(tree: dict[str, Any], tag: str) -> list[dict[str, Any]]:
    return [n for n in elements(tree) if n["tag"] == tag]


def top(tree: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in tree["children"] if c["tag"] != "#text"]


def assert_safe(tree: dict[str, Any]) -> None:
    """The invariants every render must keep, whatever the input."""
    for n in elements(tree):
        assert n["tag"] in ALLOWED_TAGS, n["tag"]
        if "href" in n:
            assert n["tag"] == "a" and classes(n) == ["md-link"], n
        if n["tag"] == "a":
            assert n["href"].startswith(("http://", "https://")), n["href"]
            assert n["rel"] == "noopener noreferrer nofollow" and n["target"] == "_blank"
            assert n["props"]["referrerPolicy"] == "no-referrer"
        assert set(n["attrs"]) <= {"role", "aria-level", "aria-label", "aria-hidden", "scope"}, n["attrs"]


def blocked(tree: dict[str, Any]) -> dict[str, Any]:
    """The one blocked link in a render, checked for shape; returns its text span."""
    assert not by_tag(tree, "a")
    [span] = by_cls(tree, "md-blocked")
    [pill] = by_cls(tree, "md-blocked-pill")
    assert pill["text"] == "link blocked" and pill["title"] == span["title"]
    return span


# ------------------------------------------------------------------ constructs (MarkdownSheet M1-M10)


@pytest.mark.parametrize(
    ("src", "cls", "level"),
    [
        ("# parse_port review", "md-h1", "3"),
        ("## Open questions", "md-h2", "4"),
        ("### Next step", "md-h3", "5"),
        ("#### four", "md-h3", "6"),
        ("###### six ##", "md-h3", "6"),
    ],
)
def test_headings_are_scaled_down(src: str, cls: str, level: str) -> None:
    [h] = top(render(src))
    assert h["tag"] == "div" and classes(h) == ["md-h", cls]
    assert h["attrs"] == {"role": "heading", "aria-level": level}
    assert h["text"] == src.lstrip("#").strip().rstrip("#").strip()


@pytest.mark.parametrize("src", ["#build", "#build is ready", "####### seven"])
def test_a_hash_without_a_space_is_a_paragraph(src: str) -> None:
    [p] = top(render(src))
    assert p["tag"] == "p" and classes(p) == ["md-p"] and p["text"] == src


def test_paragraph_keeps_line_breaks_and_splits_on_blank_lines() -> None:
    tree = render("one\ntwo\n\nthree")
    [p1, p2] = top(tree)
    assert [c["tag"] for c in p1["children"]] == ["#text", "br", "#text"]
    assert p1["text"] == "onetwo" and p2["text"] == "three"


def test_bold_italic_and_inline_code() -> None:
    tree = render("**Decided:** `0` means *pick a free port*, _really_.")
    [strong] = by_tag(tree, "strong")
    assert strong["text"] == "Decided:"
    assert [e["text"] for e in by_tag(tree, "em")] == ["pick a free port", "really"]
    [code] = by_cls(tree, "md-code")
    assert code["tag"] == "code" and code["text"] == "0"
    assert tree["text"] == "Decided: 0 means pick a free port, really."


@pytest.mark.parametrize("src", ["snake_case_name", "a_b_c and x_y", "a * b * c", "**not closed"])
def test_intraword_underscores_and_loose_stars_stay_literal(src: str) -> None:
    tree = render(src)
    assert not by_tag(tree, "em") and not by_tag(tree, "strong")
    assert tree["text"] == src


def test_code_span_is_literal_and_matches_run_lengths() -> None:
    tree = render("``a`b`` and `*x* <b>` and ``` ``` y")
    assert [c["text"] for c in by_cls(tree, "md-code")] == ["a`b", "*x* <b>", " "]  # all spaces: not stripped
    assert not by_tag(tree, "em")
    assert render("`unclosed")["text"] == "`unclosed"


def test_fenced_code_with_language_label_and_copy_button() -> None:
    tree = render("```python\ndef f(s: str) -> int:\n    return int(s)\n```\nafter")
    [pre, p] = top(tree)
    assert classes(pre) == ["md-pre"]
    [head, body] = pre["children"]
    assert classes(head) == ["md-pre-head"] and classes(body) == ["md-pre-body"]
    [lang, copy] = head["children"]
    assert classes(lang) == ["md-lang"] and lang["text"] == "python"
    assert copy["tag"] == "button" and classes(copy) == ["md-copy"] and copy["text"] == "Copy"
    assert copy["props"]["type"] == "button" and copy["attrs"] == {"aria-label": "Copy code"}
    assert body["tag"] == "pre" and body["props"]["tabIndex"] == 0
    [code] = body["children"]
    assert code["tag"] == "code" and code["text"] == "def f(s: str) -> int:\n    return int(s)"
    assert p["text"] == "after"


def diagram_case(text: str, **extra: Any) -> dict[str, Any]:
    [tree] = harness([{"text": text, "mentions": [], "localHost": "", "diagrams": True, **extra}])
    assert_safe(tree)
    return tree


def test_a_mermaid_block_offers_its_diagram() -> None:
    """A ```mermaid block keeps its code, label and Copy, and gains a "Show diagram" button that
    hands the block, its raw source and the button to diagram.js (issue #57). md.js never draws."""
    src = "flowchart LR\n  A[<b>raw</b>] --> B"
    tree = diagram_case(f"```mermaid\n{src}\n```", click=True)
    [pre] = top(tree)
    [head, body] = pre["children"]
    [lang, show, copy] = head["children"]
    assert lang["text"] == "mermaid" and copy["text"] == "Copy" and classes(copy) == ["md-copy"]
    assert show["tag"] == "button" and classes(show) == ["md-copy", "md-show-diagram"]
    assert show["text"] == "Show diagram" and show["props"]["type"] == "button"
    assert show["attrs"] == {}
    assert body["children"][0]["text"] == src  # the code is still there, as text
    assert tree["toggled"] == [{"box": "md-pre", "source": src, "button": "Show diagram"}]


def test_only_mermaid_blocks_get_the_button_and_only_with_diagram_js() -> None:
    def buttons(tree: dict[str, Any]) -> list[str]:
        return [b["text"] for b in by_tag(tree, "button")]

    assert buttons(diagram_case("```MerMaid\ngraph TD\n```")) == ["Show diagram", "Copy"]  # any case
    assert buttons(diagram_case("```python\nprint(1)\n```")) == ["Copy"]
    assert buttons(diagram_case("```\ngraph TD\n```")) == ["Copy"]  # no info word: "code"
    assert buttons(diagram_case("`mermaid` and ```mermaid inline```")) == []
    assert buttons(render("```mermaid\ngraph TD\n```")) == ["Copy"]  # no window.SBDiagram


def test_unclosed_fence_runs_to_the_end_and_keeps_markup_raw() -> None:
    [pre] = top(render("~~~\n# not a heading\n\n**raw** <b>\n"))
    assert pre["children"][0]["children"][0]["text"] == "code"  # default label
    assert pre["children"][1]["text"] == "# not a heading\n\n**raw** <b>\n"


@pytest.mark.parametrize(
    ("info", "label"),
    [
        ("sh", "sh"),
        ("c++", "c++"),
        ("", "code"),
        ("<img/src=x>", "imgsrcx"),
        ("x" * 40, "x" * 20),
    ],
)
def test_fence_language_label_is_filtered(info: str, label: str) -> None:
    [lang] = by_cls(render(f"```{info}\nx\n```"), "md-lang")
    assert lang["text"] == label


def test_triple_backticks_inside_a_line_are_inline_code_not_a_fence() -> None:
    tree = render("```ls -la``` then more\nsecond line")
    assert not by_cls(tree, "md-pre")
    assert [c["text"] for c in by_cls(tree, "md-code")] == ["ls -la"]


def test_ordered_list_keeps_its_start_number() -> None:
    [ol] = top(render("3. Reject non-digits\n4. Keep `0`\n5. Test each edge"))
    assert ol["tag"] == "ol" and classes(ol) == ["md-ol"] and ol["props"] == {"start": 3}
    assert [li["text"] for li in ol["children"]] == ["Reject non-digits", "Keep 0", "Test each edge"]
    ol1, ol2 = top(render("1. a\n2) b"))  # a new delimiter starts a new list
    assert "start" not in ol1["props"] and ol2["props"] == {"start": 2}


def test_nested_bullets_two_deep_and_a_blank_line_keeps_the_list() -> None:
    [ul] = top(render("- a\n  - b\n    - c\n\n- d\n* e"))
    assert ul["tag"] == "ul" and classes(ul) == ["md-ul"]
    assert [li["text"] for li in ul["children"]] == ["abc", "d", "e"]
    inner = [c for c in ul["children"][0]["children"] if c["tag"] == "ul"]
    [li_b] = inner[0]["children"]
    assert [c["tag"] for c in li_b["children"]] == ["#text", "ul"]


def test_task_list_box_stays_literal() -> None:
    [ul] = top(render("- [ ] todo\n- [x] done"))
    assert [li["text"] for li in ul["children"]] == ["[ ] todo", "[x] done"]


def test_blockquote_containing_a_list() -> None:
    [q] = top(render("> Picking this up:\n> - one\n> - two"))
    assert q["tag"] == "blockquote" and classes(q) == ["md-quote"]
    assert [c["tag"] for c in q["children"]] == ["p", "ul"]
    assert [li["text"] for li in q["children"][1]["children"]] == ["one", "two"]


def test_table_alignment_and_missing_and_extra_cells() -> None:
    src = "| input | result | note |\n|:--|:-:|--:|\n| `'8080'` | ok |\n| a \\| b | 2 | 3 | dropped |\nafter"
    tree = render(src)
    [wrap, p] = top(tree)
    assert classes(wrap) == ["md-table-wrap"] and wrap["props"]["tabIndex"] == 0
    [table] = wrap["children"]
    assert table["tag"] == "table" and classes(table) == ["md-table"]
    thead, tbody = table["children"]
    ths = thead["children"][0]["children"]
    assert [(th["text"], classes(th), th["attrs"]) for th in ths] == [
        ("input", ["md-al-l"], {"scope": "col"}),
        ("result", ["md-al-c"], {"scope": "col"}),
        ("note", ["md-al-r"], {"scope": "col"}),
    ]
    rows = [[td["text"] for td in tr["children"]] for tr in tbody["children"]]
    assert rows == [["'8080'", "ok", ""], ["a | b", "2", "3"]]
    assert p["text"] == "after"


def test_pipes_inside_code_spans_do_not_split_cells() -> None:
    tree = render("a | b\n--|--\n`x|y` | z")
    assert [td["text"] for td in by_tag(tree, "td")] == ["x|y", "z"]


def test_a_table_needs_a_matching_delimiter_row() -> None:
    [p] = top(render("a | b | c\n--|--\nx"))
    assert p["tag"] == "p"


@pytest.mark.parametrize("src", ["---", "***", "_ _ _", "  - - -"])
def test_thematic_break(src: str) -> None:
    [hr] = top(render(src))
    assert hr["tag"] == "hr" and classes(hr) == ["md-hr"]


def test_mention_only_when_the_broker_listed_it() -> None:
    tree = render(
        "@claude-1 add validation, then @Codex-1 review. cc @devin-1", mentions=("claude-1", "codex-1")
    )
    assert [m["text"] for m in by_cls(tree, "md-mention")] == ["@claude-1", "@Codex-1"]
    assert tree["text"].endswith("cc @devin-1")


def test_mention_inside_a_quote_is_highlighted() -> None:
    # the broker's MENTION_RE does not know Markdown, so a quoted mention does wake the agent
    tree = render("> @claude-1 add input validation", mentions=("claude-1",))
    [q] = top(tree)
    assert [m["text"] for m in by_cls(q, "md-mention")] == ["@claude-1"]


@pytest.mark.parametrize("src", ["mail a@b.com", "`@claude-1`", "@@b", "x@b", "\\@b"])
def test_not_a_mention(src: str) -> None:
    assert not by_cls(render(src, mentions=("b", "claude-1")), "md-mention")


def test_link_text_can_hold_inline_markup() -> None:
    tree = render("[**CI** `run`](https://example.com/ci)")
    [a] = by_tag(tree, "a")
    assert [c["tag"] for c in a["children"]] == ["strong", "#text", "code"]


# ------------------------------------------------------------------ injection


@pytest.mark.parametrize(
    "src",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<b>x</b>",
        "&amp; &lt;b&gt; &#106;",
        "<iframe src=x></iframe>",
        '<a href="/x">y</a>',
    ],
)
def test_raw_html_is_literal_text(src: str) -> None:
    tree = render(src)
    [p] = top(tree)
    assert p["tag"] == "p" and p["text"] == src
    assert all(c["tag"] == "#text" for c in p["children"])


@pytest.mark.parametrize(
    "src",
    [
        "![a](http://x/y.png)",
        '![alt *x*](https://example.com/a.png "t")',
        "![see https://example.com/z](https://example.com/y.png)",
        "![[inner](https://example.com/a)](https://example.com/b.png)",
    ],
)
def test_images_never_render_and_nothing_inside_becomes_a_link(src: str) -> None:
    tree = render(src)
    assert not by_tag(tree, "a") and not by_tag(tree, "em")
    assert tree["text"] == src


@pytest.mark.parametrize(
    "dest",
    [
        JS + "alert(1)",
        JS.upper() + "alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "java\nscript:alert(1)",
        "&#106;avascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "//evil.example.com",
        "/relative",
        "http://",
        "mailto:a@example.com",
        "",
    ],
)
def test_non_http_links_are_blocked(dest: str) -> None:
    tree = render(f"[x]({dest})")
    span = blocked(tree)
    assert span["text"] == "x" and span["title"] == TITLE_SCHEME


@pytest.mark.parametrize(
    "url",
    [
        "http://switchboard.localhost:7419/logout",
        "HTTP://SWITCHBOARD.LOCALHOST/",
        "http://switchboard.localhost./x",
        "http://a.switchboard.localhost/",
        "http://localhost/",
        "http://127.0.0.1:7419/",
        "http://127.1.2.3/",
        "http://2130706433/",
        "http://[::1]/",
        "http://[0:0:0:0:0:0:0:1]/",
        "http://0.0.0.0:7419/",
        "https://fpga-pi.example.com:7419/api",
    ],
)
def test_links_to_this_switchboard_are_blocked(url: str) -> None:
    host = "fpga-pi.example.com"
    tree = render(f"[x]({url})", local_host=host)
    assert blocked(tree)["title"] == TITLE_LOCAL
    [v] = harness([{"fn": "safeUrl", "raw": url, "localHost": host}])
    assert v == {"ok": False, "why": "local"}
    # a bare or angle-bracket URL takes the same path
    assert blocked(render(f"<{url}>", local_host=host))["title"] == TITLE_LOCAL


def test_a_good_link_shows_the_real_url() -> None:
    tree = render("[CI run](https://github.com/x)")
    [p] = top(tree)
    a, ext, url = p["children"]
    assert a["tag"] == "a" and classes(a) == ["md-link"] and a["text"] == "CI run"
    assert a["href"] == "https://github.com/x" and a["title"] == "https://github.com/x"
    assert a["rel"] == "noopener noreferrer nofollow" and a["target"] == "_blank"
    assert classes(ext) == ["md-ext"] and ext["attrs"] == {"aria-hidden": "true"}
    assert classes(url) == ["md-url"] and url["text"] == a["href"]


def test_link_title_and_angle_destination() -> None:
    tree = render('[a](https://example.com/p?q=1 "the title") and [b](<https://example.com/x y>)')
    assert [a["href"] for a in by_tag(tree, "a")] == [
        "https://example.com/p?q=1",
        "https://example.com/x%20y",
    ]


@pytest.mark.parametrize(
    ("src", "tail"),
    [
        ("<https://x.y/z>", ""),
        ("https://x.y/z).", ")."),
        ("(https://x.y/z)", ")"),
    ],
)
def test_autolinks_trim_trailing_punctuation(src: str, tail: str) -> None:
    tree = render(src)
    [p] = top(tree)
    [a] = by_tag(tree, "a")
    assert a["href"] == "https://x.y/z" and a["text"] == "https://x.y/z"
    assert not by_cls(tree, "md-url")  # the visible text already is the URL
    assert p["text"].endswith("https://x.y/z\u2197" + tail)


def test_bare_urls_keep_balanced_parens_and_skip_link_text() -> None:
    tree = render("see https://en.wikipedia.org/wiki/Foo_(bar), and [https://a.example](https://b.example)")
    hrefs = [a["href"] for a in by_tag(tree, "a")]
    assert hrefs == ["https://en.wikipedia.org/wiki/Foo_(bar)", "https://b.example/"]


@pytest.mark.parametrize(
    ("src", "url", "mentions"),
    [
        # Review finding: bare URLs used to be found only in the text left over after emphasis, code
        # spans and mentions had taken their pieces, so each of these linked somewhere else.
        (
            "see https://github.com/python/cpython/blob/main/Lib/__init__.py ok",
            "https://github.com/python/cpython/blob/main/Lib/__init__.py",
            (),
        ),
        (
            "https://www.npmjs.com/package/@types/node",
            "https://www.npmjs.com/package/@types/node",
            ("types",),
        ),
        ("https://x.com/a*b*c", "https://x.com/a*b*c", ()),
        ("https://x.com/a_b_/c`d", "https://x.com/a_b_/c", ()),  # a backtick ends it (URL_STOP)
        ("https://x.y/a\\_b", "https://x.y/a_b", ()),  # an escape inside keeps its character
    ],
)
def test_a_bare_url_is_taken_whole_before_inline_markup(
    src: str, url: str, mentions: tuple[str, ...]
) -> None:
    tree = render(src, mentions=mentions)
    [a] = by_tag(tree, "a")
    assert a["href"] == url and a["text"] == url
    assert not by_tag(tree, "strong") and not by_tag(tree, "em") and not by_cls(tree, "md-mention")
    assert not by_cls(tree, "md-url")  # the visible text is the whole URL


def test_emphasis_around_a_bare_url_still_wraps_it() -> None:
    tree = render("*https://x.y/z* and **https://x.y/w**, (see https://x.y/v).")
    assert [a["href"] for a in by_tag(tree, "a")] == ["https://x.y/z", "https://x.y/w", "https://x.y/v"]
    [em] = by_tag(tree, "em")
    [strong] = by_tag(tree, "strong")
    assert [c["tag"] for c in em["children"]][:1] == ["a"] and [c["tag"] for c in strong["children"]][:1] == [
        "a"
    ]
    assert tree["text"].endswith("https://x.y/v\u2197).")


@pytest.mark.parametrize(
    "url",
    [
        "https://a@b.example/",
        "https://github.com%2Fx@evil.example/",
        "https://github.com%2Famahpour%2Fswitchboard%2Factions@evil.example/",
        "https://user:secret@example.com/x",
        "https://:secret@example.com/",
    ],
)
def test_urls_with_a_user_name_or_password_are_blocked(url: str) -> None:
    """Review finding: userinfo makes the shown URL start with a trusted-looking host while the
    browser goes to the host after the '@'. Refused on every path: a link, an autolink, bare."""
    for src in (f"[CI run]({url})", f"<{url}>", f"see {url} ok"):
        tree = render(src)
        assert blocked(tree)["title"] == TITLE_CREDS, src
        assert not by_cls(tree, "md-url")
    [v] = harness([{"fn": "safeUrl", "raw": url, "localHost": ""}])
    assert v == {"ok": False, "why": "credentials"}


def test_idn_host_shows_its_punycode_form() -> None:
    tree = render("https://еxample.com")  # Cyrillic е
    [url] = by_cls(tree, "md-url")
    assert "xn--" in url["text"]
    [a] = by_tag(tree, "a")
    assert a["href"] == url["text"]


def test_a_link_inside_a_link_keeps_only_its_text() -> None:
    tree = render("[see <https://a.example/> now](https://b.example/)")
    [a] = by_tag(tree, "a")
    assert a["href"] == "https://b.example/" and a["text"] == "see https://a.example/ now"


def test_safe_url_api() -> None:
    out = harness(
        [
            {"fn": "safeUrl", "raw": " https://Example.com/a b ", "localHost": ""},
            {"fn": "safeUrl", "raw": JS + "x", "localHost": ""},
            {"fn": "safeUrl", "raw": "/x", "localHost": ""},
            {"fn": "safeUrl", "raw": "http://fpga-pi:7419/", "localHost": "FPGA-PI"},
        ]
    )
    assert out == [
        {"ok": True, "href": "https://example.com/a%20b"},
        {"ok": False, "why": "scheme"},
        {"ok": False, "why": "relative"},
        {"ok": False, "why": "local"},
    ]


# ------------------------------------------------------------------ limits and performance


def big_table(cols: int, rows: int) -> str:
    line = "|".join(["a"] * cols)
    return "\n".join([line, "|".join(["-"] * cols)] + [line] * rows)


PATHOLOGICAL = {
    "stars": "*" * 50000,
    "underscores": "_a" * 20000,
    "quotes": ">" * 10000 + "x",
    "bullets": "- " * 5000,
    "brackets": "[" * 20000,
    "backticks": "`" * 20000,
    "table": big_table(50, 500),
    "over_limit": "a" * 20001,
    "stars_under_limit": "*a" * 9000,
    "underscores_under_limit": "_a_ " * 4500,
    "bracket_parens": "[a](" * 4000,
    "link_spaces": "[a](b c" * 2500,
    "angles": "<http://a" * 2000,
    "nested_lists": "".join("  " * k + "- x\n" for k in range(100)),  # about 10k chars
    "many_lines": "a\n" * 9000,
    "code_runs": "`a``b```c" * 2000,
    "deep_pair": "*" * 9999 + "a" + "*" * 9999,
    "deep_many": ("*" * 32 + "a ") * 250 + ("a" + "*" * 32 + " ") * 250,
    # bare URLs are scanned inside the inline pass: one long run, and runs trimmed to nothing
    "bare_url_run": "https://x.y/" * 1600,
    "bare_url_trimmed": "https://.)*" * 1800,
    "bare_url_in_brackets": "[https://a.b/" * 1500,
}


def test_pathological_inputs_render_quickly() -> None:
    names = list(PATHOLOGICAL)
    out = harness([{"text": PATHOLOGICAL[n]} for n in names])
    for name, tree in zip(names, out, strict=True):
        assert tree["ms"] < 2000, (name, tree["ms"])
        assert_safe(tree)


def test_over_the_limit_is_one_plain_paragraph() -> None:
    src = "# x\n" + "*a* " * 5000 + "[l](https://example.com)"
    assert len(src) > 20000
    [p] = top(render(src))
    assert p["tag"] == "p" and p["children"] == [{"tag": "#text", "text": src}]


def test_nesting_stops_at_depth_eight() -> None:
    tree = render(">" * 10000 + "x")
    assert len(by_tag(tree, "blockquote")) == 8
    [p] = by_tag(tree, "p")
    assert p["text"] == ">" * 9992 + "x"


def test_table_limits() -> None:
    assert not by_tag(render(big_table(51, 1)), "table")  # too many columns: not a table
    tree = render(big_table(2, 510))
    [tbody] = by_tag(tree, "tbody")
    assert len(tbody["children"]) == 500
    assert top(tree)[1]["tag"] == "p"  # the rest is text


def test_emphasis_past_the_delimiter_cap_is_literal() -> None:
    src = "*a* " * 300
    tree = render(src)
    assert len(by_tag(tree, "em")) == 250  # 500 delimiter runs make 250 pairs
    assert tree["text"].count("*a*") == 50


@pytest.mark.parametrize("run", [100, 9999])
def test_inline_nesting_stops_at_sixteen_levels(run: int) -> None:
    tree = render("*" * run + "a" + "*" * run)
    assert len(by_tag(tree, "strong")) == 16  # 16 levels deep, then the stars stay literal
    rest = "*" * (run - 32)
    assert tree["text"] == rest + "a" + rest
    assert tree["ms"] < 2000


# ------------------------------------------------------------------ firstLine


@pytest.mark.parametrize(
    ("src", "max_", "want"),
    [
        ("**Next step:** flash once codex-1 signs off", 80, "Next step: flash once codex-1 signs off"),
        ("\n\n## Title *here*\nmore", 80, "Title here"),
        ("> - [the link](https://example.com) and `code`", 80, "the link and code"),
        ("```python\nprint(1)\n```", 80, "print(1)"),
        ("<b>x</b> &amp;", 80, "<b>x</b> &amp;"),
        ("word " * 40, 20, "word word word word…"),
        ("abcdefghij", 10, "abcdefghij"),
        ("abcdefghijk", 10, "abcdefghi…"),
        ("", 80, ""),
    ],
)
def test_first_line(src: str, max_: int, want: str) -> None:
    [out] = harness([{"fn": "firstLine", "text": src, "max": max_}])
    assert out["value"] == want
    assert len(out["value"]) <= max_
