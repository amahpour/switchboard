"""The pull and merge request links in a message (#816, DESIGN.md §37.16): ``reviews.pr_links``.

What these catch: a link that is not a pull or merge request being taken for one (an issue, a repository
page, a link a word only looks like); one pull request counted as two because it was copied with a tab or
a trailing mark on the end (that would ask for its review twice); and a message that opens more than a
few requests."""

from __future__ import annotations

import pytest

from switchboard import reviews

GH = "https://github.com/example/shop/pull/7"
GL = "https://gitlab.example.com/team/sub/shop/-/merge_requests/12"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (f"opened {GH}", [GH]),
        (f"opened {GH}.", [GH]),  # a full stop closes the sentence, it is not the link's
        (f"see ({GH}), and **{GH}**", [GH]),
        (f"{GH}/files {GH}/commits/abcdef0 {GH}#issuecomment-1 {GH}?diff=split", [GH]),  # one pull request
        (f"mr: {GL}/diffs", [GL]),
        (f"{GH} then {GL}", [GH, GL]),  # two, in the order written
        ("http://github.com/example/shop/pull/7", [GH]),  # the page is https
    ],
)
def test_a_pull_or_merge_request_link_is_found_once_in_its_one_form(text: str, want: list[str]) -> None:
    """Fails if a tab, a fragment, a query or a mark on the end makes a second link, or a link comes back
    in a form other than the one the board is keyed by."""
    assert reviews.pr_links(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/example/shop/issues/7",  # an issue
        "https://github.com/example/shop",  # a repository
        "https://github.com/example/shop/pull/0",  # no pull request 0
        "https://github.com/example/shop/pull/seven",
        "https://github.com/pull/7",  # no repository
        "https://gitlab.example.com/team/shop/merge_requests/12",  # no "/-/": not GitLab's address
        "https://user:pw@github.com/example/shop/pull/7",  # a login in the address
        "see example.com/pull/7 or the pull/7 page",  # a word, not a link
        "ftp://github.com/example/shop/pull/7",
        "https://example.com/pull/7",
        "",
    ],
)
def test_other_links_and_words_that_only_look_like_one_are_ignored(text: str) -> None:
    """Fails if anything but a recognised pull or merge request address counts."""
    assert reviews.pr_links(text) == []


def test_a_message_opens_at_most_a_few_requests() -> None:
    """A pasted list of links must not open a board for each. Fails if more than the limit come back."""
    text = " ".join(f"https://github.com/example/shop/pull/{n}" for n in range(1, 20))
    assert len(reviews.pr_links(text)) == reviews.MAX_LINKS_PER_MESSAGE
    assert reviews.pr_links(text, limit=2) == [f"https://github.com/example/shop/pull/{n}" for n in (1, 2)]


def test_the_canonical_address_is_the_same_for_every_way_of_writing_it() -> None:
    """The board's key and the link's key meet here. Fails if `/files` or `/diffs` is a different address."""
    assert reviews.canonical_url(GH + "/files") == GH == reviews.canonical_url(GH)
    assert reviews.canonical_url(GL + "/diffs") == GL
    assert reviews.canonical_url("https://github.com/example/shop") == ""
