"""A hosted call's key and the claims made about it (#623, DESIGN.md §54): pure functions."""

from __future__ import annotations

import hashlib

import pytest

from switchboard.meeting import keys

KEY = "sbc_" + "0123456789abcdef" * 4


def test_the_hash_covers_the_whole_key_and_the_fingerprint_is_its_first_eight_characters() -> None:
    """The relay prints the same eight characters the call bar shows, so both must be derived the same way
    from the same string: the whole key, ``sbc_`` included. Fails if the prefix is left out of the hash, or
    the fingerprint is cut anywhere else."""
    want = hashlib.sha256(KEY.encode()).hexdigest()
    assert keys.key_hash(KEY) == want
    assert keys.key_hash(KEY[len(keys.PREFIX) :]) != want  # the prefix is part of what is hashed
    assert keys.fingerprint(want) == f"{want[:4]}·{want[4:8]}"
    assert keys.fingerprint("7f3a91c2" + "0" * 56) == "7f3a·91c2"


def test_a_key_has_its_own_prefix_and_is_not_an_agent_token_or_a_listener_key() -> None:
    """A secret scanner can spot ``sbc_``, and nothing that opens another route has the same shape, so a
    key given where a token belongs (and the reverse) is not taken. Fails if the prefix collides."""
    from switchboard.broker import agent_tokens, listener_keys

    assert keys.PREFIX not in (agent_tokens.PREFIX, listener_keys.PREFIX)
    assert keys.KEY_RE.fullmatch(KEY)
    assert not agent_tokens.TOKEN_RE.fullmatch(KEY) and not listener_keys.KEY_RE.fullmatch(KEY)
    assert not keys.KEY_RE.fullmatch("sbl_" + "0" * 64) and not keys.KEY_RE.fullmatch("sbat_" + "0" * 43)


@pytest.mark.parametrize(
    "bad",
    [KEY, "A" * 64, "g" * 64, "0" * 63, "0" * 65, "", " " + "0" * 63, "0" * 64 + "\n", None, 5, b"0" * 64],
)
def test_only_a_sha256_in_hex_is_taken_as_a_key_hash(bad: object) -> None:
    """The hash is a claim from outside: 64 lower-case hex characters only. A key itself (which must never
    reach the server) is not one, and neither is a hash with padding, a newline or other case. Fails if
    anything else is kept as a hash."""
    assert keys.hash_in(bad) is None
    assert keys.hash_in("ab" * 32) == "ab" * 32


def test_a_platform_is_one_of_four_and_anything_else_is_refused() -> None:
    """A hosted call may not know which platform it is on, so ``unknown`` is a value; a value this code
    does not know is refused, not kept as one it does. Case and padding are forgiven, nothing else. Fails
    if an unknown name is accepted."""
    assert keys.PLATFORMS == ("zoom", "meet", "teams", "unknown")
    assert [keys.platform_in(p) for p in ("zoom", " Meet ", "TEAMS", "unknown")] == [
        "zoom",
        "meet",
        "teams",
        "unknown",
    ]
    for bad in ("", "webex", "zoom.us", "google meet", None, 3, ["zoom"]):
        assert keys.platform_in(bad) is None, bad


def test_a_model_name_is_text_cleaned_and_cut() -> None:
    """The model is the host's claim about itself: control characters go, runs of space collapse, and it is
    cut to 80 characters. A value that is not text is no model. Fails if it is kept raw or unbounded."""
    assert keys.model_in("large-v3-turbo") == "large-v3-turbo"
    assert keys.model_in("a\x1b[31m  b\n c") == "a[31m b c"
    assert len(keys.model_in("m" * 500)) == keys.MODEL_MAX == 80
    assert keys.model_in(None) == "" and keys.model_in(7) == ""
