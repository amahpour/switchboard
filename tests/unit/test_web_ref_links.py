"""Reference links in the Markdown renderer (#790, DESIGN.md §60): an admin's rules turn `!42` and
`ABC-123` into links, in node against the same fake DOM as ``test_web_markdown.py``.

The rules come from the server (``/api/me``), never from a message, and the renderer treats them as
untrusted anyway: only an https:// template with a fixed host, only ``$1`` replaced, the match
URL-encoded, a reference only where a word starts, and bounded work. With no rules nothing changes.
Skipped where node is missing, except under SWITCHBOARD_REQUIRE_NODE=1 (CI), where that fails."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import node_for_tests
from test_web_markdown import assert_safe, by_cls, by_tag, harness

NODE, pytestmark = node_for_tests()

MR = {"pattern": r"!(\d+)", "url": "https://gitlab.example.com/group/project/-/merge_requests/$1"}
TICKET = {"pattern": r"([A-Z][A-Z0-9]+-\d+)", "url": "https://tracker.example.com/browse/$1"}


def render(text: str, *rules: dict[str, Any]) -> dict[str, Any]:
    [tree] = harness([{"text": text, "refLinks": list(rules)}])
    assert_safe(tree)
    return tree


def hrefs(tree: dict[str, Any]) -> list[str]:
    return [a["href"] for a in by_tag(tree, "a")]


def test_a_reference_becomes_the_ordinary_link_to_its_url() -> None:
    """`!42` is a link to the template with 42 in it, built as every link is: an a.md-link with the
    safe attributes, the matched text as its words and the real URL shown after it. Fails if the match
    isn't linked, links elsewhere, or skips the page's one link path."""
    tree = render("see !42 now", MR)
    [a] = by_tag(tree, "a")
    assert a["href"] == "https://gitlab.example.com/group/project/-/merge_requests/42"
    assert a["text"] == "!42" and a["cls"] == "md-link"
    assert [u["text"] for u in by_cls(tree, "md-url")] == [a["href"]]
    assert tree["text"].startswith("see !42") and tree["text"].endswith(" now")


def test_a_ticket_key_and_a_merge_request_link_together_each_to_its_own_template() -> None:
    tree = render("ABC-123 fixed by !7, not by XY-9.", MR, TICKET)
    assert hrefs(tree) == [
        "https://tracker.example.com/browse/ABC-123",
        "https://gitlab.example.com/group/project/-/merge_requests/7",
        "https://tracker.example.com/browse/XY-9",
    ]


def test_with_no_rules_nothing_changes() -> None:
    """The setting is optional: with none, `!42` and `ABC-123` stay text, as before. Fails if a link
    appears with no rule."""
    for text in ("see !42 and ABC-123 and #5", "https://example.com/#42"):
        with_none = render(text)
        assert hrefs(with_none) == (["https://example.com/#42"] if text.startswith("https") else [])


@pytest.mark.parametrize(
    "text",
    ["abc!42", "x_!42", "!42abc", "!42x", "!!"],
)
def test_a_match_inside_a_word_is_not_a_reference(text: str) -> None:
    """A reference starts a word and doesn't end in the middle of one: `abc!42` and `!42abc` are
    words, not references. Fails if a link is made out of part of a word."""
    assert hrefs(render(text, MR)) == []


def test_the_matched_text_is_url_encoded_and_nothing_else_is_replaced() -> None:
    """The match goes into the URL through encodeURIComponent, so it can't add a path segment, a query
    or a fragment, and only `$1` is replaced: `$&` and `$'` in a template stay what they are. Fails if
    the group is put in raw or the template is run through String.replace."""
    ref = {"pattern": r"ref:([A-Z]\/[A-Z])", "url": "https://t.example.com/x/$1/y?a=$1&b=$&c=$'"}
    [a] = by_tag(render("ref:A/B", ref), "a")
    assert a["href"] == "https://t.example.com/x/A%2FB/y?a=A%2FB&b=$&c=$%27"
    assert a["text"] == "ref:A/B"


@pytest.mark.parametrize(
    "rule",
    [
        {"pattern": r"!(\d+)", "url": "http://t.example.com/$1"},  # not https
        {"pattern": r"!(\d+)", "url": "https://$1.example.com/"},  # the match would pick the host
        {"pattern": r"!(\d+)", "url": "https://user:pw@t.example.com/$1"},  # credentials
        {"pattern": r"!(\d+)", "url": "javascript:alert(1)//$1"},
        {"pattern": r"!(\d+", "url": "https://t.example.com/$1"},  # does not compile
        {"pattern": r"!\d+", "url": "https://t.example.com/$1"},  # no group at all
        {"pattern": 5, "url": "https://t.example.com/$1"},
        {"pattern": r"!(\d+)"},
        None,
        "!(\\d+)",
    ],
)
def test_a_rule_the_renderer_does_not_trust_is_no_rule(rule: Any) -> None:
    """The server refused these when saving, but the renderer doesn't rely on it: each is dropped, and
    the text stays text. Fails if any makes a link (or an error)."""
    tree = render("!42 and !7", rule)
    assert hrefs(tree) == [] and tree["text"] == "!42 and !7"


def test_only_the_first_eight_rules_count() -> None:
    rules = [{"pattern": f"r{n}:(\\d+)", "url": f"https://t.example.com/{n}/$1"} for n in range(12)]
    got = hrefs(render(" ".join(f"r{n}:1" for n in range(12)), *rules))
    assert got == [f"https://t.example.com/{n}/1" for n in range(8)]


def test_a_code_span_a_link_label_and_a_url_are_left_alone() -> None:
    """References inside `code` stay code; inside a link's words they add no second link; inside a URL
    the URL stays whole. Fails if a link nests in a link, or code or a URL is cut up."""
    tree = render("`!42` and [see !42](https://a.example.com/x) and https://b.example.com/#!42", MR)
    assert hrefs(tree) == ["https://a.example.com/x", "https://b.example.com/#!42"]
    assert [c["text"] for c in by_cls(tree, "md-code")] == ["!42"]


def test_a_message_cannot_make_more_than_fifty_links() -> None:
    """A page of references is bounded: the first fifty become links, the rest stay text."""
    tree = render(" ".join(f"!{n}" for n in range(1, 101)), MR)
    assert len(hrefs(tree)) == 50 and tree["text"].count("!") == 100


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("many starts", "!1 " * 6000),
        ("digit runs", "1-" * 9000),
        ("no match to find", "ABC-" * 4000),
        ("one long run", "!" + "9" * 19000),
        ("underscored words", "a_" * 9000),
    ],
)
def test_pathological_input_is_rendered_quickly(name: str, text: str) -> None:
    """The work a message can cost is bounded: each position tries a short window, and the number of
    tries is capped. Fails if any of these takes seconds."""
    tree = render(text, MR, TICKET, {"pattern": r"([A-Z]+-\d+x)", "url": "https://t.example.com/$1"})
    assert tree["ms"] < 2000, (name, tree["ms"])
