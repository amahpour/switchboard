"""Which tool parameters a session's MCP server has, from the version it claims in ``mcp.hello``
(#340, DESIGN.md §6.1). The broker's hints name a parameter only to a session that has it; a
claim is cut and parsed here, and anything it can't read counts as having nothing."""

from __future__ import annotations

from typing import Any, get_args

import pytest

from switchboard import __version__
from switchboard.broker.agents import name_reserved_text
from switchboard.envelope import OLDER_SWITCHBOARD, RESTART_UPDATES
from switchboard.mcp import server as srv
from switchboard.mcp.client import BrokerConn
from switchboard.models import FEATURE_SINCE, Feature, has_feature


@pytest.mark.parametrize(
    ("claim", "feature", "has"),
    [
        # the first release with each parameter is the first that has it, not the one before
        ("0.24.9", "join.resume", False),
        ("0.25.0", "join.resume", True),
        ("0.24.9", "read.before", False),
        ("0.25.0", "read.before", True),
        ("0.25.9", "review.proof", False),
        ("0.26.0", "review.proof", True),
        # later releases keep what earlier ones had, patch and suffix don't matter, majors count
        ("0.26.3", "join.resume", True),
        ("0.27.0", "review.proof", True),
        ("0.25.0.dev3+g1a2b3c", "join.resume", True),
        ("1.0.0", "review.proof", True),
        ("0.9.0", "join.resume", False),  # 9 is less than 25, not "greater as text"
        ("0.100.0", "join.resume", True),
        # nothing readable, nothing granted
        (None, "join.resume", False),
        ("", "join.resume", False),
        ("0.26", "join.resume", False),
        ("0.26.0rc1", "join.resume", False),
        ("v0.26.0", "join.resume", False),
        ("latest", "join.resume", False),
        ("0.26.0\n", "join.resume", False),
        ("0.26.0 ", "join.resume", False),
        ("-0.26.0", "join.resume", False),
        ("0.26.0." + "x" * 40, "join.resume", False),  # longer than a claim can be
        ("9" * 5 + ".0.0", "join.resume", False),
    ],
)
def test_a_claimed_version_has_a_feature_from_its_release_on(
    claim: str | None, feature: Any, has: bool
) -> None:
    """The comparison is by release number, never by text, and a claim that isn't an exact
    ``X.Y.Z`` has nothing: a hint sent on a guess is the bug (#340), and a restart costs less than
    a call the session's tool refuses."""
    assert has_feature(claim, feature) is has


@pytest.mark.parametrize("claim", [25, 0.26, ["0.26.0"], {"v": "0.26.0"}, b"0.26.0", True])
def test_a_claim_that_is_not_a_string_has_nothing(claim: Any) -> None:
    """The version arrives from outside; the broker keeps only a string, but the helper is the
    last check, so a wrong type is a claim of nothing, not an exception in a join."""
    assert has_feature(claim, "join.resume") is False


async def test_every_feature_has_a_release_and_the_current_server_has_the_parameter(tmp_path: Any) -> None:
    """The table can't drift from the tools: each feature is ``tool.parameter``, and the server
    this checkout builds has that parameter. A renamed parameter or a typo in the table would make
    every hint wrong in one direction or the other without a test noticing."""
    from fastmcp import Client

    assert set(FEATURE_SINCE) == set(get_args(Feature))
    st = srv.McpState(
        BrokerConn(tmp_path / "no.sock", backoff=(0.05, 0.1)),
        env={},
        parent_argv="/bin/zsh",
        ppid=None,
        sessions_dir=str(tmp_path),
    )
    async with Client(srv.build_server(st), mode="legacy") as c:
        have = {t.name: set(t.input_schema.get("properties", {})) for t in await c.list_tools()}
    for feature, since in FEATURE_SINCE.items():
        tool, param = feature.split(".")
        assert param in have[tool], feature
        assert has_feature(__version__, feature), (feature, since)  # this release has what it hints


NAMED = ("planner", "#build")


def test_the_reserved_name_hint_for_a_session_that_has_resume() -> None:
    """A session at 0.25.0 or later is told to join again with ``resume=``, the hint #304 added."""
    text = name_reserved_text(*NAMED, None, "0.25.0")
    assert text == (
        "planner was used by another agent in #build today; pick another, or, if it was you"
        " before a restart, join again with resume= set to the code at the end of that join's"
        " answer (yk:j...)"
    )


@pytest.mark.parametrize("claim", ["0.24.9", "0.23.0", None, "", "0.25", "garbage"])
def test_the_reserved_name_hint_for_a_session_from_before_resume_or_of_unknown_version(
    claim: str | None,
) -> None:
    """#340: the hint a session too old for ``resume`` gets doesn't name it. It is the same for a
    known old version and for one the broker can't read, and it still says to pick another name."""
    text = name_reserved_text(*NAMED, None, claim)
    assert text == (
        "planner was used by another agent in #build today; pick another. If it was you before a"
        f" restart, {OLDER_SWITCHBOARD} and its join can't take a name back. {RESTART_UPDATES}"
    )
    assert "resume" not in text


@pytest.mark.parametrize("claim", ["0.24.0", "0.26.0", None])
def test_the_reserved_name_hint_after_a_wrong_code_is_the_same_for_every_version(claim: str | None) -> None:
    """A caller that gave a code has the parameter, whatever it claims: the answer is that the
    code isn't the name's, and it isn't told to update."""
    text = name_reserved_text(*NAMED, "0" * 16, claim)
    assert text.startswith(
        "planner was used by another agent in #build today; pick another (that join code isn't this"
        " name's current one: after a /clear or a compaction"
    ), text
    assert "older than the broker" not in text


def test_each_reason_a_code_failed_has_its_own_words() -> None:
    """#370: every way a code fails to bring a name back read "that join code isn't this name's",
    so an agent with a good but retired code, or one from before join codes, couldn't tell what
    to do. Each reason now names itself, and an unknown one reads as a wrong code."""
    from switchboard.broker.agents import RECLAIM_WHY

    texts = {why: name_reserved_text(*NAMED, "0" * 16, None, why) for why in RECLAIM_WHY}
    assert len(set(texts.values())) == len(RECLAIM_WHY)
    assert "no code on record" in texts["no_code"] and "was kicked" in texts["kicked"]
    assert "another harness" in texts["harness"] and "another machine" in texts["host"]
    assert name_reserved_text(*NAMED, "0" * 16, None, "what?") == texts["mismatch"]
