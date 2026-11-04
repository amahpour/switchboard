"""A claim is also what its holder watches (DESIGN.md §47, #541): the pure matching.

A message that names one of an agent's live claims is addressed to that agent, the same as an
@mention of it. This file is the rule alone: when a text names a key (the token boundary), and
which memberships a message addresses (live claims, the sender, the sender's kind). Delivery is
``test_engine_claim_watch.py``; the whole path through a broker is ``test_claim_watch_http.py`` in
``tests/integration``.
"""

from __future__ import annotations

import pytest
from conftest import FakeClock

from switchboard import claims
from switchboard.claims import Claim, fold, names_key, watchers

PR_URL = "https://github.com/owner/repo/pull/12"


def named(text: str, key: str) -> bool:
    """Whether a message with this text names the key (what ``watchers`` asks of each claim)."""
    return names_key(fold(text), key)


# (key, text, whether the text names the key as a whole token). Each row is one decision the
# boundary rule makes; the first column of comments says what the row is about.
BOUNDARY = [
    # a merge request number, the case in the issue: a longer number is another request
    ("!56", "!56", True),
    ("!56", "please review !56 today", True),
    ("!56", "!567", False),
    ("!56", "!56a", False),
    ("!56", "(!56)", True),
    ("!56", "is !56 ready?", True),
    ("!56", "CI on !56: green.", True),
    ("!56", "!56, !57", True),
    ("!56", "!56's pipeline", True),
    ("!56", "!567 and !56", True),
    ("!56", "!567 and x!56", False),
    # the same number in another project is another request
    ("!56", "x!56", False),
    ("!56", "vlc-cli!56", False),
    # a ticket: case does not matter, a longer id or a prefix is another ticket
    ("SYSINT-645", "SYSINT-645", True),
    ("SYSINT-645", "sysint-645 is blocked", True),
    ("SYSINT-645", "SYSINT-6450", False),
    ("SYSINT-645", "XSYSINT-645", False),
    ("SYSINT-645", "OLD-SYSINT-645", False),
    ("SYSINT-645", "SYSINT-645-fix", False),
    ("SYSINT-645", "SYSINT-645_old", False),
    ("SYSINT-645", "SYSINT-645.md", False),
    ("SYSINT-645", "SYSINT-645: done", True),
    ("SYSINT-645", "done with SYSINT-645.", True),
    ("SYSINT-645", "SYSINT-645...", True),
    ("SYSINT-645", "SYSINT-645!", True),
    # Markdown around a key does not hide it (no word character, no joiner before a letter)
    ("SYSINT-645", "[SYSINT-645]", True),
    ("SYSINT-645", "**SYSINT-645**", True),
    ("SYSINT-645", "`SYSINT-645`", True),
    ("SYSINT-645", '"SYSINT-645"', True),
    # a link to the ticket is its own token, not the key
    ("SYSINT-645", "https://jira.example.com/browse/SYSINT-645", False),
    # a project-qualified merge request
    ("vlc-cli!56", "merged vlc-cli!56.", True),
    ("vlc-cli!56", "vlc-cli!56", True),
    ("vlc-cli!56", "vlc-cli!567", False),
    ("vlc-cli!56", "other-vlc-cli!56", False),
    ("vlc-cli!56", "vlc-cli!56/diffs", False),
    # a repository-qualified pull request
    ("owner/repo#12", "owner/repo#12", True),
    ("owner/repo#12", "see owner/repo#12, please", True),
    ("owner/repo#12", "owner/repo#12.", True),
    ("owner/repo#12", "owner/repo#123", False),
    ("owner/repo#12", "xowner/repo#12", False),
    ("owner/repo#12", "github.com/owner/repo#12", False),
    # a bare number is not a number of some other repository
    ("#12", "see #12", True),
    ("#12", "#123", False),
    ("#12", "owner/repo#12", False),
    # a URL to a pull request is a token of its own: its pages and its numbers are not it
    (PR_URL, f"look at {PR_URL}", True),
    (PR_URL, f"<{PR_URL}>", True),
    (PR_URL, f"({PR_URL}).", True),
    (PR_URL, f"{PR_URL}.", True),
    (PR_URL, f"{PR_URL}3", False),
    (PR_URL, f"{PR_URL}/files", False),
    (PR_URL, f"{PR_URL}#issuecomment-1", False),
    (PR_URL, f"{PR_URL}?diff=split", False),
    # a branch: a longer branch, a file in it, or a remote's copy of it is another token
    ("feat/x", "pushed feat/x", True),
    ("feat/x", "`feat/x`", True),
    ("feat/x", "on feat/x, then", True),
    ("feat/x", "feat/x/", True),
    ("feat/x", "feat/xy", False),
    ("feat/x", "feat/x-2", False),
    ("feat/x", "feat/x.py", False),
    ("feat/x", "feat/x/y", False),
    ("feat/x", "myfeat/x", False),
    ("feat/x", "refs/heads/feat/x", False),
    ("feat/x", "origin/feat/x", False),
    # a key with spaces: any white space between its words is one space
    ("release 2.1", "the release 2.1 is out", True),
    ("release 2.1", "the release   2.1 is out", True),
    ("release 2.1", "the release\n2.1 is out", True),
    ("release 2.1", "release 2.10", False),
    # compared as the agent is shown it: NFKC, zero-width characters dropped, case folded
    ("SYSINT-645", "ＳＹＳＩＮＴ－６４５", True),
    ("SYSINT-645", "SYS​INT-645", True),
    ("Ärger-1", "ärger-1 is open", True),
    # a key with no letter or digit has no token to be: it never matches
    ("!!", "!! wow", False),
    ("-", "a - b", False),
    ("_", "_", False),
]


@pytest.mark.parametrize(("key", "text", "expected"), BOUNDARY)
def test_a_key_is_named_only_as_a_whole_token(key: str, text: str, expected: bool) -> None:
    """The token boundary (DESIGN.md §47). A letter, a digit or ``_`` beside the key continues it, and
    so does ``- . / # ! : @ ? & = % + ~`` when a letter, digit or ``_`` follows it on the far side;
    anything else (white space, brackets, quotes, commas, backticks, a sentence's last ``.``) ends
    it. Fails if ``!56`` is found in ``!567``, ``SYSINT-645`` in ``SYSINT-6450``, a key in a longer
    path or in another project's number, or a key at the end of a sentence is missed."""
    assert named(text, key) is expected


def test_the_text_is_scanned_up_to_a_cap_and_no_further() -> None:
    """Bounded work for a caller that does not cap the text. A key before the cap is found, one
    after it is not, whichever way it is asked; the cap is far past a say's own (4,000 by default).
    Fails if a text of any length is searched whole."""
    pad = "x " * (claims.SCAN_MAX // 2)
    held = [Claim("!56", 1, "claude-1", 1e12)]
    assert named("!56 " + pad, "!56") and watchers("agent", "!56 " + pad, held, 0.0) == {1}
    assert not named(pad + " !56", "!56") and watchers("agent", pad + " !56", held, 0.0) == frozenset()


def live(*pairs: tuple[int, str], until: float) -> list[Claim]:
    return [Claim(key, mid, f"agent-{mid}", until) for mid, key in pairs]


def test_a_message_addresses_the_holders_of_the_live_claims_it_names() -> None:
    """Only the holders whose key is named, each once, however many of its claims are named.
    Fails if the whole room is addressed, a holder is listed for a key the text lacks, or the
    ids are those of the claims instead of the memberships."""
    clock = FakeClock()
    held = live((1, "owner/repo#12"), (1, "!56"), (2, "SYSINT-645"), (3, "build"), until=clock.now() + 60)
    assert watchers("agent", "owner/repo#12 and !56 are red", held, clock.now()) == {1}
    assert watchers("agent", "SYSINT-645 and !56", held, clock.now()) == {1, 2}
    assert watchers("agent", "nothing here, owner/repo#123, !567", held, clock.now()) == frozenset()
    assert watchers("agent", "run the BUILD again", held, clock.now()) == {3}


def test_a_claim_is_live_until_its_deadline_and_not_at_it() -> None:
    """The deadline is the end, as in ``decide`` (a key can be taken at it): at ``expires_at`` the
    claim no longer watches. Fails if an expired claim still addresses its holder, or a claim
    stops one tick early."""
    clock = FakeClock()
    held = [Claim("!56", 1, "claude-1", clock.now() + 30)]
    assert watchers("agent", "!56", held, clock.now() + 29.999) == {1}
    assert watchers("agent", "!56", held, clock.now() + 30) == frozenset()
    assert watchers("agent", "!56", held, clock.now() + 31) == frozenset()


def test_a_message_never_addresses_its_own_author_through_its_own_claim() -> None:
    """``exclude`` is the sender (and whoever the caller skips). Fails if an agent that says
    "taking !56" is addressed by the claim it holds."""
    clock = FakeClock()
    held = live((1, "!56"), (2, "!56x"), until=clock.now() + 60)
    assert watchers("agent", "taking !56", held, clock.now()) == {1}
    assert watchers("agent", "taking !56", held, clock.now(), exclude={1}) == frozenset()


@pytest.mark.parametrize("kind", ["human", "agent", "meeting"])
def test_a_person_an_agent_and_a_call_can_address_a_holder(kind: str) -> None:
    """The three senders a message comes from today: the reach of an @mention (§8.1, §52)."""
    held = [Claim("!56", 1, "claude-1", 1e12)]
    assert watchers(kind, "!56", held, 0.0) == {1}


@pytest.mark.parametrize("kind", ["system", "robot", ""])
def test_any_other_sender_kind_addresses_nobody(kind: str) -> None:
    """Fails closed (CLAUDE.md): switchboard's own lines and a kind this code does not know have
    no say in who is addressed. Fails if the default for an unknown kind is to match."""
    held = [Claim("!56", 1, "claude-1", 1e12)]
    assert watchers(kind, "!56", held, 0.0) == frozenset()
