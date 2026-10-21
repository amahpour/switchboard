"""/review <url> [note]: a person starts a review board (#248, DESIGN.md §37.8). Parsing, the
role, and the one message it posts as the person, which carries only switchboard's words,
the person's URL and the person's note (#177)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.broker.commands import (
    HELP_TEXT,
    Actor,
    CommandError,
    parse_command,
    required_role,
    review_request,
)
from switchboard.broker.hub import Hub
from switchboard.broker.service import BrokerInfo, RoomService, ServiceError
from switchboard.config import Config
from switchboard.models import Room
from switchboard.store import Store

PR = "https://github.com/example-org/shop/pull/7"


def room() -> Room:
    return Room(
        id=1,
        name="#build",
        created_at=0.0,
        created_by="alice",
        paused=False,
        paused_reason=None,
        budget_per_hour=60,
        budget_remaining=60,
        budget_window_start=0.0,
        budget_notice_window=None,
        hop_count=0,
        hop_limit=6,
        last_msg_at=None,
    )


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (f"/review {PR}", (PR, "")),
        (f"  /REVIEW   {PR}   ", (PR, "")),
        (f"/review {PR} look hard at the money paths", (PR, "look hard at the money paths")),
        (f"/review {PR}\nfirst line\n\nsecond", (PR, "first line second")),
    ],
)
def test_review_takes_a_url_and_an_optional_note(text: str, want: tuple[str, str]) -> None:
    cmd = parse_command(text)
    assert (cmd.name, cmd.args) == ("review", want)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("/review", "usage: /review <url> [note]"),
        ("/review   ", "usage: /review <url> [note]"),
        (
            "/review codex-1 claude-1",
            "/review: url must be an http(s) link to the pull request or merge request",
        ),
        (
            "/review ftp://example.com/x",
            "/review: url must be an http(s) link to the pull request or merge request",
        ),
        ("/review https://example.com/" + "a" * 500, "/review: url is too long (520 > 500 characters)"),
    ],
)
def test_review_refuses_what_isnt_a_url_and_says_why(text: str, message: str) -> None:
    """0.3's /review took two names; a person who types it from habit is told the form now."""
    with pytest.raises(CommandError) as e:
        parse_command(text)
    assert (e.value.code, e.value.message) == ("bad_request", message)


def test_review_needs_what_posting_a_message_needs() -> None:
    """It posts a chat message as the person, like /catchup and `switchboard say`."""
    assert required_role(parse_command(f"/review {PR}"), room()) == "human_cli"


def test_the_request_mentions_each_agent_and_carries_only_the_persons_words() -> None:
    text = review_request(["claude-1", "codex-1"], PR, "")
    assert text.startswith("@claude-1 @codex-1 ")
    assert PR in text and 'review(action="raise")' in text and 'review(action="show")' in text
    noted = review_request(["claude-1"], PR, "the tax order matters most")
    assert noted == review_request(["claude-1"], PR, "") + "\n\nNote: the tax order matters most"


def test_the_request_asks_for_markdown_a_diagram_and_a_recommendation() -> None:
    """The board's guidance where each agent reads it (#423): details in Markdown with a Mermaid
    diagram when it explains better, a question asked with the option the agent recommends, and
    the others' questions agreed with or answered with another pick. Fails on the old request,
    which said none of it, so details arrived as flat paragraphs."""
    text = review_request(["claude-1"], PR, "")
    assert "write its detail in Markdown" in text and "Mermaid diagram" in text
    assert "with the option you recommend" in text
    assert 'agree with the others\' questions or pick another option with review(action="concur")' in text


def test_the_request_asks_agents_to_settle_a_contest_between_themselves() -> None:
    """#424: a contested finding goes back to its raiser, to drop or insist on, and a person gets only
    what the agents still disagree on. The request says so where every agent reads it. Fails on the
    old request, which let every contest land on the person."""
    text = review_request(["claude-1"], PR, "")
    assert "when one of yours is contested, drop it or insist on it with your reason" in text
    assert 'review(action="insist")' in text
    assert "leave a person only what you still disagree on" in text


def test_the_request_tells_agents_to_pass_the_link_as_url_when_other_boards_are_open() -> None:
    """#445: a room may hold several boards, and an agent that has acted on none would be asked which.
    The request says to give every call the pull request's link, once, so the link is not repeated
    (it is a link in the page, and the one in the sentence above it is the person's own). Fails if
    the request leaves agents to find out from a refusal, or repeats the URL."""
    text = review_request(["claude-1"], PR, "")
    assert "give every call this pull request's link as its url" in text
    assert text.count(PR) == 1


def test_help_lists_review() -> None:
    assert "  /review <url> [note]" in HELP_TEXT


def test_a_room_service_without_boards_refuses_review(tmp_path: Path, clock: FakeClock) -> None:
    """The human side on its own (as these unit tests build it) has no boards: /review says so
    and posts nothing, rather than failing on a missing attribute."""
    store = Store(db.open_db(tmp_path / "y.db"), clock)
    svc = RoomService(store, Hub(), Config(), BrokerInfo(port=7419, test_mode=True), clock)
    room = svc.create_room("#build")
    p = store.upsert_participant("test", "test:agent:claude-1", status="idle")
    store.create_membership(room.id, p.id, "claude-1", "h-claude-1")
    with pytest.raises(ServiceError) as e:
        svc.command("#build", f"/review {PR}", Actor(role="human", via="web"))
    assert e.value.message == "/review: this broker has no review boards"
    assert [m for m in store.history(room.id) if m.kind == "chat"] == []


def test_a_note_too_long_for_one_message_is_refused_before_the_board_opens(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The request has to fit [delivery] max_msg_chars; a note that doesn't is refused, saying
    how much fits, and nothing is posted. (This RoomService has no boards: the check comes first.)"""
    store = Store(db.open_db(tmp_path / "y.db"), clock)
    cfg = Config().with_delivery(max_msg_chars=1000)
    svc = RoomService(store, Hub(), cfg, BrokerInfo(port=7419, test_mode=True), clock)
    room = svc.create_room("#build")
    p = store.upsert_participant("test", "test:agent:claude-1", status="idle")
    store.create_membership(room.id, p.id, "claude-1", "h-claude-1")
    fits = 1000 - len(review_request(["claude-1"], PR, "")) - len("\n\nNote: ")
    with pytest.raises(ServiceError) as e:
        svc.command("#build", f"/review {PR} " + "n" * 1200, Actor(role="human", via="web"))
    assert (
        e.value.message
        == f"/review: the note is too long (1200 characters; at most {fits} fit in one message)"
    )
    assert [m for m in store.history(room.id) if m.kind == "chat"] == []
