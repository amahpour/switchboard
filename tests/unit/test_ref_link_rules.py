"""What an admin may save as a reference-link rule (#790, DESIGN.md §60): a small, linear pattern with one
group and an https:// template with a fixed host. Pure functions, no broker."""

from __future__ import annotations

import re

import pytest

from switchboard import ref_links as rl


@pytest.mark.parametrize(
    "pattern",
    [
        r"!(\d+)",
        r"#(\d+)",
        r"([A-Z][A-Z0-9]+-\d+)",
        r"!(\d{1,6})",
        r"ref:([A-Z]\/[A-Z])",
        r"MR-(\w+)",
        r"(x)\d+",
        r"\#(\d+)",
    ],
)
def test_the_patterns_people_will_want_are_accepted(pattern: str) -> None:
    assert rl.check_pattern(pattern) == pattern


@pytest.mark.parametrize(
    ("pattern", "why"),
    [
        ("", "text"),
        (None, "text"),
        (5, "text"),
        (r"\d+", "needs one"),
        (r"(\d+)(\d+)", "exactly one"),
        (r"((\d+))", "exactly one"),
        (r"(a+)+", "can't be repeated"),
        (r"(\d+)*", "can't be repeated"),
        (r"(a|b)", "not allowed"),
        (r"a|(b)", "not allowed"),
        (r"(?:a)(b)", r"no \(\?"),
        (r"(?<n>a)", r"no \(\?"),
        (r"^(\d+)", "not allowed"),
        (r"(\d+)$", "not allowed"),
        (r".(\d+)", "not allowed"),
        (r"(.+)", "not allowed"),
        (r"(\d+", "never closed"),
        (r"(\d+))", "has to close"),
        (r"()", "can't be empty"),
        (r"(\d*)", "match nothing"),
        (r"(a?)", "match nothing"),
        (r"(\d+)+x", "can't be repeated"),
        (r"(\d++)", "one repeat"),
        (r"(\d+?)", "one repeat"),
        (r"(\d{1,999})", "a count is"),
        (r"(\d{3,1})", "low to high"),
        (r"(\d{x})", "a count is"),
        (r"(\b\d+)", "backslash"),
        (r"(\1)", "backslash"),
        (r"(\p{L}+)", "backslash"),
        (r"([^a]+)", "a \\[class\\]"),
        (r"([a-]+)", "a \\[class\\]"),
        (r"([z-a]+)", "low-to-high"),
        (r"([\d]+)", "a \\[class\\]"),
        (r"([]+)", "a \\[class\\]"),
        (r"(\d+)+", "can't be repeated"),
        ("(" + "a+" * 4 + ")", "too many repeats"),
        ("(" + "a?" * 7 + "b)", "too many repeats"),
        ("(" + "a" * 70 + ")", "longer than"),
        ("(١+)", "ASCII"),
        ("(a\n)", "ASCII"),
        (r"(\d+)\x41", "backslash"),
    ],
)
def test_a_pattern_that_could_backtrack_or_differ_between_engines_is_refused(
    pattern: object, why: str
) -> None:
    """Matching runs in the viewer's browser, which can't be stopped: the pattern is a small subset that
    means the same everywhere and has no shape that blows up. Fails if any of these shapes is accepted,
    or is refused for another reason than the one named."""
    with pytest.raises(rl.RuleError, match=why):
        rl.check_pattern(pattern)


def test_a_pattern_that_takes_too_long_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second lock: the pattern is tried on long awkward input against a time limit. With the limit
    at nothing every pattern is too slow, and the refusal says so. Fails if the timing is not applied."""
    monkeypatch.setattr(rl, "PROBE_LIMIT_S", -1.0)
    with pytest.raises(rl.RuleError, match="takes too long"):
        rl.check_pattern(r"!(\d+)")


def test_the_accepted_patterns_match_the_same_way_in_python_as_in_the_page() -> None:
    """The subset is chosen so Python and a browser read it alike; what the page will find is what this
    says. A spot check of the group and the edges on the examples of the issue."""
    got = re.compile(r"!(\d+)", re.ASCII).search("see !42.")
    assert got is not None and got.group(0) == "!42" and got.group(1) == "42"
    assert re.compile(r"([A-Z][A-Z0-9]+-\d+)", re.ASCII).fullmatch("ABC-123")


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.example.com/g/p/-/merge_requests/$1",
        "https://tracker.example.com/browse/$1",
        "https://example.com:8443/x?id=$1#top",
        "https://example.com/$1/and/$1",
    ],
)
def test_https_templates_with_a_fixed_host_are_accepted(url: str) -> None:
    assert rl.check_template(url) == url


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("", "text"),
        (None, "text"),
        ("http://example.com/$1", "https://"),
        ("//example.com/$1", "https://"),
        ("ftp://example.com/$1", "https://"),
        ("javascript:alert(1)//$1", "https://"),
        ("HTTPS://example.com/$1", "https://"),
        ("https://example.com/", r"contain \$1"),
        ("https://example.com/$2", "only \\$1"),
        ("https://example.com/$&", "only \\$1"),
        ("https://example.com/$$1", "only \\$1"),
        ("https://$1.example.com/", "host"),
        ("https://example.com$1", "host"),
        ("https://user@example.com/$1", "not allowed|host"),
        ("https://user:pw@example.com/$1", "not allowed|host"),
        ("https://example.com/a b/$1", "not allowed"),
        ('https://example.com/"/$1', "not allowed"),
        ("https://example.com/<x>/$1", "not allowed"),
        ("https://example.com/\\$1", "not allowed"),
        ("https://examäple.com/$1", "not allowed"),
        ("https://example.com/$1$1$1$1", "at most three"),
        ("https://example.com:99999/$1", "port"),
        ("https://_bad_.example.com/$1", "plain host"),
        ("https:///x/$1", "host"),
        ("https://example.com/" + "x" * 100 + "$1", "longer than"),
    ],
)
def test_a_template_that_could_send_a_reader_elsewhere_is_refused(url: object, why: str) -> None:
    """The host is fixed text of the admin's: no `$1` in it, no credentials, no other scheme, and no
    `$` but `$1`. Fails if a message could choose where the link goes."""
    with pytest.raises(rl.RuleError, match=why):
        rl.check_template(url)


def test_a_list_is_checked_whole_and_each_error_names_its_rule() -> None:
    good = {"pattern": r"!(\d+)", "url": "https://example.com/$1"}
    assert [r.pattern for r in rl.check_rules([good])] == [r"!(\d+)"]
    assert rl.check_rules([]) == []
    with pytest.raises(rl.RuleError, match="rule 2: .*https://"):
        rl.check_rules([good, {"pattern": r"#(\d+)", "url": "http://example.com/$1"}])
    with pytest.raises(rl.RuleError, match="twice"):
        rl.check_rules([good, good])
    with pytest.raises(rl.RuleError, match="at most 8"):
        rl.check_rules(
            [{"pattern": f"r{n}:(\\d+)", "url": f"https://e.example.com/{n}/$1"} for n in range(9)]
        )
    for bad in (None, {}, "x", [1], [{"pattern": r"!(\d+)"}], [{**good, "extra": 1}]):
        with pytest.raises(rl.RuleError):
            rl.check_rules(bad)


def test_a_rule_must_fit_the_row_it_is_kept_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each rule is one broker_settings row of at most 200 characters (the schema). The largest pattern and
    URL allowed fit by design (60 and 100 characters, at most 196 as JSON); this is the lock behind that
    arithmetic: were a limit raised without the row, a rule is refused when saved, never cut when stored.
    Fails if a rule too long for its row is accepted."""
    biggest = {"pattern": "(" + r"\-" * 29 + ")", "url": "https://example.com/" + "p" * 78 + "$1"}
    assert len(biggest["pattern"]) == rl.PATTERN_MAX and len(biggest["url"]) == rl.TEMPLATE_MAX
    [rule] = rl.check_rules([biggest])
    assert len(rl._encode(rule)) <= rl.ROW_MAX
    monkeypatch.setattr(rl, "ROW_MAX", len(rl._encode(rule)) - 1)
    with pytest.raises(rl.RuleError, match="too long to keep"):
        rl.check_rules([biggest])
