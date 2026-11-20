"""A review board's text has its obvious secrets cut out where it is read (DESIGN.md §11, §37.2, #890).

``reviews.text`` reads every string a board takes from an agent or a person, so it is where ``redact`` runs:
a card's title, detail, place and options, a reason, a ticket, a proof, a link. What these catch: a field
that is stored as written (the secret then reaches the database, the page and the room), a link that keeps
a secret or is mangled into a broken one instead of refused, a ``where`` whose path is split at the marker's
own colon, a scan of text that is then refused as too long, and a marker that pushes a value past the
column's CHECK. Every secret is built from pieces, so no scanner reads this file as a leak."""

from __future__ import annotations

import pytest

from switchboard import reviews
from switchboard.reviews import Item, ReviewError

TOKEN = "ghp" + "_" + "aB3" * 12
PASSWORD = "password=" + "hunter2" * 2
GH = "[redacted: github token]"


def item(**kw: object) -> Item:
    base: dict[str, object] = dict(
        id=1, n=1, kind="finding", state="raised", title="t", detail="", file="", lines="",
        raised_by="codex-1", owner="", commit="", reason="", options=(), recommend=None,
        answer="", answered_by="",
    )  # fmt: skip
    base.update(kw)
    return Item(**base)  # type: ignore[arg-type]


def test_text_cuts_a_secret_out_and_keeps_the_rest() -> None:
    """The one reader every board field goes through redacts with say's patterns. Fails if a field is
    stored as written."""
    out = reviews.text(f"  leaked {TOKEN} and {PASSWORD} here ", "detail", reviews.MAX_DETAIL)
    assert out == f"leaked {GH} and password=[redacted: password or token] here"
    assert reviews.text("nothing to see", "title", reviews.MAX_TITLE) == "nothing to see"
    assert reviews.text(out, "detail", reviews.MAX_DETAIL) == out  # a marker is never redacted again


def test_text_over_the_limit_is_refused_before_anything_searches_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """As for say: the length is checked first, so a huge value costs a length check and not a scan. Fails
    (the patched redact raises) if redact runs on text that is then refused."""

    def boom(text: str) -> None:
        raise AssertionError("redact ran on text over the limit")

    monkeypatch.setattr(reviews, "redact", boom)
    with pytest.raises(ReviewError, match="too long"):
        reviews.text("x" * (reviews.MAX_TITLE + 1), "title", reviews.MAX_TITLE)


def test_a_marker_that_would_overflow_the_column_is_refused_and_names_no_value() -> None:
    """A card's column has the field's limit as a CHECK (§37.3), and a marker can be longer than what it
    replaced: a value under the limit that redaction lengthens past it is refused with what to do, never
    stored to fail the CHECK. Fails if the longer value comes back (the store would then raise)."""
    value = "x" * (reviews.MAX_TITLE - len(PASSWORD) - 1) + " " + PASSWORD
    assert len(value) <= reviews.MAX_TITLE
    with pytest.raises(ReviewError, match="secret") as e:
        reviews.text(value, "title", reviews.MAX_TITLE)
    assert "hunter2" not in str(e.value) and "password or token" in str(e.value)


@pytest.mark.parametrize(
    "link",
    [
        f"https://example.com/shop/pull/7?access_token={'hunter2' * 2}",
        f"https://example.com/{TOKEN}/pull/7",
    ],
)
def test_a_link_that_holds_a_secret_is_refused(link: str) -> None:
    """A board's URL is its identity and a proof link is drawn as a link: a marker in either would make a
    broken link, so one that holds a secret is refused, saying the kind and never the value. Fails if the
    link is stored with the secret, or redacted into a link that no longer opens."""
    for read in (reviews.url, reviews.proof):
        with pytest.raises(ReviewError, match="looks like a secret") as e:
            read(link)
        assert "hunter2" not in str(e.value) and TOKEN not in str(e.value)
    assert reviews.url("https://example.com/shop/pull/7") == "https://example.com/shop/pull/7"


def test_a_proof_that_is_a_test_name_is_redacted() -> None:
    """A proof that is not a link (a test's name) is text like any other. Fails if it is stored as written."""
    assert reviews.proof(f"test_checkout with {TOKEN}") == f"test_checkout with {GH}"


def test_where_splits_the_place_before_redacting_its_path() -> None:
    """The marker has a colon in it, and ``where`` splits a place at its last colon: the path and the lines
    are split first and each is read on its own. Fails if the secret stays in the path, or if the marker's
    colon is taken for the one before the lines (refused as bad lines)."""
    assert reviews.where(f"shop/{TOKEN}.py:40-58") == (f"shop/{GH}.py", "40-58")
    assert reviews.where(f"shop/{TOKEN}.py") == (f"shop/{GH}.py", "")


def test_options_and_an_edit_are_redacted() -> None:
    """A question's or a fix's options, and an edit's title, detail and place. Fails if any is kept as
    written."""
    assert reviews.options([f"use {TOKEN}", "rotate it"]) == (f"use {GH}", "rotate it")
    changes = reviews.edit_changes(
        item(),
        "codex-1",
        {"title": f"t {TOKEN}", "detail": PASSWORD, "where": f"a/{TOKEN}:3"},
        by_person=False,
    )
    assert changes == {
        "title": f"t {GH}",
        "detail": "password=[redacted: password or token]",
        "file": f"a/{GH}",
        "lines": "3",
    }


def test_a_link_with_credentials_is_refused_for_a_url_and_a_proof() -> None:
    """A password has no token's shape, so ``user:password@host`` passes every pattern and used to be stored
    and shown as a link (#890). Fails if either reader keeps it, or if the refusal repeats the password."""
    for link in ("https://alice:hunter2@example.com/shop/pull/7", "https://alice@example.com/shop/pull/7"):
        for read in (reviews.url, reviews.proof):
            with pytest.raises(ReviewError, match="user name or password") as e:
                read(link)
            assert "hunter2" not in str(e.value)
    assert reviews.proof("https://ci.example.com/run/7?a=b@c") == "https://ci.example.com/run/7?a=b@c"
    assert reviews.url("https://example.com/shop/pull/7@x") == "https://example.com/shop/pull/7@x"


def test_scrub_cuts_secrets_and_link_credentials_and_is_idempotent() -> None:
    """``scrub`` is for text that is only shown or posted, whatever an older row holds. Fails if a token or
    a link's password survives, if it is not stable when applied twice, or if a plain link changes."""
    out = reviews.scrub(f"see https://alice:hunter2@example.com/x and {TOKEN}")
    assert "hunter2" not in out and TOKEN not in out and GH in out
    assert reviews.scrub(out) == out
    plain = "https://example.com/shop/pull/7 by @bob"
    assert reviews.scrub(plain) == plain


def test_where_refuses_a_secret_that_spans_its_colon() -> None:
    """A pattern that reaches across the colon (a quoted name, then its value) is in neither half, so the
    halves alone keep it, and the board draws them joined again. Fails if the value is stored as lines."""
    for bad in ('"token":' + "12345678", "password=" + "12345678" + ":40"):
        with pytest.raises(ReviewError, match="across its colon") as e:
            reviews.where(bad)
        assert "12345678" not in str(e.value)
    assert reviews.where("src/app.py:40-58") == ("src/app.py", "40-58")
    with pytest.raises(ReviewError, match="across its colon"):
        reviews.edit_changes(item(), "codex-1", {"where": '"token":' + "12345678"}, by_person=False)


def test_scrub_cuts_a_links_whole_authority_before_a_marker_can_hide_the_rest() -> None:
    """A token inside ``user:pass@`` becomes a marker with a space in it, and a space ends the credentials
    match, so what followed the token was kept. The credentials go first. Fails if any part survives."""
    out = reviews.scrub(f"see https://bob:{TOKEN}:hunter2@example.com/x")
    assert "hunter2" not in out and TOKEN not in out and "example.com/x" in out
    assert reviews.scrub(out) == out


def test_credentials_go_up_to_the_last_at_sign_as_a_url_parser_reads_them() -> None:
    """``user:p@ss@host`` has its credentials up to the last ``@``, and a pattern that stopped at the first
    left ``ss@host`` in the link. Fails if any of the password is kept by ``scrub`` or ``clean_url``, or if
    a link without credentials, or an ``@`` in its path, changes."""
    link = "https://al" + "ice:p@" + "ss@example.com/shop/pull/7"
    assert reviews.clean_url(link) == "https://example.com/shop/pull/7"
    assert "ss@" not in reviews.scrub(f"see {link}") and "example.com/shop/pull/7" in reviews.scrub(link)
    plain = "https://example.com/shop/pull/7@x?a=b@c"
    assert reviews.clean_url(plain) == reviews.scrub(plain) == plain


def test_clean_url_keeps_a_link_a_link_and_is_stable() -> None:
    """The upgrade's URL is read back by ``url`` (no space in a marker), so a cleaned board still opens by
    it; applying it again changes nothing. Fails if the cleaned URL is refused or changes on a second run."""
    cleaned = reviews.clean_url(f"https://example.com/shop/pull/7?access_token={PASSWORD}&x={TOKEN}")
    assert cleaned == "https://example.com/shop/pull/7?access_token=[redacted]&x=[redacted]"
    assert reviews.url(cleaned) == cleaned and reviews.clean_url(cleaned) == cleaned
