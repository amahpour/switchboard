"""The safe query and the snippet reader (DESIGN.md §58, #724): pure functions, no index.

What these catch: a person's text reaching the index as syntax (an operator, a column filter, a stray quote
would raise an error or change what matches), a query with no limit on its size, and a snippet whose
highlight offsets point past the text or are counted in the wrong unit."""

from __future__ import annotations

import sqlite3

import pytest

from switchboard import search


@pytest.mark.parametrize(
    ("typed", "made"),
    [
        ("migration rollback", '"migration" "rollback"*'),
        ("  Migration   ", '"Migration"*'),
        ('"schema 49" migr', '"schema 49" "migr"*'),
        ('migration "rollback path"', '"migration" "rollback path"'),  # a phrase last: no prefix
        ("a", '"a"'),  # one letter is not a prefix match on the whole index
        ("rollback-plan", '"rollback" "plan"*'),  # a dash is a separator, as the index holds it
        ("café", '"café"*'),
        ("“quoted phrase”", '"quoted phrase"'),  # curly quotes close a phrase too
        ('"one"', '"one"*'),  # a phrase of one word is a word
        ('unclosed "quote here', '"unclosed" "quote" "here"*'),
    ],
)
def test_what_is_typed_becomes_quoted_terms(typed: str, made: str) -> None:
    """Each word is a quoted term, terms are ANDed (a space), a phrase is one quoted term of its words and
    only the last bare word is a prefix. Fails if a word is passed through bare or the phrase is split."""
    assert search.match_query(typed) == made


@pytest.mark.parametrize(
    "hostile",
    [
        "NEAR(a b)",
        "a OR b",
        "a AND NOT b",
        "-b",
        "col:x",
        "text : x",
        "^start",
        "a*",
        "*",
        "(",
        ")",
        "a (b",
        '"',
        '""',
        '"" ""',
        'a"b"c"',
        "'; DROP TABLE messages; --",
        "\x00\x01 a \x02",
        "\\",
        "{ text }: a",
        "a +b",
        "NOT",
        "OR OR OR",
        "a b c d e f g h i j k l m n o p q r s t u v w x y z",
        "x" * 5000,
        "\U0001f600 \U0001f600a",
        "‮ rtl ‬",
        "é café",
    ],
)
def test_nothing_a_person_types_is_ever_index_syntax(hostile: str) -> None:
    """Whatever is typed, the index accepts the made query: run against a real FTS5 table, it never raises.
    Fails if an operator, a column filter, a quote or a control character reaches the index as syntax."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='unicode61 remove_diacritics 2')")
    con.execute("INSERT INTO t(text) VALUES('near or and not a b text col x')")
    q = search.match_query(hostile)
    if q is not None:
        con.execute("SELECT rowid FROM t WHERE t MATCH ?", (q,)).fetchall()
        assert q.count('"') % 2 == 0
        assert all(part.startswith('"') for part in q.replace('" "', '"\x00"').split("\x00"))


def test_operators_are_words_and_not_operators() -> None:
    """``near or x`` finds the line that has those three words: NEAR and OR are matched as words, not read as
    an operator. Fails if OR makes it a union (it would match lines with either) or NEAR raises."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='unicode61 remove_diacritics 2')")
    con.executemany("INSERT INTO t(text) VALUES(?)", [("near or x",), ("only near",), ("only x",)])
    q = search.match_query("near OR x")
    assert [r[0] for r in con.execute("SELECT rowid FROM t WHERE t MATCH ?", (q,))] == [1]


@pytest.mark.parametrize("empty", ["", "   ", "---", "()", '""', "*", "\x00", "_", None, 5, b"x"])
def test_a_query_without_a_word_is_none(empty: object) -> None:
    """No searchable word: None, which the route answers with no results rather than an error."""
    assert search.match_query(empty) is None


def test_the_query_is_capped_at_200_characters_and_12_terms() -> None:
    """Fails if a long paste is searched whole: 200 characters, then at most 12 words, a phrase's included."""
    words = [f"w{i}" for i in range(30)]
    made = search.match_query(" ".join(words))
    assert made is not None
    assert len(made.split(" ")) == 12
    assert search.match_query("a" * 300) == '"' + "a" * 64 + '"*'  # one word, and a word is cut to 64
    long = " ".join(["abcdefghi"] * 40)  # 400 characters, 40 words
    assert len(search.terms_of(long)) <= 200 // 10 + 1
    tail = "x " * 99 + '"late phrase here"'  # the phrase starts after character 200
    assert all(t != "late phrase here" for t, _ in search.terms_of(tail))
    assert (
        len(search.terms_of('"a b c d e f" g h i j k l m n o')) == 7
    )  # the phrase uses six of the 12... and a-f
    assert sum(len(t.split(" ")) for t, _ in search.terms_of('"a b c d e f" g h i j k l m n o')) == 12


def test_the_words_of_a_query() -> None:
    assert search.words_of('"schema 49" Migration -x') == ["schema", "49", "Migration", "x"]


def test_snippet_ranges_are_offsets_in_the_cleaned_text() -> None:
    """The index's markers become ranges and disappear from the text; white space collapses to single spaces
    before the ranges are counted. Fails if the markers stay in the text or the offsets drift after a
    newline."""
    o, c = search.markers("n1")
    raw = f"…the {o}roll{c}back\n\n  plan {o}now{c} …"
    text, marks = search.parse_snippet(raw, "n1")
    assert text == "…the rollback plan now …"
    assert [text[a:b] for a, b in marks] == ["roll", "now"]


def test_snippet_offsets_count_utf16_units() -> None:
    """A page counts string offsets in UTF-16 units, so an emoji before a hit is two. Fails if offsets are
    code points (the highlight would land one character early)."""
    o, c = search.markers("n")
    text, marks = search.parse_snippet(f"\U0001f600 {o}hit{c}", "n")
    assert marks == [[3, 6]]
    page = text.encode("utf-16-le")
    assert page[marks[0][0] * 2 : marks[0][1] * 2].decode("utf-16-le") == "hit"


def test_text_that_looks_like_a_marker_does_not_make_a_highlight() -> None:
    """The markers carry a per-call nonce, so a message that holds control characters cannot fake a
    highlight. Fails if a fixed marker were used."""
    text, marks = search.parse_snippet("\x02x\x03evil\x03x\x02 plain", "other")
    assert marks == []
    assert "evil" in text


def test_a_long_snippet_is_cut_and_its_ranges_stay_inside() -> None:
    """Fails if a range reaches past the cut text (a highlight of nothing or of the ellipsis)."""
    o, c = search.markers("n")
    raw = "x" * 300 + f" {o}tail{c}"
    text, marks = search.parse_snippet(raw, "n")
    assert len(text) <= search.SNIPPET_MAX and text.endswith("…")
    assert marks == []
    raw = f"{o}head{c} " + "y" * 300
    text, marks = search.parse_snippet(raw, "n")
    assert marks == [[0, 4]] and len(text) <= search.SNIPPET_MAX


def test_control_characters_are_dropped_from_a_snippet() -> None:
    o, c = search.markers("n")
    text, marks = search.parse_snippet(f"a\x1b[31m {o}b{c}‮", "n")
    assert "\x1b" not in text and "‮" not in text
    assert [text[a:b] for a, b in marks] == ["b"]
