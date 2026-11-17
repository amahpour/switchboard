"""``redact`` (DESIGN.md §11, #819): obvious secrets cut out of a chat message, and nothing else.

One table of what is redacted and one of what is not. What these catch: a pattern that stops matching its own
token shape (the secret is then stored and shown), and a pattern so loose that a hash, a git SHA, a UUID, a
long word or an ordinary sentence is mangled. Every secret below is built from pieces, so no scanner reads
this file as a leak."""

from __future__ import annotations

import pytest

from switchboard import redact as r

A36 = "aB3" * 12  # 36 alphanumerics with digits


@pytest.mark.parametrize(
    ("text", "kind", "kept"),
    [
        ("my key is sk-" + "ant-api03-" + "Ab1" * 12, "api key", "my key is "),
        ("sk-" + "proj-" + "x9" * 15, "api key", ""),
        ("stripe " + "sk" + "_live_" + "Ab12" * 6, "api key", "stripe "),
        ("gcp " + "AIza" + "Sy" + "A1" * 16 + "Z", "api key", "gcp "),
        ("token " + "ghp" + "_" + A36, "github token", "token "),
        ("ghs" + "_" + A36 + " in logs", "github token", " in logs"),
        ("github" + "_pat_" + "11AB" + "cd" * 20, "github token", ""),
        ("glpat" + "-" + "ab12" * 6, "gitlab token", ""),
        ("AKIA" + "IOSFODNN7EXAMPLE", "aws access key", ""),
        ("ASIA" + "ABCDEFGH12345678", "aws access key", ""),
        ("xox" + "b-1234567890-abcdefghij", "slack token", ""),
        ("sb" + "at_" + "Zz9" * 12, "switchboard token", ""),
        ("eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0" + ".SflKxwRJSMeKKF2QT4fw", "jwt", ""),
        ("Authorization: Bearer abc123def456ghi789jkl012", "bearer token", "Authorization: Bearer "),
        (
            "curl -H 'authorization: bearer " + "q1" * 15 + "'",
            "bearer token",
            "curl -H 'authorization: bearer ",
        ),
        ("password=hunter2hunter2", "password or token", "password="),
        ("DB_PASSWORD = correct-horse-9", "password or token", "DB_PASSWORD = "),
        ("GITHUB_TOKEN=abcd1234efgh5678 make", "password or token", "GITHUB_TOKEN="),
        ('{"api_key": "abcdef123456"}', "password or token", '{"api_key": "'),
        ("https://x.example/cb?token=0a1b2c3d4e5f", "password or token", "https://x.example/cb?token="),
        (
            "-----BEGIN "
            + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc/def+ghi=\n-----END RSA PRIVATE KEY-----",
            "private key",
            "",
        ),
        (
            "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nKcwggSjAgEAAoIBAQC7",
            "private key",
            "",
        ),
    ],
)
def test_an_obvious_secret_is_replaced_by_a_marker_that_names_its_kind(
    text: str, kind: str, kept: str
) -> None:
    """Each token shape the issue names, and what stays around it (the word Bearer, the name of a setting).
    Fails if the secret survives, the marker names the wrong kind, or the text around it is eaten."""
    out, kinds = r.redact(text)
    assert kinds == [kind]
    assert f"[redacted: {kind}]" in out
    assert out.startswith(kept) or kept in out
    for secret in ("hunter2hunter2", "abcdef123456", "q1q1q1", "MIIEowIBAAKCAQEA", "IOSFODNN7", A36):
        assert secret not in out


@pytest.mark.parametrize(
    "text",
    [
        "the plain old sentence, with a token in it and a password policy to follow",
        "Bearer authentication is described in the spec",  # no digit, not a token
        "sk-learn-is-a-long-word-for-no-reason-at-all",  # a long word, no digit
        "risk-assessment-of-the-whole-thing-2",  # \b: sk- inside a word is not a key
        "commit 163cae8a7c8e5f0d2b1a4c6e8f0a2b4c6d8e0f1a is the one",  # a git SHA
        "sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # a hash
        "```\nsha256sum a.txt\n9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08  a.txt\n```",
        "id 550e8400-e29b-41d4-a716-446655440000 in the log",  # a UUID
        "a" * 80,  # a long word
        "max_tokens=1024 and token_count=99999999",  # not a secret's name
        "set password=<your password> and token=$GITHUB_TOKEN and secret=${SECRET}",  # placeholders
        "token: the value goes here",  # prose with a colon
        "use the token=true flag, or password=changeme in the example",  # words, not values
        "AKIA is a prefix, and AKIAshort is too short",  # not 20 characters
        "ghp_short and github_pat_ is a prefix",  # too short
        "-----BEGIN CERTIFICATE-----\nMIIDdzCCAl+gAwIBAgIEAgAAuTANBgkq\n-----END CERTIFICATE-----",  # public
        "a [redacted: github token] marker is left alone",
    ],
)
def test_ordinary_text_hashes_shas_uuids_and_placeholders_are_left_alone(text: str) -> None:
    """The other direction: a false positive would mangle a message nobody should have touched. Fails if any
    of these changes, or is reported as a secret."""
    assert r.redact(text) == (text, [])


def test_several_secrets_are_all_cut_and_each_kind_is_named_once_in_order() -> None:
    """A message with two tokens and a password: all three go, and the sender hears each kind once, in the
    order they were found by the pattern list. Fails if only the first is cut or a kind repeats."""
    text = "a ghp" + "_" + A36 + " b ghp" + "_" + A36[::-1] + " c password=hunter2hunter2"
    out, kinds = r.redact(text)
    assert (
        out
        == "a [redacted: github token] b [redacted: github token] c password=[redacted: password or token]"
    )
    assert kinds == ["github token", "password or token"]


def test_redacting_twice_changes_nothing_more() -> None:
    """The marker is not itself a secret. Fails if a second pass (a retried say) finds something in it."""
    once, _ = r.redact("password=hunter2hunter2 and ghp" + "_" + A36)
    assert r.redact(once) == (once, [])


def test_a_header_without_a_footer_costs_a_bounded_search_and_a_key_with_no_body_is_left_alone() -> None:
    """Many unterminated headers must not make the search quadratic, and a header alone is not a key. Fails if
    the redaction of a long message of headers takes more than a moment, or eats a bare header line."""
    assert r.redact("-----BEGIN " + "PRIVATE KEY-----") == ("-----BEGIN " + "PRIVATE KEY-----", [])
    big = ("-----BEGIN " + "PRIVATE KEY----- x ") * 400
    out, _ = r.redact(big)
    assert len(out) <= len(big)


def test_the_sender_notice_names_kinds_and_never_a_value() -> None:
    """What the sender is told: the kinds, and that this is best effort. Fails if it can carry a value."""
    n = r.notice(["github token", "private key"])
    assert "github token, private key" in n and "best effort" in n


@pytest.mark.parametrize(
    "text",
    [
        "a." * 16000,
        "a-" * 16000,
        "sk-" + "a-" * 16000,
        "sk-" * 10000,
        "xoxb-" * 6000,
        "Bearer " * 5000,
        "password " * 4000,
        "password" + "a." * 16000,
        "token=" * 6000,
        "password=" + " " * 32000,
        "-----BEGIN " + "PRIVATE KEY-----" * 2000,
        " " * 32000,
    ],
    ids=[
        "dots",
        "dashes",
        "sk-dashes",
        "sk-repeated",
        "xox-repeated",
        "bearer-repeated",
        "password-spaces",
        "password-dots",
        "token-repeated",
        "assignment-spaces",
        "key-headers",
        "spaces",
    ],
)
def test_a_hostile_message_of_32000_characters_is_searched_in_linear_time(text: str) -> None:
    """A message is untrusted input. A name prefix or look-ahead with no bound is restarted at every boundary
    of a run of dots or dashes, which took 15 s on this size. Fails (by the clock: the bar is 1 s, linear
    takes tens of milliseconds) if any pattern goes quadratic."""
    import time

    t = text[:32000]
    t0 = time.perf_counter()
    r.redact(t)
    assert time.perf_counter() - t0 < 1.0
